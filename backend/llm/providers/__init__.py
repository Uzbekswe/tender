"""Provider adapters behind one call shape (ARCHITECTURE §6.2).

A provider takes a ProviderRequest and returns the raw text plus usage. It raises
TransientProviderError for failures worth retrying (429, 5xx, timeouts, connection
errors); the service layer does the retrying. ProviderUnavailable (llm/client.py)
means the provider cannot be used at all and is never retried. Other exceptions are fatal.

Providers: claude_cli (default), anthropic_api, openrouter, handoff (development only),
opencode_cli (free OpenCode models, synthetic testing only).
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

from llm.client import ImageInput


class TransientProviderError(Exception):
    """Retryable failure. retry_after is the server's Retry-After in seconds, if given."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    text: str
    images: tuple[ImageInput, ...] = ()


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    schema: type[BaseModel]
    schema_name: str
    temperature: float
    timeout_s: float
    effort: str | None = None  # optional output effort for the tier (anthropic_api only)


@dataclass
class ProviderResponse:
    text: str
    model_answered: str | None = None  # the model that actually answered (may differ from the request)
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


PROVIDER_NAMES = ("claude_cli", "anthropic_api", "openrouter", "handoff", "opencode_cli")


def get_provider(name: str) -> Provider:
    """Build the provider named by LLM_PROVIDER or models.toml `provider`."""
    from app.settings import get_settings

    settings = get_settings()
    if name == "claude_cli":
        from llm.providers.claude_cli import ClaudeCliProvider

        return ClaudeCliProvider(settings.claude_cli_path)
    if name == "anthropic_api":
        from llm.providers.anthropic_api import AnthropicAPIProvider

        return AnthropicAPIProvider(api_key=settings.anthropic_api_key)
    if name == "openrouter":
        from llm.providers.openrouter import OpenRouterProvider

        return OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )
    if name == "handoff":
        from llm.providers.handoff import HandoffProvider

        return HandoffProvider(settings.data_dir / "llm_handoff")
    if name == "opencode_cli":
        from llm.providers.opencode_cli import OpencodeCliProvider

        return OpencodeCliProvider(settings.opencode_cli_path, agent=settings.opencode_agent)
    if name in {"openai", "gemini"}:
        raise NotImplementedError(f"LLM provider '{name}' is not implemented yet")
    raise ValueError(f"unknown LLM provider '{name}'; use one of: {', '.join(PROVIDER_NAMES)}")
