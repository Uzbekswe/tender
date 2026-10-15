"""Tests for engine.status (ARCHITECTURE §6.6). One test per decision-table row."""

from __future__ import annotations

import pytest

from engine import status as st
from engine.status import EvidenceView, confidence, decide


def ev(*, verified: bool = True, method: str | None = "exact", page_corrected: bool = False,
       read_quality: float = 1.0, from_confirming_doc: bool = True,
       subject_is_bidder: bool | None = True) -> EvidenceView:
    return EvidenceView(
        verified=verified,
        method=method,
        page_corrected=page_corrected,
        read_quality=read_quality,
        from_confirming_doc=from_confirming_doc,
        subject_is_bidder=subject_is_bidder,
    )


GOOD = [ev()]           # verified, exact, confirming, bidder
NONE: list[EvidenceView] = []


def run(*, judgement=None, needs_expert=False, reasons=(), evidence=GOOD, req_type="scored",
        numeric_decided=False, threshold=80.0):
    return decide(
        judgement=judgement,
        needs_expert=needs_expert,
        reasons=set(reasons),
        evidence=list(evidence),
        req_type=req_type,
        numeric_decided=numeric_decided,
        review_threshold=threshold,
    )


# ---------- decision table, row by row ----------

@pytest.mark.parametrize("reason", [st.LLM_ERROR, st.EQUIVALENT_NEEDS_EXPERT, st.NO_DEADLINE])
def test_row1_review_reasons_need_human_review(reason):
    d = run(judgement="satisfies", reasons=[reason])
    assert d.status == "needs_review"


def test_row1_needs_expert_needs_human_review():
    d = run(judgement="satisfies", needs_expert=True)
    assert d.status == "needs_review"


def test_row1_unreadable_without_verified_evidence_needs_review():
    d = run(judgement="satisfies", reasons=[st.UNREADABLE_IN_SCOPE], evidence=[ev(verified=False)])
    assert d.status == "needs_review"


def test_row1_unreadable_with_verified_evidence_does_not_trigger_row1():
    d = run(judgement="satisfies", reasons=[st.UNREADABLE_IN_SCOPE], evidence=GOOD)
    assert d.status == "meets"


@pytest.mark.parametrize("judgement", ["contradicts"])
def test_row2_contradiction_with_verified_evidence_does_not_meet(judgement):
    d = run(judgement=judgement)
    assert d.status == "does_not_meet"
    assert d.notes


def test_row2_value_fail_with_verified_evidence_does_not_meet():
    d = run(judgement="satisfies", reasons=[st.VALUE_FAIL], numeric_decided=True)
    assert d.status == "does_not_meet"
    assert d.method == "rule"


def test_row2_value_fail_without_verified_evidence_is_not_does_not_meet():
    d = run(reasons=[st.VALUE_FAIL], evidence=[ev(verified=False)])
    assert d.status == "needs_review"


def test_row3_absent_does_not_meet_rule_confidence_100():
    d = run(reasons=[st.ABSENT], evidence=NONE, numeric_decided=True)
    assert d.status == "does_not_meet"
    assert d.confidence == 100.0
    assert d.confidence_parts["absent"] is True
    assert d.confidence_parts["quote_factor"] == 1.0
    assert d.confidence_parts["read_factor"] == 1.0


def test_row3_absent_by_llm_uses_method_factor_only():
    d = run(judgement="not_addressed", reasons=[st.ABSENT], evidence=NONE)
    assert d.status == "does_not_meet"
    assert d.confidence == 90.0


def test_row3_absent_is_not_capped_at_50():
    d = run(judgement="not_addressed", reasons=[st.ABSENT], evidence=[ev(verified=False)])
    assert d.status == "does_not_meet"
    assert d.confidence == 90.0


@pytest.mark.parametrize("reason", [st.ATTRIBUTION, st.CLAIM_ONLY, st.WRONG_DOC_TYPE])
def test_row4_not_confirmed(reason):
    d = run(judgement="satisfies", reasons=[reason])
    assert d.status == "not_confirmed"


@pytest.mark.parametrize("reason", [st.EXPIRED, st.PARTIAL_COVERAGE])
def test_row5_partial_reasons(reason):
    d = run(judgement="satisfies", reasons=[reason])
    assert d.status == "partial"


def test_row5_partially_satisfies_is_partial():
    d = run(judgement="partially_satisfies")
    assert d.status == "partial"


def test_row6_contradiction_between_bid_documents_needs_review_even_if_satisfied():
    d = run(judgement="satisfies", reasons=[st.CONTRADICTION])
    assert d.status == "needs_review"
    assert any("different values" in n for n in d.notes)


def test_row7_satisfies_with_verified_confirming_bidder_evidence_meets():
    d = run(judgement="satisfies")
    assert d.status == "meets"
    assert d.confidence == 90.0          # exact 1.0 x read 1.0 x llm 0.9
    assert d.method == "llm"


def test_row7_value_ok_meets_by_rule():
    d = run(reasons=[st.VALUE_OK], numeric_decided=True)
    assert d.status == "meets"
    assert d.confidence == 100.0
    assert d.method == "rule"


def test_row8_anything_else_needs_review():
    d = run(judgement=None)
    assert d.status == "needs_review"
    d2 = run(judgement="not_addressed", evidence=GOOD)
    assert d2.status == "needs_review"


# ---------- threshold ----------

def test_threshold_downgrades_meets_below_review_threshold():
    fuzzy = [ev(method="fuzzy_ocr", read_quality=1.0)]   # 0.9 x 1.0 x 0.9 = 81 (fine)
    assert run(judgement="satisfies", evidence=fuzzy).status == "meets"
    assert run(judgement="satisfies", evidence=fuzzy, threshold=85).status == "needs_review"


def test_threshold_override_applies_whatever_the_row_gave():
    d = run(reasons=[st.ABSENT], evidence=NONE, threshold=95)
    assert d.confidence == 90.0
    assert d.status == "needs_review"


def test_threshold_default_is_80():
    assert run(judgement="satisfies").status == "meets"          # 90 >= 80
    assert run(judgement="satisfies", threshold=90.5).status == "needs_review"


def test_cannot_judge_halves_confidence_and_needs_review():
    d = run(judgement="cannot_judge", evidence=GOOD)
    assert d.confidence == 50.0
    assert d.status == "needs_review"


# ---------- mandatory Meets guard ----------

def test_mandatory_meets_with_exact_quote_is_kept():
    d = run(judgement="satisfies", req_type="mandatory", threshold=0)
    assert d.status == "meets"


def test_mandatory_meets_with_fuzzy_on_poor_page_is_downgraded():
    d = run(judgement="satisfies", req_type="mandatory", threshold=0,
            evidence=[ev(method="fuzzy_ocr", read_quality=0.5)])
    assert d.status == "needs_review"
    assert any("mandatory" in n for n in d.notes)


def test_mandatory_meets_with_fuzzy_on_good_page_is_kept():
    d = run(judgement="satisfies", req_type="mandatory", threshold=0,
            evidence=[ev(method="fuzzy_ocr", read_quality=0.85)])
    assert d.status == "meets"


def test_scored_meets_with_fuzzy_on_poor_page_is_not_downgraded_by_guard():
    d = run(judgement="satisfies", req_type="scored", threshold=0,
            evidence=[ev(method="fuzzy_ocr", read_quality=0.5)])
    assert d.status == "meets"


def test_mandatory_guard_uses_the_evidence_that_supports_meets():
    # A strong exact quote from a partner does not make the bidder's weak fuzzy item strong.
    evidence = [
        ev(method="exact", subject_is_bidder=False),
        ev(method="fuzzy_ocr", read_quality=0.5),
    ]
    d = run(judgement="satisfies", req_type="mandatory", threshold=0, evidence=evidence)
    assert d.status == "needs_review"


# ---------- partner evidence and unverified evidence ----------

def test_partner_evidence_never_gives_meets():
    partner = [ev(subject_is_bidder=False)]
    d = run(judgement="satisfies", evidence=partner)
    assert d.status != "meets"
    assert d.status == "needs_review"


def test_partner_evidence_from_confirming_doc_still_never_gives_meets():
    partner = [ev(subject_is_bidder=False, from_confirming_doc=True)]
    assert run(judgement="satisfies", evidence=partner, reasons=[st.VALUE_OK]).status == "needs_review"


def test_bidder_evidence_plus_partner_evidence_meets_on_bidder_evidence():
    evidence = [ev(subject_is_bidder=False), ev(subject_is_bidder=True)]
    assert run(judgement="satisfies", evidence=evidence).status == "meets"


def test_unknown_subject_counts_as_not_partner():
    evidence = [ev(subject_is_bidder=None)]
    assert run(judgement="satisfies", evidence=evidence).status == "meets"


def test_meets_needs_evidence_from_a_confirming_document():
    evidence = [ev(from_confirming_doc=False)]
    assert run(judgement="satisfies", evidence=evidence).status == "needs_review"


def test_unverified_evidence_is_ignored():
    unverified = [ev(verified=False)]
    d = run(judgement="satisfies", evidence=unverified)
    assert d.status == "needs_review"
    assert d.confidence <= 50.0
    assert d.confidence_parts["no_verified_evidence"] is True


def test_unverified_exact_quote_does_not_count_for_meets():
    d = run(judgement="satisfies", evidence=[ev(verified=False, method="exact")])
    assert d.status != "meets"


def test_unverified_only_non_absent_case_is_capped_at_50_and_needs_review():
    d = run(reasons=[st.ATTRIBUTION], evidence=NONE)
    assert d.confidence == 50.0
    assert d.status == "needs_review"    # the row gave not_confirmed; the threshold overrides it


# ---------- confidence parts ----------

def test_confidence_parts_exact_llm():
    value, parts = confidence(GOOD, method="llm", judgement="satisfies", needs_expert=False)
    assert value == 90.0
    assert parts == {
        "quote_factor": 1.0, "read_factor": 1.0, "method_factor": 0.9,
        "no_verified_evidence": False, "absent": False, "cap": None,
    }


def test_confidence_fuzzy_ocr_page_corrected_rule():
    evidence = [ev(method="fuzzy_ocr", read_quality=0.8, page_corrected=True)]
    value, parts = confidence(evidence, method="rule", judgement="satisfies", needs_expert=False)
    assert value == pytest.approx(100 * 0.9 * 0.95 * 0.8 * 1.0)
    assert parts["quote_factor"] == pytest.approx(0.9 * 0.95)
    assert parts["read_factor"] == 0.8
    assert parts["method_factor"] == 1.0


def test_confidence_best_quote_and_worst_read_across_verified_evidence():
    evidence = [
        ev(method="fuzzy_ocr", read_quality=0.7),
        ev(method="exact", read_quality=0.95),
    ]
    value, parts = confidence(evidence, method="rule", judgement="satisfies", needs_expert=False)
    assert parts["quote_factor"] == 1.0          # best verified quote
    assert parts["read_factor"] == 0.7           # worst read quality used
    assert value == pytest.approx(70.0)


def test_confidence_ignores_unverified_items_in_read_factor():
    evidence = [ev(read_quality=1.0), ev(verified=False, read_quality=0.1)]
    _, parts = confidence(evidence, method="rule", judgement="satisfies", needs_expert=False)
    assert parts["read_factor"] == 1.0


@pytest.mark.parametrize("judgement,needs_expert,expected", [
    ("satisfies", False, 90.0),        # llm
    ("cannot_judge", False, 50.0),     # unsure
    ("satisfies", True, 50.0),         # needs_expert
])
def test_confidence_method_factor_for_llm(judgement, needs_expert, expected):
    value, _ = confidence(GOOD, method="llm", judgement=judgement, needs_expert=needs_expert)
    assert value == pytest.approx(expected)


def test_confidence_without_verified_evidence_is_capped_at_50():
    value, parts = confidence([], method="rule", judgement="satisfies", needs_expert=False)
    assert value == 50.0
    assert parts["cap"] == 50.0
    assert parts["absent"] is False


def test_decide_reports_method_and_parts():
    d = run(judgement="satisfies", numeric_decided=True)
    assert d.method == "rule"
    assert d.confidence == 100.0
    assert set(d.confidence_parts) >= {"quote_factor", "read_factor", "method_factor"}
    assert d.reasons == ()
    assert d.notes == ()
