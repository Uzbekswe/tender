"""Scores, tender points, verdict, forecast and impact (docs/ARCHITECTURE.md §7, spec §7).

Pure functions: no database, no LLM. engine.score_stages turns table rows into ScoreItem and
SchemeView values, and passes the latest status overrides in `overrides` (or already applied).

Conventions
- Items with excluded=True are ignored everywhere. So are platform items (platform=True): the
  e-procurement platform confirms them, so they never move a percentage, the range, a counter,
  the tender points, the verdict or the forecast.
- S = sum(w * k) / sum(w) over decided items. Needs-review items are left out of S and shown as
  a range: S_min counts them as k = 0 and S_max as k = 1, with their full weight in the
  denominator. A review item therefore never moves the headline number on its own.
- Weights: mandatory / desirable from settings; scored items take the linked criterion's
  max_points when a scheme exists and the criterion is known, else the settings weight.
- Percentages are rounded to 0.1 at the output only. Sums are kept exact until then.
- Tender points (§7.2): each criterion gets a band [lo, hi] from its linked items' statuses. The
  band of one item is: meets -> top level, partial -> middle level, not confirmed or does not
  meet -> zero level, needs review -> lowest..highest level. A criterion with several linked
  items takes the weakest value of each end. Machine-readable level rules (`rule`) are not
  evaluated yet; the level comes from the status only.
- Forecast: every non-meets item whose action_type is not request_clarification or not_fixable
  becomes meets (an item without an action_type counts as fixable).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

MEETS = "meets"
PARTIAL = "partial"
NOT_CONFIRMED = "not_confirmed"
DOES_NOT_MEET = "does_not_meet"
NEEDS_REVIEW = "needs_review"
STATUSES: tuple[str, ...] = (MEETS, PARTIAL, NOT_CONFIRMED, DOES_NOT_MEET, NEEDS_REVIEW)

MANDATORY = "mandatory"
SCORED = "scored"
DESIRABLE = "desirable"
GOODS = "goods"
BIDDER = "bidder"

READY = "ready"
NEEDS_REVISION = "needs_revision"
REJECTION_RISK = "rejection_risk"

POINTS_PASS = "pass"
POINTS_FAIL = "fail"
POINTS_UNCERTAIN = "uncertain"

NOT_FORECAST_ACTIONS = frozenset({"request_clarification", "not_fixable"})
FORECAST_FIXABLE = frozenset({"partial", "not_confirmed", "does_not_meet"})


def _val(x: Any) -> Any:
    """Enum members and plain values alike: the stored string."""
    return getattr(x, "value", x)


# ---------- inputs ----------

@dataclass(frozen=True)
class ScoreItem:
    """One requirement with its effective status (after any override)."""
    requirement_id: int
    code: str
    scope: str                       # goods | bidder
    category: str
    req_type: str                    # mandatory | scored | desirable
    status: str                      # meets | partial | not_confirmed | does_not_meet | needs_review
    criterion_id: int | None = None
    action_type: str | None = None   # None when there is no recommendation (meets)
    excluded: bool = False
    names_confirming_doc: bool = False  # the tender names the document that confirms the item
    platform: bool = False              # verified on the e-procurement platform, not by documents


@dataclass(frozen=True)
class LevelView:
    points: float
    condition_text: str = ""
    rule: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CriterionView:
    id: int
    number: int
    name: str
    max_points: float
    levels: tuple[LevelView, ...] = ()


@dataclass(frozen=True)
class SchemeView:
    stated_max: float | None
    pass_threshold: float | None
    criteria: tuple[CriterionView, ...] = ()
    platform_scored: tuple[Mapping[str, Any], ...] = ()


_DEFAULT_WEIGHTS = {MANDATORY: 3.0, SCORED: 2.0, DESIRABLE: 1.0}
_DEFAULT_K = {MEETS: 1.0, PARTIAL: 0.5, NOT_CONFIRMED: 0.3, DOES_NOT_MEET: 0.0}


@dataclass(frozen=True)
class ScoringSettings:
    review_threshold: float = 80.0
    weights: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    k: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_K))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ScoringSettings:
        """Build from config/scoring.toml merged with Project.settings ([weights], [k], review_threshold)."""
        weights = {**_DEFAULT_WEIGHTS, **{str(n): float(v) for n, v in (data.get("weights") or {}).items()}}
        k = {**_DEFAULT_K, **{str(n): float(v) for n, v in (data.get("k") or {}).items()}}
        return cls(
            review_threshold=float(data.get("review_threshold", 80.0)),
            weights=weights,
            k=k,
        )


# ---------- outputs ----------

@dataclass(frozen=True)
class CriterionPoints:
    criterion_id: int
    number: int
    name: str
    max_points: float
    points_min: float
    points_max: float
    linked_requirement_ids: tuple[int, ...]


@dataclass(frozen=True)
class ScoreResult:
    overall: float
    range_min: float
    range_max: float
    goods: float | None
    bidder: float | None
    by_category: dict[str, float | None]
    mandatory_met: int
    mandatory_total: int
    counts: dict[str, int]
    forecast: float
    verdict: str
    deciding_requirement_ids: list[int]
    tender_points_min: float | None
    tender_points_max: float | None
    pass_threshold: float | None
    tender_points_verdict: str | None
    criteria_points: list[CriterionPoints]
    scheme_mismatch: bool
    criteria_sum: float | None


# ---------- internals ----------

Pair = tuple[ScoreItem, str]


def _effective(items: Iterable[ScoreItem], overrides: Mapping[int, Any] | None) -> list[Pair]:
    ov = {int(k): _val(v) for k, v in (overrides or {}).items()}
    pairs: list[Pair] = []
    for item in items:
        if item.excluded or item.platform:
            continue
        status = ov.get(item.requirement_id, _val(item.status))
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r} for requirement {item.requirement_id}")
        pairs.append((item, status))
    return pairs


def _weight(item: ScoreItem, settings: ScoringSettings, criteria: Mapping[int, CriterionView]) -> float:
    if item.req_type == SCORED and item.criterion_id is not None and item.criterion_id in criteria:
        return float(criteria[item.criterion_id].max_points)
    return float(settings.weights[item.req_type])


def _share(pairs: list[Pair], weight_of: Any, k_of: Any) -> float | None:
    """S over decided items (review items excluded). None when nothing is decided."""
    num = den = 0.0
    for item, status in pairs:
        if status == NEEDS_REVIEW:
            continue
        w = weight_of(item)
        den += w
        num += w * k_of(status)
    return None if den == 0 else 100.0 * num / den


def _range(pairs: list[Pair], weight_of: Any, k_of: Any) -> tuple[float, float]:
    total = sum(weight_of(item) for item, _ in pairs)
    if total == 0:
        return 0.0, 0.0
    decided = sum(weight_of(item) * k_of(s) for item, s in pairs if s != NEEDS_REVIEW)
    review = sum(weight_of(item) for item, s in pairs if s == NEEDS_REVIEW)
    return 100.0 * decided / total, 100.0 * (decided + review) / total


def _level_points(c: CriterionView) -> tuple[float, float, float, float, float]:
    """(top, zero, lowest, highest, middle) points of a criterion's levels."""
    pts = sorted({float(level.points) for level in c.levels})
    if not pts:
        # No levels recorded: meets = max, partial = half of max, else 0; review = 0..max.
        top = float(c.max_points)
        return top, 0.0, 0.0, top, top * 0.5
    top, lowest, highest = pts[-1], pts[0], pts[-1]
    zero = 0.0 if 0.0 in pts else lowest
    between = [p for p in pts if zero < p < top]
    if between:
        middle = min(between, key=lambda p: (abs(p - top / 2), -p))
    else:
        below = [p for p in pts if p <= top / 2]
        middle = max(below) if below else zero  # top * 0.5 rounded down to an existing level
    return top, zero, lowest, highest, middle


def _band_for(c: CriterionView, status: str) -> tuple[float, float]:
    top, zero, lowest, highest, middle = _level_points(c)
    if status == MEETS:
        return top, top
    if status == PARTIAL:
        return middle, middle
    if status in (NOT_CONFIRMED, DOES_NOT_MEET):
        return zero, zero
    return lowest, highest  # needs review: anything between the lowest and highest level


def _criterion_band(c: CriterionView, statuses: list[str]) -> tuple[float, float]:
    if not statuses:
        _, _, lowest, highest, _ = _level_points(c)
        return lowest, highest
    bands = [_band_for(c, s) for s in statuses]
    return min(lo for lo, _ in bands), min(hi for _, hi in bands)


def _tender_points(
    pairs: list[Pair], scheme: SchemeView | None
) -> tuple[list[CriterionPoints], float | None, float | None]:
    if scheme is None or not scheme.criteria:
        return [], None, None
    linked: dict[int, list[tuple[int, str]]] = {}
    for item, status in pairs:
        if item.criterion_id is not None:
            linked.setdefault(item.criterion_id, []).append((item.requirement_id, status))
    points: list[CriterionPoints] = []
    for c in sorted(scheme.criteria, key=lambda x: (x.number, x.id)):
        rows = linked.get(c.id, [])
        lo, hi = _criterion_band(c, [s for _, s in rows])
        points.append(CriterionPoints(
            criterion_id=c.id,
            number=c.number,
            name=c.name,
            max_points=float(c.max_points),
            points_min=lo,
            points_max=hi,
            linked_requirement_ids=tuple(rid for rid, _ in rows),
        ))
    return points, sum(p.points_min for p in points), sum(p.points_max for p in points)


def _criteria_sum(scheme: SchemeView | None) -> float | None:
    if scheme is None:
        return None
    return float(sum(c.max_points for c in scheme.criteria))


def _pct(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


# ---------- public helpers ----------

def points_verdict(points_min: float | None, points_max: float | None, threshold: float | None) -> str | None:
    """pass when the lower estimate reaches the threshold, fail when even the upper one does not, else uncertain."""
    if threshold is None or points_min is None or points_max is None:
        return None
    if points_min >= threshold:
        return POINTS_PASS
    if points_max < threshold:
        return POINTS_FAIL
    return POINTS_UNCERTAIN


def scheme_mismatch(scheme: SchemeView | None) -> bool:
    """True when the criteria maxima do not add up to the stated maximum (14 vs 15 on the test tender)."""
    if scheme is None or scheme.stated_max is None:
        return False
    return not math.isclose(_criteria_sum(scheme) or 0.0, scheme.stated_max, abs_tol=1e-9)


def _raw_overall(
    items: Iterable[ScoreItem], scheme: SchemeView | None, settings: ScoringSettings,
    overrides: Mapping[int, Any] | None,
) -> float:
    criteria = {c.id: c for c in scheme.criteria} if scheme is not None else {}
    pairs = _effective(items, overrides)
    value = _share(pairs, lambda it: _weight(it, settings, criteria), lambda s: settings.k[s])
    return value if value is not None else 0.0


# ---------- the score ----------

def compute(
    items: list[ScoreItem],
    scheme: SchemeView | None,
    settings: ScoringSettings,
    overrides: Mapping[int, Any] | None = None,
) -> ScoreResult:
    """Score, range, splits, counters, tender points, verdict and forecast (§7.1 to §7.3)."""
    criteria = {c.id: c for c in scheme.criteria} if scheme is not None else {}
    pairs = _effective(items, overrides)

    def weight_of(item: ScoreItem) -> float:
        return _weight(item, settings, criteria)

    def k_of(status: str) -> float:
        return float(settings.k[status])

    overall = _share(pairs, weight_of, k_of)
    range_min, range_max = _range(pairs, weight_of, k_of)
    goods = _share([p for p in pairs if p[0].scope == GOODS], weight_of, k_of)
    bidder = _share([p for p in pairs if p[0].scope == BIDDER], weight_of, k_of)
    by_category = {
        cat: _pct(_share([p for p in pairs if p[0].category == cat], weight_of, k_of))
        for cat in sorted({item.category for item, _ in pairs})
    }

    mandatory = [p for p in pairs if p[0].req_type == MANDATORY]
    counts = {s: 0 for s in STATUSES}
    for _, status in pairs:
        counts[status] += 1

    # Only real problems with a concrete fix count as fixed; review items stay under review
    # (they are a check, not a fix), and "check manually" promises nothing.
    forecast_pairs: list[Pair] = [
        (item, MEETS if status in FORECAST_FIXABLE and item.action_type
         and item.action_type not in NOT_FORECAST_ACTIONS and item.action_type != "check_manually" else status)
        for item, status in pairs
    ]
    # Conservative: items still under review count as not met (the low end of the range),
    # so the forecast says what the fixes alone guarantee.
    forecast = _range(forecast_pairs, weight_of, k_of)[0]

    criteria_points, pmin, pmax = _tender_points(pairs, scheme)
    threshold = scheme.pass_threshold if scheme is not None else None
    gate = threshold is not None and bool(criteria)
    rej_points = gate and pmax is not None and pmax < threshold
    block_points = gate and pmin is not None and pmin < threshold

    linked_nonmeet = [item.requirement_id for item, s in pairs if item.criterion_id is not None and s != MEETS]
    trig_rejection = [
        item.requirement_id for item, s in mandatory
        if s == DOES_NOT_MEET or (s == NOT_CONFIRMED and item.names_confirming_doc)
    ]
    mandatory_open = [item.requirement_id for item, s in mandatory if s != MEETS]

    if trig_rejection or rej_points:
        verdict = REJECTION_RISK
        deciding = trig_rejection + (linked_nonmeet if rej_points else [])
    elif mandatory and not mandatory_open and not block_points:  # no mandatory item at all is not Ready
        verdict = READY
        deciding = [item.requirement_id for item, _ in mandatory]
    else:
        verdict = NEEDS_REVISION
        deciding = mandatory_open + (linked_nonmeet if block_points else [])

    return ScoreResult(
        overall=_pct(overall if overall is not None else 0.0),
        range_min=_pct(range_min),
        range_max=_pct(range_max),
        goods=_pct(goods),
        bidder=_pct(bidder),
        by_category=by_category,
        mandatory_met=sum(1 for _, s in mandatory if s == MEETS),
        mandatory_total=len(mandatory),
        counts=counts,
        forecast=_pct(forecast),
        verdict=verdict,
        deciding_requirement_ids=sorted(set(deciding)),
        tender_points_min=pmin,
        tender_points_max=pmax,
        pass_threshold=threshold,
        tender_points_verdict=points_verdict(pmin, pmax, threshold),
        criteria_points=criteria_points,
        scheme_mismatch=scheme_mismatch(scheme),
        criteria_sum=_criteria_sum(scheme),
    )


def impact(
    items: list[ScoreItem],
    scheme: SchemeView | None,
    settings: ScoringSettings,
    requirement_id: int,
    overrides: Mapping[int, Any] | None = None,
) -> float:
    """Overall score if only this requirement became meets, minus the current overall score (0.1 precision)."""
    base = _raw_overall(items, scheme, settings, overrides)
    after = _raw_overall(items, scheme, settings, {**(overrides or {}), requirement_id: MEETS})
    return round(after - base, 1) + 0.0  # + 0.0 turns -0.0 into 0.0
