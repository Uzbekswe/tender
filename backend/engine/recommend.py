"""Recommendations from reason codes (docs/ARCHITECTURE.md §4.2 stage 14, §6.7, spec §8).

Pure and deterministic: no LLM. The action type, criticality and wording come from fixed rules
and templates. Every template is filled only from the requirement's own fields (code, parameter,
value, unit, confirmation method, tender page) and from the reason codes. Nothing else is
inserted.

"Never change facts" is enforced by `no_new_facts`: every number in a rendered text must also
occur in the requirement or in the bid evidence the recommendation was built from. A text that
fails the check is replaced by a number-free fallback sentence.

Action types (spec §8) are the five from ARCHITECTURE §5, plus `check_manually` for items that
need a human to look at pages (unreadable pages, LLM errors, a missing deadline). That sixth
value is a deliberate addition: none of the five fits "look at it yourself".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine.status import (
    ABSENT,
    ATTRIBUTION,
    CLAIM_ONLY,
    CONTRADICTION,
    EQUIVALENT_NEEDS_EXPERT,
    EXPIRED,
    LLM_ERROR,
    NO_DEADLINE,
    PARTIAL_COVERAGE,
    QUOTE_UNVERIFIED,
    UNREADABLE_IN_SCOPE,
    VALUE_FAIL,
    WRONG_DOC_TYPE,
)
from engine.verify import numbers_in

# Action types
ADD_DOCUMENT = "add_document"
CORRECT_DATA = "correct_data"
REPLACE_GOODS = "replace_goods"
REQUEST_CLARIFICATION = "request_clarification"
NOT_FIXABLE = "not_fixable"
CHECK_MANUALLY = "check_manually"
ACTION_TYPES = (ADD_DOCUMENT, CORRECT_DATA, REPLACE_GOODS, REQUEST_CLARIFICATION, NOT_FIXABLE, CHECK_MANUALLY)

# Statuses (same strings as engine.scoring and app.models.Status)
_MEETS = "meets"
_PARTIAL = "partial"
_NOT_CONFIRMED = "not_confirmed"
_DOES_NOT_MEET = "does_not_meet"
_NEEDS_REVIEW = "needs_review"

_REVIEW_ORDER = (LLM_ERROR, UNREADABLE_IN_SCOPE, NO_DEADLINE, EQUIVALENT_NEEDS_EXPERT, QUOTE_UNVERIFIED, CONTRADICTION)
_OPEN_ORDER = (VALUE_FAIL, ABSENT, ATTRIBUTION, WRONG_DOC_TYPE, CLAIM_ONLY, EXPIRED, PARTIAL_COVERAGE, CONTRADICTION)


@dataclass(frozen=True)
class ReqView:
    """The requirement fields a recommendation may use."""
    code: str
    scope: str                       # goods | bidder
    parameter: str
    req_type: str                    # mandatory | scored | desirable
    operator: str = "text"
    value: str | None = None
    value_max: str | None = None
    unit: str | None = None
    confirmation_method: str | None = None
    source_quote: str = ""
    source_page: int | None = None   # tender page number
    source_file: str | None = None   # tender document file name
    parameter_ru: str | None = None


@dataclass(frozen=True)
class EvidenceRef:
    """A verified bid evidence item, as the recommendation may quote it."""
    file_name: str | None
    page: int | None
    quote: str
    value_raw: str | None = None


@dataclass(frozen=True)
class RecView:
    problem: str
    action: str
    action_type: str
    criticality: str                 # high | medium | low
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CollectEntry:
    """One row of the "still to collect" list."""
    requirement_id: int
    code: str
    parameter: str
    mandatory: bool
    action_type: str
    impact_pct: float
    action: str


@dataclass(frozen=True)
class QuestionView:
    """A project question with the quotes that back it. Each source: {"quote", "file", "page"}."""
    kind: str
    text: str
    sources: Sequence[dict[str, Any]] = ()


# ---------- the fact guard ----------

def no_new_facts(text: str, allowed_texts: Iterable[str]) -> bool:
    """True when every number in `text` also occurs in one of `allowed_texts`.

    Numbers are compared in canonical form (see engine.verify.numbers_in), so "2,5" and "2.5" match.
    """
    allowed: set[str] = set()
    for source in allowed_texts:
        if source:
            allowed.update(numbers_in(source))
    return all(number in allowed for number in numbers_in(text))


def _allowed_texts(req: ReqView, evidence: Sequence[EvidenceRef]) -> list[str]:
    parts = [
        req.code, req.parameter, req.parameter_ru or "", req.value or "", req.value_max or "",
        req.unit or "", req.confirmation_method or "", req.source_quote,
        str(req.source_page) if req.source_page is not None else "",
    ]
    for ev in evidence:
        parts += [ev.quote, ev.value_raw or "", str(ev.page) if ev.page is not None else ""]
    return parts


# ---------- wording ----------

_CONDITION = {
    "ru": {">=": "не менее {v}{u}", "<=": "не более {v}{u}", "=": "равно {v}{u}", "range": "от {v} до {m}{u}"},
    "en": {">=": "at least {v}{u}", "<=": "at most {v}{u}", "=": "equal to {v}{u}", "range": "from {v} to {m}{u}"},
    "uz": {">=": "kamida {v}{u}", "<=": "koʻpi bilan {v}{u}", "=": "{v}{u} ga teng", "range": "{v} dan {m}{u} gacha"},
}

# Templates use {req} (the requirement phrase) and {conf} (" (confirmation: ...)" or "").
_TEXT: dict[str, dict[str, tuple[str, str]]] = {
    "ru": {
        "VALUE_FAIL_GOODS": (
            "Характеристика товара не соответствует требованию {req}.",
            "Замените товар на модель, которая соответствует требованию, или запросите разъяснение у заказчика. Значения в документах не изменяйте.",
        ),
        "VALUE_FAIL_CONFLICT": (
            "Данные участника различаются в документах заявки: {req}.",
            "Приведите документы к одному значению по достоверному источнику (регистрационному или учредительному документу). Значения не подгоняйте под требование.",
        ),
        "VALUE_FAIL_BIDDER": (
            "Данные участника не соответствуют требованию {req}.",
            "Требование нельзя выполнить до срока подачи. Уточните у заказчика, возможно ли участие на этих условиях.",
        ),
        "ABSENT": (
            "В заявке не найдено подтверждение требования {req}.",
            "Добавьте в заявку документ, который подтверждает требование{conf}.",
        ),
        "ATTRIBUTION": (
            "Подтверждение в заявке относится не к участнику, а к другой организации: {req}.",
            "Приложите собственный документ участника{conf}. Если субподряд допустим, запросите разъяснение у заказчика.",
        ),
        "WRONG_DOC_TYPE": (
            "Документ в заявке не того типа, который требует документация: {req}.",
            "Приложите документ требуемого типа{conf}.",
        ),
        "CLAIM_ONLY": (
            "Требование {req} подтверждено только заявлением участника, а документация требует подтверждающий документ.",
            "Приложите подтверждающий документ{conf}.",
        ),
        "EXPIRED": (
            "Срок действия документа, подтверждающего {req}, истёк до срока подачи.",
            "Приложите действующий документ{conf}.",
        ),
        "PARTIAL_COVERAGE": (
            "Заявка подтверждает требование {req} только частично.",
            "Дополните заявку недостающей частью подтверждения{conf}.",
        ),
        "CONTRADICTION": (
            "Документы заявки дают разные значения для требования {req}.",
            "Приведите документы к одному значению по достоверному источнику. Значения не подгоняйте под требование.",
        ),
        "CONTRADICTION_REVIEW": (
            "Документы заявки дают разные значения для требования {req}.",
            "Проверьте оба источника вручную и определите, какое значение верное.",
        ),
        "EQUIVALENT_NEEDS_EXPERT": (
            "Предложен эквивалент, и его соответствие требованию {req} требует экспертной оценки.",
            "Запросите у заказчика разъяснение, допустим ли такой эквивалент.",
        ),
        "LLM_ERROR": (
            "Проверка требования {req} не завершилась.",
            "Повторите проверку или проверьте требование вручную.",
        ),
        "UNREADABLE_IN_SCOPE": (
            "Страницы, где может быть подтверждение требования {req}, не удалось прочитать.",
            "Проверьте указанные страницы вручную.",
        ),
        "NO_DEADLINE": (
            "У документа есть срок действия, но срок подачи в проекте не указан: {req}.",
            "Укажите срок подачи в настройках проекта и повторите проверку.",
        ),
        "QUOTE_UNVERIFIED": (
            "Цитата, на которую опирается проверка требования {req}, не найдена на странице.",
            "Проверьте страницу вручную и подтвердите или отклоните результат.",
        ),
        "NEEDS_REVIEW": (
            "Требование {req} требует проверки человеком.",
            "Проверьте подтверждение вручную.",
        ),
        "DOES_NOT_MEET_GOODS": (
            "Требование {req} не выполняется.",
            "Замените товар или запросите разъяснение у заказчика. Значения в документах не изменяйте.",
        ),
        "DOES_NOT_MEET": (
            "Требование {req} не подтверждено заявкой.",
            "Приложите подтверждающий документ{conf}.",
        ),
        "PARTIAL": (
            "Требование {req} подтверждено частично.",
            "Дополните подтверждение{conf}.",
        ),
        "NOT_CONFIRMED": (
            "Требование {req} не подтверждено.",
            "Приложите подтверждающий документ{conf}.",
        ),
    },
    "en": {
        "VALUE_FAIL_GOODS": (
            "The product characteristic does not meet the requirement {req}.",
            "Replace the product with a model that meets the requirement, or ask the customer for clarification. Do not change the values in the documents.",
        ),
        "VALUE_FAIL_CONFLICT": (
            "The bidder's data differs between the bid documents: {req}.",
            "Align the documents to one value from a reliable source (registration or founding document). Do not adjust values to match the requirement.",
        ),
        "VALUE_FAIL_BIDDER": (
            "The bidder's data does not meet the requirement {req}.",
            "The requirement cannot be met before the deadline. Ask the customer whether participation on these terms is possible.",
        ),
        "ABSENT": (
            "The bid does not contain evidence for the requirement {req}.",
            "Add a document that confirms the requirement{conf}.",
        ),
        "ATTRIBUTION": (
            "The evidence in the bid is about another organisation, not the bidder: {req}.",
            "Attach the bidder's own document{conf}. If subcontracting is allowed, ask the customer for clarification.",
        ),
        "WRONG_DOC_TYPE": (
            "The bid document is not the type the tender requires: {req}.",
            "Attach a document of the required type{conf}.",
        ),
        "CLAIM_ONLY": (
            "The requirement {req} is supported only by the bidder's statement, while the tender requires a confirming document.",
            "Attach the confirming document{conf}.",
        ),
        "EXPIRED": (
            "The document that confirms {req} expired before the submission deadline.",
            "Attach a valid document{conf}.",
        ),
        "PARTIAL_COVERAGE": (
            "The bid confirms the requirement {req} only in part.",
            "Complete the bid with the missing part of the evidence{conf}.",
        ),
        "CONTRADICTION": (
            "The bid documents give different values for the requirement {req}.",
            "Align the documents to one value from a reliable source. Do not adjust values to match the requirement.",
        ),
        "CONTRADICTION_REVIEW": (
            "The bid documents give different values for the requirement {req}.",
            "Check both sources by hand and decide which value is correct.",
        ),
        "EQUIVALENT_NEEDS_EXPERT": (
            "An equivalent is offered, and whether it meets the requirement {req} needs expert judgement.",
            "Ask the customer whether such an equivalent is acceptable.",
        ),
        "LLM_ERROR": (
            "The check of the requirement {req} did not complete.",
            "Run the check again or check the requirement by hand.",
        ),
        "UNREADABLE_IN_SCOPE": (
            "Pages that may hold the evidence for {req} could not be read.",
            "Check the listed pages by hand.",
        ),
        "NO_DEADLINE": (
            "The document has a validity date, but the submission deadline is not set in the project: {req}.",
            "Set the submission deadline in the project settings and run the check again.",
        ),
        "QUOTE_UNVERIFIED": (
            "The quote the check of {req} relies on was not found on the page.",
            "Check the page by hand and confirm or reject the result.",
        ),
        "NEEDS_REVIEW": (
            "The requirement {req} needs a human review.",
            "Check the evidence by hand.",
        ),
        "DOES_NOT_MEET_GOODS": (
            "The requirement {req} is not met.",
            "Replace the product or ask the customer for clarification. Do not change the values in the documents.",
        ),
        "DOES_NOT_MEET": (
            "The requirement {req} is not confirmed by the bid.",
            "Attach a confirming document{conf}.",
        ),
        "PARTIAL": (
            "The requirement {req} is confirmed only in part.",
            "Complete the confirmation{conf}.",
        ),
        "NOT_CONFIRMED": (
            "The requirement {req} is not confirmed.",
            "Attach a confirming document{conf}.",
        ),
    },
    "uz": {
        "VALUE_FAIL_GOODS": (
            "Tovar xarakteristikasi {req} talabiga mos kelmaydi.",
            "Tovarni talabga mos modelga almashtiring yoki buyurtmachidan tushuntirish soʻrang. Hujjatlardagi qiymatlarni oʻzgartirmang.",
        ),
        "VALUE_FAIL_CONFLICT": (
            "Ishtirokchi maʼlumotlari taklif hujjatlarida bir-biriga mos kelmaydi: {req}.",
            "Hujjatlarni ishonchli manbaga (roʻyxatdan oʻtish yoki taʼsis hujjatiga) koʻra bitta qiymatga keltiring. Qiymatlarni talabga moslab oʻzgartirmang.",
        ),
        "VALUE_FAIL_BIDDER": (
            "Ishtirokchi maʼlumotlari {req} talabiga mos kelmaydi.",
            "Talabni taklif topshirish muddatigacha bajarib boʻlmaydi. Buyurtmachidan shu shartlarda ishtirok etish mumkinligini aniqlang.",
        ),
        "ABSENT": (
            "Taklifda {req} talabini tasdiqlovchi hujjat topilmadi.",
            "Talabni tasdiqlovchi hujjatni taklifga qoʻshing{conf}.",
        ),
        "ATTRIBUTION": (
            "Taklifdagi tasdiqlash ishtirokchiga emas, boshqa tashkilotga tegishli: {req}.",
            "Ishtirokchining oʻz hujjatini ilova qiling{conf}. Agar subpudratchilikka ruxsat etilgan boʻlsa, buyurtmachidan tushuntirish soʻrang.",
        ),
        "WRONG_DOC_TYPE": (
            "Taklifdagi hujjat tender talab qilgan turdagi emas: {req}.",
            "Talab qilingan turdagi hujjatni ilova qiling{conf}.",
        ),
        "CLAIM_ONLY": (
            "{req} talabi faqat ishtirokchining bayonoti bilan tasdiqlangan, hujjatlashtirish esa tasdiqlovchi hujjatni talab qiladi.",
            "Tasdiqlovchi hujjatni ilova qiling{conf}.",
        ),
        "EXPIRED": (
            "{req} talabini tasdiqlovchi hujjatning amal qilish muddati taklif topshirish muddatidan oldin tugagan.",
            "Amal qiladigan hujjatni ilova qiling{conf}.",
        ),
        "PARTIAL_COVERAGE": (
            "Taklif {req} talabini faqat qisman tasdiqlaydi.",
            "Taklifni tasdiqlashning yetishmayotgan qismi bilan toʻldiring{conf}.",
        ),
        "CONTRADICTION": (
            "Taklif hujjatlari {req} talabi uchun turli qiymatlarni koʻrsatadi.",
            "Hujjatlarni ishonchli manbaga koʻra bitta qiymatga keltiring. Qiymatlarni talabga moslab oʻzgartirmang.",
        ),
        "CONTRADICTION_REVIEW": (
            "Taklif hujjatlari {req} talabi uchun turli qiymatlarni koʻrsatadi.",
            "Ikkala manbani qoʻlda tekshiring va qaysi qiymat toʻgʻri ekanligini aniqlang.",
        ),
        "EQUIVALENT_NEEDS_EXPERT": (
            "Ekvivalent taklif qilingan, uning {req} talabiga mosligini ekspert baholashi kerak.",
            "Buyurtmachidan bunday ekvivalent qabul qilinishi mumkinligini soʻrang.",
        ),
        "LLM_ERROR": (
            "{req} talabini tekshirish yakunlanmadi.",
            "Tekshiruvni qayta ishga tushiring yoki talabni qoʻlda tekshiring.",
        ),
        "UNREADABLE_IN_SCOPE": (
            "{req} talabining tasdiqlanishi boʻlishi mumkin boʻlgan sahifalarni oʻqib boʻlmadi.",
            "Koʻrsatilgan sahifalarni qoʻlda tekshiring.",
        ),
        "NO_DEADLINE": (
            "Hujjatning amal qilish muddati bor, lekin loyihada taklif topshirish muddati koʻrsatilmagan: {req}.",
            "Loyiha sozlamalarida taklif topshirish muddatini kiriting va tekshiruvni qayta ishga tushiring.",
        ),
        "QUOTE_UNVERIFIED": (
            "{req} talabi tekshiruvi tayanadigan iqtibos sahifada topilmadi.",
            "Sahifani qoʻlda tekshiring va natijani tasdiqlang yoki rad eting.",
        ),
        "NEEDS_REVIEW": (
            "{req} talabi odam tomonidan tekshirilishi kerak.",
            "Tasdiqlashni qoʻlda tekshiring.",
        ),
        "DOES_NOT_MEET_GOODS": (
            "{req} talabi bajarilmagan.",
            "Tovarni almashtiring yoki buyurtmachidan tushuntirish soʻrang. Hujjatlardagi qiymatlarni oʻzgartirmang.",
        ),
        "DOES_NOT_MEET": (
            "{req} talabi taklif bilan tasdiqlanmagan.",
            "Tasdiqlovchi hujjatni ilova qiling{conf}.",
        ),
        "PARTIAL": (
            "{req} talabi faqat qisman tasdiqlangan.",
            "Tasdiqlashni toʻldiring{conf}.",
        ),
        "NOT_CONFIRMED": (
            "{req} talabi tasdiqlanmagan.",
            "Tasdiqlovchi hujjatni ilova qiling{conf}.",
        ),
    },
}

_FALLBACK = {
    "ru": (
        "Требование не подтверждено; проверьте его вручную.",
        "Проверьте требование вручную.",
    ),
    "en": (
        "The requirement is not confirmed; check it by hand.",
        "Check the requirement by hand.",
    ),
    "uz": (
        "Talab tasdiqlanmagan; uni qoʻlda tekshiring.",
        "Talabni qoʻlda tekshiring.",
    ),
}


def _lang(lang: str) -> str:
    return lang if lang in ("ru", "uz", "en") else "ru"


def _phrase(req: ReqView, lang: str) -> str:
    """The requirement as one phrase: code, parameter and condition. Only requirement fields."""
    head = req.parameter_ru if (lang == "ru" and req.parameter_ru) else req.parameter
    op = (req.operator or "text").strip().lower()
    condition = ""
    unit = f" {req.unit}" if req.unit else ""
    if op in ("range",) and req.value and req.value_max:
        condition = _CONDITION[lang]["range"].format(v=req.value, m=req.value_max, u=unit)
    elif op in _CONDITION[lang] and req.value:
        condition = _CONDITION[lang][op].format(v=req.value, u=unit)
    text = f"{req.code}: {head}"
    return f"{text} ({condition})" if condition else text


def _conf(req: ReqView, lang: str) -> str:
    if not req.confirmation_method:
        return ""
    label = {"ru": "подтверждение", "uz": "tasdiqlash", "en": "confirmation"}[lang]
    return f" ({label}: {req.confirmation_method})"


# ---------- routing ----------

def _key_for(status: str, reasons: Sequence[str]) -> str | None:
    order = _REVIEW_ORDER if status == _NEEDS_REVIEW else _OPEN_ORDER
    for code in order:
        if code in reasons:
            return code
    return None


def _route(req: ReqView, status: str, reasons: Sequence[str]) -> tuple[str, str]:
    """(template key, action type) for a non-meets status."""
    key = _key_for(status, reasons)
    if status == _NEEDS_REVIEW:
        if key == EQUIVALENT_NEEDS_EXPERT:
            return "EQUIVALENT_NEEDS_EXPERT", REQUEST_CLARIFICATION
        if key == CONTRADICTION:
            return "CONTRADICTION_REVIEW", CHECK_MANUALLY
        if key is not None:
            return key, CHECK_MANUALLY
        return "NEEDS_REVIEW", CHECK_MANUALLY

    goods = req.scope == "goods"
    if key == VALUE_FAIL:
        if goods:
            return "VALUE_FAIL_GOODS", REPLACE_GOODS
        # A text condition failed by the model's reading (no number compared) is usually data that
        # differs between the bid's own documents, e.g. a date in a form vs the registry extract:
        # the bidder can correct it. Only a measured value below the requirement is not fixable.
        if CONTRADICTION in reasons or req.operator == "text":
            return "VALUE_FAIL_CONFLICT", CORRECT_DATA
        return "VALUE_FAIL_BIDDER", NOT_FIXABLE
    if key == CONTRADICTION:
        return "CONTRADICTION", CORRECT_DATA
    if key in (ABSENT, ATTRIBUTION, WRONG_DOC_TYPE, CLAIM_ONLY, EXPIRED, PARTIAL_COVERAGE):
        return key, ADD_DOCUMENT

    # No reason code: fall back on the status alone.
    if status == _DOES_NOT_MEET:
        return ("DOES_NOT_MEET_GOODS", REPLACE_GOODS) if goods else ("DOES_NOT_MEET", ADD_DOCUMENT)
    if status == _PARTIAL:
        return "PARTIAL", ADD_DOCUMENT
    if status == _NOT_CONFIRMED:
        return "NOT_CONFIRMED", ADD_DOCUMENT
    raise ValueError(f"no recommendation for status {status!r}")


def _criticality(action_type: str, req: ReqView) -> str:
    if action_type == REQUEST_CLARIFICATION:
        return "medium"
    if action_type == CORRECT_DATA:
        return "high"
    return "high" if req.req_type == "mandatory" else "medium"


def _sources(req: ReqView, evidence: Sequence[EvidenceRef]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if req.source_file or req.source_page is not None or req.source_quote:
        out.append({"side": "tender", "file": req.source_file, "page": req.source_page, "quote": req.source_quote})
    for ev in evidence:
        out.append({"side": "bid", "file": ev.file_name, "page": ev.page, "quote": ev.quote})
    return out


def _safe(text: str, allowed: list[str], fallback: str) -> str:
    return text if no_new_facts(text, allowed) else fallback


# ---------- public API ----------

def recommend(
    req: ReqView,
    status: str,
    reasons: Iterable[str],
    evidence_summary: Sequence[EvidenceRef],
    lang: str = "ru",
) -> RecView:
    """The recommendation for one non-meets requirement (§6.7).

    `status` is the effective status (after any override). `reasons` are the reason codes of the
    match. `evidence_summary` is the verified bid evidence; it is quoted in `sources` only.
    """
    if status == _MEETS:
        raise ValueError("a requirement that meets has no recommendation")
    lang = _lang(lang)
    reason_list = list(reasons)
    evidence = list(evidence_summary)

    key, action_type = _route(req, status, reason_list)
    problem_tpl, action_tpl = _TEXT[lang][key]
    values = {"req": _phrase(req, lang), "conf": _conf(req, lang)}
    allowed = _allowed_texts(req, evidence)
    fallback_problem, fallback_action = _FALLBACK[lang]
    problem = _safe(problem_tpl.format(req=values["req"], conf=""), allowed, fallback_problem)
    action = _safe(action_tpl.format(**values), allowed, fallback_action)

    return RecView(
        problem=problem,
        action=action,
        action_type=action_type,
        criticality=_criticality(action_type, req),
        sources=_sources(req, evidence),
    )


def collect_list(entries: Iterable[CollectEntry]) -> list[CollectEntry]:
    """The "still to collect" list: add_document actions, mandatory first, then by impact."""
    rows = [e for e in entries if e.action_type == ADD_DOCUMENT]
    return sorted(rows, key=lambda e: (not e.mandatory, -e.impact_pct, e.code))


_LETTER_HEAD = {
    "ru": ("Запрос на разъяснение документации", "Прошу разъяснить следующие положения документации:",
           "Цитата", "стр.", "файл"),
    "en": ("Request for clarification of the tender documentation",
           "Please clarify the following provisions of the tender documentation:",
           "Quote", "p.", "file"),
    "uz": ("Tender hujjatiga tushuntirish soʻrovi",
           "Tender hujjatining quyidagi qoidalarini tushuntirib berishingizni soʻraymiz:",
           "Iqtibos", "b.", "fayl"),
}


def clarification_request(questions: Iterable[QuestionView], lang: str = "ru") -> str:
    """Plain-text draft of the clarification request, one numbered item per question with its quotes.

    Only the questions and their tender quotes are used. Question numbers are structural.
    """
    lang = _lang(lang)
    title, intro, quote_label, page_label, file_label = _LETTER_HEAD[lang]
    lines = [title, "", intro, ""]
    for number, question in enumerate(questions, start=1):
        lines.append(f"{number}. {question.text}")
        for src in question.sources:
            quote = str(src.get("quote") or "").strip()
            if not quote:
                continue
            where = []
            if src.get("file"):
                where.append(f"{file_label} {src['file']}")
            if src.get("page") is not None:
                where.append(f"{page_label} {src['page']}")
            tail = f" ({', '.join(where)})" if where else ""
            lines.append(f"   {quote_label}{tail}: «{quote}»")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
