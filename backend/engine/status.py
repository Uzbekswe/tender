"""Stage 12: status and confidence (ARCHITECTURE §6.6).

Pure Python. Code turns the LLM judgement and the reason codes from stages 8-11 into one
of the five statuses (models.Status values) and a confidence from signals code can
measure. The LLM never sets status or confidence.

Decision order: first matching row wins, then the review threshold, then the
mandatory-Meets guard.
"""

from __future__ import annotations

from dataclasses import dataclass

# Reason codes (set by stages 8-11).
ABSENT = "ABSENT"
VALUE_FAIL = "VALUE_FAIL"
VALUE_OK = "VALUE_OK"
PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
CLAIM_ONLY = "CLAIM_ONLY"
WRONG_DOC_TYPE = "WRONG_DOC_TYPE"
EXPIRED = "EXPIRED"
ATTRIBUTION = "ATTRIBUTION"
CONTRADICTION = "CONTRADICTION"
EQUIVALENT_NEEDS_EXPERT = "EQUIVALENT_NEEDS_EXPERT"
UNREADABLE_IN_SCOPE = "UNREADABLE_IN_SCOPE"
QUOTE_UNVERIFIED = "QUOTE_UNVERIFIED"
NO_DEADLINE = "NO_DEADLINE"
LLM_ERROR = "LLM_ERROR"
# Set by assess for a requirement the e-procurement platform checks (no bid documents are read).
PLATFORM_CHECK = "PLATFORM_CHECK"

# Requirement.verification values: where the requirement is confirmed.
VERIFICATION_DOCUMENTS = "documents"
VERIFICATION_PLATFORM = "platform"

# Status values (same strings as app.models.Status; kept here so this module stays free
# of SQLModel imports).
MEETS = "meets"
PARTIAL = "partial"
NOT_CONFIRMED = "not_confirmed"
DOES_NOT_MEET = "does_not_meet"
NEEDS_REVIEW = "needs_review"

MANDATORY = "mandatory"          # models.ReqType value
DEFAULT_REVIEW_THRESHOLD = 80.0
STRONG_FUZZY_READ_QUALITY = 0.8

QUOTE_FACTOR_BY_METHOD = {"exact": 1.0, "fuzzy_ocr": 0.9}
PAGE_CORRECTED_FACTOR = 0.95
METHOD_FACTOR_RULE = 1.0
METHOD_FACTOR_LLM = 0.9
METHOD_FACTOR_UNSURE = 0.5       # LLM said cannot_judge, or needs_expert
NO_VERIFIED_CAP = 50.0


@dataclass(frozen=True)
class EvidenceView:
    verified: bool
    method: str | None          # "exact" | "fuzzy_ocr" | None
    page_corrected: bool
    read_quality: float         # 0..1 of the page
    from_confirming_doc: bool   # doc type is one of the requirement's evidence_doc_types (or none specified)
    subject_is_bidder: bool | None


@dataclass(frozen=True)
class Decision:
    status: str                 # models.Status value
    reasons: tuple[str, ...]
    confidence: float           # 0..100
    confidence_parts: dict
    method: str                 # "rule" | "llm"
    notes: tuple[str, ...]      # plain-language reasons for the UI, English


_NOTE_BY_REASON = {
    LLM_ERROR: "The model did not return a usable answer for this requirement.",
    UNREADABLE_IN_SCOPE: "A page that could contain the evidence could not be read.",
    EQUIVALENT_NEEDS_EXPERT: "An equivalent product or document needs an expert to judge.",
    NO_DEADLINE: "The document has a validity date but the submission deadline is not set.",
    PLATFORM_CHECK: "The e-procurement platform checks this, not the bid documents.",
    VALUE_FAIL: "The value in the bid does not meet the required value.",
    CONTRADICTION: "Bid documents give different values for this requirement.",
    ABSENT: "Nothing relevant was found in the bid after a wide search.",
    ATTRIBUTION: "The statement in the bid is about another company, not the bidder.",
    CLAIM_ONLY: "The bidder only claims this; the tender asks for a confirming document.",
    WRONG_DOC_TYPE: "The evidence is a different type of document than the tender requires.",
    EXPIRED: "The evidence document has expired.",
    PARTIAL_COVERAGE: "The bid covers only part of the requirement.",
    QUOTE_UNVERIFIED: "A quoted statement could not be found in the source page.",
    VALUE_OK: "The value in the bid meets the requirement.",
    "needs_expert": "An expert must judge this requirement.",
    "contradicts": "The bid evidence contradicts the requirement.",
    "partially_satisfies": "The bid only partly satisfies the requirement.",
}


def _is_usable(e: EvidenceView) -> bool:
    """Verified evidence that is not about someone else (partner, customer, reference)."""
    return e.verified and e.subject_is_bidder is not False


def _quote_factor(e: EvidenceView) -> float:
    factor = QUOTE_FACTOR_BY_METHOD.get(e.method or "", 0.5)
    if e.page_corrected:
        factor *= PAGE_CORRECTED_FACTOR
    return factor


def _method_factor(method: str, judgement: str | None, needs_expert: bool) -> float:
    if needs_expert or judgement == "cannot_judge":
        return METHOD_FACTOR_UNSURE
    return METHOD_FACTOR_RULE if method == "rule" else METHOD_FACTOR_LLM


def _confidence(evidence: list[EvidenceView], *, method: str, judgement: str | None,
                needs_expert: bool, absent: bool) -> tuple[float, dict]:
    verified = [e for e in evidence if e.verified]
    method_factor = _method_factor(method, judgement, needs_expert)

    if not verified:
        if absent:
            # Absence: nothing to quote, so the quote and read factors are 1.0.
            value = 100.0 * 1.0 * 1.0 * method_factor
            parts = {"quote_factor": 1.0, "read_factor": 1.0, "method_factor": method_factor,
                     "no_verified_evidence": True, "absent": True, "cap": None}
        else:
            value = min(NO_VERIFIED_CAP, 100.0 * method_factor)
            parts = {"quote_factor": None, "read_factor": None, "method_factor": method_factor,
                     "no_verified_evidence": True, "absent": False, "cap": NO_VERIFIED_CAP}
        return round(value, 2), parts

    quote_factor = max(_quote_factor(e) for e in verified)
    read_factor = min(e.read_quality for e in verified)
    value = 100.0 * quote_factor * read_factor * method_factor
    parts = {"quote_factor": quote_factor, "read_factor": read_factor, "method_factor": method_factor,
             "no_verified_evidence": False, "absent": False, "cap": None}
    return round(value, 2), parts


def confidence(evidence: list[EvidenceView], *, method: str, judgement: str | None, needs_expert: bool) -> tuple[float, dict]:
    """Confidence 0..100 and its factors (§6.6), for a decision that is not an absence.

    With no verified evidence the result is capped at 50. A caller deciding an ABSENT
    requirement should use `decide`, which knows the reason codes.
    """
    return _confidence(evidence, method=method, judgement=judgement, needs_expert=needs_expert, absent=False)


def _table_row(*, judgement: str | None, needs_expert: bool, reasons: set[str],
               evidence: list[EvidenceView]) -> tuple[str, list[str]]:
    """First matching row of the §6.6 decision table. Returns (status, notes)."""
    verified = [e for e in evidence if e.verified]
    usable = [e for e in evidence if _is_usable(e)]

    # 1. Needs human review, before any judgement is trusted.
    if LLM_ERROR in reasons:
        return NEEDS_REVIEW, [LLM_ERROR]
    if UNREADABLE_IN_SCOPE in reasons and not verified:
        return NEEDS_REVIEW, [UNREADABLE_IN_SCOPE]
    if EQUIVALENT_NEEDS_EXPERT in reasons:
        return NEEDS_REVIEW, [EQUIVALENT_NEEDS_EXPERT]
    if needs_expert:
        return NEEDS_REVIEW, ["needs_expert"]
    if NO_DEADLINE in reasons:
        return NEEDS_REVIEW, [NO_DEADLINE]

    # 2. Does not meet: wrong value or contradicting verified evidence.
    if (VALUE_FAIL in reasons and usable) or (judgement == "contradicts" and usable):
        return DOES_NOT_MEET, [VALUE_FAIL] if VALUE_FAIL in reasons else ["contradicts"]

    # 3. Does not meet: absent.
    if ABSENT in reasons:
        return DOES_NOT_MEET, [ABSENT]

    # 4. Not confirmed: the statement is about someone else, or not the confirming document.
    blocking = [r for r in (ATTRIBUTION, CLAIM_ONLY, WRONG_DOC_TYPE) if r in reasons]
    if blocking:
        return NOT_CONFIRMED, blocking

    # 5. Partial.
    partial = [r for r in (EXPIRED, PARTIAL_COVERAGE) if r in reasons]
    if partial or judgement == "partially_satisfies":
        return PARTIAL, partial or ["partially_satisfies"]

    # 6. Contradiction between bid documents: a person must choose.
    if CONTRADICTION in reasons:
        return NEEDS_REVIEW, [CONTRADICTION]

    # 7. Meets: judgement satisfies (or VALUE_OK) and verified, confirming, bidder evidence.
    satisfied = judgement == "satisfies" or VALUE_OK in reasons
    if satisfied and any(e.from_confirming_doc for e in usable):
        return MEETS, [VALUE_OK] if VALUE_OK in reasons else ["satisfies"]

    # 8. Anything else.
    return NEEDS_REVIEW, ["no_clear_outcome"]


def _strong_evidence(evidence: list[EvidenceView]) -> bool:
    """A verified exact quote, or a fuzzy match on a well-read page, among the items that
    can support Meets (usable and from a confirming document)."""
    return any(
        _is_usable(e) and e.from_confirming_doc and (
            e.method == "exact"
            or (e.method == "fuzzy_ocr" and e.read_quality >= STRONG_FUZZY_READ_QUALITY)
        )
        for e in evidence
    )


def decide(*, judgement: str | None, needs_expert: bool, reasons: set[str], evidence: list[EvidenceView],
           req_type: str, numeric_decided: bool, review_threshold: float = DEFAULT_REVIEW_THRESHOLD) -> Decision:
    """Status, confidence and notes for one requirement (§6.6).

    `numeric_decided` means a numeric or date rule decided the outcome (method "rule");
    otherwise the LLM judgement did (method "llm"). Only verified evidence counts. Partner
    evidence (subject_is_bidder False) never gives Meets.
    """
    reason_set = set(reasons)
    method = "rule" if numeric_decided else "llm"
    verified = [e for e in evidence if e.verified]

    status, row_notes = _table_row(
        judgement=judgement, needs_expert=needs_expert, reasons=reason_set, evidence=evidence,
    )
    conf, parts = _confidence(
        evidence, method=method, judgement=judgement, needs_expert=needs_expert,
        absent=ABSENT in reason_set and not verified,
    )

    notes: list[str] = []
    for item in row_notes:
        note = _NOTE_BY_REASON.get(item)
        if note:
            notes.append(note)
    if status == NEEDS_REVIEW and not notes:
        notes.append("The outcome is not clear enough to decide automatically.")

    if conf < review_threshold:
        if status != NEEDS_REVIEW:
            notes.append(f"Confidence {conf:g} is below the review threshold {review_threshold:g}.")
        status = NEEDS_REVIEW

    if status == MEETS and req_type == MANDATORY and not _strong_evidence(evidence):
        status = NEEDS_REVIEW
        notes.append("A mandatory requirement needs an exact quote, or a scanned page read with high quality, to be met.")

    return Decision(
        status=status,
        reasons=tuple(sorted(reason_set)),
        confidence=conf,
        confidence_parts=parts,
        method=method,
        notes=tuple(notes),
    )
