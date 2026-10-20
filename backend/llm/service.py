"""DefaultLLMClient: the LLMClient implementation (ARCHITECTURE §6.2-6.4).

Per call: resolve the tier and model (models.toml [tasks] -> [tiers]), resolve the prompt
version, render the prompt, compute the cache key, return the stored response on a hit,
otherwise call the provider through the rate limiter with retries. The text is parsed
into the Pydantic schema; on failure one repair call is made. If the answer still does
not validate and a higher tier exists, the whole call is retried once on that tier
(escalation). Every provider response is stored in LlmCall, repair and escalated calls
included.

Models are never switched silently. If the provider answered with another model, the
warning "model_switched" is added and the caller marks the run non-reproducible.
ProviderUnavailable is never retried and propagates to the caller.
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlmodel import Session
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from app import settings as app_settings
from app.models import LlmCall
from llm import cache
from llm.client import CallMeta, ImageInput, LLMError
from llm.prompts import PromptError, load_prompt, render
from llm.providers import (
    ChatMessage,
    Provider,
    ProviderRequest,
    ProviderResponse,
    TransientProviderError,
    get_provider,
)
from llm.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MODEL_SWITCHED = "model_switched"
ESCALATION_REASON = "invalid_json"
REPAIR_TEMPLATE = "Your JSON failed validation: {errors}. Return only valid JSON matching the schema."
MAX_BACKOFF_S = 16.0  # backoff 2, 4, 8, 16, 16... seconds, each multiplied by a random factor in JITTER
JITTER = (0.75, 1.25)
MAX_RETRY_AFTER_S = 120.0
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class LLMLimits:
    """The [limits] table of models.toml."""
    parallel_calls: int = 4
    requests_per_minute: int = 18
    max_retries: int = 5  # total attempts per provider call (retries = max_retries - 1)
    request_timeout_s: float = 180.0
    temperature: float = 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "LLMLimits":
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


class CallStats:
    """In-process counters of one client (the pipeline builds one client per run).

    One complete() call is one count, as in the engine's per-stage llm_calls. It is a
    cache hit when every request it made was answered from the cache. snapshot() is
    JSON-able and is read by engine.pipeline.llm_metrics_of:
    - counts: {"task|model": n} complete() calls that reached a provider;
    - cache_hits_by: {"task|model": n} complete() calls answered from the cache;
    - cache_hits, tokens_in, tokens_out, cost_usd: totals over provider requests (a hit costs nothing);
    - escalations: [{task, from, to, reason}].
    reset() starts again from zero.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._counts: dict[str, int] = {}
        self._cache_hits_by: dict[str, int] = {}
        self._escalations: list[dict[str, str]] = []
        self._tokens_in = 0
        self._tokens_out = 0
        self._cost_usd = 0.0

    def record(self, meta: CallMeta) -> None:
        key = f"{meta.task}|{meta.model_requested}"
        if meta.cache_hit:
            self._cache_hits_by[key] = self._cache_hits_by.get(key, 0) + 1
            return
        self._counts[key] = self._counts.get(key, 0) + 1
        self._tokens_in += meta.tokens_in or 0
        self._tokens_out += meta.tokens_out or 0
        self._cost_usd += meta.cost or 0.0

    def record_escalation(self, task: str, from_tier: str, to_tier: str, reason: str) -> None:
        self._escalations.append({"task": task, "from": from_tier, "to": to_tier, "reason": reason})

    def snapshot(self) -> dict[str, Any]:
        return {
            "counts": dict(sorted(self._counts.items())),
            "cache_hits_by": dict(sorted(self._cache_hits_by.items())),
            "cache_hits": sum(self._cache_hits_by.values()),
            "tokens_in": self._tokens_in,
            "tokens_out": self._tokens_out,
            "cost_usd": round(self._cost_usd, 6),
            "escalations": [dict(item) for item in self._escalations],
        }


@dataclass
class _Attempt:
    text: str
    model_answered: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost: float | None
    latency_ms: int
    cache_hit: bool


class DefaultLLMClient:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        provider: Provider,
        *,
        provider_name: str,
        tasks: Mapping[str, str],
        prompt_versions: Mapping[str, str] | None = None,
        limits: LLMLimits | None = None,
        prompts_dir: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tiers: Mapping[str, str] | None = None,
        escalation_order: Sequence[str] = (),
        escalation_enabled: bool = True,
        effort: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._provider_name = provider_name
        self._tasks = dict(tasks)
        self._tiers = dict(tiers or {})
        self._escalation_order = tuple(escalation_order)
        self._escalation_enabled = escalation_enabled
        self._effort = dict(effort or {})
        self._prompt_versions = dict(prompt_versions or {})
        self._limits = limits or LLMLimits()
        self._prompts_dir = prompts_dir
        self._sleep = sleep
        self._limiter = RateLimiter(self._limits.parallel_calls, self._limits.requests_per_minute)
        self.stats = CallStats()

    async def complete(
        self,
        task: str,
        prompt: str,
        variables: dict,
        schema: type[T],
        images: list[ImageInput] | None = None,
        tier: str | None = None,
    ) -> tuple[T, CallMeta]:
        tier_name, model = self._resolve(task, tier)
        version, system_text, user_template = self._load_prompt(prompt)
        messages = [ChatMessage("user", self._render(user_template, variables), tuple(images or ()))]
        if system_text:
            # The system section is a template too (e.g. the doc_types list in page_cards).
            messages.insert(0, ChatMessage("system", self._render(system_text, variables)))

        parsed, meta, error = await self._complete_on(task, tier_name, model, version, messages, schema)
        self.stats.record(meta)
        if parsed is not None:
            return parsed, meta

        next_name = _next_in(self._escalation_order, self._escalation_enabled, tier_name) if tier_name else None
        if next_name is None:
            raise LLMError(f"{task}: output failed validation after one repair call: {error}")

        logger.warning("escalation task=%s from=%s to=%s reason=%s", task, tier_name, next_name, ESCALATION_REASON)
        self.stats.record_escalation(task, tier_name, next_name, ESCALATION_REASON)
        # Fresh request on the next tier: the original messages, not the repair transcript.
        parsed, escalated, error = await self._complete_on(
            task, next_name, self._model_of_tier(next_name), version, messages, schema
        )
        self.stats.record(escalated)
        if parsed is None:
            raise LLMError(f"{task}: output failed validation after escalation to {next_name}: {error}")
        escalated.escalated_from = tier_name
        return parsed, escalated

    async def _complete_on(
        self,
        task: str,
        tier_name: str | None,
        model: str,
        version: str,
        messages: list[ChatMessage],
        schema: type[BaseModel],
    ) -> tuple[BaseModel | None, CallMeta, str | None]:
        """One call on one model: the first answer, plus one repair call if it does not validate."""
        effort = self._effort.get(tier_name) if tier_name else None
        first = await self._send(task, model, version, messages, schema, effort)
        parsed, error = _parse_output(first.text, schema)
        attempts = [first]
        if parsed is None:
            repair = [
                *messages,
                ChatMessage("assistant", first.text),
                ChatMessage("user", REPAIR_TEMPLATE.format(errors=error)),
            ]
            second = await self._send(task, model, version, repair, schema, effort)
            attempts.append(second)
            parsed, error = _parse_output(second.text, schema)
        return parsed, _combine_meta(task, model, version, attempts, tier_name), error

    # ----- one cached request -----

    async def _send(
        self,
        task: str,
        model: str,
        version: str,
        messages: list[ChatMessage],
        schema: type[BaseModel],
        effort: str | None = None,
    ) -> _Attempt:
        temperature = self._limits.temperature
        image_hashes = [image_hash(image) for message in messages for image in message.images]
        key = cache.cache_key(
            self._provider_name,
            model,
            version,
            [{"role": m.role, "content": m.text} for m in messages],
            image_hashes,
            temperature,
            schema.__name__,
            extra={"effort": effort} if effort else None,
        )
        with self._session_factory() as session:
            row = cache.get(session, key)
            if row is not None:
                # Nothing was spent on a hit, so cost and latency are zero; tokens are the original call's.
                return _Attempt(row.response, row.model_answered, row.tokens_in, row.tokens_out,
                                0.0, 0, cache_hit=True)

        request = ProviderRequest(
            model=model,
            messages=tuple(messages),
            schema=schema,
            schema_name=schema.__name__,
            temperature=temperature,
            timeout_s=self._limits.request_timeout_s,
            effort=effort,
        )
        started = time.perf_counter()
        response = await self._call_provider(request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        call = LlmCall(
            key=key,
            task=task,
            provider=self._provider_name,
            model_requested=model,
            model_answered=response.model_answered,
            prompt_version=version,
            request=_request_log(messages, model, temperature, schema.__name__, effort),
            response=response.text,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost=response.cost,
            latency_ms=latency_ms,
        )
        with self._session_factory() as session:
            stored = cache.put(session, call)
            attempt = _Attempt(stored.response, stored.model_answered, stored.tokens_in,
                               stored.tokens_out, stored.cost, latency_ms, cache_hit=False)
        return attempt

    async def _call_provider(self, request: ProviderRequest) -> ProviderResponse:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(TransientProviderError),
            stop=stop_after_attempt(max(1, self._limits.max_retries)),
            wait=_backoff,
            reraise=True,
            sleep=self._sleep,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._attempt(request)
        except TransientProviderError as exc:
            raise LLMError(f"{request.model}: provider unavailable after retries: {exc}") from exc
        raise LLMError(f"{request.model}: no response after retries")  # not reached; keeps the type checker happy

    async def _attempt(self, request: ProviderRequest) -> ProviderResponse:
        async with self._limiter:
            try:
                return await asyncio.wait_for(self._provider.complete(request), timeout=request.timeout_s)
            except TimeoutError as exc:
                raise TransientProviderError(f"request timed out after {request.timeout_s}s") from exc
            except (LLMError, TransientProviderError):
                raise  # ProviderUnavailable is an LLMError: it passes through unchanged and is not retried
            except Exception as exc:
                raise LLMError(f"{request.model}: provider error: {type(exc).__name__}: {exc}") from exc

    # ----- helpers -----

    def _resolve(self, task: str, tier: str | None) -> tuple[str | None, str]:
        """(tier name or None for a literal model id, model id)."""
        if tier is not None:
            return tier, self._model_of_tier(tier)
        target = self._tasks.get(task)
        if not target:
            raise LLMError(f"no model configured for task '{task}' in models.toml [tasks]")
        if target in self._tiers:
            return target, self._tiers[target]
        return None, target  # old configs and the handoff provider name a model id directly

    def _model_of_tier(self, tier: str) -> str:
        model = self._tiers.get(tier)
        if not model:
            raise LLMError(f"unknown tier '{tier}': not in models.toml [tiers]")
        return model

    def _load_prompt(self, prompt: str) -> tuple[str, str, str]:
        try:
            return load_prompt(prompt, version=self._prompt_versions.get(prompt), prompts_dir=self._prompts_dir)
        except PromptError as exc:
            raise LLMError(str(exc)) from exc

    def _render(self, template: str, variables: Mapping[str, object]) -> str:
        try:
            return render(template, variables)
        except PromptError as exc:
            raise LLMError(str(exc)) from exc


def next_tier(tier: str) -> str | None:
    """The tier one step up the [escalation] order in models.toml, or None.

    None at the top, for an unknown tier, or when escalation is disabled.
    """
    config = app_settings.load_toml(app_settings.get_settings().models_config)
    escalation = config.get("escalation", {})
    return _next_in(tuple(escalation.get("order", ())), bool(escalation.get("enabled", True)), tier)


def default_tier(task: str) -> str | None:
    """The tier a task uses by default in models.toml [tasks]; None when the task names a literal model id."""
    config = app_settings.load_toml(app_settings.get_settings().models_config)
    value = config.get("tasks", {}).get(task, "")
    return value if value in config.get("tiers", {}) else None


def _next_in(order: Sequence[str], enabled: bool, tier: str) -> str | None:
    if not enabled or tier not in order:
        return None
    index = list(order).index(tier)
    return order[index + 1] if index + 1 < len(order) else None


def provider_name() -> str:
    """The provider this install uses: LLM_PROVIDER (env wins), else models.toml `provider`, else claude_cli.

    A `:agent` suffix in LLM_PROVIDER is ignored here; the cache identity carries it.
    """
    settings = app_settings.get_settings()
    config = app_settings.load_toml(settings.models_config)
    name = (settings.llm_provider or config.get("provider") or "claude_cli").strip()
    return name.split(":", 1)[0].strip()


def cache_provider_name(name: str | None = None) -> str:
    """The provider identity in the LLM cache and in stage answer keys.

    The OpenCode CLI answer depends on the agent as well as the model. The built-in
    `plan` agent is part of the name, so switching agents cannot reuse old answers.
    """
    base = (name or provider_name()).strip()
    if base == "opencode_cli":
        agent = (app_settings.get_settings().opencode_agent or "plan").strip()
        return f"opencode_cli:{agent}"
    return base


def get_llm_client(session_factory: Callable[[], Session]) -> DefaultLLMClient:
    """Build the client from settings (env wins) and models.toml."""
    settings = app_settings.get_settings()
    config = app_settings.load_toml(settings.models_config)
    name = provider_name()
    limits_data = dict(config.get("limits", {}))
    if settings.llm_concurrency > 0:
        limits_data["parallel_calls"] = settings.llm_concurrency
    escalation = config.get("escalation", {})
    return DefaultLLMClient(
        session_factory,
        get_provider(name),
        provider_name=cache_provider_name(name),
        tasks=config.get("tasks", {}),
        prompt_versions=config.get("prompt_versions", {}),
        limits=LLMLimits.from_dict(limits_data),
        prompts_dir=settings.prompts_dir,
        tiers=config.get("tiers", {}),
        escalation_order=tuple(escalation.get("order", ())),
        escalation_enabled=bool(escalation.get("enabled", True)),
        effort=config.get("effort", {}),
    )


def image_hash(image: ImageInput) -> str:
    return hashlib.sha256(image.png_bytes).hexdigest()


def _request_log(messages: list[ChatMessage], model: str, temperature: float, schema_name: str,
                 effort: str | None = None) -> dict:
    """Request as stored in LlmCall.request: images replaced by their hashes. Every input of the cache
    key is here (engine.demo recomputes the key from it), so `effort` is kept when it is set."""
    log: dict = {
        "model": model,
        "temperature": temperature,
        "schema": schema_name,
        "messages": [
            {
                "role": m.role,
                "content": m.text,
                "image_hashes": [image_hash(image) for image in m.images],
            }
            for m in messages
        ],
    }
    if effort:
        log["effort"] = effort
    return log


def _combine_meta(
    task: str, model: str, version: str, attempts: list[_Attempt], tier: str | None
) -> CallMeta:
    answered = next((a.model_answered for a in reversed(attempts) if a.model_answered), None)
    switched = any(a.model_answered and not _same_model(a.model_answered, model) for a in attempts)
    return CallMeta(
        task=task,
        model_requested=model,
        model_answered=answered,
        prompt_version=version,
        cache_hit=all(a.cache_hit for a in attempts),
        latency_ms=sum(a.latency_ms for a in attempts),
        tokens_in=_sum_optional(a.tokens_in for a in attempts),
        tokens_out=_sum_optional(a.tokens_out for a in attempts),
        cost=_sum_optional(a.cost for a in attempts),
        warnings=[MODEL_SWITCHED] if switched else [],
        tier=tier,
    )


def _same_model(answered: str, requested: str) -> bool:
    """OpenRouter may drop the ':free' variant suffix from the answering model id; that is not a switch."""
    return answered.split(":", 1)[0] == requested.split(":", 1)[0]


def _sum_optional(values: Iterable[float | None]) -> float | None:
    numbers = [v for v in values if v is not None]
    return sum(numbers) if numbers else None


def _backoff(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome is not None else None
    if isinstance(exc, TransientProviderError) and exc.retry_after is not None:
        return min(max(exc.retry_after, 0.0), MAX_RETRY_AFTER_S)
    base = float(min(2 ** state.attempt_number, MAX_BACKOFF_S))
    return base * random.uniform(*JITTER)


def _parse_output(text: str, schema: type[T]) -> tuple[T | None, str | None]:
    """Return (parsed, None) on success, or (None, error message) for the repair prompt."""
    last_error = "no JSON object found in the response"
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON ({exc.msg} at character {exc.pos})"
            continue
        try:
            return schema.model_validate(data), None
        except ValidationError as exc:
            return None, _format_errors(exc)
    return None, last_error


def _json_candidates(text: str) -> Iterator[str]:
    """Outermost {...} of the whole text, then of the first ``` fenced block if it differs."""
    text = text.strip()
    outer = _outermost_object(text)
    if outer is not None:
        yield outer
    fenced = _FENCE.search(text)
    if fenced is not None:
        inner = _outermost_object(fenced.group(1))
        if inner is not None and inner != outer:
            yield inner


def _outermost_object(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else None


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'root'}: {err['msg']}" for err in exc.errors()
    )
