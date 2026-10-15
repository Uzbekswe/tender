"""Uzbek (Latin) as a third language: the UI strings match the Russian key set, the report language
"uz" is accepted by the API schemas, and the language switch knows it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import ProjectCreate, ProjectUpdate
from app.web.i18n import LANGS, pick_lang, translator

I18N = Path(__file__).resolve().parents[2] / "i18n"
PLACEHOLDER = re.compile(r"\{[^{}]*\}")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _strings(lang: str) -> dict[str, str]:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_uzbek_has_exactly_the_russian_keys() -> None:
    ru, uz = _strings("ru"), _strings("uz")
    assert set(uz) == set(ru)
    assert set(_strings("en")) == set(ru)


@pytest.mark.parametrize("key", sorted(_strings("ru")))
def test_uzbek_keeps_the_placeholders_of_each_key(key: str) -> None:
    ru, uz = _strings("ru"), _strings("uz")
    assert sorted(PLACEHOLDER.findall(uz[key])) == sorted(PLACEHOLDER.findall(ru[key]))


def test_uzbek_values_are_filled_and_written_in_latin() -> None:
    for key, text in _strings("uz").items():
        assert text.strip(), key
        assert not CYRILLIC.search(text), key


def test_uzbek_uses_the_official_apostrophes() -> None:
    # oʻ and gʻ take the modifier letter turned comma (U+02BB), the tutuq belgisi the modifier letter
    # apostrophe (U+02BC). No ASCII or typographic apostrophe is used in the strings.
    text = " ".join(_strings("uz").values())
    assert "ʻ" in text and "ʼ" in text
    assert "'" not in text and "’" not in text and "‘" not in text


def test_uzbek_keeps_the_product_name_the_code_and_stir() -> None:
    uz = _strings("uz")
    assert uz["app.name"] == "Tender-Sverka"
    assert uz["customer.bidders.stir_label"] == "STIR"
    assert uz["customer.bidders.stir"] == "STIR, ixtiyoriy"


def test_uzbek_is_a_ui_language_and_the_switch_picks_it() -> None:
    assert "uz" in LANGS
    assert pick_lang("uz") == "uz"
    assert pick_lang("de") == "ru"  # unknown codes still fall back to the default
    assert translator("uz")("nav.projects") == "Loyihalar"


def test_uzbek_terms_follow_the_procurement_glossary() -> None:
    uz = _strings("uz")
    assert uz["checklist.title"] == "Talablar nazorat roʻyxati"
    assert uz["status.meets"] == "Mos keladi"
    assert uz["status.does_not_meet"] == "Mos kelmaydi"
    assert uz["status.needs_review"] == "Tekshirish kerak"
    assert uz["committee.sentence.needs_review"] == "Komissiya bahosini talab qiladi: {parameter}."
    assert uz["checklist.scheme.pass"] == "Oʻtish bali"
    assert uz["projects.col.deadline"] == "Taklif topshirish muddati"


def test_report_language_uz_is_accepted_by_the_schemas() -> None:
    assert ProjectCreate(name="Lot", report_lang="uz").report_lang == "uz"
    assert ProjectUpdate(report_lang="uz").report_lang == "uz"
    with pytest.raises(ValidationError):
        ProjectCreate(name="Lot", report_lang="de")
    with pytest.raises(ValidationError):
        ProjectUpdate(report_lang="de")
