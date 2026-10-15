"""Stage 4 page cards with a fake LLM (no network). Async code runs via asyncio.run."""

import asyncio

from engine.cards import DOC_TYPES, CardInput, describe_pages
from llm.client import CallMeta, LLMError
from llm.schemas import Entity, PageCardOut, PageCardsOut

PAGE_TEXT = (
    "СЕРТИФИКАТ № 123/45 от 05.10.2026\n"
    "Выдан ООО «Demotex», STIR 123456789. Срок действия до 05.10.2027 года."
)
OTHER_PAGE = "Техническое предложение участника на поставку насосного оборудования и запчастей."


class FakeLLM:
    """Replies are popped in order: a PageCardsOut, or an exception to raise."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def complete(self, task, prompt, variables, schema, images=None, tier=None):
        self.calls.append({"task": task, "prompt": prompt, "variables": variables, "schema": schema, "tier": tier})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        meta = CallMeta(task=task, model_requested="m", model_answered="m",
                        prompt_version=f"{prompt}@v1", cache_hit=False)
        return reply, meta


def make_page(page_id=1, label="DOC-07 p.41", text=PAGE_TEXT, number=1, doc_pages=None):
    pages = doc_pages if doc_pages is not None else {number: text}
    return CardInput(
        page_id=page_id,
        label=label,
        file_name="cert.pdf",
        text=text,
        is_ocr=False,
        doc_pages=pages,
        doc_pages_ocr={n: False for n in pages},
        page_number=number,
    )


def cert_card(label="DOC-07 p.41", **overrides) -> PageCardOut:
    base = dict(
        page_ref=label,
        doc_type="certificate",
        title="Сертификат",
        issuer="ООО «Demotex»",
        doc_number="123/45",
        doc_date="05.10.2026",
        valid_until="05.10.2027",
        entities=[],
        people_count=0,
        summary_ru="Сертификат соответствия.",
        keywords=["сертификат"],
        field_quotes={
            "title": "СЕРТИФИКАТ № 123/45",
            "issuer": "Выдан ООО «Demotex»",
            "doc_number": "СЕРТИФИКАТ № 123/45 от 05.10.2026",
            "doc_date": "СЕРТИФИКАТ № 123/45 от 05.10.2026",
            "valid_until": "Срок действия до 05.10.2027 года",
        },
    )
    base.update(overrides)
    return PageCardOut(**base)


def run(llm, pages, batch_size=5):
    return asyncio.run(describe_pages(llm, pages, batch_size=batch_size))


def test_verified_fields_are_kept_and_dates_become_iso():
    llm = FakeLLM(PageCardsOut(cards=[cert_card()]))

    results, metas = run(llm, [make_page()])

    res = results[0]
    assert res.dropped == []
    assert res.fields["doc_type"] == "certificate"
    assert res.fields["title"] == "Сертификат"
    assert res.fields["issuer"] == "ООО «Demotex»"
    assert res.fields["doc_number"] == "123/45"
    assert res.fields["doc_date"] == "2026-10-05"
    assert res.fields["valid_until"] == "2027-10-05"
    assert set(res.fields["field_quotes"]) == {"title", "issuer", "doc_number", "doc_date", "valid_until"}
    assert res.fields["summary_ru"] == "Сертификат соответствия."
    assert res.fields["keywords"] == ["сертификат"]
    assert len(metas) == 1


def test_unparseable_date_is_kept_as_written():
    page = make_page(text=PAGE_TEXT + " Срок действия бессрочно.")
    card = cert_card(valid_until="бессрочно", field_quotes={"valid_until": "Срок действия бессрочно."})

    # Four of five filled fields drop, so the batch is re-asked once on the next tier (second reply).
    results, _ = run(FakeLLM(PageCardsOut(cards=[card]), PageCardsOut(cards=[card])), [page])

    assert results[0].fields["valid_until"] == "бессрочно"
    assert "valid_until" not in results[0].dropped


def test_field_with_non_verifying_quote_is_dropped():
    card = cert_card(field_quotes={
        "title": "СЕРТИФИКАТ № 123/45",
        "issuer": "Совсем другой текст которого нет на странице",
        "doc_number": "СЕРТИФИКАТ № 123/45 от 05.10.2026",
        "doc_date": "СЕРТИФИКАТ № 123/45 от 05.10.2026",
        "valid_until": "Срок действия до 05.10.2027 года",
    })

    results, _ = run(FakeLLM(PageCardsOut(cards=[card])), [make_page()])

    res = results[0]
    assert res.dropped == ["issuer"]
    assert res.fields["issuer"] is None
    assert "issuer" not in res.fields["field_quotes"]
    assert res.fields["title"] == "Сертификат"


def test_field_without_quote_is_dropped():
    card = cert_card(field_quotes={})

    # Every filled field drops, so the page is re-asked once on the next tier (second reply).
    results, _ = run(FakeLLM(PageCardsOut(cards=[card]), PageCardsOut(cards=[card])), [make_page()])

    assert set(results[0].dropped) == {"title", "issuer", "doc_number", "doc_date", "valid_until"}
    assert results[0].fields["doc_number"] is None
    assert results[0].fields["field_quotes"] == {}


def test_number_missing_from_quote_drops_the_numeric_field():
    card = cert_card(doc_number="123/46", field_quotes={
        "title": "СЕРТИФИКАТ № 123/45",
        "issuer": "Выдан ООО «Demotex»",
        "doc_number": "СЕРТИФИКАТ № 123/45 от",  # verifies, but does not contain 46
        "doc_date": "СЕРТИФИКАТ № 123/45 от 05.10.2026",
        "valid_until": "Срок действия до 05.10.2027 года",
    })

    results, _ = run(FakeLLM(PageCardsOut(cards=[card])), [make_page()])

    assert "doc_number" in results[0].dropped
    assert results[0].fields["doc_number"] is None


def test_entity_with_bad_quote_is_dropped_and_good_one_kept():
    good = Entity(name="ООО «Demotex»", stir="123456789", role="issuer", quote="выдан ООО «Demotex»")
    bad = Entity(name="Фантом", stir="987654321", role="other", quote="Фантом Групп не упоминается здесь")
    card = cert_card(entities=[good, bad])

    results, _ = run(FakeLLM(PageCardsOut(cards=[card])), [make_page()])

    res = results[0]
    assert res.fields["entities"] == [{"name": "ООО «Demotex»", "stir": "123456789", "role": "issuer"}]
    assert "entity:Фантом" in res.dropped


def test_invalid_stir_becomes_none():
    ent = Entity(name="ООО «Demotex»", stir="12345", role="issuer", quote="выдан ООО «Demotex»")

    results, _ = run(FakeLLM(PageCardsOut(cards=[cert_card(entities=[ent])])), [make_page()])

    assert results[0].fields["entities"] == [{"name": "ООО «Demotex»", "stir": None, "role": "issuer"}]


def test_stir_with_separators_is_normalised():
    ent = Entity(name="ООО «Demotex»", stir="12-345-6789", role="issuer", quote="выдан ООО «Demotex»")

    results, _ = run(FakeLLM(PageCardsOut(cards=[cert_card(entities=[ent])])), [make_page()])

    assert results[0].fields["entities"][0]["stir"] == "123456789"


def test_unknown_doc_type_becomes_other_with_warning():
    results, _ = run(FakeLLM(PageCardsOut(cards=[cert_card(doc_type="mystery_paper")])), [make_page()])

    assert results[0].fields["doc_type"] == "other"
    assert any("mystery_paper" in w for w in results[0].warnings)


def test_doc_type_spelling_variants_are_accepted():
    results, _ = run(FakeLLM(PageCardsOut(cards=[cert_card(doc_type="Technical Proposal")])), [make_page()])

    assert results[0].fields["doc_type"] == "technical_proposal"
    assert results[0].warnings == []


def test_missing_card_becomes_other_with_warning():
    pages = [make_page(page_id=1, label="DOC-01 p.1"), make_page(page_id=2, label="DOC-01 p.2", text=OTHER_PAGE)]
    # The batch is re-asked once on the next tier; the second reply still has no card for page 2.
    llm = FakeLLM(PageCardsOut(cards=[cert_card(label="DOC-01 p.1")]),
                  PageCardsOut(cards=[cert_card(label="DOC-01 p.1")]))

    results, _ = run(llm, pages)

    assert results[1].page_id == 2
    assert results[1].fields["doc_type"] == "other"
    assert results[1].warnings == ["no card returned"]
    assert results[0].fields["doc_type"] == "certificate"


def test_labels_match_ignoring_case_and_whitespace():
    card = cert_card(label="doc-07   p.41")

    results, _ = run(FakeLLM(PageCardsOut(cards=[card])), [make_page(label="DOC-07 p.41")])

    assert results[0].fields["doc_type"] == "certificate"
    assert results[0].warnings == []


def test_batches_of_batch_size_in_input_order():
    pages = [make_page(page_id=i, label=f"DOC-01 p.{i}", number=i, doc_pages={i: OTHER_PAGE})
             for i in range(1, 8)]
    llm = FakeLLM(
        PageCardsOut(cards=[PageCardOut(page_ref=f"DOC-01 p.{i}", doc_type="act") for i in range(1, 6)]),
        PageCardsOut(cards=[PageCardOut(page_ref=f"DOC-01 p.{i}", doc_type="invoice") for i in (6, 7)]),
    )

    results, metas = run(llm, pages, batch_size=5)

    assert [len(c["variables"]["pages"]) for c in llm.calls] == [5, 2]
    assert [r.page_id for r in results] == list(range(1, 8))
    assert [r.fields["doc_type"] for r in results] == ["act"] * 5 + ["invoice"] * 2
    assert len(metas) == 2
    assert llm.calls[0]["variables"]["doc_types"] == DOC_TYPES
    assert llm.calls[0]["task"] == "page_cards"
    assert llm.calls[0]["prompt"] == "page_cards"


def test_batch_failure_falls_back_to_one_call_per_page():
    pages = [make_page(page_id=1, label="DOC-01 p.1", doc_pages={1: OTHER_PAGE}),
             make_page(page_id=2, label="DOC-01 p.2", doc_pages={1: OTHER_PAGE}, number=2)]
    llm = FakeLLM(
        LLMError("batch too large"),
        PageCardsOut(cards=[PageCardOut(page_ref="DOC-01 p.1", doc_type="act")]),
        PageCardsOut(cards=[PageCardOut(page_ref="DOC-01 p.2", doc_type="invoice")]),
    )

    results, metas = run(llm, pages)

    assert [len(c["variables"]["pages"]) for c in llm.calls] == [2, 1, 1]
    assert [r.fields["doc_type"] for r in results] == ["act", "invoice"]
    assert all(any("batch too large" in w for w in r.warnings) for r in results)
    assert len(metas) == 2


def test_page_that_fails_alone_is_other_and_does_not_stop_the_others():
    pages = [make_page(page_id=1, label="DOC-01 p.1", doc_pages={1: OTHER_PAGE}),
             make_page(page_id=2, label="DOC-01 p.2", doc_pages={1: OTHER_PAGE}, number=2)]
    llm = FakeLLM(
        LLMError("bad batch"),
        LLMError("page 1 failed"),
        PageCardsOut(cards=[PageCardOut(page_ref="DOC-01 p.2", doc_type="invoice")]),
        LLMError("escalated call failed"),  # page 1 has no card: the re-ask fails, the first answer stands
    )

    results, _ = run(llm, pages)

    assert results[0].fields["doc_type"] == "other"
    assert any("page 1 failed" in w for w in results[0].warnings)
    assert results[1].fields["doc_type"] == "invoice"


def test_long_page_text_is_truncated_in_the_prompt_with_a_warning():
    long_text = OTHER_PAGE + " " + ("слово " * 1500)
    assert len(long_text) > 6000
    page = make_page(text=long_text, doc_pages={1: long_text})
    llm = FakeLLM(PageCardsOut(cards=[PageCardOut(page_ref="DOC-07 p.41", doc_type="act")]))

    results, _ = run(llm, [page])

    sent = llm.calls[0]["variables"]["pages"][0]["text"]
    assert len(sent) == 6000
    assert any("truncated" in w for w in results[0].warnings)


def test_people_count_and_keywords_are_cleaned():
    card = cert_card(people_count=-2, keywords=["  насос ", "", "  "])

    results, _ = run(FakeLLM(PageCardsOut(cards=[card])), [make_page()])

    assert results[0].fields["people_count"] == 0
    assert results[0].fields["keywords"] == ["насос"]
