"""Claude Code CLI provider (default): runs `claude -p` as a headless subprocess.

The call uses the account the local Claude Code CLI is logged into, so no API key
is needed. Each call is one stateless request: tools are disabled (`--tools ""`),
customisations are skipped (`--safe-mode`) and no session is saved.

Verified CLI facts (Claude Code 2.1.300):
- stdin takes ONE stream-json line `{"type":"user","message":{...}}`; images go in as
  base64 image blocks. A multi-message request (the repair call) is flattened into
  that one user message.
- stdout is JSON lines. The last `"type":"result"` line carries the answer:
  `structured_output` (set when `--json-schema` is given; preferred) or `result` (text).
- The CLI has no temperature flag. Determinism comes from the response cache.
- Cost is `total_cost_usd` as reported by the CLI; for a subscription it is informational.
"""

import asyncio
import os
import base64
import json
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence

from llm.client import ImageInput, LLMError, ProviderUnavailable
from llm.providers import ChatMessage, ProviderRequest, ProviderResponse, TransientProviderError

CLI_NOT_FOUND = (
    "Claude CLI не найден. Установите Claude Code и выполните вход: claude\n"
    "Claude CLI not found. Install Claude Code, then run `claude` once to log in."
)
CLI_NOT_LOGGED_IN = (
    "Claude CLI: не выполнен вход. Запустите `claude` в терминале и выполните /login\n"
    "Claude CLI: not logged in. Run `claude` in a terminal and use /login."
)
SYSTEM_FALLBACK = "You are a precise assistant. Follow the instructions in the user message exactly."
PREVIOUS_ANSWER_HEADER = "Your previous answer:"
_AUTH_PATTERN = re.compile(r"not logged in|log in|login|authenticat|oauth|credential", re.IGNORECASE)
_TRANSIENT_PATTERN = re.compile(r"rate limit|overloaded|usage limit|too many requests", re.IGNORECASE)
_SNIPPET_CHARS = 300

# (argv, stdin bytes, timeout seconds) -> (returncode, stdout bytes, stderr bytes)
Runner = Callable[[Sequence[str], bytes, float], Awaitable[tuple[int, bytes, bytes]]]


class ClaudeCliProvider:
    def __init__(self, path: str = "claude", *, runner: Runner | None = None) -> None:
        self._path = path
        self._runner = runner or run_subprocess

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        system_text, rest = split_system(request.messages)
        schema_json = json.dumps(request.schema.model_json_schema(), ensure_ascii=False)
        argv = build_argv(self._path, request.model, system_text, schema_json)
        returncode, stdout, stderr = await self._runner(argv, build_stdin(rest), request.timeout_s)

        result = last_result(stdout)
        if result is None:
            text = _decode(stderr) + "\n" + _decode(stdout)
            if _AUTH_PATTERN.search(text):
                raise ProviderUnavailable(CLI_NOT_LOGGED_IN)
            if returncode == 0:
                raise LLMError(f"{request.model}: Claude CLI returned no result line")
            raise TransientProviderError(f"Claude CLI exited with code {returncode}: {_snippet(text)}")
        return response_from_result(result, request.model)


# ----- command line and stdin -----

def build_argv(path: str, model: str, system_text: str, schema_json: str) -> list[str]:
    """The exact command. A list, never a shell string."""
    return [
        path,
        "-p",
        "--model", model,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--tools", "",
        "--safe-mode",
        "--no-session-persistence",
        "--system-prompt", system_text,
        "--json-schema", schema_json,
    ]


def split_system(messages: Sequence[ChatMessage]) -> tuple[str, list[ChatMessage]]:
    """System text for --system-prompt, and the remaining (user / assistant) messages."""
    system = "\n\n".join(m.text for m in messages if m.role == "system").strip()
    return (system or SYSTEM_FALLBACK), [m for m in messages if m.role != "system"]


def flatten(messages: Sequence[ChatMessage]) -> tuple[str, list[ImageInput]]:
    """One user text. Earlier assistant turns are quoted under a header; images keep their order."""
    parts: list[str] = []
    images: list[ImageInput] = []
    for message in messages:
        images.extend(message.images)
        if message.role == "assistant":
            parts.append(f"{PREVIOUS_ANSWER_HEADER}\n{message.text}")
        else:
            parts.append(message.text)
    return "\n\n".join(part for part in parts if part), images


def build_content(messages: Sequence[ChatMessage]) -> list[dict]:
    text, images = flatten(messages)
    blocks: list[dict] = []
    for image in images:
        if image.label:
            blocks.append({"type": "text", "text": f"Image: {image.label}"})
        data = base64.b64encode(image.png_bytes).decode("ascii")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    blocks.append({"type": "text", "text": text})
    return blocks


def build_stdin(messages: Sequence[ChatMessage]) -> bytes:
    """One stream-json input line: a single user message, newline terminated."""
    line = {"type": "user", "message": {"role": "user", "content": build_content(messages)}}
    return (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")


# ----- output -----

def last_result(stdout: bytes) -> dict | None:
    """The last JSON object with type "result" in the stream-json output, or None."""
    found: dict | None = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # not JSON (should not happen with stream-json); ignore
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    return found


def response_from_result(result: dict, requested: str) -> ProviderResponse:
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        raise _error_from_result(result, requested)

    structured = result.get("structured_output")
    if isinstance(structured, dict):
        text = json.dumps(structured, ensure_ascii=False)
    else:
        text = str(result.get("result") or "")

    usage = result.get("usage") or {}
    tokens_in = tokens_out = None
    if usage:
        # Cached input is part of the prompt, so it counts towards tokens_in.
        tokens_in = sum(int(usage.get(key) or 0) for key in
                        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        tokens_out = int(usage.get("output_tokens") or 0)

    model_usage = result.get("modelUsage") or {}
    answered = requested if requested in model_usage else next(iter(model_usage), None)
    cost = result.get("total_cost_usd")
    return ProviderResponse(
        text=text,
        model_answered=answered,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=float(cost) if cost is not None else None,
    )


def _error_from_result(result: dict, requested: str) -> Exception:
    status = result.get("api_error_status")
    message = str(result.get("result") or result.get("subtype") or "unknown error")
    if status in (401, 403):
        return ProviderUnavailable(CLI_NOT_LOGGED_IN)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return TransientProviderError(f"Claude CLI API status {status}: {_snippet(message)}")
    if _AUTH_PATTERN.search(message):
        return ProviderUnavailable(CLI_NOT_LOGGED_IN)
    if _TRANSIENT_PATTERN.search(message):
        return TransientProviderError(f"Claude CLI busy: {_snippet(message)}")
    return LLMError(f"{requested}: Claude CLI error: {_snippet(message)}")


# ----- subprocess and CLI discovery -----

# The CLI must use the logged-in Claude account. An API key or token in the environment
# would take precedence over it, so they are removed from the child process.
_ACCOUNT_OVERRIDES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def cli_environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _ACCOUNT_OVERRIDES}


async def run_subprocess(argv: Sequence[str], stdin: bytes, timeout_s: float) -> tuple[int, bytes, bytes]:
    """Run argv without a shell. On timeout or cancel the process is killed."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=cli_environment(),
        )
    except FileNotFoundError as exc:
        raise ProviderUnavailable(CLI_NOT_FOUND) from exc
    except OSError as exc:
        raise ProviderUnavailable(f"Claude CLI cannot be started: {exc.strerror or type(exc).__name__}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout_s)
    except TimeoutError as exc:
        await _kill(process)
        raise TransientProviderError(f"Claude CLI timed out after {timeout_s:.0f} s") from exc
    except BaseException:
        await _kill(process)
        raise
    return process.returncode or 0, stdout, stderr


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
        await process.wait()


def cli_found(path: str) -> bool:
    return shutil.which(path) is not None


def cli_version(path: str, timeout_s: float = 10.0) -> str | None:
    """First line of `claude --version`, or None when it cannot be run or times out."""
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    return text.splitlines()[0] if text else None


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _snippet(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:_SNIPPET_CHARS] + ("..." if len(collapsed) > _SNIPPET_CHARS else "")
