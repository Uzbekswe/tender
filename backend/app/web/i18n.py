"""UI strings for the web pages. Russian is the default; Uzbek (Latin) and English are the other two.

Strings live in backend/i18n/ru.json, uz.json and en.json (flat keys). A missing key in the
chosen language falls back to Russian, then to the key itself.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

I18N_DIR = Path(__file__).resolve().parents[2] / "i18n"
LANGS = ("ru", "uz", "en")
DEFAULT_LANG = "ru"
LANG_COOKIE = "lang"


@lru_cache(maxsize=None)
def _load(lang: str) -> dict[str, str]:
    with open(I18N_DIR / f"{lang}.json", encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.items()}


def pick_lang(raw: str | None) -> str:
    return raw if raw in LANGS else DEFAULT_LANG


class Translator:
    def __init__(self, lang: str) -> None:
        self.lang = lang
        self._strings = _load(lang)
        self._fallback = _load(DEFAULT_LANG)

    def __call__(self, key: str, **kwargs: Any) -> str:
        text = self._strings.get(key) or self._fallback.get(key) or key
        return text.format(**kwargs) if kwargs else text

    def get(self, key: str, default: str) -> str:
        """Translation if the key exists in this language (or the fallback), else the default."""
        return self._strings.get(key) or self._fallback.get(key) or default


@lru_cache(maxsize=None)
def translator(lang: str) -> Translator:
    return Translator(pick_lang(lang))
