"""Scoring, tender points, verdict, forecast and impact (ARCHITECTURE §7). Pure functions, no DB.

Expected numbers are worked out by hand in the comments. Default weights are mandatory 3,
scored 2, desirable 1; k is meets 1, partial 0.5, not confirmed 0.3, does not meet 0.
"""

from __future__ import annotations

import pytest

from engine.scoring import (
    CriterionView,
    LevelView,
    SchemeView,
    ScoreItem,
    ScoringSettings,
    compute,
    impact,
    points_verdict,
    scheme_mismatch,
)

SETTINGS = ScoringSettings()
DEFAULT_LEVELS = {4: [4, 2, 0], 2: [2, 0], 3: [3, 1, 0], 1: [1, 0]}


def _item(
    rid: int,
    status: str,
    *,
    scope: str = "goods",
    req_type: str = "mandatory",
    category: str = "Technical",
    criterion_id: int | None = None,
    action: str | None = None,
    excluded: bool = False,
    names_doc: bool = False,
    platform: bool = False,
) -> ScoreItem:
    prefix = "T" if scope == "goods" else "U"
    return ScoreItem(
        requirement_id=rid,
        code=f"{prefix}-{rid:03d}",
        scope=scope,
        category=category,
        req_type=req_type,
        status=status,
        criterion_id=criterion_id,
        action_type=action,
        excluded=excluded,
        names_confirming_doc=names_doc,
        platform=platform,
    )


def _scheme(maxima=(4, 2, 3, 1, 4), stated: float | None = 15, threshold: float | None = 11) -> SchemeView:
    criteria = []
    for number, mx in enumerate(maxima, start=1):
        levels = tuple(LevelView(points=float(p)) for p in DEFAULT_LEVELS[mx])
        criteria.append(CriterionView(
            id=100 + number, number=number, name=f"Criterion {number}", max_points=float(mx), levels=levels,
        ))
    return SchemeView(stated_max=stated, pass_threshold=threshold, criteria=tuple(criteria))


# ---------- S, range, splits, counters ----------

def test_overall_is_weighted_share_of_decided_items():
    items = [
        _item(1, "meets"),                         # 3 * 1.0
        _item(2, "does_not_meet"),                 # 3 * 0
        _item(3, "partial", req_type="desirable"),  # 1 * 0.5
        _item(4, "not_confirmed", req_type="scored"),  # 2 * 0.3 (no scheme: settings weight 2)
    ]
    # (3 + 0 + 0.5 + 0.6) / (3 + 3 + 1 + 2) = 4.1 / 9 = 45.56 %
    result = compute(items, None, SETTINGS)
    assert result.overall == pytest.approx(45.6)
    assert result.mandatory_met == 1
    assert result.mandatory_total == 2
    assert result.counts == {"meets": 1, "partial": 1, "not_confirmed": 1, "does_not_meet": 1, "needs_review": 0}


def test_needs_review_is_excluded_and_shown_as_range():
    items = [_item(1, "meets"), _item(2, "needs_review")]
    result = compute(items, None, SETTINGS)
    # S uses only item 1: 100 %. Range: item 2 counted as 0 and as 1, weight 3 each, total 6.
    assert result.overall == 100.0
    assert result.range_min == pytest.approx(50.0)
    assert result.range_max == pytest.approx(100.0)


def test_range_brackets_a_partly_reviewed_score():
    items = [_item(1, "meets"), _item(2, "partial"), _item(3, "needs_review")]
    result = compute(items, None, SETTINGS)
    # Decided: meets (3) + partial (3 * 0.5) = 4.5 of 6 -> S = 75 %.
    # Range over total weight 9: S_min = 4.5 / 9 = 50 %, S_max = (4.5 + 3) / 9 = 83.3 %.
    assert result.overall == 75.0
    assert result.range_min == pytest.approx(50.0)
    assert result.range_max == pytest.approx(83.3)


def test_scored_items_take_the_criterion_points_as_weight():
    scheme = _scheme()
    items = [
        _item(1, "meets", req_type="scored", criterion_id=101),        # criterion 1: max 4
        _item(2, "does_not_meet", req_type="scored", criterion_id=102),  # criterion 2: max 2
        _item(3, "partial", req_type="scored", criterion_id=103),      # criterion 3: max 3
    ]
    # (4 * 1 + 2 * 0 + 3 * 0.5) / (4 + 2 + 3) = 5.5 / 9 = 61.1 %
    result = compute(items, scheme, SETTINGS)
    assert result.overall == pytest.approx(61.1)


def test_scored_item_without_a_criterion_uses_the_settings_weight():
    scheme = _scheme()
    items = [
        _item(1, "meets", req_type="scored", criterion_id=None),  # settings weight 2
        _item(2, "does_not_meet", req_type="scored", criterion_id=101),  # criterion 1: weight 4
    ]
    # (2 * 1 + 4 * 0) / (2 + 4) = 33.3 %
    assert compute(items, scheme, SETTINGS).overall == pytest.approx(33.3)


def test_goods_bidder_and_category_split():
    items = [
        _item(1, "meets", scope="goods", category="Technical"),
        _item(2, "does_not_meet", scope="goods", category="Technical"),
        _item(3, "meets", scope="bidder", req_type="desirable", category="Documents"),
    ]
    result = compute(items, None, SETTINGS)
    assert result.goods == 50.0          # 3 / 6
    assert result.bidder == 100.0        # 1 / 1
    assert result.by_category == {"Documents": 100.0, "Technical": 50.0}
    assert result.overall == pytest.approx(57.1)  # (3 + 1) / (3 + 3 + 1)


def test_excluded_items_are_ignored():
    items = [_item(1, "meets"), _item(2, "does_not_meet", excluded=True)]
    result = compute(items, None, SETTINGS)
    assert result.overall == 100.0
    assert result.mandatory_total == 1
    assert sum(result.counts.values()) == 1


def test_overrides_replace_statuses_without_touching_the_items():
    items = [_item(1, "meets"), _item(2, "does_not_meet")]
    base = compute(items, None, SETTINGS)
    changed = compute(items, None, SETTINGS, overrides={2: "meets"})
    assert base.overall == 50.0
    assert changed.overall == 100.0
    assert items[1].status == "does_not_meet"


def test_unknown_status_is_an_error():
    with pytest.raises(ValueError):
        compute([_item(1, "maybe")], None, SETTINGS)


def test_settings_merge_project_overrides_over_defaults():
    project = ScoringSettings.from_mapping({"weights": {"mandatory": 5}, "k": {"partial": 0.25}})
    assert project.weights["mandatory"] == 5.0
    assert project.weights["scored"] == 2.0
    items = [_item(1, "meets"), _item(2, "partial")]
    # (5 * 1 + 5 * 0.25) / (5 + 5) = 62.5 %
    assert compute(items, None, project).overall == 62.5


# ---------- tender points (§7.2) ----------

def test_tender_points_pick_levels_from_statuses_and_flag_the_mismatch():
    scheme = _scheme()  # maxima 4, 2, 3, 1, 4 = 14; stated 15; threshold 11
    items = [
        _item(1, "meets", criterion_id=101),            # level 4 -> 4..4
        _item(2, "needs_review", criterion_id=102),     # lowest 0 .. highest 2
        _item(3, "partial", criterion_id=103),          # middle level 1 -> 1..1
        _item(4, "not_confirmed", criterion_id=104),    # zero level -> 0..0
        _item(5, "needs_review", criterion_id=105),     # 0 .. 4
    ]
    result = compute(items, scheme, SETTINGS)
    assert result.tender_points_min == 5.0     # 4 + 0 + 1 + 0 + 0
    assert result.tender_points_max == 11.0    # 4 + 2 + 1 + 0 + 4
    assert result.pass_threshold == 11.0
    assert result.tender_points_verdict == "uncertain"
    assert result.scheme_mismatch is True
    assert result.criteria_sum == 14.0
    by_number = {c.number: c for c in result.criteria_points}
    assert (by_number[3].points_min, by_number[3].points_max) == (1.0, 1.0)


def test_scheme_mismatch_is_only_a_flag_and_scoring_continues():
    scheme = _scheme(stated=14)
    assert scheme_mismatch(scheme) is False
    assert scheme_mismatch(_scheme(stated=15)) is True
    result = compute([_item(1, "meets", criterion_id=101)], _scheme(stated=15), SETTINGS)
    assert result.overall == 100.0  # still scored


def test_all_criteria_met_is_a_pass():
    scheme = _scheme()
    items = [_item(100 + n, "meets", req_type="scored", criterion_id=100 + n) for n in range(1, 6)]
    result = compute(items, scheme, SETTINGS)
    assert (result.tender_points_min, result.tender_points_max) == (14.0, 14.0)
    assert result.tender_points_verdict == "pass"


def test_all_criteria_not_confirmed_is_a_fail():
    scheme = _scheme()
    items = [_item(100 + n, "not_confirmed", req_type="scored", criterion_id=100 + n) for n in range(1, 6)]
    result = compute(items, scheme, SETTINGS)
    assert result.tender_points_max == 0.0
    assert result.tender_points_verdict == "fail"


def test_partial_without_a_middle_level_rounds_down_to_an_existing_level():
    scheme = SchemeView(
        stated_max=4, pass_threshold=None,
        criteria=(CriterionView(id=1, number=1, name="Only", max_points=4.0,
                                levels=(LevelView(points=4.0), LevelView(points=0.0))),),
    )
    # Levels 4 and 0 only: 4 * 0.5 = 2, rounded down to the existing level 0.
    result = compute([_item(1, "partial", criterion_id=1)], scheme, SETTINGS)
    assert result.tender_points_min == 0.0
    assert result.tender_points_max == 0.0


def test_partial_with_no_levels_recorded_is_half_the_maximum():
    scheme = SchemeView(
        stated_max=4, pass_threshold=None,
        criteria=(CriterionView(id=1, number=1, name="Bare", max_points=4.0, levels=()),),
    )
    result = compute([_item(1, "partial", criterion_id=1)], scheme, SETTINGS)
    assert result.tender_points_min == 2.0
    assert result.tender_points_max == 2.0


def test_points_verdict_thresholds():
    assert points_verdict(11, 14, 11) == "pass"
    assert points_verdict(9, 10, 11) == "fail"
    assert points_verdict(9, 12, 11) == "uncertain"
    assert points_verdict(9, 12, None) is None


# ---------- verdict (§7.3) ----------

def test_ready_when_every_mandatory_item_meets():
    items = [_item(1, "meets"), _item(2, "meets"), _item(3, "partial", req_type="desirable")]
    result = compute(items, None, SETTINGS)
    assert result.verdict == "ready"
    assert result.deciding_requirement_ids == [1, 2]


def test_no_mandatory_items_is_not_ready():
    # A run with nothing mandatory (or nothing matched at all) must not claim "Ready to submit".
    result = compute([_item(1, "meets", req_type="desirable")], None, SETTINGS)
    assert result.verdict == "needs_revision"
    assert compute([], None, SETTINGS).verdict == "needs_revision"


def test_mandatory_does_not_meet_is_rejection_risk_and_it_decides():
    items = [_item(1, "does_not_meet"), _item(2, "meets")]
    result = compute(items, None, SETTINGS)
    assert result.verdict == "rejection_risk"
    assert result.deciding_requirement_ids == [1]


def test_mandatory_not_confirmed_with_a_named_document_is_rejection_risk():
    items = [_item(1, "not_confirmed", names_doc=True), _item(2, "meets")]
    result = compute(items, None, SETTINGS)
    assert result.verdict == "rejection_risk"
    assert result.deciding_requirement_ids == [1]


def test_mandatory_not_confirmed_without_a_named_document_needs_revision():
    items = [_item(1, "not_confirmed", names_doc=False), _item(2, "meets")]
    result = compute(items, None, SETTINGS)
    assert result.verdict == "needs_revision"
    assert result.deciding_requirement_ids == [1]


def test_mandatory_under_review_blocks_ready():
    items = [_item(1, "needs_review"), _item(2, "meets")]
    result = compute(items, None, SETTINGS)
    assert result.verdict == "needs_revision"
    assert result.deciding_requirement_ids == [1]


def test_points_max_below_pass_threshold_is_rejection_risk():
    scheme = _scheme()  # threshold 11, criteria sum 14
    items = [_item(100 + n, "not_confirmed", req_type="scored", criterion_id=100 + n) for n in range(1, 6)]
    result = compute(items, scheme, SETTINGS)
    assert result.verdict == "rejection_risk"
    assert result.deciding_requirement_ids == [101, 102, 103, 104, 105]


def test_points_min_below_threshold_blocks_ready():
    scheme = _scheme()
    items = [_item(100 + n, "meets", req_type="scored", criterion_id=100 + n) for n in range(2, 6)]
    items.append(_item(101, "needs_review", req_type="scored", criterion_id=101))
    # Criterion 1 is under review: 0..4. Points 0..14 -> points_min 10 < 11, so Ready is blocked.
    result = compute(items, scheme, SETTINGS)
    assert result.tender_points_min == 10.0
    assert result.verdict == "needs_revision"
    assert result.deciding_requirement_ids == [101]


# ---------- forecast and impact ----------

def test_forecast_skips_request_clarification_and_not_fixable():
    items = [
        _item(1, "does_not_meet", action="add_document"),          # fixable -> meets
        _item(2, "does_not_meet", action="request_clarification"),  # stays
        _item(3, "does_not_meet", action="not_fixable"),           # stays
        _item(4, "partial", action=None),                          # no fix proposed: stays partial
        _item(5, "needs_review", action="check_manually"),         # a check, not a fix: stays in review
    ]
    result = compute(items, None, SETTINGS)
    # Current: decided items 1-4 with weights 3 each; partial gives 1.5 -> 1.5 / 12 = 12.5 %.
    assert result.overall == 12.5
    # Forecast: only item 1 becomes meets; 2 and 3 stay at 0, 4 stays partial, and 5 (under
    # review) counts as not met: total weight 15, 3 + 1.5 = 4.5 -> 30 %.
    assert result.forecast == 30.0
    assert result.forecast >= result.overall


def test_impact_is_the_score_if_only_that_item_meets():
    items = [_item(1, "meets"), _item(2, "meets"), _item(3, "does_not_meet", action="add_document")]
    assert compute(items, None, SETTINGS).overall == pytest.approx(66.7)
    assert impact(items, None, SETTINGS, 3) == 33.3


def test_impact_of_an_item_that_already_meets_is_zero():
    items = [_item(1, "meets"), _item(2, "does_not_meet")]
    assert impact(items, None, SETTINGS, 1) == 0.0


def test_impact_respects_overrides_already_applied():
    items = [_item(1, "meets"), _item(2, "does_not_meet"), _item(3, "does_not_meet")]
    # Item 2 is overridden to meets first; the impact of item 3 is then 100 - 66.7 = 33.3.
    assert impact(items, None, SETTINGS, 3, overrides={2: "meets"}) == 33.3


# ---------- platform items ----------

def test_platform_items_never_move_the_score_counters_or_the_verdict():
    base = [_item(1, "meets"), _item(2, "meets"), _item(3, "partial", req_type="desirable")]
    with_platform = base + [
        _item(4, "does_not_meet", platform=True),
        _item(5, "needs_review", req_type="mandatory", platform=True),
    ]
    alone = compute(base, None, SETTINGS)
    mixed = compute(with_platform, None, SETTINGS)
    assert mixed.verdict == alone.verdict == "ready"
    assert mixed.overall == alone.overall
    assert (mixed.range_min, mixed.range_max) == (alone.range_min, alone.range_max)
    assert (mixed.mandatory_met, mixed.mandatory_total) == (alone.mandatory_met, alone.mandatory_total) == (2, 2)
    assert mixed.counts == alone.counts
    assert mixed.forecast == alone.forecast
    assert mixed.deciding_requirement_ids == alone.deciding_requirement_ids


def test_a_failing_platform_item_cannot_cause_rejection_risk():
    result = compute([_item(1, "meets"), _item(2, "does_not_meet", platform=True)], None, SETTINGS)
    assert result.verdict == "ready"
    assert (result.mandatory_met, result.mandatory_total) == (1, 1)
    assert result.deciding_requirement_ids == [1]


def test_platform_items_are_left_out_of_the_tender_points():
    scheme = _scheme(maxima=(4,), stated=None, threshold=None)
    documents_only = compute([_item(1, "meets")], scheme, SETTINGS)
    linked_platform = compute(
        [_item(1, "meets"), _item(2, "does_not_meet", criterion_id=101, platform=True)], scheme, SETTINGS,
    )
    assert linked_platform.criteria_points[0].linked_requirement_ids == ()
    assert (linked_platform.tender_points_min, linked_platform.tender_points_max) == (
        documents_only.tender_points_min, documents_only.tender_points_max,
    )
