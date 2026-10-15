"""Stage 3: read a page image with a vision model (ARCHITECTURE §4.2).

Pure orchestration, no I/O of its own: the caller passes an `LLMClient` and a
`render(rotation)` function that returns PNG bytes of the page rotated clockwise
by `rotation` degrees (rendered at vision dpi by the caller).

Flow:
1. Read the page as rendered (rotation 0). One retry after an LLMError (the
   client already retries transport errors itself). Two failures -> unreadable.
2. If the model reports rotation r != 0, render with r and read once more. The
   second read is kept only if its read_quality is higher. There is never a third read.
3. Empty or near-empty text is classified: blank page, or unreadable when the
   model also marked illegible fragments.

read_quality (0..1), computed by `_read_quality`:
  base      = score_text_layer(text).word_ratio      (share of real words, engine.textlayer)
  penalty   = min(0.3, 0.05 * illegible_fragments)
  q         = clamp(base - penalty, 0, 1)
  If text_layer is non-empty and score_text_layer(text_layer).score >= 0.5 and
  rapidfuzz is importable, blend in agreement with the text layer:
  agreement = fuzz.token_set_ratio(text, text_layer) / 100
  q         = 0.7 * q + 0.3 * agreement, clamped to 0..1.
  Without the blend, q is the base minus the penalty. Blank page: 1.0. Unreadable: 0.0.

Failures never propagate as exceptions from `read_page`: they come back as
`unreadable=True` with the error text in `warnings`. Errors from `render` are
not LLM failures and do propagate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from engine.textlayer import score_text_layer
from llm.client import CallMeta, ImageInput, LLMClient, LLMError, ProviderUnavailable
from llm.schemas import PageRead

MIN_READ_CHARS = 20          # below this many non-space characters the page counts as empty
ILLEGIBLE_STEP = 0.05        # quality lost per illegible fragment
MAX_ILLEGIBLE_PENALTY = 0.3
TEXT_LAYER_MIN_SCORE = 0.5   # text layer must be at least this good to be used as a reference
AGREEMENT_WEIGHT = 0.3
RETRIES = 1                  # one retry at this level, after the client's own retries


@dataclass
class PageReadResult:
    text: str
    rotation: int            # rotation applied to get the text that was kept (0/90/180/270)
    has_stamp: str           # "yes" | "no" | "unsure"
    has_signature: str
    has_qr: str
    read_quality: float      # 0..1, see module docstring
    unreadable: bool
    warnings: list[str]
    metas: list[CallMeta] = field(default_factory=list)


async def read_page(llm: LLMClient, label: str, render: Callable[[int], bytes],
                    text_layer: str = "") -> PageReadResult:
    """Read one page with the vision model; see the module docstring for the rules."""
    warnings: list[str] = []
    metas: list[CallMeta] = []

    first = await _ask(llm, label, render(0), warnings, metas)
    if first is None:
        return _failed(warnings, metas)

    chosen, rotation = first, 0
    if first.rotation != 0:
        second = await _ask(llm, label, render(first.rotation), warnings, metas)
        if second is not None:
            if second.rotation != 0:
                warnings.append(f"rotation still reported as {second.rotation} after re-read")
            if _read_quality(second.text, second.illegible_fragments, text_layer) > _read_quality(
                first.text, first.illegible_fragments, text_layer
            ):
                chosen, rotation = second, first.rotation
                warnings.append(f"page rotated {first.rotation} degrees before reading")

    text = chosen.text.strip()
    chars = len("".join(text.split()))
    illegible = max(0, chosen.illegible_fragments)
    unreadable = False
    if chars < MIN_READ_CHARS and illegible > 0:
        unreadable = True
        quality = 0.0
        warnings.append(f"no readable text ({illegible} illegible fragments)")
    elif chars == 0:
        quality = 1.0
        warnings.append("blank page")
    else:
        quality = _read_quality(text, illegible, text_layer)

    return PageReadResult(
        text=text,
        rotation=rotation,
        has_stamp=chosen.has_stamp,
        has_signature=chosen.has_signature,
        has_qr=chosen.has_qr,
        read_quality=quality,
        unreadable=unreadable,
        warnings=warnings,
        metas=metas,
    )


async def _ask(llm: LLMClient, label: str, png: bytes,
               warnings: list[str], metas: list[CallMeta]) -> PageRead | None:
    """One read_page call with one retry on LLMError. None when both attempts fail.
    ProviderUnavailable is raised at once: no retry, and the run fails with its message."""
    for attempt in range(RETRIES + 1):
        try:
            out, meta = await llm.complete(
                task="read_page",
                prompt="read_page",
                variables={"label": label},
                schema=PageRead,
                images=[ImageInput(png, label)],
            )
        except ProviderUnavailable:
            raise
        except LLMError as exc:
            if attempt < RETRIES:
                warnings.append(f"read_page failed, retrying: {exc}")
                continue
            warnings.append(f"read_page failed after retry: {exc}")
            return None
        metas.append(meta)
        return out
    return None  # not reached


def _failed(warnings: list[str], metas: list[CallMeta]) -> PageReadResult:
    return PageReadResult(
        text="",
        rotation=0,
        has_stamp="unsure",
        has_signature="unsure",
        has_qr="unsure",
        read_quality=0.0,
        unreadable=True,
        warnings=warnings,
        metas=metas,
    )


def _read_quality(text: str, illegible: int, text_layer: str) -> float:
    """The read_quality formula from the module docstring."""
    base = score_text_layer(text, min_chars=1).word_ratio
    penalty = min(MAX_ILLEGIBLE_PENALTY, ILLEGIBLE_STEP * max(0, illegible))
    quality = min(1.0, max(0.0, base - penalty))
    if text_layer and score_text_layer(text_layer).score >= TEXT_LAYER_MIN_SCORE:
        agreement = _agreement(text, text_layer)
        if agreement is not None:
            quality = (1 - AGREEMENT_WEIGHT) * quality + AGREEMENT_WEIGHT * agreement
    return min(1.0, max(0.0, quality))


def _agreement(text: str, text_layer: str) -> float | None:
    """token_set_ratio of the two texts as 0..1, or None when rapidfuzz is not installed."""
    try:
        from rapidfuzz import fuzz  # lazy: optional at import time
    except ImportError:
        return None
    return fuzz.token_set_ratio(text, text_layer) / 100.0


__all__ = ["PageReadResult", "read_page"]
