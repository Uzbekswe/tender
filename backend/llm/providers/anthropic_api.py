"""Anthropic Messages API provider (production path; needs ANTHROPIC_API_KEY).

The `anthropic` package is imported lazily, so the app runs without it until this
provider is selected. Sampling parameters are not sent: current Claude models reject
non-default temperature/top_p. Retries belong to the service layer (max_retries=0).
"""

import base64
from typing import Any

from llm.client import LLMError, ProviderUnavailable
from llm.providers import ChatMessage, ProviderRequest, ProviderResponse, TransientProviderError

MAX_TOKENS = 16000
MISSING_PACKAGE = (
    "Пакет 'anthropic' не установлен: выполните `uv sync` в backend.\n"
    "Package 'anthropic' is not installed: run `uv sync` in backend."
)
MISSING_KEY = (
    "ANTHROPIC_API_KEY не задан (backend/.env).\n"
    "ANTHROPIC_API_KEY is not set (backend/.env)."
)
ACCESS_DENIED = (
    "Anthropic API: доступ запрещён, проверьте ANTHROPIC_API_KEY.\n"
    "Anthropic API: access denied, check ANTHROPIC_API_KEY."
)


class AnthropicAPIProvider:
    def __init__(self, api_key: str, *, client: Any = None, sdk: Any = None) -> None:
        """`client` and `sdk` are injection points for tests; by default both come from the anthropic package."""
        self._api_key = api_key
        self._client = client
        self._sdk = sdk

    def _ensure_ready(self) -> None:
        if self._sdk is None:
            try:
                import anthropic as sdk
            except ImportError as exc:
                raise ProviderUnavailable(MISSING_PACKAGE) from exc
            self._sdk = sdk
        if self._client is None:
            if not self._api_key:
                raise ProviderUnavailable(MISSING_KEY)
            self._client = self._sdk.AsyncAnthropic(api_key=self._api_key, max_retries=0)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self._ensure_ready()
        system_text = "\n\n".join(m.text for m in request.messages if m.role == "system").strip()
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": MAX_TOKENS,
            "messages": to_api_messages(request.messages),
        }
        if system_text:
            kwargs["system"] = system_text
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}

        try:
            response = await self._client.messages.create(**kwargs)
        except (self._sdk.AuthenticationError, self._sdk.PermissionDeniedError) as exc:
            raise ProviderUnavailable(ACCESS_DENIED) from exc
        except self._sdk.RateLimitError as exc:
            raise TransientProviderError(str(exc), retry_after=_retry_after(exc)) from exc
        except self._sdk.APIConnectionError as exc:
            # APITimeoutError is a subclass of APIConnectionError
            raise TransientProviderError(f"{type(exc).__name__}: {exc}") from exc
        except self._sdk.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientProviderError(f"Anthropic API status {exc.status_code}: {exc}") from exc
            raise LLMError(f"{request.model}: Anthropic API status {exc.status_code}: {exc}") from exc
        return _to_response(response, request.model)


def to_api_messages(messages: tuple[ChatMessage, ...] | list[ChatMessage]) -> list[dict[str, Any]]:
    """Real user / assistant turns, images before the text block. The system text goes in `system`."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        content: list[dict[str, Any]] = []
        for image in message.images:
            if image.label:
                content.append({"type": "text", "text": f"Image: {image.label}"})
            data = base64.b64encode(image.png_bytes).decode("ascii")
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
        if message.text:
            content.append({"type": "text", "text": message.text})
        out.append({"role": message.role, "content": content})
    return out


def _to_response(response: Any, requested: str) -> ProviderResponse:
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        raise LLMError(f"{requested}: refused")
    if stop_reason == "max_tokens":
        raise LLMError(f"{requested}: truncated (max_tokens reached)")
    text = "".join(
        block.text for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )
    usage = getattr(response, "usage", None)
    tokens_in = tokens_out = None
    if usage is not None:
        tokens_in = sum(int(getattr(usage, key, 0) or 0) for key in
                        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    return ProviderResponse(
        text=text,
        model_answered=getattr(response, "model", None) or requested,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=None,  # the Messages API does not report a cost; the price table is not kept here
    )


def _retry_after(exc: Any) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
