"""Recommendations: reason codes to action types, RU and EN wording, the no-new-facts guard (§4.2 stage 14, §6.7)."""

from __future__ import annotations

import pytest

from engine.recommend import (
    ADD_DOCUMENT,
    CHECK_MANUALLY,
    CORRECT_DATA,
    NOT_FIXABLE,
    REPLACE_GOODS,
    REQUEST_CLARIFICATION,
    CollectEntry,
    EvidenceRef,
    QuestionView,
    ReqView,
    clarification_request,
    collect_list,
    no_new_facts,
    recommend,
)


def _req(**overrides) -> ReqView:
    base = dict(
        code="T-014",
        scope="goods",
        parameter="Rated power",
        req_type="mandatory",
        operator=">=",
        value="2.5",
        unit="kW",
        confirmation_method="Datasheet, page with the rated power",
        source_quote="Rated power not less than 2.5 kW",
        source_page=28,
        source_file="tender.pdf",
    )
    base.update(overrides)
    return ReqView(**base)


def _evidence(**overrides) -> list[EvidenceRef]:
    base = dict(file_name="proposal.pdf", page=41, quote="Rated power 3.0 kW", value_raw="3.0 kW")
    base.update(overrides)
    return [EvidenceRef(**base)]


# ---------- reason code -> action type ----------

@pytest.mark.parametrize(
    ("status", "reasons", "scope", "req_type", "action_type", "criticality"),
    [
        ("does_not_meet", ["ABSENT"], "bidder", "mandatory", ADD_DOCUMENT, "high"),
        ("does_not_meet", ["ABSENT"], "bidder", "desirable", ADD_DOCUMENT, "medium"),
        ("not_confirmed", ["CLAIM_ONLY"], "bidder", "mandatory", ADD_DOCUMENT, "high"),
        ("not_confirmed", ["WRONG_DOC_TYPE"], "bidder", "scored", ADD_DOCUMENT, "medium"),
        ("not_confirmed", ["EXPIRED"], "bidder", "mandatory", ADD_DOCUMENT, "high"),
        ("not_confirmed", ["ATTRIBUTION"], "bidder", "mandatory", ADD_DOCUMENT, "high"),
        ("partial", ["PARTIAL_COVERAGE"], "bidder", "mandatory", ADD_DOCUMENT, "high"),
        ("does_not_meet", ["VALUE_FAIL"], "goods", "mandatory", REPLACE_GOODS, "high"),
        ("does_not_meet", ["VALUE_FAIL"], "bidder", "mandatory", NOT_FIXABLE, "high"),
        ("does_not_meet", ["VALUE_FAIL", "CONTRADICTION"], "bidder", "mandatory", CORRECT_DATA, "high"),
        ("does_not_meet", ["CONTRADICTION"], "bidder", "mandatory", CORRECT_DATA, "high"),
        ("needs_review", ["EQUIVALENT_NEEDS_EXPERT"], "goods", "mandatory", REQUEST_CLARIFICATION, "medium"),
        ("needs_review", ["UNREADABLE_IN_SCOPE"], "goods", "mandatory", CHECK_MANUALLY, "high"),
        ("needs_review", ["NO_DEADLINE"], "bidder", "desirable", CHECK_MANUALLY, "medium"),
        ("needs_review", ["LLM_ERROR"], "goods", "mandatory", CHECK_MANUALLY, "high"),
        ("needs_review", ["QUOTE_UNVERIFIED"], "bidder", "mandatory", CHECK_MANUALLY, "high"),
        ("needs_review", [], "bidder", "mandatory", CHECK_MANUALLY, "high"),
    ],
)
def test_reason_codes_map_to_action_types_and_criticality(
    status, reasons, scope, req_type, action_type, criticality,
):
    req = _req(scope=scope, req_type=req_type)
    rec = recommend(req, status, reasons, _evidence(), "ru")
    assert rec.action_type == action_type
    assert rec.criticality == criticality
    assert rec.problem and rec.action


def test_correct_data_is_always_high_and_clarification_is_medium():
    assert recommend(_req(req_type="desirable"), "does_not_meet", ["CONTRADICTION"], [], "en").criticality == "high"
    assert recommend(_req(req_type="mandatory"), "needs_review", ["EQUIVALENT_NEEDS_EXPERT"], [], "en").criticality == "medium"


def test_meets_has_no_recommendation():
    with pytest.raises(ValueError):
        recommend(_req(), "meets", [], [], "ru")


def test_a_status_without_reason_codes_still_gets_a_recommendation():
    assert recommend(_req(scope="goods"), "does_not_meet", [], [], "ru").action_type == REPLACE_GOODS
    assert recommend(_req(scope="bidder"), "does_not_meet", [], [], "ru").action_type == ADD_DOCUMENT
    assert recommend(_req(), "partial", [], [], "en").action_type == ADD_DOCUMENT


# ---------- wording ----------

def test_russian_and_english_wording_differ_and_both_name_the_requirement():
    req = _req(parameter_ru="Номинальная мощность")
    ru = recommend(req, "does_not_meet", ["ABSENT"], [], "ru")
    en = recommend(req, "does_not_meet", ["ABSENT"], [], "en")
    assert "Добавьте" in ru.action
    assert "Add a document" in en.action
    assert "T-014" in ru.problem and "T-014" in en.problem
    assert "Номинальная мощность" in ru.problem  # the Russian parameter, when present
    assert "Rated power" in en.problem


def test_condition_comes_from_the_requirement_fields_only():
    rec = recommend(_req(), "does_not_meet", ["VALUE_FAIL"], _evidence(), "ru")
    assert "не менее 2.5 kW" in rec.problem
    assert "3.0" not in rec.problem and "3.0" not in rec.action  # bid value is never quoted in the text


def test_confirmation_method_appears_in_the_action():
    rec = recommend(_req(scope="bidder", confirmation_method="Registration certificate"), "not_confirmed",
                    ["CLAIM_ONLY"], [], "en")
    assert "confirmation: Registration certificate" in rec.action


def test_every_rendered_text_passes_the_guard():
    req = _req(value="24", unit="months", code="U-007", parameter="Experience", operator=">=")
    evidence = _evidence(quote="Contract No. 12 for 36 months", value_raw="36 months", page=2)
    for lang in ("ru", "en"):
        for status, reasons in [
            ("does_not_meet", ["ABSENT"]), ("does_not_meet", ["VALUE_FAIL"]), ("partial", ["PARTIAL_COVERAGE"]),
            ("needs_review", ["NO_DEADLINE"]),
        ]:
            rec = recommend(req, status, reasons, evidence, lang)
            allowed = [req.code, req.parameter, req.value, req.unit, req.source_quote,
                       evidence[0].quote, evidence[0].value_raw, "28", "2"]
            assert no_new_facts(rec.problem, allowed)
            assert no_new_facts(rec.action, allowed)


def test_sources_list_the_tender_and_each_bid_evidence():
    rec = recommend(_req(), "does_not_meet", ["VALUE_FAIL"], _evidence(), "ru")
    assert rec.sources[0] == {"side": "tender", "file": "tender.pdf", "page": 28,
                              "quote": "Rated power not less than 2.5 kW"}
    assert rec.sources[1] == {"side": "bid", "file": "proposal.pdf", "page": 41, "quote": "Rated power 3.0 kW"}


# ---------- the guard ----------

def test_guard_accepts_numbers_from_the_requirement_and_rejects_an_invented_one():
    allowed = ["T-014", "Rated power", "2.5", "kW", "Rated power not less than 2.5 kW"]
    assert no_new_facts("Requirement T-014: not less than 2.5 kW.", allowed)
    assert not no_new_facts("Requirement T-014: not less than 3.7 kW.", allowed)


def test_guard_compares_numbers_in_canonical_form():
    assert no_new_facts("Warranty 24 months", ["24"])
    assert no_new_facts("Power 2,5 kW", ["2.5 kW"])
    assert no_new_facts("Price 150 000 000 UZS", ["150000000"])
    assert not no_new_facts("Price 150 000 001 UZS", ["150000000"])


def test_guard_passes_text_without_numbers():
    assert no_new_facts("Replace the product.", [])


# ---------- still to collect ----------

def _entry(code: str, action_type: str, *, mandatory: bool, impact: float) -> CollectEntry:
    return CollectEntry(
        requirement_id=int(code[-3:]), code=code, parameter=f"parameter {code}", mandatory=mandatory,
        action_type=action_type, impact_pct=impact, action="Add a document",
    )


def test_collect_list_keeps_add_document_only_and_orders_mandatory_then_impact():
    rows = collect_list([
        _entry("U-001", ADD_DOCUMENT, mandatory=False, impact=9.0),
        _entry("U-002", ADD_DOCUMENT, mandatory=True, impact=2.0),
        _entry("U-003", ADD_DOCUMENT, mandatory=True, impact=6.0),
        _entry("T-004", REPLACE_GOODS, mandatory=True, impact=20.0),
        _entry("U-005", CHECK_MANUALLY, mandatory=True, impact=5.0),
    ])
    assert [r.code for r in rows] == ["U-003", "U-002", "U-001"]


# ---------- clarification request ----------

def test_clarification_request_lists_questions_with_their_quotes_ru_and_en():
    questions = [
        QuestionView(
            kind="scheme_mismatch",
            text="The criteria maxima add up to 14, but the stated maximum is 15.",
            sources=[{"quote": "Maximum score 15", "file": "tender.pdf", "page": 28}],
        ),
        QuestionView(kind="contradiction", text="Warranty is stated as 24 and as 12 months.", sources=[]),
    ]
    ru = clarification_request(questions, "ru")
    en = clarification_request(questions, "en")
    assert ru.startswith("Запрос на разъяснение документации")
    assert "1. The criteria maxima add up to 14" in ru
    assert "«Maximum score 15»" in ru and "стр. 28" in ru
    assert "2. Warranty is stated as 24 and as 12 months." in ru
    assert en.startswith("Request for clarification")
    assert "«Maximum score 15»" in en and "p. 28" in en
    assert ru.endswith("\n")


def test_clarification_request_with_no_questions_is_still_a_letter():
    body = clarification_request([], "en")
    assert body.startswith("Request for clarification")
    assert "1." not in body
