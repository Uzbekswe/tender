"""Committee wording (docs/customer-side-decisions.md §6): sentences, short reasons, i18n parity.

The English texts are checked against the rules of §6: never "will be rejected", no advice to the
bidder. The Russian texts are checked for the same key set and for the subject-to-rejection phrase.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.web.i18n import translator
from engine.committee import SHORT_REASON_CODES, committee_sentence, short_reason, verdict_text

I18N = Path(__file__).resolve().parents[2] / "i18n"
ADVICE = re.compile(
    r"\b(will be rejected|should|must|need to|needs to|recommend\w*|fix|replace|submit|correct|"
    r"add a|add the|please|advise\w*)\b",
    re.IGNORECASE,
)
STATUSES = ["meets", "partial", "not_confirmed", "does_not_meet", "needs_review"]
REASON_SETS = [
    [], ["ABSENT"], ["VALUE_FAIL"], ["CLAIM_ONLY"], ["WRONG_DOC_TYPE"], ["EXPIRED"],
    ["ATTRIBUTION"], ["PARTIAL_COVERAGE"], ["UNREADABLE_IN_SCOPE"], ["VALUE_OK"],
]


@pytest.fixture
def en():
    return translator("en")


@pytest.fixture
def ru():
    return translator("ru")


def _sentence(t, status, reasons, *, mandatory=False, names_doc=False, bid="3.0 kW", tender="≥ 2.5 kW"):
    return committee_sentence(
        status, reasons, parameter="Rated power", mandatory=mandatory, names_confirming_doc=names_doc,
        bid_value=bid, tender_value=tender, t=t,
    )


# ---------- i18n parity ----------

def test_committee_keys_exist_in_both_languages():
    en = json.loads((I18N / "en.json").read_text(encoding="utf-8"))
    ru = json.loads((I18N / "ru.json").read_text(encoding="utf-8"))
    en_keys = {k for k in en if k.startswith("committee.")}
    ru_keys = {k for k in ru if k.startswith("committee.")}
    assert en_keys and en_keys == ru_keys
    assert {"committee.advisory", "committee.verdict.rejection_risk", "committee.non_price_only",
            "committee.order_not_certain", "committee.not_provided", "committee.observations",
            "committee.subject_to_rejection", "committee.ranking.preliminary"} <= en_keys


def test_every_reason_code_has_a_short_text_in_both_languages(en, ru):
    for code in SHORT_REASON_CODES:
        assert en(f"committee.short.{code}") != f"committee.short.{code}"
        assert ru(f"committee.short.{code}") != f"committee.short.{code}"


# ---------- advice and rejection wording ----------

@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("reasons", REASON_SETS)
@pytest.mark.parametrize("mandatory", [False, True])
def test_english_sentences_have_no_advice_and_never_say_will_be_rejected(en, status, reasons, mandatory):
    text = _sentence(en, status, reasons, mandatory=mandatory, names_doc=True)
    assert text
    assert not ADVICE.search(text), text


def test_mandatory_failure_says_subject_to_rejection_by_the_committee(en):
    text = _sentence(en, "does_not_meet", ["ABSENT"], mandatory=True)
    assert "subject to rejection by the committee" in text.lower()
    assert "will be rejected" not in text.lower()


def test_optional_failure_is_not_called_subject_to_rejection(en):
    text = _sentence(en, "does_not_meet", ["ABSENT"], mandatory=False)
    assert "subject to rejection" not in text.lower()


def test_mandatory_not_confirmed_is_subject_to_rejection_only_when_the_tender_names_the_document(en):
    assert "subject to rejection" in _sentence(en, "not_confirmed", ["CLAIM_ONLY"], mandatory=True, names_doc=True).lower()
    assert "subject to rejection" not in _sentence(en, "not_confirmed", ["CLAIM_ONLY"], mandatory=True, names_doc=False).lower()


# ---------- the §6 table ----------

def test_claim_only_reads_as_the_bidder_states_it(en):
    assert _sentence(en, "not_confirmed", ["CLAIM_ONLY"]) == (
        "Bidder states Rated power; no supporting document in the package."
    )


def test_absent_reads_as_a_document_not_provided(en):
    assert _sentence(en, "does_not_meet", ["ABSENT"]) == (
        "Bidder did not provide a document confirming Rated power."
    )


def test_value_failure_states_bid_and_tender_values(en):
    assert _sentence(en, "does_not_meet", ["VALUE_FAIL"]) == "Bid states 3.0 kW; tender requires ≥ 2.5 kW."


def test_value_failure_without_a_tender_value_falls_back_to_a_generic_sentence(en):
    text = committee_sentence(
        "does_not_meet", ["VALUE_FAIL"], parameter="Rated power", mandatory=False, names_confirming_doc=False,
        bid_value=None, tender_value=None, t=en,
    )
    assert text == "The bid does not meet the requirement: Rated power."


def test_needs_review_is_left_to_the_committee(en):
    assert _sentence(en, "needs_review", ["UNREADABLE_IN_SCOPE"]) == "Committee to assess: Rated power."


def test_partial_and_met_sentences(en):
    assert _sentence(en, "meets", ["VALUE_OK"]) == "Requirement met: Rated power."
    assert _sentence(en, "partial", ["PARTIAL_COVERAGE"]).startswith("Partly met: Rated power.")


def test_expired_and_wrong_type_and_attribution(en):
    assert "expired" in _sentence(en, "not_confirmed", ["EXPIRED"]).lower()
    assert "different type" in _sentence(en, "not_confirmed", ["WRONG_DOC_TYPE"]).lower()
    assert "another company" in _sentence(en, "not_confirmed", ["ATTRIBUTION"]).lower()


def test_russian_sentence_for_a_failed_mandatory_item_uses_the_committee_phrase(ru):
    text = _sentence(ru, "does_not_meet", ["ABSENT"], mandatory=True)
    assert "подлежит отклонению по решению комиссии" in text


# ---------- short reasons and verdicts ----------

def test_short_reason_is_the_first_known_code(en):
    assert short_reason("does_not_meet", ["UNKNOWN", "VALUE_FAIL", "ABSENT"], en) == "Value does not meet requirement"


def test_short_reason_is_empty_for_a_plain_meets(en):
    assert short_reason("meets", [], en) == ""


def test_short_reason_for_a_platform_item_says_so(en):
    assert short_reason("needs_review", [], en, platform=True) == "Checked by the platform"


def test_verdict_text_covers_the_three_verdicts(en):
    assert verdict_text("ready", en) == "Meets all mandatory requirements (advisory)"
    assert verdict_text("needs_revision", en) == "Open points for the committee"
    assert verdict_text("rejection_risk", en) == "Subject to rejection by the committee"
    assert verdict_text(None, en) is None


# ---------- Uzbek wording (report language "uz") ----------

@pytest.fixture
def uz():
    return translator("uz")


def test_uzbek_committee_sentences_keep_the_committee_rule(uz):
    mandatory = _sentence(uz, "does_not_meet", ["ABSENT"], mandatory=True)
    assert mandatory == (
        "Ishtirokchi quyidagini tasdiqlovchi hujjat taqdim etmadi: Rated power. "
        "Majburiy talabga mos kelmaydi, komissiya qaroriga koʻra rad etilishi mumkin."
    )
    assert "rad etiladi" not in mandatory  # never "will be rejected"
    assert _sentence(uz, "does_not_meet", ["ABSENT"]) == (
        "Ishtirokchi quyidagini tasdiqlovchi hujjat taqdim etmadi: Rated power."
    )
    assert _sentence(uz, "does_not_meet", ["VALUE_FAIL"]) == (
        "Taklifdagi qiymat: 3.0 kW; hujjatlashtirish talab qiladi: ≥ 2.5 kW."
    )
    assert _sentence(uz, "meets", []) == "Talab bajarilgan: Rated power."
    assert _sentence(uz, "needs_review", []) == "Komissiya bahosini talab qiladi: Rated power."


def test_uzbek_verdicts_and_short_reasons(uz):
    assert verdict_text("ready", uz) == "Barcha majburiy talablarga mos keladi (tavsiyaviy)"
    assert verdict_text("rejection_risk", uz) == "Komissiya qaroriga koʻra rad etilishi mumkin"
    assert uz("committee.short.ABSENT") == "Ishtirokchi tomonidan taqdim etilmagan"
    assert uz("committee.advisory") == "Tavsiyaviy: qarorni baholash komissiyasi qabul qiladi"
