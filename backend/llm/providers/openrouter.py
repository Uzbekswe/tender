"""OpenRouter provider using the official openai SDK (ARCHITECTURE §6.2-6.3).

Structured output: the JSON Schema goes in `response_format`. If OpenRouter rejects
that format (a 400 that names response_format or json_schema), the call is retried
once with `json_object` and the schema is appended to the system prompt.
"""

import base64
import json
from typing import Any

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from llm.providers import ChatMessage, ProviderRequest, ProviderResponse, TransientProviderError

APP_TITLE = "Tender-Sverka"
APP_REFERER = "http://localhost"  # sent as HTTP-Referer; OpenRouter uses it for app attribution
SCHEMA_HINT = (
    "Respond only with a JSON object that validates against this JSON Schema:\n{schema}"
)


class OpenRouterProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if client is None:
            if not api_key:
                raise ValueError("OPENROUTER_API_KEY is not set (see backend/.env.example)")
            # max_retries=0: retries belong to the service layer, so they are counted once
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                default_headers={"HTTP-Referer": APP_REFERER, "X-Title": APP_TITLE},
            )
        self._client = client

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            return await self._complete(request)
        except openai.RateLimitError as exc:
            raise TransientProviderError(str(exc), retry_after=_retry_after(exc)) from exc
        except (openai.InternalServerError, openai.APIConnectionError) as exc:
            # APITimeoutError is a subclass of APIConnectionError
            raise TransientProviderError(f"{type(exc).__name__}: {exc}") from exc

    async def _complete(self, request: ProviderRequest) -> ProviderResponse:
        messages = to_openai_messages(request.messages)
        json_schema_format = {
            "type": "json_schema",
            "json_schema": {
                "name": request.schema_name,
                "schema": request.schema.model_json_schema(),
                "strict": False,
            },
        }
        try:
            response = await self._create(request, messages, json_schema_format)
        except openai.BadRequestError as exc:
            if not _rejects_response_format(exc):
                raise
            messages = with_schema_hint(messages, request.schema)
            response = await self._create(request, messages, {"type": "json_object"})
        return _to_response(response)

    async def _create(
        self,
        request: ProviderRequest,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any],
    ) -> Any:
        return await self._client.chat.completions.create(
            model=request.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=request.temperature,
            response_format=response_format,  # type: ignore[arg-type]
            timeout=request.timeout_s,
        )


def to_openai_messages(messages: tuple[ChatMessage, ...]) -> list[dict[str, Any]]:
    """Plain string content, or text + image_url parts when a message carries images."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if not message.images:
            out.append({"role": message.role, "content": message.text})
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": message.text}]
        for image in message.images:
            if image.label:
                parts.append({"type": "text", "text": f"Image: {image.label}"})
            encoded = base64.b64encode(image.png_bytes).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        out.append({"role": message.role, "content": parts})
    return out


def with_schema_hint(messages: list[dict[str, Any]], schema: type[BaseModel]) -> list[dict[str, Any]]:
    """Return a copy of the messages with the JSON Schema appended to the system prompt."""
    hint = SCHEMA_HINT.format(schema=json.dumps(schema.model_json_schema(), ensure_ascii=False))
    result = [dict(m) for m in messages]
    for message in result:
        if message["role"] == "system":
            message["content"] = f"{message['content']}\n\n{hint}"
            return result
    return [{"role": "system", "content": hint}, *result]


def _to_response(response: Any) -> ProviderResponse:
    if not response.choices:
        raise TransientProviderError("empty response from provider: no choices")
    text = response.choices[0].message.content or ""
    usage = response.usage
    cost = getattr(usage, "cost", None) if usage is not None else None  # OpenRouter adds usage.cost
    return ProviderResponse(
        text=text,
        model_answered=response.model,
        tokens_in=usage.prompt_tokens if usage is not None else None,
        tokens_out=usage.completion_tokens if usage is not None else None,
        cost=float(cost) if cost is not None else None,
    )


def _rejects_response_format(exc: openai.BadRequestError) -> bool:
    message = str(exc).lower()
    return "response_format" in message or "json_schema" in message


def _retry_after(exc: openai.APIStatusError) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # HTTP-date form is not used by OpenRouter; fall back to backoff
