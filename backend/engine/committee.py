"""Committee wording for the customer side (docs/customer-side-decisions.md §6).

Pure functions. The caller passes `t`, a translation callable `t(key, **kwargs) -> str` (the
Translator of app.web.i18n), so this module does not depend on the web layer and stays testable.

Rules the texts follow (§6): never "will be rejected" (say "subject to rejection by the
committee"); no advice to the bidder; every statement is about what the bid does or does not
contain. All strings come from the `committee.*` keys in i18n/ru.json and i18n/en.json.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

Translate = Callable[..., str]

SHORT_REASON_CODES = frozenset({
    "ABSENT", "VALUE_FAIL", "VALUE_OK", "PARTIAL_COVERAGE", "CLAIM_ONLY", "WRONG_DOC_TYPE",
    "EXPIRED", "ATTRIBUTION", "CONTRADICTION", "EQUIVALENT_NEEDS_EXPERT", "UNREADABLE_IN_SCOPE",
    "QUOTE_UNVERIFIED", "NO_DEADLINE", "LLM_ERROR",
})


def verdict_text(verdict: str | None, t: Translate) -> str | None:
    """Committee wording of a verdict (ready / needs_revision / rejection_risk)."""
    return None if verdict is None else t(f"committee.verdict.{verdict}")


def short_reason(status: str | None, reasons: Iterable[str], t: Translate, *, platform: bool = False) -> str:
    """A 3 to 6 word reason for a matrix cell: the first reason code that has a short text.

    Empty when the status has no reason (a plain "meets"). Platform-checked items say so.
    """
    if platform:
        return t("committee.short.platform")
    for code in reasons or []:
        if code in SHORT_REASON_CODES:
            return t(f"committee.short.{code}")
    return ""


def committee_sentence(
    status: str,
    reasons: Iterable[str] | None,
    *,
    parameter: str,
    mandatory: bool,
    names_confirming_doc: bool,
    bid_value: str | None,
    tender_value: str | None,
    t: Translate,
) -> str:
    """One committee sentence for a requirement result, per the §6 table.

    `names_confirming_doc`: the tender names the document that confirms the item. A mandatory
    item that is not confirmed and has such a document is subject to rejection, as in the verdict
    rule (ARCHITECTURE §7.3). `bid_value` and `tender_value` are used for value failures only.
    """
    codes = set(reasons or [])
    if status == "meets":
        sentence = t("committee.sentence.meets", parameter=parameter)
    elif status == "partial":
        sentence = t("committee.sentence.partial", parameter=parameter)
    elif status == "not_confirmed":
        if "ATTRIBUTION" in codes:
            key = "attribution"
        elif "EXPIRED" in codes:
            key = "expired"
        elif "WRONG_DOC_TYPE" in codes:
            key = "wrong_doc_type"
        else:
            key = "claim_only"  # the bidder states it; no supporting document in the package
        sentence = t(f"committee.sentence.{key}", parameter=parameter)
    elif status == "does_not_meet":
        if "VALUE_FAIL" in codes and tender_value:
            sentence = t(
                "committee.sentence.value_fail",
                bid_value=bid_value or t("committee.sentence.no_value"),
                tender_value=tender_value,
            )
        elif "VALUE_FAIL" in codes:
            sentence = t("committee.sentence.value_generic", parameter=parameter)
        else:
            sentence = t("committee.sentence.absent", parameter=parameter)
    else:  # needs_review
        return t("committee.sentence.needs_review", parameter=parameter)

    if mandatory and (status == "does_not_meet" or (status == "not_confirmed" and names_confirming_doc)):
        sentence = f"{sentence} {t('committee.sentence.mandatory_failure')}"
    return sentence
