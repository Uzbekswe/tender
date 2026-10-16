"""Provider tests: Claude CLI (command line, stream-json parsing, errors) and the Anthropic API
provider with a fake client and fake SDK classes. No real model call is made."""

import base64
import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from llm.client import LLMError, ProviderUnavailable
from llm.providers import ChatMessage, ProviderRequest, TransientProviderError, get_provider
from llm.providers import anthropic_api as anthropic_mod
from llm.providers import claude_cli as cli
from llm.providers.anthropic_api import AnthropicAPIProvider
from llm.client import ImageInput


class Item(BaseModel):
    name: str
    count: int


PNG = b"\x89PNG synthetic bytes"


def make_request(messages, model="claude-haiku-5-5", effort=None) -> ProviderRequest:
    return ProviderRequest(
        model=model,
        messages=tuple(messages),
        schema=Item,
        schema_name="Item",
        temperature=0,
        timeout_s=30,
        effort=effort,
    )


def result_line(**fields) -> bytes:
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"name": "Acme", "count": 1}',
        "usage": {"input_tokens": 3, "output_tokens": 9, "cache_read_input_tokens": 100,
                  "cache_creation_input_tokens": 20},
        "total_cost_usd": 0.0004,
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 3}},
    }
    base.update(fields)
    return json.dumps(base).encode() + b"\n"


INIT_LINE = b'{"type":"system","subtype":"init","model":"claude-haiku-5-5"}\n'
ASSISTANT_LINE = b'{"type":"assistant","message":{"content":[{"type":"text","text":"{}"}]}}\n'


class FakeRunner:
    """Records the call and returns a canned (returncode, stdout, stderr)."""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.argv = None
        self.stdin = None
        self.timeout = None

    async def __call__(self, argv, stdin, timeout_s):
        self.argv, self.stdin, self.timeout = list(argv), stdin, timeout_s
        return self.returncode, self.stdout, self.stderr


# ----- Claude CLI: command line -----

def test_command_line_has_the_verified_flags_and_no_shell():
    argv = cli.build_argv("/opt/bin/claude", "claude-opus-5-5", "SYS", '{"type": "object"}')

    assert argv[0] == "/opt/bin/claude"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == ""  # all tools disabled
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--json-schema") + 1] == '{"type": "object"}'
    for flag in ("-p", "--verbose", "--safe-mode", "--no-session-persistence"):
        assert flag in argv
    assert "--max-turns" not in argv
    assert all(isinstance(part, str) for part in argv)


async def test_provider_passes_model_and_schema_to_the_runner():
    runner = FakeRunner(stdout=INIT_LINE + ASSISTANT_LINE + result_line())
    provider = cli.ClaudeCliProvider("claude", runner=runner)

    response = await provider.complete(make_request([ChatMessage("user", "hello")], model="claude-sonnet-5-5"))

    assert runner.argv[runner.argv.index("--model") + 1] == "claude-sonnet-5-5"
    schema = json.loads(runner.argv[runner.argv.index("--json-schema") + 1])
    assert schema["properties"]["count"]["type"] == "integer"
    assert runner.timeout == 30
    assert response.model_answered == "claude-haiku-5-5"  # requested is not a key of modelUsage here: first key


# ----- Claude CLI: stdin message -----

def test_stdin_is_one_line_with_images_before_the_text():
    image = ImageInput(png_bytes=PNG, label="DOC-07 p.41")
    line = cli.build_stdin([ChatMessage("user", "Read the page.", (image,))])

    assert line.endswith(b"\n") and line.count(b"\n") == 1
    message = json.loads(line)
    assert message["type"] == "user"
    assert message["message"]["role"] == "user"
    content = message["message"]["content"]
    assert content[0] == {"type": "text", "text": "Image: DOC-07 p.41"}
    assert content[1]["type"] == "image"
    assert content[1]["source"] == {"type": "base64", "media_type": "image/png",
                                    "data": base64.b64encode(PNG).decode("ascii")}
    assert content[-1] == {"type": "text", "text": "Read the page."}


def test_multi_message_repair_request_is_flattened_into_one_user_message():
    messages = [
        ChatMessage("system", "You extract JSON."),
        ChatMessage("user", "Extract the count."),
        ChatMessage("assistant", "I think it is three."),
        ChatMessage("user", "Your JSON failed validation: count: must be int. Return only valid JSON."),
    ]
    system_text, rest = cli.split_system(messages)
    line = json.loads(cli.build_stdin(rest))

    assert system_text == "You extract JSON."
    assert [m.role for m in rest] == ["user", "assistant", "user"]
    text = line["message"]["content"][-1]["text"]
    assert "Extract the count." in text
    assert "Your previous answer:\nI think it is three." in text
    assert text.index("Extract the count.") < text.index("Your previous answer:") < text.index("failed validation")
    assert len(line["message"]["content"]) == 1  # no image blocks for text-only messages


def test_missing_system_text_uses_a_neutral_fallback():
    system_text, _ = cli.split_system([ChatMessage("user", "hi")])
    assert system_text == cli.SYSTEM_FALLBACK


# ----- Claude CLI: stream-json output -----

async def test_success_uses_structured_output_and_reports_usage():
    stdout = INIT_LINE + ASSISTANT_LINE + result_line(structured_output={"name": "Acme", "count": 5},
                                                      result="ignored text")
    provider = cli.ClaudeCliProvider("claude", runner=FakeRunner(stdout=stdout))

    response = await provider.complete(make_request([ChatMessage("user", "x")]))

    assert json.loads(response.text) == {"name": "Acme", "count": 5}
    assert response.model_answered == "claude-haiku-5-5"
    assert response.tokens_in == 3 + 100 + 20  # cache reads and writes are part of the prompt
    assert response.tokens_out == 9
    assert response.cost == pytest.approx(0.0004)


async def test_success_falls_back_to_result_text_without_structured_output():
    provider = cli.ClaudeCliProvider("claude", runner=FakeRunner(stdout=result_line()))

    response = await provider.complete(make_request([ChatMessage("user", "x")]))

    assert response.text == '{"name": "Acme", "count": 1}'


def test_last_result_line_wins_and_non_json_lines_are_ignored():
    stdout = result_line(result="first") + b"not json at all\n" + result_line(result="second")

    assert cli.last_result(stdout)["result"] == "second"
    assert cli.last_result(b"just text\n") is None


def test_answering_model_is_the_requested_one_when_present():
    result = {"modelUsage": {"other-model": {}, "claude-opus-5-5": {}}, "result": "{}"}
    response = cli.response_from_result(result, "claude-opus-5-5")
    assert response.model_answered == "claude-opus-5-5"


async def test_is_error_with_auth_status_is_provider_unavailable():
    stdout = result_line(is_error=True, subtype="error", api_error_status=401, result="Invalid credentials")
    provider = cli.ClaudeCliProvider("claude", runner=FakeRunner(stdout=stdout))

    with pytest.raises(ProviderUnavailable, match="не выполнен вход"):
        await provider.complete(make_request([ChatMessage("user", "x")]))


async def test_is_error_with_login_text_is_provider_unavailable():
    stdout = result_line(is_error=True, subtype="error", result="Please run /login to continue")
    provider = cli.ClaudeCliProvider("claude", runner=FakeRunner(stdout=stdout))

    with pytest.raises(ProviderUnavailable):
        await provider.complete(make_request([ChatMessage("user", "x")]))


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_rate_limit_and_overload_are_transient(status):
    stdout = result_line(is_error=True, subtype="error", api_error_status=status, result="API error")
    provider = cli.ClaudeCliProvider("claude", runner=FakeRunner(stdout=stdout))

    with pytest.raises(TransientProviderError, match=str(status)):
        await provider.complete(make_request([ChatMessage("user", "x")]))


async def test_usage_limit_text_is_transient_and_other_errors_are_llm_errors():
    limited = cli.ClaudeCliProvider("claude", runner=FakeRunner(
        stdout=result_line(is_error=True, subtype="error", result="Claude usage limit reached")))
    with pytest.raises(TransientProviderError):
        await limited.complete(make_request([ChatMessage("user", "x")]))

    broken = cli.ClaudeCliProvider("claude", runner=FakeRunner(
        stdout=result_line(is_error=True, subtype="error_max_turns", result="bad request shape")))
    with pytest.raises(LLMError) as info:
        await broken.complete(make_request([ChatMessage("user", "x")]))
    assert not isinstance(info.value, (TransientProviderError, ProviderUnavailable))


async def test_exit_without_result_line_is_classified_by_stderr():
    auth = cli.ClaudeCliProvider("claude", runner=FakeRunner(returncode=1, stderr=b"Not logged in. Run /login"))
    with pytest.raises(ProviderUnavailable):
        await auth.complete(make_request([ChatMessage("user", "x")]))

    crash = cli.ClaudeCliProvider("claude", runner=FakeRunner(returncode=1, stderr=b"segfault"))
    with pytest.raises(TransientProviderError, match="exited with code 1"):
        await crash.complete(make_request([ChatMessage("user", "x")]))

    silent = cli.ClaudeCliProvider("claude", runner=FakeRunner(returncode=0))
    with pytest.raises(TransientProviderError, match="no result line"):  # retried by the service
        await silent.complete(make_request([ChatMessage("user", "x")]))


async def test_timeout_kills_the_process_and_is_transient():
    started = time.monotonic()
    with pytest.raises(TransientProviderError, match="timed out"):
        await cli.run_subprocess([sys.executable, "-c", "import time; time.sleep(60)"], b"", 0.5)
    assert time.monotonic() - started < 20


async def test_missing_binary_is_provider_unavailable_with_clear_text():
    with pytest.raises(ProviderUnavailable, match="не найден") as info:
        await cli.run_subprocess(["/nonexistent/claude-binary-for-tests"], b"", 5)
    assert "Claude CLI not found" in str(info.value)


def test_cli_discovery_without_running_a_model():
    assert cli.cli_found("/nonexistent/claude-binary-for-tests") is False
    assert cli.cli_version("/nonexistent/claude-binary-for-tests") is None


def test_get_provider_builds_the_new_providers_without_network(data_dir):
    assert isinstance(get_provider("claude_cli"), cli.ClaudeCliProvider)
    assert type(get_provider("anthropic_api")).__name__ == "AnthropicAPIProvider"
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_provider("nope")


# ----- Anthropic API provider with a fake client -----

class APIStatusError(Exception):
    def __init__(self, message="", status_code=400, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class FakeSDK:
    class APIError(Exception):
        pass

    class APIStatusError(APIStatusError):
        pass

    class AuthenticationError(APIStatusError):
        pass

    class PermissionDeniedError(APIStatusError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(APIConnectionError):
        pass


SDK = SimpleNamespace(**{name: getattr(FakeSDK, name) for name in dir(FakeSDK) if not name.startswith("_")})


class FakeMessages:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def api_response(text='{"name": "Acme", "count": 2}', stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model="claude-haiku-5-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=4, cache_read_input_tokens=5,
                              cache_creation_input_tokens=0),
    )


def api_provider(reply) -> tuple[AnthropicAPIProvider, FakeMessages]:
    messages = FakeMessages(reply)
    return AnthropicAPIProvider("test-key", client=SimpleNamespace(messages=messages), sdk=SDK), messages


async def test_api_success_sends_system_turns_images_and_no_sampling_params():
    provider, fake = api_provider(api_response())
    request = make_request([
        ChatMessage("system", "SYS"),
        ChatMessage("user", "Read.", (ImageInput(png_bytes=PNG, label="p1"),)),
        ChatMessage("assistant", "old answer"),
        ChatMessage("user", "Repair."),
    ])

    response = await provider.complete(request)

    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == anthropic_mod.MAX_TOKENS
    assert call["system"] == "SYS"
    assert "temperature" not in call and "top_p" not in call
    assert "output_config" not in call  # only when an effort is configured
    roles = [m["role"] for m in call["messages"]]
    assert roles == ["user", "assistant", "user"]
    first = call["messages"][0]["content"]
    assert first[0] == {"type": "text", "text": "Image: p1"}
    assert first[1]["type"] == "image" and first[1]["source"]["media_type"] == "image/png"
    assert first[-1] == {"type": "text", "text": "Read."}
    assert response.text == '{"name": "Acme", "count": 2}'
    assert response.model_answered == "claude-haiku-5-5"
    assert response.tokens_in == 10 + 5 + 0
    assert response.tokens_out == 4


async def test_api_effort_is_sent_only_when_configured():
    provider, fake = api_provider(api_response())
    await provider.complete(make_request([ChatMessage("user", "x")], effort="high"))
    assert fake.calls[0]["output_config"] == {"effort": "high"}


async def test_api_refusal_and_truncation_are_llm_errors():
    refused, _ = api_provider(api_response(stop_reason="refusal"))
    with pytest.raises(LLMError, match="refused"):
        await refused.complete(make_request([ChatMessage("user", "x")]))

    truncated, _ = api_provider(api_response(stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="truncated"):
        await truncated.complete(make_request([ChatMessage("user", "x")]))


@pytest.mark.parametrize("error, expected", [
    (FakeSDK.AuthenticationError("bad key", 401), ProviderUnavailable),
    (FakeSDK.PermissionDeniedError("denied", 403), ProviderUnavailable),
    (FakeSDK.RateLimitError("slow down", 429, headers={"retry-after": "7"}), TransientProviderError),
    (FakeSDK.APIConnectionError("no route"), TransientProviderError),
    (FakeSDK.APITimeoutError("too slow"), TransientProviderError),
    (FakeSDK.APIStatusError("overloaded", 529), TransientProviderError),
    (FakeSDK.APIStatusError("boom", 503), TransientProviderError),
    (FakeSDK.APIStatusError("bad request", 400), LLMError),
])
async def test_api_errors_map_to_the_typed_errors(error, expected):
    provider, _ = api_provider(error)
    with pytest.raises((LLMError, TransientProviderError, ProviderUnavailable)) as info:
        await provider.complete(make_request([ChatMessage("user", "x")]))
    assert isinstance(info.value, expected)
    if expected is LLMError:  # a plain LLMError must not be retried as transient
        assert not isinstance(info.value, TransientProviderError)


async def test_api_rate_limit_keeps_retry_after():
    provider, _ = api_provider(FakeSDK.RateLimitError("slow", 429, headers={"retry-after": "7"}))
    with pytest.raises(TransientProviderError) as info:
        await provider.complete(make_request([ChatMessage("user", "x")]))
    assert info.value.retry_after == 7.0


def test_api_missing_package_is_provider_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` raise ImportError
    provider = AnthropicAPIProvider("test-key")
    with pytest.raises(ProviderUnavailable, match="'anthropic' не установлен"):
        provider._ensure_ready()


async def test_api_missing_key_is_provider_unavailable():
    provider = AnthropicAPIProvider("", sdk=SDK)
    with pytest.raises(ProviderUnavailable, match="ANTHROPIC_API_KEY"):
        await provider.complete(make_request([ChatMessage("user", "x")]))


# ----- /api/v1/llm/status -----

def test_cli_child_environment_drops_api_credentials(monkeypatch) -> None:
    from llm.providers.claude_cli import cli_environment

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test-not-real")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = cli_environment()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
