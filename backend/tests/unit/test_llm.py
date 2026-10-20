"""LLM package tests with a fake provider (no network, in-memory SQLite)."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlmodel import Session, SQLModel, create_engine, select

import app.models  # noqa: F401  (registers the tables)
from app.models import LlmCall
from llm import cache
from llm.client import ImageInput, LLMError, ProviderUnavailable
from llm.prompts import PromptError, load_prompt
from llm.providers import ProviderRequest, ProviderResponse, TransientProviderError
from llm.service import JITTER, DefaultLLMClient, LLMLimits, default_tier, next_tier

FIXTURE_PROMPTS = Path(__file__).resolve().parent.parent / "fixtures" / "prompts"
TASKS = {"demo_task": "test/model:free"}


class Item(BaseModel):
    name: str
    count: int


class FakeProvider:
    """Replies are popped in order: a str (text), a ProviderResponse, or an exception to raise."""

    def __init__(self, *replies: object) -> None:
        self._replies = list(replies)
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, ProviderResponse):
            return reply
        return ProviderResponse(text=str(reply), model_answered=request.model, tokens_in=12, tokens_out=7, cost=0.001)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine


TIERS = {"easy": "m/easy", "standard": "m/standard", "hard": "m/hard"}
ESCALATION = ("easy", "standard", "hard")


def make_client(engine, provider, *, sleeps=None, limits=None, prompt_versions=None, tasks=None,
                tiers=None, escalation_order=(), escalation_enabled=True, effort=None) -> DefaultLLMClient:
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return DefaultLLMClient(
        lambda: Session(engine),
        provider,
        provider_name="fake",
        tasks=TASKS if tasks is None else tasks,
        prompt_versions=prompt_versions,
        limits=limits or LLMLimits(max_retries=3, parallel_calls=4, requests_per_minute=10_000),
        prompts_dir=FIXTURE_PROMPTS,
        sleep=fake_sleep,
        tiers=tiers,
        escalation_order=escalation_order,
        escalation_enabled=escalation_enabled,
        effort=effort,
    )


async def run_demo(client: DefaultLLMClient, name: str = "Acme", images=None):
    return await client.complete("demo_task", "demo", {"name": name}, Item, images)


async def test_cache_hit_returns_same_result_without_calling_provider(engine):
    provider = FakeProvider('{"name": "Acme", "count": 2}')
    client = make_client(engine, provider)

    first, first_meta = await run_demo(client)
    second, second_meta = await run_demo(client)

    assert len(provider.requests) == 1
    assert second == first
    assert first_meta.cache_hit is False
    assert second_meta.cache_hit is True
    assert second_meta.cost == 0.0
    assert second_meta.prompt_version == "demo@v2"


async def test_fenced_json_is_parsed(engine):
    reply = 'Here is the result:\n```json\n{"name": "Acme", "count": 5}\n```\nDone.'
    provider = FakeProvider(reply)
    client = make_client(engine, provider)

    parsed, meta = await run_demo(client)

    assert parsed == Item(name="Acme", count=5)
    assert len(provider.requests) == 1
    assert meta.model_answered == "test/model:free"


async def test_repair_call_after_invalid_json_then_success(engine):
    provider = FakeProvider("Sorry, no JSON here", '{"name": "Acme", "count": 3}')
    client = make_client(engine, provider)

    parsed, _ = await run_demo(client)

    assert parsed == Item(name="Acme", count=3)
    assert len(provider.requests) == 2
    repair_messages = provider.requests[1].messages
    assert repair_messages[-1].role == "user"
    assert "failed validation" in repair_messages[-1].text
    assert repair_messages[-2].role == "assistant"

    # The repair call is cached under its own key: a replay makes no provider calls.
    replayed, replay_meta = await run_demo(client)
    assert replayed == parsed
    assert len(provider.requests) == 2
    assert replay_meta.cache_hit is True


async def test_second_invalid_output_raises_llm_error(engine):
    provider = FakeProvider("not json", "still not json")
    client = make_client(engine, provider)

    with pytest.raises(LLMError, match="validation"):
        await run_demo(client)
    assert len(provider.requests) == 2


async def test_schema_violation_triggers_repair(engine):
    provider = FakeProvider('{"name": "Acme", "count": "many"}', '{"name": "Acme", "count": 9}')
    client = make_client(engine, provider)

    parsed, _ = await run_demo(client)

    assert parsed.count == 9
    assert "count" in provider.requests[1].messages[-1].text


async def test_model_switched_warning(engine):
    switched = ProviderResponse(text='{"name": "Acme", "count": 1}', model_answered="other/model-x")
    provider = FakeProvider(switched)
    client = make_client(engine, provider)

    _, meta = await run_demo(client)

    assert meta.warnings == ["model_switched"]
    assert meta.model_answered == "other/model-x"
    assert meta.model_requested == "test/model:free"


async def test_variant_suffix_is_not_a_model_switch(engine):
    same = ProviderResponse(text='{"name": "Acme", "count": 1}', model_answered="test/model")
    provider = FakeProvider(same)
    client = make_client(engine, provider)

    _, meta = await run_demo(client)

    assert meta.warnings == []


async def test_transient_error_is_retried_and_honours_retry_after(engine):
    sleeps: list[float] = []
    provider = FakeProvider(TransientProviderError("429", retry_after=7.0), '{"name": "Acme", "count": 1}')
    client = make_client(engine, provider, sleeps=sleeps)

    parsed, _ = await run_demo(client)

    assert parsed == Item(name="Acme", count=1)
    assert len(provider.requests) == 2
    assert sleeps == [7.0]


async def test_transient_errors_without_retry_after_back_off_exponentially(engine):
    sleeps: list[float] = []
    provider = FakeProvider(TransientProviderError("503"), TransientProviderError("503"), '{"name": "A", "count": 1}')
    client = make_client(engine, provider, sleeps=sleeps, limits=LLMLimits(max_retries=5, requests_per_minute=10_000))

    await run_demo(client)

    # exponential base 2 s, 4 s, each with jitter (see JITTER in llm/service.py)
    assert len(sleeps) == 2
    assert 2.0 * JITTER[0] <= sleeps[0] <= 2.0 * JITTER[1]
    assert 4.0 * JITTER[0] <= sleeps[1] <= 4.0 * JITTER[1]


async def test_transient_errors_exhaust_retries(engine):
    sleeps: list[float] = []
    provider = FakeProvider(TransientProviderError("500"), TransientProviderError("500"))
    client = make_client(engine, provider, sleeps=sleeps, limits=LLMLimits(max_retries=2, requests_per_minute=10_000))

    with pytest.raises(LLMError, match="unavailable after retries"):
        await run_demo(client)
    assert len(provider.requests) == 2


async def test_images_are_stored_as_hashes_only(engine):
    provider = FakeProvider('{"name": "Acme", "count": 4}')
    client = make_client(engine, provider)
    image = ImageInput(png_bytes=b"\x89PNG fake bytes", label="DOC-07 p.41")

    await run_demo(client, images=[image])

    with Session(engine) as session:
        row = session.exec(select(LlmCall)).one()
    user_message = row.request["messages"][-1]
    assert user_message["image_hashes"] == [hashlib.sha256(image.png_bytes).hexdigest()]
    assert "PNG" not in str(row.request)


def test_cache_key_is_stable_regardless_of_dict_key_order():
    in_order = [{"role": "user", "content": "hi"}]
    reversed_keys = [{"content": "hi", "role": "user"}]

    key = cache.cache_key("openrouter", "m", "p@v1", in_order, [], 0, "Item")

    assert cache.cache_key("openrouter", "m", "p@v1", reversed_keys, [], 0, "Item") == key
    assert cache.cache_key("openrouter", "m", "p@v1", in_order, [], 0.0, "Item") == key  # 0 and 0.0 are one key
    assert cache.cache_key("openrouter", "m", "p@v2", in_order, [], 0, "Item") != key
    assert len(key) == 64


def test_prompt_version_picks_highest_vn():
    version, system, user = load_prompt("demo", prompts_dir=FIXTURE_PROMPTS)

    assert version == "demo@v2"
    assert system == "You are a demo assistant. Answer in JSON."
    assert user == "Version two. Company: {{ name }}."


def test_prompt_version_can_be_pinned():
    version, _, user = load_prompt("demo", version="v1", prompts_dir=FIXTURE_PROMPTS)

    assert version == "demo@v1"
    assert "Version one" in user
    assert load_prompt("demo", version=1, prompts_dir=FIXTURE_PROMPTS)[0] == "demo@v1"


def test_missing_prompt_version_raises():
    with pytest.raises(PromptError):
        load_prompt("demo", version=9, prompts_dir=FIXTURE_PROMPTS)
    with pytest.raises(PromptError):
        load_prompt("no_such_prompt", prompts_dir=FIXTURE_PROMPTS)


async def test_pinned_prompt_version_from_config_is_used(engine):
    provider = FakeProvider('{"name": "Acme", "count": 1}')
    client = make_client(engine, provider, prompt_versions={"demo": "v1"})

    _, meta = await run_demo(client)

    assert meta.prompt_version == "demo@v1"
    assert "Version one" in provider.requests[0].messages[-1].text


def test_system_section_is_rendered_with_variables(tmp_path):
    import asyncio

    from llm.prompts import load_prompt, render

    folder = tmp_path / "sys_demo"
    folder.mkdir()
    (folder / "v1.md").write_text("## system\nTypes: {{ items | join(', ') }}\n\n## user\nHi {{ name }}\n", encoding="utf-8")
    _, system_text, user_template = load_prompt("sys_demo", prompts_dir=tmp_path)
    assert render(system_text, {"items": ["a", "b"], "name": "x"}).strip() == "Types: a, b"
    assert asyncio.iscoroutinefunction(__import__("llm.service", fromlist=["DefaultLLMClient"]).DefaultLLMClient.complete)


# ----- tiers, escalation, stats, provider unavailable -----

async def test_task_tier_resolves_to_model_and_is_reported(engine):
    provider = FakeProvider('{"name": "Acme", "count": 1}')
    client = make_client(engine, provider, tasks={"demo_task": "easy"}, tiers=TIERS)

    _, meta = await run_demo(client)

    assert provider.requests[0].model == "m/easy"
    assert meta.model_requested == "m/easy"
    assert meta.tier == "easy"
    assert meta.escalated_from is None


async def test_explicit_tier_overrides_the_task_tier(engine):
    provider = FakeProvider('{"name": "Acme", "count": 1}')
    client = make_client(engine, provider, tasks={"demo_task": "easy"}, tiers=TIERS)

    _, meta = await client.complete("demo_task", "demo", {"name": "Acme"}, Item, tier="hard")

    assert provider.requests[0].model == "m/hard"
    assert meta.tier == "hard"


async def test_literal_model_in_tasks_is_used_as_is(engine):
    provider = FakeProvider('{"name": "Acme", "count": 1}')
    client = make_client(engine, provider, tasks={"demo_task": "test/model:free"}, tiers=TIERS)

    _, meta = await run_demo(client)

    assert provider.requests[0].model == "test/model:free"
    assert meta.tier is None


async def test_unknown_tier_raises_llm_error(engine):
    client = make_client(engine, FakeProvider(), tasks={"demo_task": "easy"}, tiers=TIERS)

    with pytest.raises(LLMError, match="unknown tier 'nope'"):
        await client.complete("demo_task", "demo", {"name": "Acme"}, Item, tier="nope")


async def test_invalid_json_escalates_once_to_the_next_tier(engine, caplog):
    provider = FakeProvider("not json", "still not json", '{"name": "Acme", "count": 7}')
    client = make_client(engine, provider, tasks={"demo_task": "easy"}, tiers=TIERS,
                         escalation_order=ESCALATION)

    with caplog.at_level("WARNING", logger="llm.service"):
        parsed, meta = await run_demo(client)

    assert parsed == Item(name="Acme", count=7)
    assert [r.model for r in provider.requests] == ["m/easy", "m/easy", "m/standard"]
    # The escalated call is a fresh request: the same messages as the first call, no repair transcript.
    assert provider.requests[2].messages == provider.requests[0].messages
    assert meta.tier == "standard"
    assert meta.escalated_from == "easy"
    assert meta.model_requested == "m/standard"
    assert "escalation task=demo_task from=easy to=standard reason=invalid_json" in caplog.text
    assert client.stats.snapshot()["escalations"] == [
        {"task": "demo_task", "from": "easy", "to": "standard", "reason": "invalid_json"}
    ]


async def test_escalated_call_that_also_fails_raises_llm_error(engine):
    provider = FakeProvider("bad", "bad", "bad", "bad")
    client = make_client(engine, provider, tasks={"demo_task": "easy"}, tiers=TIERS,
                         escalation_order=ESCALATION)

    with pytest.raises(LLMError, match="after escalation to standard"):
        await run_demo(client)
    assert len(provider.requests) == 4


async def test_no_escalation_from_the_top_tier_or_when_disabled(engine):
    top = FakeProvider("bad", "bad")
    client = make_client(engine, top, tasks={"demo_task": "hard"}, tiers=TIERS, escalation_order=ESCALATION)
    with pytest.raises(LLMError, match="after one repair call"):
        await run_demo(client)
    assert len(top.requests) == 2

    disabled = FakeProvider("bad", "bad")
    client = make_client(engine, disabled, tasks={"demo_task": "easy"}, tiers=TIERS,
                         escalation_order=ESCALATION, escalation_enabled=False)
    with pytest.raises(LLMError, match="after one repair call"):
        await run_demo(client)
    assert len(disabled.requests) == 2
    assert client.stats.snapshot()["escalations"] == []


async def test_provider_unavailable_is_not_retried_and_propagates(engine):
    sleeps: list[float] = []
    provider = FakeProvider(ProviderUnavailable("Claude CLI не найден"))
    client = make_client(engine, provider, sleeps=sleeps, tasks={"demo_task": "easy"}, tiers=TIERS,
                         escalation_order=ESCALATION)

    with pytest.raises(ProviderUnavailable, match="не найден"):
        await run_demo(client)
    assert len(provider.requests) == 1
    assert sleeps == []


def test_next_tier_follows_models_toml(data_dir):
    assert next_tier("easy") == "standard"
    assert next_tier("standard") == "hard"
    assert next_tier("hard") is None
    assert next_tier("unknown") is None


def test_default_tier_of_a_task(data_dir):
    assert default_tier("read_page") == "hard"
    assert default_tier("confirm_contradictions") == "easy"
    assert default_tier("no_such_task") is None


async def test_stats_count_calls_per_task_and_model_and_cache_hits(engine):
    provider = FakeProvider('{"name": "Acme", "count": 2}', '{"name": "Beta", "count": 3}')
    client = make_client(engine, provider)

    await run_demo(client)          # provider call
    await run_demo(client)          # cache hit
    await run_demo(client, "Beta")  # provider call

    snapshot = client.stats.snapshot()
    assert snapshot["counts"] == {"demo_task|test/model:free": 2}
    assert snapshot["cache_hits_by"] == {"demo_task|test/model:free": 1}
    assert snapshot["cache_hits"] == 1
    assert snapshot["tokens_in"] == 24  # two provider calls, 12 each; the cache hit costs nothing
    assert snapshot["tokens_out"] == 14
    assert snapshot["cost_usd"] == pytest.approx(0.002)
    assert snapshot["escalations"] == []
    json.dumps(snapshot)  # JSON-able

    client.stats.reset()
    assert client.stats.snapshot()["counts"] == {}
    assert client.stats.snapshot()["cache_hits"] == 0


async def test_stats_record_escalation_and_the_failed_tier_spend(engine):
    provider = FakeProvider("bad", "bad", '{"name": "Acme", "count": 7}')
    client = make_client(engine, provider, tasks={"demo_task": "easy"}, tiers=TIERS,
                         escalation_order=ESCALATION)

    await run_demo(client)

    snapshot = client.stats.snapshot()
    assert snapshot["counts"] == {"demo_task|m/easy": 1, "demo_task|m/standard": 1}
    assert snapshot["tokens_in"] == 36  # 12 per provider call, all three calls counted
    assert snapshot["escalations"] == [{"task": "demo_task", "from": "easy", "to": "standard", "reason": "invalid_json"}]


async def test_effort_from_config_reaches_the_provider_and_changes_the_cache_key(engine):
    provider = FakeProvider('{"name": "Acme", "count": 1}', '{"name": "Acme", "count": 1}')
    client = make_client(engine, provider, tasks={"demo_task": "hard"}, tiers=TIERS, effort={"hard": "high"})
    await run_demo(client)
    assert provider.requests[0].effort == "high"

    # Same request without effort is a different cache entry: a second provider call happens.
    plain = make_client(engine, provider, tasks={"demo_task": "hard"}, tiers=TIERS)
    await run_demo(plain)
    assert len(provider.requests) == 2
    assert provider.requests[1].effort is None


def test_cache_key_extra_is_ignored_when_empty():
    messages = [{"role": "user", "content": "hi"}]
    base = cache.cache_key("p", "m", "v", messages, [], 0, "Item")
    assert cache.cache_key("p", "m", "v", messages, [], 0, "Item", extra=None) == base
    assert cache.cache_key("p", "m", "v", messages, [], 0, "Item", extra={"effort": "high"}) != base


def test_get_llm_client_uses_env_provider_and_concurrency(data_dir, monkeypatch):
    from app.settings import get_settings
    from llm.service import get_llm_client

    monkeypatch.setenv("LLM_PROVIDER", "anthropic_api")
    monkeypatch.setenv("LLM_CONCURRENCY", "7")
    get_settings.cache_clear()
    client = get_llm_client(lambda: Session(create_engine("sqlite://")))
    assert client._provider_name == "anthropic_api"
    assert client._limits.parallel_calls == 7

    monkeypatch.delenv("LLM_PROVIDER")
    monkeypatch.delenv("LLM_CONCURRENCY")
    get_settings.cache_clear()
    default = get_llm_client(lambda: Session(create_engine("sqlite://")))
    assert default._provider_name == "claude_cli"
    assert default._limits.parallel_calls == 4
    assert default._tiers["hard"] == "claude-opus-5-5"


def test_opencode_cache_identity_includes_the_agent(data_dir, monkeypatch):
    from app.settings import get_settings
    from llm.service import cache_provider_name, get_llm_client

    config = Path(__file__).resolve().parents[2] / "config" / "models.opencode.toml"
    monkeypatch.setenv("LLM_PROVIDER", "opencode_cli")
    monkeypatch.setenv("OPENCODE_AGENT", "plan")
    monkeypatch.setenv("MODELS_CONFIG", str(config))
    get_settings.cache_clear()
    try:
        assert cache_provider_name() == "opencode_cli:plan"
        client = get_llm_client(lambda: Session(create_engine("sqlite://")))
        assert client._provider_name == "opencode_cli:plan"
        assert client._tiers["easy"] == "opencode/glm-5.3-flash-free"

        monkeypatch.setenv("OPENCODE_AGENT", "build")
        get_settings.cache_clear()
        assert cache_provider_name() == "opencode_cli:build"
    finally:
        get_settings.cache_clear()


def test_call_tag_includes_the_opencode_agent(data_dir, monkeypatch):
    from app.settings import get_settings
    from llm.prompts import call_tag

    config = Path(__file__).resolve().parents[2] / "config" / "models.opencode.toml"
    monkeypatch.setenv("LLM_PROVIDER", "opencode_cli")
    monkeypatch.setenv("MODELS_CONFIG", str(config))
    get_settings.cache_clear()
    try:
        tag = call_tag("page_cards", "page_cards")
    finally:
        get_settings.cache_clear()
    assert tag.startswith("opencode_cli:plan:opencode/glm-5.3-flash-free|page_cards@v")


def test_call_tag_resolves_tier_to_model(data_dir, monkeypatch):
    from llm.prompts import call_tag

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from app.settings import get_settings
    get_settings.cache_clear()
    try:
        tag = call_tag("confirm_contradictions", "confirm_contradictions")
    finally:
        get_settings.cache_clear()
    assert tag.startswith("claude_cli:claude-haiku-5-5|confirm_contradictions@v")
