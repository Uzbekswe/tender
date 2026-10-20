"""OpenCode CLI provider (free models, synthetic testing only).

The provider runs `opencode run` as a headless subprocess. It uses the built-in
`plan` agent because the free tier rejects custom agents that set `permissions`,
`tools` or `permission` with a 403. The agent is read-only, but it can still read
files, so this provider is for fictional demo data, never for real tenders.

Verified CLI facts (OpenCode 2.0.32):
- The prompt can be passed on stdin when no message argument is given.
- `--format json` writes JSON lines to stdout. The answer is in one or more
  `{"type":"text","part":{"type":"text","text":"..."}}` events. Errors are
  `{"type":"error","error":{"type":"...","message":"..."}}` events.
- `--file` may be repeated; each image is attached to the message.
- The CLI does not report the answering model, token usage or cost in this format.
  `model_answered` is therefore None, not a guess.
- A sanitized HOME and XDG tree is required for free use without a login. The
  process also runs in an empty temporary working directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from llm.client import ImageInput, LLMError, ProviderUnavailable
from llm.providers import ChatMessage, ProviderRequest, ProviderResponse, TransientProviderError

CLI_NOT_FOUND = (
    "OpenCode CLI не найден. Установите OpenCode и проверьте `opencode --version`.\n"
    "OpenCode CLI not found. Install OpenCode and check `opencode --version`."
)
FREE_TIER_HINT = (
    "OpenCode CLI: бесплатный тариф работает только со встроенным агентом plan. "
    "Пользовательский агент с permissions, tools или permission получает 403.\n"
    "OpenCode CLI: the free tier works only with the built-in plan agent. "
    "A custom agent with permissions, tools or permission gets a 403."
)
SYSTEM_FALLBACK = "You are a precise assistant. Follow the instructions in the user message exactly."
PREVIOUS_ANSWER_HEADER = "Your previous answer:"
SCHEMA_HINT = "Return only one JSON object that validates against this JSON Schema:\n{schema}"
IMAGES_HEADER = "Attached images, in order:"
_SNIPPET_CHARS = 300
_AUTH_PATTERN = re.compile(
    r"free.?tier|not logged in|log in|login|authenticat|oauth|credential|unauthorized|forbidden|403",
    re.IGNORECASE,
)
_TRANSIENT_PATTERN = re.compile(
    r"rate limit|too many requests|overloaded|timed? ?out|timeout|temporarily|connection|econnreset|"
    r"service unavailable|server error|status 5\d\d",
    re.IGNORECASE,
)

# (argv, stdin bytes, timeout seconds, cwd, environment) -> (returncode, stdout, stderr)
Runner = Callable[
    [Sequence[str], bytes, float, Path, Mapping[str, str]],
    Awaitable[tuple[int, bytes, bytes]],
]


@dataclass(frozen=True)
class Runtime:
    """The private directory tree used by one CLI call."""

    root: Path
    home: Path
    config: Path
    data: Path
    cache: Path
    state: Path
    tmp: Path
    work: Path
    files: Path


class OpencodeCliProvider:
    def __init__(
        self,
        path: str = "opencode",
        *,
        agent: str = "plan",
        runner: Runner | None = None,
    ) -> None:
        self._path = path
        self._agent = (agent or "plan").strip()
        self._runner = runner or run_subprocess

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        system_text, rest = split_system(request.messages)
        with tempfile.TemporaryDirectory(prefix="opencode-cli-") as raw_root:
            runtime = prepare_runtime(Path(raw_root))
            files = write_images(rest, runtime.files)
            message = build_stdin(rest, system_text, request.schema)
            argv = build_argv(self._path, request.model, self._agent, files)
            returncode, stdout, stderr = await self._runner(
                argv, message, request.timeout_s, runtime.work, cli_environment(runtime)
            )

        text, error = parse_events(stdout)
        if error is not None:
            raise error_from_event(error)
        if returncode != 0:
            raise failure_from_process(returncode, stdout, stderr)
        if not text:
            raise TransientProviderError(
                f"{request.model}: OpenCode CLI returned no text event: {_snippet(_decode(stderr))}"
            )
        # OpenCode does not name the answering model in its JSON output. Returning None
        # keeps the service from claiming that the requested model answered.
        return ProviderResponse(text=text, model_answered=None)


# ----- command line, stdin and temporary files -----

def build_argv(path: str, model: str, agent: str, files: Sequence[Path] = ()) -> list[str]:
    """The exact command. A list, never a shell string. The prompt goes on stdin."""
    argv = [
        path,
        "run",
        "--standalone",
        "--format", "json",
        "--agent", agent or "plan",
        "--model", model,
    ]
    for file in files:
        argv.extend(["--file", str(file)])
    return argv


def split_system(messages: Sequence[ChatMessage]) -> tuple[str, list[ChatMessage]]:
    """System text for the prompt preamble, and the remaining (user / assistant) messages."""
    system = "\n\n".join(m.text for m in messages if m.role == "system").strip()
    return (system or SYSTEM_FALLBACK), [m for m in messages if m.role != "system"]


def build_stdin(
    messages: Sequence[ChatMessage],
    system_text: str,
    schema: type[BaseModel],
) -> bytes:
    """One UTF-8 prompt: system preamble, image labels, the conversation, then the schema."""
    parts = [system_text]
    labels = [image.label or f"image {number}" for number, image in enumerate(images_of(messages), 1)]
    if labels:
        parts.append(IMAGES_HEADER + "\n" + "\n".join(
            f"{number}. {label}" for number, label in enumerate(labels, 1)
        ))
    for message in messages:
        text = message.text
        if message.role == "assistant":
            text = f"{PREVIOUS_ANSWER_HEADER}\n{text}"
        if text:
            parts.append(text)
    parts.append(SCHEMA_HINT.format(schema=json.dumps(schema.model_json_schema(), ensure_ascii=False)))
    return ("\n\n".join(part for part in parts if part) + "\n").encode("utf-8")


def images_of(messages: Sequence[ChatMessage]) -> list[ImageInput]:
    return [image for message in messages for image in message.images]


def prepare_runtime(root: Path) -> Runtime:
    """Create the empty HOME, XDG and working directories for one call."""
    paths = {
        "home": root / "home",
        "config": root / "config",
        "data": root / "data",
        "cache": root / "cache",
        "state": root / "state",
        "tmp": root / "tmp",
        "work": root / "work",
        "files": root / "files",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return Runtime(root=root, **paths)


def write_images(messages: Sequence[ChatMessage], folder: Path) -> list[Path]:
    """Write each image as a PNG. The order is the order of the messages."""
    files: list[Path] = []
    for number, image in enumerate(images_of(messages)):
        path = folder / f"image-{number:02d}.png"
        path.write_bytes(image.png_bytes)
        files.append(path)
    return files


# The CLI must see a private config and no inherited credentials. Keep only the
# system settings that network access and process startup need.
_KEEP_ENV = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "TERM", "SHELL",
    "USER", "LOGNAME", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)


def cli_environment(runtime: Runtime) -> dict[str, str]:
    """A sanitized environment: private HOME/XDG trees and no OpenCode or model keys."""
    env = {key: value for key, value in os.environ.items() if key in _KEEP_ENV}
    env.update({
        "HOME": str(runtime.home),
        "XDG_CONFIG_HOME": str(runtime.config),
        "XDG_DATA_HOME": str(runtime.data),
        "XDG_CACHE_HOME": str(runtime.cache),
        "XDG_STATE_HOME": str(runtime.state),
        "TMPDIR": str(runtime.tmp),
        "NO_COLOR": "1",
        "CI": "1",
        "TERM": "dumb",
    })
    return env


# ----- output -----

def parse_events(stdout: bytes) -> tuple[str | None, dict | None]:
    """The answer text and the first error object in the JSON-lines output."""
    texts: list[str] = []
    error: dict | None = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error" and error is None:
            candidate = event.get("error")
            error = candidate if isinstance(candidate, dict) else {"message": str(candidate or "unknown error")}
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            texts.append(str(part["text"]))
    return final_text(texts), error


def final_text(texts: Sequence[str]) -> str | None:
    """The last text block that contains a JSON object, or all text joined."""
    for text in reversed(texts):
        stripped = text.strip()
        if "{" in stripped and "}" in stripped:
            return stripped
    joined = "\n".join(text for text in texts if text).strip()
    return joined or None


def error_from_event(error: Mapping[str, object]) -> Exception:
    kind = str(error.get("type") or "unknown")
    message = str(error.get("message") or kind)
    text = f"{kind}: {message}"
    if "free" in kind.lower() or "free tier" in message.lower() or "freetier" in text.lower():
        return ProviderUnavailable(f"{FREE_TIER_HINT}\n{_snippet(text)}")
    if _AUTH_PATTERN.search(text):
        return ProviderUnavailable(f"OpenCode CLI: {_snippet(text)}")
    if "no-route" in kind.lower() or "model unavailable" in message.lower():
        return ProviderUnavailable(f"OpenCode CLI: {_snippet(text)}")
    if _TRANSIENT_PATTERN.search(text):
        return TransientProviderError(f"OpenCode CLI: {_snippet(text)}")
    return LLMError(f"OpenCode CLI: {_snippet(text)}")


def failure_from_process(returncode: int, stdout: bytes, stderr: bytes) -> Exception:
    text = _decode(stderr) + "\n" + _decode(stdout)
    if _AUTH_PATTERN.search(text):
        if "free" in text.lower() or "403" in text:
            return ProviderUnavailable(f"{FREE_TIER_HINT}\n{_snippet(text)}")
        return ProviderUnavailable(f"OpenCode CLI: {_snippet(text)}")
    if _TRANSIENT_PATTERN.search(text):
        return TransientProviderError(f"OpenCode CLI: {_snippet(text)}")
    return TransientProviderError(f"OpenCode CLI exited with code {returncode}: {_snippet(text)}")


# ----- subprocess and CLI discovery -----

async def run_subprocess(
    argv: Sequence[str],
    stdin: bytes,
    timeout_s: float,
    cwd: Path,
    env: Mapping[str, str],
) -> tuple[int, bytes, bytes]:
    """Run argv without a shell. On timeout or cancel the process is killed."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=dict(env),
        )
    except FileNotFoundError as exc:
        raise ProviderUnavailable(CLI_NOT_FOUND) from exc
    except OSError as exc:
        raise ProviderUnavailable(
            f"OpenCode CLI cannot be started: {exc.strerror or type(exc).__name__}"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout_s)
    except TimeoutError as exc:
        await _kill(process)
        raise TransientProviderError(f"OpenCode CLI timed out after {timeout_s:.0f} s") from exc
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
    """First line of `opencode --version`, or None when it cannot be run or times out."""
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
