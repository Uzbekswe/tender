"""Quality escalation shared by the stages (brief B): re-ask a unit once on a stronger tier.

- cards (engine.cards): a batch whose filled fields are dropped too often, or that has a page
  without a card, is re-asked once on the next tier.
- extract (engine.tender_stages): a window whose requirement quotes fail verification too often
  is re-asked once on the next tier; the answer with more verified requirements is kept.
- match (engine.bid_stages): a mandatory requirement with conflict or doubt is re-asked alone once
  with task "match_hard" (tier hard).

Every escalation is written to Run.progress["llm"]["escalations"] as
{stage, unit, from_tier, to_tier, reason}; an entry for the same (stage, unit) is written only once.

ProviderUnavailable (the CLI is missing or not logged in) is never escalated and never swallowed:
stages re-raise it, so the run fails with its message.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.db import session_scope
from app.models import Run
from app.runner import _rmw_lock
from llm.client import ProviderUnavailable

__all__ = [
    "ProviderUnavailable",
    "default_tier",
    "escalation_tiers",
    "raise_first_error",
    "record_escalations",
]


def default_tier(task: str) -> str | None:
    """The tier a task uses by default (config/models.toml). None when the config names none."""
    from llm.service import default_tier as _default_tier

    return _default_tier(task)


def escalation_tiers(task: str) -> tuple[str, str] | None:
    """(default tier, next tier) of a task, or None when it is already on the top tier."""
    from llm.service import next_tier

    current = default_tier(task)
    if current is None:
        return None
    higher = next_tier(current)
    if higher is None:
        return None
    return current, higher


def record_escalations(run_id: int, entries: Iterable[dict[str, Any]]) -> None:
    """Append escalation entries to Run.progress["llm"]["escalations"], once per (stage, unit)."""
    entries = list(entries)
    if not entries:
        return
    with _rmw_lock, session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        llm = dict(run.progress.get("llm") or {})
        logged = list(llm.get("escalations") or [])
        seen = {(item.get("stage"), item.get("unit")) for item in logged}
        for entry in entries:
            key = (entry["stage"], entry["unit"])
            if key in seen:
                continue
            seen.add(key)
            logged.append({
                "stage": entry["stage"],
                "unit": entry["unit"],
                "from_tier": entry.get("from_tier"),
                "to_tier": entry.get("to_tier"),
                "reason": entry.get("reason", ""),
            })
        llm["escalations"] = logged
        run.progress = {**run.progress, "llm": llm}  # reassigned: SQLAlchemy sees the change
        session.add(run)


def raise_first_error(results: Iterable[Any]) -> None:
    """After asyncio.gather(..., return_exceptions=True): re-raise the ProviderUnavailable if there is
    one, else the first error. Lets the other units finish before the stage fails."""
    errors = [item for item in results if isinstance(item, BaseException)]
    if not errors:
        return
    for error in errors:
        if isinstance(error, ProviderUnavailable):
            raise error
    raise errors[0]
