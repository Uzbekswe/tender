"""LLM client interface (ARCHITECTURE §6.2).

SHARED CONTRACT: the engine only calls `LLMClient.complete`. The implementation
(providers, cache, retries, rate limiting, escalation) lives in llm/service.py;
the signature below is the contract the engine relies on.
"""

from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ImageInput:
    png_bytes: bytes
    label: str = ""  # e.g. "DOC-07 p.41"


@dataclass
class CallMeta:
    """What happened on a call; stored on LlmCall and summarised on the Run."""
    task: str
    model_requested: str
    model_answered: str | None
    prompt_version: str
    cache_hit: bool
    latency_ms: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    warnings: list[str] = field(default_factory=list)
    tier: str | None = None  # tier the answer came from; None when the task names a literal model
    escalated_from: str | None = None  # the tier that failed before this answer, if escalation happened


class LLMError(Exception):
    """Raised when a call fails after retries or the output never validates."""


class ProviderUnavailable(LLMError):
    """The provider cannot be used at all (no CLI, not logged in, no API key, package missing).

    Not retried. It propagates out of the stage so that the run fails with this message.
    The text is shown to the user, so it must be clear and must never contain a secret.
    """


class LLMClient(Protocol):
    async def complete(
        self,
        task: str,  # key in config/models.toml [tasks], e.g. "read_page"
        prompt: str,  # prompt name in prompts/, e.g. "read_page"; latest version unless pinned in config
        variables: dict,  # values rendered into the prompt template
        schema: type[T],  # Pydantic model the response must validate against
        images: list[ImageInput] | None = None,
        tier: str | None = None,  # override the task's tier, e.g. "hard"; None = the task's own tier
    ) -> tuple[T, CallMeta]:
        ...
