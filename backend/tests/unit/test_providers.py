"""Provider tests: Claude CLI (command line, stream-json parsing, errors), the Anthropic API
provider with a fake client and fake SDK classes, and the OpenCode CLI with a fake runner.
No real model call is made."""

import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from llm.client import LLMError, ProviderUnavailable
from llm.providers import ChatMessage, ProviderRequest, TransientProviderError, get_provider
from llm.providers import anthropic_api as anthropic_mod
from llm.providers import claude_cli as cli
from llm.providers import opencode_cli as oc
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
    assert type(get_provider("opencode_cli")).__name__ == "OpencodeCliProvider"
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_provider("nope")


# ----- OpenCode CLI provider with a fake runner -----

class FakeOpencodeRunner:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.argv = None
        self.stdin = None
        self.timeout = None
        self.cwd = None
        self.cwd_is_dir = None
        self.env = None
        self.files = []
        self.file_bytes = []

    async def __call__(self, argv, stdin, timeout_s, cwd, env):
        self.argv, self.stdin, self.timeout = list(argv), stdin, timeout_s
        self.cwd, self.cwd_is_dir, self.env = cwd, cwd.is_dir(), dict(env)
        self.files = [Path(argv[i + 1]) for i, part in enumerate(argv) if part == "--file"]
        self.file_bytes = [path.read_bytes() for path in self.files]
        return self.returncode, self.stdout, self.stderr


def opencode_text_line(text: str) -> bytes:
    event = {"type": "text", "part": {"type": "text", "text": text}}
    return json.dumps(event).encode() + b"\n"


def opencode_error_line(kind: str, message: str) -> bytes:
    event = {"type": "error", "error": {"type": kind, "message": message}}
    return json.dumps(event).encode() + b"\n"


async def test_opencode_command_line_uses_the_plan_agent_and_stdin(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/tmp/must-not-be-inherited")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    runner = FakeOpencodeRunner(stdout=opencode_text_line('{"name": "Acme", "count": 1}'))
    provider = oc.OpencodeCliProvider("/opt/bin/opencode", runner=runner)

    response = await provider.complete(make_request(
        [ChatMessage("system", "SYS"), ChatMessage("user", "hello")],
        model="opencode/glm-5.3-flash-free",
    ))

    assert runner.argv[:2] == ["/opt/bin/opencode", "run"]
    assert "--standalone" in runner.argv
    assert runner.argv[runner.argv.index("--format") + 1] == "json"
    assert runner.argv[runner.argv.index("--agent") + 1] == "plan"
    assert runner.argv[runner.argv.index("--model") + 1] == "opencode/glm-5.3-flash-free"
    assert "--file" not in runner.argv  # no images in this request
    assert "hello" not in runner.argv  # the prompt goes on stdin, not the command line
    assert runner.cwd_is_dir is True
    assert Path(runner.env["HOME"]).parent == runner.cwd.parent
    assert runner.env["XDG_CONFIG_HOME"].startswith(str(runner.cwd.parent))
    assert "OPENCODE_CONFIG_DIR" not in runner.env
    assert "OPENAI_API_KEY" not in runner.env
    prompt = runner.stdin.decode()
    assert "SYS" in prompt and "hello" in prompt
    assert "JSON Schema" in prompt and '"count"' in prompt
    assert response.text == '{"name": "Acme", "count": 1}'
    assert response.model_answered is None  # the CLI does not report it


async def test_opencode_images_are_written_and_attached_in_order():
    first = ImageInput(png_bytes=PNG + b"1", label="DOC-01 p.1")
    second = ImageInput(png_bytes=PNG + b"2", label="DOC-02 p.2")
    runner = FakeOpencodeRunner(stdout=opencode_text_line('{"name": "Acme", "count": 2}'))
    provider = oc.OpencodeCliProvider("opencode", runner=runner)

    await provider.complete(make_request(
        [ChatMessage("user", "Read both.", (first, second))],
        model="opencode/glm-5.3-flash-free",
    ))

    assert len(runner.files) == 2
    assert runner.file_bytes == [first.png_bytes, second.png_bytes]
    prompt = runner.stdin.decode()
    assert "Attached images, in order:" in prompt
    assert "1. DOC-01 p.1" in prompt and "2. DOC-02 p.2" in prompt


def test_opencode_parse_events_uses_the_last_json_text_block():
    stdout = (
        b"not json\n"
        + opencode_text_line("I will answer now.")
        + opencode_text_line('{"name": "Acme", "count": 3}')
    )

    text, error = oc.parse_events(stdout)

    assert text == '{"name": "Acme", "count": 3}'
    assert error is None
    assert oc.parse_events(b"plain text\n") == (None, None)


@pytest.mark.parametrize("kind, message, expected, fragment", [
    ("unknown", "403 FreeTierError: free tier is limited", ProviderUnavailable, "plan agent"),
    ("provider.no-route", "Model unavailable: opencode/nope", ProviderUnavailable, "Model unavailable"),
    ("provider.rate_limit", "Rate limit reached", TransientProviderError, "Rate limit"),
    ("unknown", "Image could not be decoded: a.png", LLMError, "Image could not be decoded"),
])
async def test_opencode_error_events_map_to_typed_errors(kind, message, expected, fragment):
    runner = FakeOpencodeRunner(stdout=opencode_error_line(kind, message))
    provider = oc.OpencodeCliProvider("opencode", runner=runner)

    with pytest.raises((LLMError, TransientProviderError, ProviderUnavailable)) as info:
        await provider.complete(make_request([ChatMessage("user", "x")]))

    assert isinstance(info.value, expected)
    assert fragment in str(info.value)
    if expected is LLMError:
        assert not isinstance(info.value, (TransientProviderError, ProviderUnavailable))


async def test_opencode_exit_without_text_is_transient_and_missing_binary_is_unavailable():
    silent = oc.OpencodeCliProvider("opencode", runner=FakeOpencodeRunner(returncode=0))
    with pytest.raises(TransientProviderError, match="no text event"):
        await silent.complete(make_request([ChatMessage("user", "x")]))

    crashed = oc.OpencodeCliProvider(
        "opencode",
        runner=FakeOpencodeRunner(returncode=1, stderr=b"rate limit"),
    )
    with pytest.raises(TransientProviderError, match="rate limit"):
        await crashed.complete(make_request([ChatMessage("user", "x")]))

    free_tier = oc.OpencodeCliProvider(
        "opencode",
        runner=FakeOpencodeRunner(returncode=1, stderr=b"403 FreeTierError: free tier denied"),
    )
    with pytest.raises(ProviderUnavailable, match="FreeTierError"):
        await free_tier.complete(make_request([ChatMessage("user", "x")]))

    with pytest.raises(ProviderUnavailable, match="OpenCode CLI not found"):
        await oc.run_subprocess(
            ["/nonexistent/opencode-binary-for-tests"], b"", 5, Path.cwd(), {}
        )


def test_opencode_cli_discovery_without_running_a_model():
    assert oc.cli_found("/nonexistent/opencode-binary-for-tests") is False
    assert oc.cli_version("/nonexistent/opencode-binary-for-tests") is None


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
