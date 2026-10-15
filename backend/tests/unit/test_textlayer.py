"""Unit tests for engine.textlayer (stage 2). All samples are synthetic strings."""

from engine.textlayer import detect_language, needs_vision, score_text_layer

RU_SENTENCE = (
    "В соответствии с требованиями тендерной документации, участник на основании "
    "договора поставляет оборудование для объекта, не позднее указанного срока."
)
RU_WITH_LATIN_MODEL = (
    "Ноутбук Lenovo ThinkPad T14 Gen 3 соответствует требованиям технической спецификации."
)
UZ_LATIN_APOSTROPHES = (
    "O‘zbekiston Respublikasi hududida o'tkazilgan tender hujjatlari g‘ozi bo'yicha ko'rib chiqiladi."
)
UZ_LATIN_VARIANTS = "oʻzbek gʻozi koʻp oʻrta ko`p g‘ozi o’rta tilida ota-ona"
UZ_CYRILLIC = (
    "Тендер ҳужжатлари ўзбек тилида ва рус тилида тайёрланган ҳамда ўз вақтида "
    "топширилиши керак."
)
EN_SENTENCE = (
    "The bidder shall submit all required documents for the tender before the deadline "
    "stated in the notice."
)
# Cyrillic words rendered with Latin look-alike glyphs (broken font mapping).
GARBLED_LOOKALIKE = "ckaHep oTnpaBka Ha 3aKa3 " * 4  # invented: Russian words in Latin look-alike letters


def test_russian_page_scores_high() -> None:
    result = score_text_layer(RU_SENTENCE)
    assert result.score >= 0.85
    assert result.reason == "ok"
    assert not needs_vision(result.score)
    assert detect_language(RU_SENTENCE) == "ru"


def test_russian_page_with_latin_model_name_scores_high() -> None:
    result = score_text_layer(RU_WITH_LATIN_MODEL)
    assert result.score >= 0.85
    assert detect_language(RU_WITH_LATIN_MODEL) == "ru"


def test_uzbek_latin_with_apostrophe_variants_scores_high() -> None:
    assert score_text_layer(UZ_LATIN_APOSTROPHES).score >= 0.85
    result = score_text_layer(UZ_LATIN_VARIANTS)
    assert result.score >= 0.85
    assert result.lookalike_ratio == 0.0  # "ko'p" and "o'rta" are Uzbek words, not garble
    assert detect_language(UZ_LATIN_APOSTROPHES) == "uz_latn"
    assert detect_language(UZ_LATIN_VARIANTS) == "uz_latn"


def test_uzbek_cyrillic_scores_high() -> None:
    assert score_text_layer(UZ_CYRILLIC).score >= 0.85
    assert detect_language(UZ_CYRILLIC) == "uz_cyrl"


def test_english_scores_high_and_is_english() -> None:
    assert score_text_layer(EN_SENTENCE).score >= 0.85
    assert detect_language(EN_SENTENCE) == "en"


def test_english_with_only_lookalike_letters_is_not_garble() -> None:
    # Every word uses only look-alike letters, but English function words make it real.
    text = "The new one is not that the one, the tent is ten of the one"
    result = score_text_layer(text)
    assert result.lookalike_ratio == 0.0
    assert result.score >= 0.85


def test_lookalike_garble_scores_below_half() -> None:
    result = score_text_layer(GARBLED_LOOKALIKE)
    assert result.score < 0.5
    assert result.lookalike_ratio > 0.5
    assert needs_vision(result.score)
    assert result.reason == "Latin look-alike garble"


def test_digit_inside_letters_counts_as_garble() -> None:
    result = score_text_layer("Тендер OKa3aHIte ОКАЗАНИЕ УСЛУГ ПО ДОСТАВКЕ ТОВАРОВ " * 2)
    assert result.lookalike_ratio > 0.0


def test_mixed_script_words_are_penalised() -> None:
    text = "Тендерная документация OKазаHIte соответствует требованиям"
    result = score_text_layer(text)
    assert result.mixed_script_ratio > 0.0
    assert result.score < 0.7
    assert needs_vision(result.score)


def test_replacement_characters_are_penalised() -> None:
    text = "Тендерная документация " * 3 + "�" * 30
    result = score_text_layer(text)
    assert result.replacement_ratio > 0.3
    assert result.score < 0.7
    assert needs_vision(result.score)


def test_empty_page_needs_vision() -> None:
    result = score_text_layer("")
    assert result.score == 0.0
    assert result.chars == 0
    assert needs_vision(result.score)


def test_too_little_text_scores_zero() -> None:
    result = score_text_layer("Тендер на поставку")  # under 40 non-space characters
    assert result.score == 0.0
    assert result.reason == "too little text"


def test_numbers_only_page_scores_zero() -> None:
    numbers = "12 500 000 45 678 90 12 500 000 45 678 90 12 500 000 " * 2
    result = score_text_layer(numbers)
    assert result.score == 0.0
    assert result.reason == "no word tokens"


def test_needs_vision_threshold() -> None:
    assert needs_vision(0.69)
    assert not needs_vision(0.7)


def test_detect_language_unknown_for_empty_and_numbers() -> None:
    assert detect_language("") == "unknown"
    assert detect_language("12345 67890") == "unknown"


def test_detect_language_mixed_when_two_languages_are_substantial() -> None:
    assert detect_language("Документы в с на: the offer and the list of bidders") == "mixed"


def test_detect_language_cyrillic_without_stopwords_is_russian() -> None:
    assert detect_language("Лот номер двадцать шесть") == "ru"


def test_equipment_table_with_codes_and_units_is_good_text() -> None:
    # Russian equipment lists are mostly units, inventory numbers and model codes.
    rows = "\n".join(
        f"{i}  Видео камера наружного наблюдения HIKVISION{i}  DS-2CD2642  шт  1  2212223002{i:04d}  14.07.2015"
        for i in range(20, 40)
    )
    result = score_text_layer("№  Наименование работ  Ед. изм  Кол-во  Инв. №\n" + rows)
    assert result.score >= 0.7, result
