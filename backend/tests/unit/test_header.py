"""The header: one language button with a menu of the three languages, and the theme button.

The theme is a cookie (`theme`: light or dark, absent means follow the system). The server puts it on
<html data-theme="..."> so the first paint already has the right colours."""

import re

import pytest
from fastapi.testclient import TestClient

LANG_LABELS = {"ru": "Русский", "uz": "Oʻzbekcha", "en": "English"}
THEME_LABELS = {
    "ru": "Сменить тему оформления",
    "uz": "Mavzuni almashtirish",
    "en": "Switch colour theme",
}
MENU = re.compile(r'<details class="lang-menu">(.*?)</details>', re.S)


def _page(client: TestClient, lang: str, theme: str | None = None) -> str:
    client.cookies.set("lang", lang)
    if theme is None:
        client.cookies.delete("theme")
    else:
        client.cookies.set("theme", theme)
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("lang", ["ru", "uz", "en"])
def test_one_language_button_shows_the_current_language_and_the_menu_lists_all_three(
    client: TestClient, lang: str
) -> None:
    text = _page(client, lang)
    assert text.count('class="lang-menu"') == 1
    menu = MENU.search(text)
    assert menu is not None
    button, items = menu.group(1).split('<ul class="lang-list">', 1)

    assert f'<span class="lang-code">{lang.upper()}</span>' in button
    for code, name in LANG_LABELS.items():
        assert f'href="/lang/{code}?next=/"' in items
        assert f'hreflang="{code}"' in items
        assert name in items
    # exactly one item is the current language, and it is marked
    assert items.count('aria-current="true"') == 1
    current = re.search(r'<a href="/lang/(\w+)[^"]*"[^>]*aria-current="true"', items)
    assert current is not None and current.group(1) == lang


def test_the_old_ru_uz_en_links_are_gone(client: TestClient) -> None:
    text = _page(client, "ru")
    assert 'hreflang="uz">UZ<' not in text
    assert ">EN</a>" not in text


@pytest.mark.parametrize(
    ("theme", "expected"),
    [("dark", 'data-theme="dark"'), ("light", 'data-theme="light"')],
)
def test_theme_cookie_sets_data_theme_on_html(client: TestClient, theme: str, expected: str) -> None:
    text = _page(client, "ru", theme=theme)
    assert re.search(r'<html lang="ru" ' + re.escape(expected) + r'>', text)


def test_no_theme_cookie_renders_no_data_theme_so_the_system_decides(client: TestClient) -> None:
    text = _page(client, "ru")
    assert '<html lang="ru">' in text
    assert "data-theme" not in text.split("<head>", 1)[0]


def test_an_unknown_theme_value_is_ignored(client: TestClient) -> None:
    text = _page(client, "ru", theme="blue")
    assert '<html lang="ru">' in text


@pytest.mark.parametrize("lang", ["ru", "uz", "en"])
def test_theme_button_has_a_translated_label(client: TestClient, lang: str) -> None:
    text = _page(client, lang)
    label = THEME_LABELS[lang]
    assert (
        f'<button type="button" id="theme-toggle" class="theme-toggle" '
        f'aria-label="{label}" title="{label}">'
    ) in text
    # the script that flips the theme writes the cookie the server reads
    assert "document.cookie = 'theme=' + next" in text
    assert "max-age=31536000; SameSite=Lax" in text
