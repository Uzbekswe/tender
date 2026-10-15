"""Pure tender-extraction logic (engine.extract). No DB, no LLM."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.extract import (
    assign_codes,
    build_toc,
    build_windows,
    candidate_contradiction_groups,
    choose_scheme_pages,
    confidence,
    find_scheme_pages,
    merge_drafts,
    placed_quote_method,
    scheme_checks,
)
from llm.schemas import RequirementDraft, SchemeCriterion, SchemeLevel, SchemeOut


def _pages(n: int, texts: dict[int, str] | None = None) -> list[tuple[int, str]]:
    texts = texts or {}
    return [(i, texts.get(i, f"plain page {i} with some running text")) for i in range(1, n + 1)]


def _draft(**overrides) -> RequirementDraft:
    fields = dict(
        scope="goods", category="Technical", parameter="Warranty", parameter_ru="Гарантия",
        operator=">=", value="24", unit="months", req_type="mandatory", page=1,
        quote="The warranty period shall be 24 months from delivery.",
    )
    fields.update(overrides)
    return RequirementDraft(**fields)


# ---------- windows ----------


def test_windows_fixed_size_with_one_page_overlap():
    windows = build_windows(_pages(66))
    assert [(w.first, w.last) for w in windows] == [(1, 12), (12, 23), (23, 34), (34, 45), (45, 56), (56, 66)]
    assert [w.index for w in windows] == list(range(6))


def test_windows_small_document_is_one_window():
    windows = build_windows(_pages(5))
    assert [(w.first, w.last) for w in windows] == [(1, 5)]


def test_windows_empty_input():
    assert build_windows([]) == []


def test_windows_prefer_a_section_heading_inside_the_size_band():
    texts = {10: "II. Qualification of the bidder\nsome text"}
    windows = build_windows(_pages(20, texts))
    # The heading on page 10 is 9 pages from the start: a cut there keeps the window in 6..15.
    assert windows[0].last == 9
    assert windows[1].first == 9  # one page overlap
    for window in windows[:-1]:
        assert 6 <= len(window.pages) <= 15


def test_windows_heading_outside_the_band_is_ignored():
    texts = {2: "Annex 1. Something early"}
    windows = build_windows(_pages(14, texts))
    assert [(w.first, w.last) for w in windows] == [(1, 12), (12, 14)]


def test_windows_keep_gaps_in_page_numbers():
    pages = [(1, "a"), (2, "b"), (5, "c"), (9, "d")]
    windows = build_windows(pages)
    assert [(w.first, w.last) for w in windows] == [(1, 9)]
    assert [n for n, _ in windows[0].pages] == [1, 2, 5, 9]


# ---------- toc and scheme pages ----------


def test_toc_lists_headings_with_pages_and_annexes_first():
    pages = [
        (1, "I. General provisions\nThe supplier shall deliver goods."),
        (4, "Приложение 2. Форма заявки"),
        (9, "ilova 1-ilova. Texnik talablar"),
    ]
    toc = build_toc(pages)
    lines = toc.splitlines()
    assert lines[0] == "стр. 4: Приложение 2. Форма заявки"
    assert any("стр. 1: I. General provisions" == line for line in lines)
    assert all(line.startswith("стр. ") for line in lines)


def test_toc_is_capped():
    pages = [(i, f"Annex {i}. heading") for i in range(1, 200)]
    assert len(build_toc(pages, max_lines=60).splitlines()) == 60


def test_scheme_pages_are_keyword_pages_plus_next():
    pages = [
        (1, "General text."),
        (2, "Scoring: maximum ball 14 points."),
        (3, "Plain text."),
        (5, "Критерий 1 — опыт, балл 4"),
    ]
    assert find_scheme_pages(pages) == [2, 3, 5]


def test_scheme_pages_empty_when_no_keyword():
    assert find_scheme_pages(_pages(4)) == []


def test_scheme_pages_match_cyrillic_and_uzbek_keywords():
    pages = [(1, "O'tish bali: 9"), (2, "Mezon: 3"), (3, "проходной балл 9")]
    assert find_scheme_pages(pages) == [1, 2, 3]


def test_choose_scheme_pages_picks_the_densest_contiguous_block():
    pages = [
        (1, "ball once"),
        (2, "text"),
        (3, "text"),
        (10, "ball ball ball"),
        (11, "балл балл"),
    ]
    assert choose_scheme_pages(pages) == [10, 11]


def test_choose_scheme_pages_caps_the_length():
    pages = [(i, "ball") for i in range(1, 30)]
    assert choose_scheme_pages(pages, cap=8) == list(range(1, 9))


# ---------- merge ----------


def test_merge_unites_passes_and_dedupes_same_quote():
    a = _draft(quote="The warranty period shall be 24 months from delivery.")
    b = _draft(quote="The warranty period shall be 24 months from delivery of goods.")
    merged = merge_drafts({"A": [a], "B": [b]})
    assert len(merged) == 1
    assert merged[0].found_in_passes == 2
    assert merged[0].draft.quote == b.quote  # longer quote is the main draft
    assert merged[0].alternatives[0].quote == a.quote


def test_merge_keeps_requirements_that_differ_only_by_number():
    a = _draft(quote="Warranty period shall be 24 months from delivery.", value="24")
    b = _draft(quote="Warranty period shall be 12 months from delivery.", value="12", page=1)
    merged = merge_drafts({"A": [a], "B": [b]})
    assert len(merged) == 2
    assert [m.found_in_passes for m in merged] == [1, 1]


def test_merge_same_parameter_value_page_is_a_duplicate_even_with_other_wording():
    a = _draft(quote="Warranty period: 24 months, see the contract.", parameter_ru="Гарантия", value="24")
    b = _draft(quote="Гарантийный срок составляет 24 месяца, как указано.", parameter_ru="Гарантия", value="24")
    merged = merge_drafts({"A": [a], "B": [b]})
    assert len(merged) == 1
    assert merged[0].found_in_passes == 2


def test_merge_drops_non_requirements_and_scopes_do_not_mix():
    goods = _draft(scope="goods")
    bidder = _draft(scope="bidder")
    preamble = _draft(scope="goods", is_requirement=False, quote="Preamble text that is long enough to count.")
    merged = merge_drafts({"A": [goods, preamble], "B": [bidder]})
    assert [m.scope for m in merged] == ["goods", "bidder"]


def test_merge_within_one_pass_counts_one_pass():
    a1 = _draft(quote="The warranty period shall be 24 months from delivery.")
    a2 = _draft(quote="The warranty period shall be 24 months from delivery.")  # overlap page
    merged = merge_drafts({"A": [a1, a2]})
    assert len(merged) == 1
    assert merged[0].found_in_passes == 1


def test_merge_similar_items_in_one_pass_stay_separate():
    a1 = _draft(quote="The warranty period shall be 24 months from delivery.")
    a2 = _draft(quote="The warranty period shall be 24 months for spare parts only.")
    merged = merge_drafts({"A": [a1, a2]})
    assert len(merged) == 2
    assert [m.found_in_passes for m in merged] == [1, 1]


def test_merge_aligns_criterion_fragments_across_passes():
    row = dict(
        scope="bidder", category="Scoring", req_type="scored", page=28, operator="text", value=None,
        parameter_ru="Рекомендательные письма заказчиков по ранее выполненным работам",
    )
    a = _draft(quote="mijozlardan ijobiy fikr xatlari", **row)
    b = _draft(quote="5 ta xat mavjud - 3", **row)
    merged = merge_drafts({"A": [a], "B": [b]})
    assert len(merged) == 1
    assert merged[0].found_in_passes == 2
    assert merged[0].draft.quote == a.quote  # the longer quote is the main draft
    assert {c.quote for c in merged[0].candidates} == {a.quote, b.quote}


def test_merge_never_pairs_drafts_whose_numbers_differ():
    a = _draft(parameter_ru="Гарантийный срок", value="12",
               quote="The warranty period shall be 12 months from delivery.")
    b = _draft(parameter_ru="Гарантийный срок", value="24",
               quote="The warranty period shall be 24 months from delivery.")
    merged = merge_drafts({"A": [a], "B": [b]})
    assert len(merged) == 2
    assert [m.found_in_passes for m in merged] == [1, 1]


def test_merge_aligns_each_pass_one_to_one_with_more_than_two_passes():
    a = _draft(parameter_ru="Гарантия", quote="The warranty period shall be 24 months from delivery.")
    b = _draft(parameter_ru="Гарантия", quote="The warranty period shall be 24 months from delivery of goods.")
    c1 = _draft(parameter_ru="Гарантия", quote="Warranty 24 months from delivery.")
    c2 = _draft(parameter_ru="Гарантия", quote="Warranty 24 months from delivery, see annex.")
    merged = merge_drafts({"A": [a], "B": [b], "C": [c1, c2]})
    assert sorted(m.passes for m in merged) == [("A", "B", "C"), ("C",)]
    assert [m.found_in_passes for m in merged if m.passes == ("A", "B", "C")] == [3]


def test_merge_output_is_deterministic_and_ordered_by_page():
    a = [
        _draft(page=3, parameter_ru="Гарантия", quote="Warranty 24 months on page three."),
        _draft(page=1, scope="bidder", parameter_ru="Опыт", operator="=", value="3",
               quote="The bidder shall have 3 years of experience."),
    ]
    b = [_draft(page=1, scope="bidder", parameter_ru="Опыт", operator="=", value="3",
                quote="The bidder shall have 3 years of experience in supply.")]
    first = merge_drafts({"A": a, "B": b})
    second = merge_drafts({"B": b, "A": a})
    summary = [(m.draft.quote, m.passes) for m in first]
    assert summary == [(m.draft.quote, m.passes) for m in second]
    assert [m.page for m in first] == sorted(m.page for m in first)


# ---------- codes ----------


def test_codes_follow_page_order_and_scope():
    items = [
        SimpleNamespace(scope="bidder", page=3),
        SimpleNamespace(scope="goods", page=2),
        SimpleNamespace(scope="goods", page=1),
        SimpleNamespace(scope="bidder", page=1),
    ]
    assert assign_codes(items) == ["U-002", "T-002", "T-001", "U-001"]


def test_codes_skip_taken_codes():
    items = [SimpleNamespace(scope="goods", page=1), SimpleNamespace(scope="goods", page=2)]
    assert assign_codes(items, taken={"T-001"}) == ["T-002", "T-003"]


# ---------- confidence ----------


@pytest.mark.parametrize(
    ("passes", "method", "expected"),
    [
        (2, "exact", 100.0),
        (1, "exact", 85.0),
        (2, "page_corrected", 95.0),
        (1, "fuzzy", 76.5),
    ],
)
def test_confidence_factors(passes, method, expected):
    value, parts = confidence(passes, method)
    assert value == pytest.approx(expected)
    assert parts["quote_method"] == method
    assert parts["found_in_passes"] == passes


def test_confidence_rejects_unknown_method():
    with pytest.raises(ValueError):
        confidence(1, "guessed")


def test_placed_quote_method_precedence():
    assert placed_quote_method("fuzzy_ocr", True) == "fuzzy"
    assert placed_quote_method("exact", True) == "page_corrected"
    assert placed_quote_method("exact", False) == "exact"


# ---------- scheme checks ----------


def _scheme(criteria: list[tuple[int, float]], stated: float | None, threshold: float | None = None) -> SchemeOut:
    return SchemeOut(
        has_scheme=True,
        stated_max=stated,
        pass_threshold=threshold,
        criteria=[
            SchemeCriterion(
                number=n, name=f"c{n}", max_points=m,
                levels=[SchemeLevel(points=m, condition_text="x")], page=1, quote="criterion text",
            )
            for n, m in criteria
        ],
    )


def test_scheme_sum_mismatch_message():
    issues = scheme_checks(_scheme([(1, 4), (2, 2), (3, 3), (4, 1), (5, 4)], stated=15, threshold=11))
    assert issues == ["Сумма максимумов критериев 14 не равна заявленному максимуму 15"]


def test_scheme_messages_follow_the_report_language():
    scheme = _scheme([(1, 4), (2, 2), (3, 3), (4, 1), (5, 4)], stated=15, threshold=11)
    assert scheme_checks(scheme, "en") == ["Criteria maxima sum to 14 but stated maximum is 15"]
    assert scheme_checks(scheme, "ru") == ["Сумма максимумов критериев 14 не равна заявленному максимуму 15"]
    # Anything but "en" falls back to Russian, like the rest of the report.
    assert scheme_checks(scheme, "xx") == scheme_checks(scheme, "ru")


def test_scheme_consistent_has_no_issues():
    assert scheme_checks(_scheme([(1, 4), (2, 3)], stated=7, threshold=4)) == []


def test_scheme_threshold_above_maximum():
    issues = scheme_checks(_scheme([(1, 4)], stated=4, threshold=5))
    assert "Проходной балл 5 выше заявленного максимума 4" in issues
    assert "Pass threshold 5 is above the stated maximum 4" in scheme_checks(
        _scheme([(1, 4)], stated=4, threshold=5), "en",
    )


def test_scheme_without_table_has_no_checks():
    assert scheme_checks(SchemeOut(has_scheme=False)) == []


def test_scheme_level_maximum_mismatch_and_duplicate_numbers():
    scheme = SchemeOut(
        has_scheme=True,
        criteria=[
            SchemeCriterion(number=1, name="a", max_points=3, levels=[SchemeLevel(points=2, condition_text="x")], page=1, quote="q"),
            SchemeCriterion(number=1, name="b", max_points=1, levels=[], page=1, quote="q"),
        ],
    )
    issues = scheme_checks(scheme)
    assert any("максимум 3 баллов, а наивысший уровень даёт 2" in issue for issue in issues)
    assert any("повторяется" in issue for issue in issues)


# ---------- contradiction candidates ----------


def _req(parameter_ru: str, operator: str, value: str | None, unit: str | None, code: str = "T-001"):
    return SimpleNamespace(
        code=code, parameter=parameter_ru, parameter_ru=parameter_ru,
        operator=operator, value=value, value_max=None, unit=unit,
    )


def test_contradiction_groups_same_parameter_different_values():
    items = [
        _req("Гарантийный срок", ">=", "24", "месяцев", "T-001"),
        _req("Гарантийный срок", ">=", "12", "месяцев", "T-002"),
        _req("Срок поставки", ">=", "30", "дней", "T-003"),
    ]
    groups = candidate_contradiction_groups(items)
    assert len(groups) == 1
    assert [g.code for g in groups[0]] == ["T-001", "T-002"]


def test_contradiction_groups_identical_requirements_are_not_a_group():
    items = [_req("Гарантия", ">=", "24", "месяцев"), _req("Гарантия", ">=", "24", "месяцев")]
    assert candidate_contradiction_groups(items) == []


def test_contradiction_groups_similar_names_are_clustered():
    items = [
        _req("Гарантийный срок на товар", ">=", "24", "месяцев", "T-001"),
        _req("Гарантийный срок", "exists", None, None, "T-002"),
    ]
    groups = candidate_contradiction_groups(items)
    assert len(groups) == 1


def test_contradiction_groups_operator_difference_counts():
    items = [_req("Гарантия", ">=", "24", "месяцев", "T-001"), _req("Гарантия", "<=", "24", "месяцев", "T-002")]
    assert len(candidate_contradiction_groups(items)) == 1
