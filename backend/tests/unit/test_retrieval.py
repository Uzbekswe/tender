"""Tests for engine.retrieval (ARCHITECTURE §4.2 stage 8). Synthetic Russian corpus, no LLM."""

from __future__ import annotations

from engine.retrieval import (
    group_pages,
    Candidates,
    PageDoc,
    PageIndex,
    ReqQuery,
    build_terms,
    candidates,
    group_queries,
)


def _page(page_id: int, doc_type: str | None, text: str, *, document_id: int = 1,
          summary: str = "", keywords: tuple[str, ...] = ()) -> PageDoc:
    return PageDoc(
        page_id=page_id,
        label=f"DOC-{document_id:02d} p.{page_id}",
        document_id=document_id,
        doc_type=doc_type,
        text=text,
        summary=summary,
        keywords=keywords,
    )


def _query(req_id: int, terms: tuple[str, ...], doc_types: tuple[str, ...] = (), *,
           scope: str = "bidder", category: str = "supply", lot_item: str | None = None) -> ReqQuery:
    return ReqQuery(req_id=req_id, terms=terms, evidence_doc_types=doc_types,
                    scope=scope, category=category, lot_item=lot_item)


def _filler(n: int, start: int = 900) -> list[PageDoc]:
    """Unrelated pages. BM25 needs a corpus larger than the query terms' document
    frequency, otherwise a term found on half the pages gets a zero or negative weight."""
    return [_page(start + i, "other", f"Служебная записка {i} регистрационный журнал {i}.")
            for i in range(n)]


def _corpus() -> list[PageDoc]:
    return [
        _page(1, "contract", "Договор поставки оборудования № 12 от 01.02.2025 заключен сторонами."),
        _page(2, "contract", "Настоящий договор регулирует условия исполнения обязательств."),
        _page(3, "technical_proposal", "Технические характеристики: мощность 2,5 кВт, гарантия 24 месяца."),
        _page(4, "letter", "Письмо о гарантийных обязательствах поставщика."),
        _page(5, "staff_list", "Список персонала: инженеры и монтажники, опыт работы."),
    ]


# ---------- tokenisation and BM25 ----------

def test_stemming_matches_inflected_russian_forms():
    index = PageIndex(_corpus())
    genitive = index.search(["договора"], top_k=5)
    nominative = index.search(["договор"], top_k=5)
    assert genitive and nominative
    assert {pid for pid, _ in genitive} == {pid for pid, _ in nominative}
    assert {1, 2} <= {pid for pid, _ in genitive}


def test_search_returns_positive_scores_best_first():
    index = PageIndex(_corpus())
    hits = index.search(["гарантия", "гарантийных"], top_k=8)
    scores = [score for _, score in hits]
    assert hits and all(score > 0 for score in scores)
    assert scores == sorted(scores, reverse=True)
    assert hits[0][0] in {3, 4}


def test_search_unknown_terms_and_empty_index():
    assert PageIndex(_corpus()).search(["вертолет"], top_k=5) == []
    assert PageIndex([]).search(["договор"], top_k=5) == []
    assert PageIndex([]).search([], top_k=5) == []


def test_stopwords_and_short_tokens_do_not_match():
    index = PageIndex(_corpus())
    # "и" and "по" are stopwords and "от" is shorter than three letters.
    assert index.search(["и", "по", "от"], top_k=5) == []


def test_uzbek_apostrophes_are_unified():
    pages = [
        _page(1, "technical_proposal", "O‘rnatish va sozlash ishlari bajariladi."),
        _page(2, "letter", "Boshqa matn bu yerda."),
    ] + _filler(20)
    index = PageIndex(pages)
    hits = index.search(["o'rnatish"], top_k=5)
    assert [pid for pid, _ in hits] == [1]


def test_summary_and_keywords_are_searchable():
    pages = [
        _page(1, "other", "Общий текст без специальных слов.", summary="Штатное расписание", keywords=("персонал",)),
        _page(2, "other", "Общий текст без специальных слов."),
    ] + _filler(20)
    index = PageIndex(pages)
    assert [pid for pid, _ in index.search(["персонала"], top_k=5)] == [1]
    assert [pid for pid, _ in index.search(["штатное"], top_k=5)] == [1]


def test_summary_and_keywords_weigh_more_than_plain_text():
    pages = [
        _page(1, "other", "Документ упоминает сертификат один раз."),
        _page(2, "other", "Документ упоминает сертификат один раз.", keywords=("сертификат",)),
    ] + _filler(20)
    hits = PageIndex(pages).search(["сертификат"], top_k=5)
    assert [pid for pid, _ in hits] == [2, 1]


# ---------- candidates ----------

def test_doc_type_pages_come_first_then_search_hits():
    pages = [
        _page(1, "contract", "Текст договора без искомых слов."),
        _page(2, "technical_proposal", "Гарантия 24 месяца, гарантия производителя, гарантия сервиса."),
        _page(3, "contract", "Ещё один документ."),
    ]
    q = _query(1, ("гарантия",), ("contract",))
    result = candidates(q, PageIndex(pages), cap=12)
    assert result.by_doc_type == [1, 3]
    assert result.by_search == [2]
    assert result.page_ids == [1, 3, 2]
    assert result.dropped == []


def test_doc_type_pages_ordered_by_search_score_then_page_order():
    pages = [
        _page(1, "contract", "Текст без слов."),
        _page(2, "contract", "Поставка поставка поставка оборудования."),
        _page(3, "contract", "Поставка оборудования."),
        _page(4, "contract", "Текст без слов."),
    ] + _filler(20)
    q = _query(1, ("поставка",), ("contract",))
    result = candidates(q, PageIndex(pages), cap=12)
    assert result.by_doc_type == [2, 3, 1, 4]


def test_cap_cuts_and_reports_dropped_pages():
    pages = [_page(i, "contract", f"Договор номер {i}.") for i in range(1, 16)]
    q = _query(1, ("договор",), ("contract",))
    result = candidates(q, PageIndex(pages), cap=12)
    assert len(result.page_ids) == 12
    assert result.page_ids == list(range(1, 13))
    assert result.dropped == [13, 14, 15]
    assert result.by_doc_type == list(range(1, 16))


def test_goods_scope_adds_proposal_pages_bidder_scope_does_not():
    pages = [
        _page(1, "contract", "Договор."),
        _page(2, "technical_proposal", "Описание оборудования без совпадений."),
        _page(3, "price_offer", "Цены без совпадений."),
        _page(4, "letter", "Письмо без совпадений."),
    ]
    index = PageIndex(pages)
    goods = candidates(_query(1, ("договор",), ("contract",), scope="goods"), index, cap=12)
    bidder = candidates(_query(2, ("договор",), ("contract",), scope="bidder"), index, cap=12)
    assert goods.page_ids == [1, 2, 3]
    assert bidder.page_ids == [1]


def test_wide_mode_returns_top_25_search_hits_regardless_of_doc_type():
    pages = [_page(i, "letter", f"Гарантия номер {i}.") for i in range(1, 31)]
    pages.append(_page(31, "contract", "Договор без слова."))
    pages += _filler(40)
    q = _query(1, ("гарантия",), ("contract",))
    result = candidates(q, PageIndex(pages), wide=True)
    assert len(result.page_ids) == 25
    assert result.by_doc_type == []
    assert result.dropped == []
    assert 31 not in result.page_ids
    assert result.by_search == result.page_ids


def test_wide_mode_includes_non_matching_doc_type_hits():
    pages = [
        _page(1, "contract", "Текст без слов."),
        _page(2, "letter", "Гарантия производителя."),
    ] + _filler(20)
    q = _query(1, ("гарантия",), ("contract",))
    narrow = candidates(q, PageIndex(pages), cap=12)
    wide = candidates(q, PageIndex(pages), wide=True)
    assert narrow.by_doc_type == [1]          # contract page is a doc-type match, though it has no hit
    assert narrow.by_search == [2]            # the letter is found by search only
    assert narrow.page_ids == [1, 2]
    assert wide.page_ids == [2]               # wide mode ignores the doc type


def test_no_evidence_types_and_no_hits_gives_no_candidates():
    pages = [_page(1, "letter", "Текст.")]
    result = candidates(_query(1, ("вертолет",), ()), PageIndex(pages), cap=12)
    assert result == Candidates(page_ids=[], by_doc_type=[], by_search=[], dropped=[])


# ---------- grouping ----------



def test_group_only_merges_same_scope_category_and_lot():
    queries = [
        _query(1, ("x",), (), scope="goods", category="supply", lot_item="A"),
        _query(2, ("x",), (), scope="goods", category="supply", lot_item="B"),
        _query(3, ("x",), (), scope="bidder", category="supply", lot_item="A"),
        _query(4, ("x",), (), scope="goods", category="supply", lot_item="A"),
    ]
    cands = {i: Candidates(page_ids=[1], by_doc_type=[], by_search=[], dropped=[]) for i in range(1, 5)}
    # Batches are ordered by (scope, lot_item): bidder before goods, then lot A before lot B.
    assert group_queries(queries, cands) == [[3], [1, 4], [2]]


def test_group_is_deterministic_by_req_id():
    queries = [_query(9, ("x",), ()), _query(2, ("x",), ()), _query(5, ("x",), ())]
    cands = {i: Candidates(page_ids=[1], by_doc_type=[], by_search=[], dropped=[]) for i in (2, 5, 9)}
    assert group_queries(queries, cands) == [[2, 5, 9]]
    assert group_queries(list(reversed(queries)), cands) == [[2, 5, 9]]



def test_build_terms_drops_stopwords_and_short_words():
    terms = build_terms([], "и по от двигатель", None, "это на данный момент")
    assert terms == ("двигатель", "данный", "момент")


def test_build_terms_limits_parameter_and_quote_words_to_12_most_informative():
    words = [f"слово{'х' * n}" for n in range(1, 16)]  # 15 distinct words, lengths 7..21
    parameter = " ".join(words)
    terms = build_terms([], parameter, None, "")
    assert len(terms) == 12
    kept = set(terms)
    assert f"слово{'х' * 15}" in kept  # longest kept
    assert f"слово{'х' * 1}" not in kept  # shortest dropped
    # Kept words stay in their original order.
    positions = [words.index(t) for t in terms]
    assert positions == sorted(positions)


def test_build_terms_strips_and_is_order_stable_on_empty_input():
    assert build_terms([], "", None, "") == ()
    assert build_terms(["  договор  "], "", None, "") == ("договор",)


def test_common_term_in_small_package_is_still_found() -> None:
    # With Okapi BM25 a term on most pages of a small package scores zero; it must still match.
    pages = [
        PageDoc(page_id=i, label=f"DOC-01 p.{i}", document_id=1, doc_type="contract",
                text=("Договор оказания услуг по утилизации" if i < 4 else "Справка о составе работников"))
        for i in range(1, 5)
    ]
    hits = PageIndex(pages).search(["договор"], top_k=8)
    assert [pid for pid, _ in hits] == [1, 2, 3]



def test_groups_by_scope_in_batches_of_max_group() -> None:
    queries = [ReqQuery(req_id=i, terms=("x",), evidence_doc_types=(), scope="bidder" if i <= 8 else "goods",
                        category=f"free text {i}") for i in range(1, 11)]
    cands = {q.req_id: Candidates(page_ids=[q.req_id], by_doc_type=[], by_search=[], dropped=[]) for q in queries}
    assert group_queries(queries, cands, max_group=6) == [[1, 2, 3, 4, 5, 6], [7, 8], [9, 10]]


def test_group_pages_round_robin_keeps_each_requirements_best_pages() -> None:
    cands = {
        1: Candidates(page_ids=[10, 11, 12, 13], by_doc_type=[], by_search=[], dropped=[]),
        2: Candidates(page_ids=[20, 10, 21, 22], by_doc_type=[], by_search=[], dropped=[]),
    }
    assert group_pages([1, 2], cands, max_pages=4) == [10, 20, 11, 12]


# ---------- grouping by requirement code bucket ----------

def _coded_queries(codes: list[str], *, scope: str = "bidder") -> list[ReqQuery]:
    return [ReqQuery(req_id=i, terms=("x",), evidence_doc_types=(), scope=scope,
                     category="free text", code=code) for i, code in enumerate(codes, start=1)]


def _coded_cands(queries: list[ReqQuery]) -> dict[int, Candidates]:
    return {q.req_id: Candidates(page_ids=[q.req_id], by_doc_type=[], by_search=[], dropped=[]) for q in queries}


def _batch_codes(queries: list[ReqQuery]) -> list[list[str]]:
    """Batches as lists of requirement codes, so that comparisons do not depend on req_id."""
    by_id = {q.req_id: q.code for q in queries}
    return [[by_id[rid] for rid in batch] for batch in group_queries(queries, _coded_cands(queries))]


def test_removing_one_requirement_changes_only_its_code_bucket() -> None:
    codes = [f"U-{n:03d}" for n in range(1, 19)]  # U-001..U-018: buckets 0, 1 and 2 of six each
    before = _batch_codes(_coded_queries(codes))
    after = _batch_codes(_coded_queries([c for c in codes if c != "U-008"]))
    assert before == [
        [f"U-{n:03d}" for n in range(1, 7)],
        [f"U-{n:03d}" for n in range(7, 13)],
        [f"U-{n:03d}" for n in range(13, 19)],
    ]
    assert after[0] == before[0]  # bucket 0 keeps identical members
    assert after[2] == before[2]  # bucket 2 keeps identical members
    assert after[1] == ["U-007", "U-009", "U-010", "U-011", "U-012"]  # only bucket 1 shrank


def test_manually_added_code_beyond_the_range_forms_its_own_bucket() -> None:
    codes = [f"U-{n:03d}" for n in range(1, 13)]
    before = _batch_codes(_coded_queries(codes))
    after = _batch_codes(_coded_queries(codes + ["U-099"]))
    assert after == before + [["U-099"]]


def test_codes_are_ordered_by_number_inside_a_bucket_and_codeless_items_come_last() -> None:
    queries = _coded_queries(["U-004", "NOCODE", "U-002", "U-001"])
    assert _batch_codes(queries) == [["U-001", "U-002", "U-004"], ["NOCODE"]]
