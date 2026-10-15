"""Stage 2: decide whether a page's PDF text layer can be trusted (ARCHITECTURE §4.2).

Pure code, no LLM. `score_text_layer` returns 0..1; pages below 0.7 go to the
vision read (stage 3). `detect_language` labels a page as uz_latn, uz_cyrl, ru,
en, mixed or unknown.

Formula (all ratios are shares of the word tokens of the page):
  Token   = a run of letters and digits; apostrophe variants (' ’ ‘ ʻ ʼ ` ´)
            count as part of a word; only tokens with >= 2 letters are counted.
  valid   = letters of one script (Latin or Cyrillic), a vowel present, no digit,
            and not a Latin look-alike token (below).
  mixed   = letters of both scripts in one token ("OKазаHIte"): a broken font map.
  lookalike = a token that is only Latin look-alike letters of Cyrillic words
            (a c e p o x y k m h t b u n r H K M T B I), when the page is not Latin
            text (English or Uzbek Latin context, see below); or a digit between two
            letters ("OKa3aHIte", 3 stands for З).
  other   = everything else (digits such as "T14", vowel-less tokens such as "www").

  word_ratio, mixed_script_ratio, lookalike_ratio = count / counted tokens, where
            "other" tokens are neutral (left out) unless they are more than 60% of
            all tokens
  replacement_ratio = U+FFFD count / non-space characters

  score = clamp(word_ratio
                - mixed_script_ratio
                - 2 * max(0, lookalike_ratio - 0.3)
                - 3 * replacement_ratio, 0, 1)

  score = 0 when the page has fewer than 40 non-space characters, or no word tokens.
  Latin context (the look-alike check is off): 3 or more English function words, or
  3 or more Uzbek apostrophe words (o', g' and variants), or 2 or more Uzbek Latin
  function words. Then Latin-looking words are real words (Uzbek "ko'p", "o'rta").
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

MIN_CHARS = 40
VISION_THRESHOLD = 0.7
LOOKALIKE_FREE_SHARE = 0.3  # Uzbek Latin has short Latin-looking words ("kerak", "ota")
LOOKALIKE_PENALTY = 2.0
REPLACEMENT_PENALTY = 3.0
MAX_NEUTRAL_SHARE = 0.6  # above this, codes/abbreviations count against the page
ENGLISH_CONTEXT_HITS = 3

VALID = "valid"
MIXED = "mixed"
LOOKALIKE = "lookalike"
OTHER = "other"

LOOKALIKE_LATIN = frozenset("acepoxykmhtbunrHKMTBI")
_CYRILLIC_VOWELS = frozenset("аеёиоуыэюяіїєў")
_LATIN_VOWELS = frozenset("aeiouy")
_ENGLISH_FUNCTION_WORDS = frozenset(
    {"the", "and", "of", "to", "for", "with", "that", "this", "from", "are", "shall", "is", "be"}
)

_APOSTROPHES = "'‘’ʻʼ`´ʹ"
_APOS_CLASS = re.escape(_APOSTROPHES)
_TOKEN_RE = re.compile(rf"[^\W_]+(?:[{_APOS_CLASS}][^\W_]+)*")
_DIGIT_BETWEEN_LETTERS = re.compile(r"[^\W\d_]\d+[^\W\d_]")

# Language markers. Uzbek Cyrillic and Russian share some letters, so those count half.
_RU_WEIGHTS = {
    "и": 0.5, "в": 0.5, "с": 0.5,
    "на": 1.0, "не": 1.0, "что": 1.0, "для": 1.0, "по": 1.0, "от": 1.0,
}
_UZ_CYR_LETTERS = frozenset("ўқғҳ")
_UZ_CYR_WORDS = frozenset({"ва", "учун", "бўйича", "билан", "мавжуд", "ҳамда", "хизмат"})
_UZ_LAT_WORDS = frozenset({
    "va", "uchun", "bo'yicha", "bilan", "mavjud", "hamda", "xizmat",
    "ushbu", "hujjat", "tilida", "yoki", "kerak",
})
_EN_WORDS = _ENGLISH_FUNCTION_WORDS


@dataclass(frozen=True)
class TextLayerScore:
    score: float  # 0..1, see the module docstring
    chars: int  # non-space characters on the page
    word_ratio: float
    mixed_script_ratio: float
    replacement_ratio: float
    reason: str  # "ok" or the problems found
    lookalike_ratio: float = 0.0


def score_text_layer(text: str, min_chars: int = MIN_CHARS) -> TextLayerScore:
    # Vision output passes min_chars=1: a short page read from an image is still real text.
    normalised = unicodedata.normalize("NFKC", text)
    chars = len("".join(normalised.split()))
    replacement_ratio = normalised.count("�") / chars if chars else 0.0

    if chars < min_chars:
        return TextLayerScore(0.0, chars, 0.0, 0.0, replacement_ratio, "too little text", 0.0)

    tokens = [tok for tok in _TOKEN_RE.findall(normalised) if _letter_count(tok) >= 2]
    if not tokens:
        return TextLayerScore(0.0, chars, 0.0, 0.0, replacement_ratio, "no word tokens", 0.0)

    latin_context = _latin_context(normalised, tokens)
    counts = Counter(_classify(tok, latin_context) for tok in tokens)
    # Codes and abbreviations ("шт", "WFP", "2CD2642", "HIKVISION20") are normal in
    # equipment tables, so they are neutral. If they dominate the page it is more
    # likely junk, and they count against it again.
    total = len(tokens)
    if counts[OTHER] / total <= MAX_NEUTRAL_SHARE and counts[OTHER] < total:
        total -= counts[OTHER]
    word_ratio = counts[VALID] / total
    mixed_ratio = counts[MIXED] / total
    lookalike_ratio = counts[LOOKALIKE] / total

    raw = (
        word_ratio
        - mixed_ratio
        - LOOKALIKE_PENALTY * max(0.0, lookalike_ratio - LOOKALIKE_FREE_SHARE)
        - REPLACEMENT_PENALTY * replacement_ratio
    )
    score = min(1.0, max(0.0, raw))

    if score >= VISION_THRESHOLD:
        reason = "ok"
    else:
        problems = []
        if mixed_ratio > 0.05:
            problems.append("mixed Latin/Cyrillic words")
        if lookalike_ratio > LOOKALIKE_FREE_SHARE:
            problems.append("Latin look-alike garble")
        if replacement_ratio > 0.01:
            problems.append("replacement characters")
        reason = "; ".join(problems) or "few real words"

    return TextLayerScore(
        score=round(score, 3),
        chars=chars,
        word_ratio=round(word_ratio, 3),
        mixed_script_ratio=round(mixed_ratio, 3),
        replacement_ratio=round(replacement_ratio, 4),
        reason=reason,
        lookalike_ratio=round(lookalike_ratio, 3),
    )


def needs_vision(score: float) -> bool:
    return score < VISION_THRESHOLD


def detect_language(text: str) -> str:
    """Return uz_latn, uz_cyrl, ru, en, mixed or unknown (per page)."""
    norm = unicodedata.normalize("NFKC", text).lower()
    norm = re.sub(f"[{_APOS_CLASS}]", "'", norm)
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)*", norm)

    uz_cyrl_letters = sum(1 for ch in norm if ch in _UZ_CYR_LETTERS)
    scores = {
        "ru": sum(_RU_WEIGHTS.get(w, 0.0) for w in words),
        "uz_cyrl": uz_cyrl_letters + sum(1 for w in words if w in _UZ_CYR_WORDS),
        "uz_latn": len(re.findall(r"[og]'", norm)) + sum(1 for w in words if w in _UZ_LAT_WORDS),
        "en": sum(1 for w in words if w in _EN_WORDS),
    }
    total = sum(scores.values())
    if total == 0:
        cyrillic = sum(1 for ch in norm if _script(ch) == "cyrillic")
        return "ru" if cyrillic >= 10 and uz_cyrl_letters == 0 else "unknown"
    if total < 2:
        return "unknown"

    shares = {lang: value / total for lang, value in scores.items() if value > 0}
    if sum(1 for share in shares.values() if share > 0.3) >= 2:
        return "mixed"
    return max(shares, key=shares.__getitem__)


# ---------- helpers ----------


def _script(ch: str) -> str | None:
    if ch in _APOSTROPHES or not ch.isalpha():
        return None
    if ch.isascii():
        return "latin"
    cp = ord(ch)
    if (0xC0 <= cp <= 0x24F and cp not in (0xD7, 0xF7)) or 0x1E00 <= cp <= 0x1EFF:
        return "latin"
    if 0x400 <= cp <= 0x52F:
        return "cyrillic"
    return "other"


def _letter_count(token: str) -> int:
    return sum(1 for ch in token if _script(ch) is not None)


def _has_vowel(token: str) -> bool:
    return any(ch in _LATIN_VOWELS or ch in _CYRILLIC_VOWELS for ch in token.lower())


def _plain(token: str) -> str:
    """Lowercase, with every apostrophe variant written as a plain '."""
    return re.sub(f"[{_APOS_CLASS}]", "'", token.lower())


def _english_hits(tokens: list[str]) -> int:
    return sum(1 for tok in tokens if _plain(tok) in _ENGLISH_FUNCTION_WORDS)


def _latin_context(text: str, tokens: list[str]) -> bool:
    """True when Latin-looking words are real words (English or Uzbek Latin)."""
    apostrophe_words = len(re.findall(rf"[og][{_APOS_CLASS}][^\W\d_]", text.lower()))
    uz_function_words = sum(1 for tok in tokens if _plain(tok) in _UZ_LAT_WORDS)
    return (
        _english_hits(tokens) >= ENGLISH_CONTEXT_HITS
        or apostrophe_words >= 3
        or uz_function_words >= 2
    )


def _classify(token: str, latin_context: bool) -> str:
    scripts = {_script(ch) for ch in token} - {None}
    if {"latin", "cyrillic"} <= scripts:
        return MIXED
    if _DIGIT_BETWEEN_LETTERS.search(token):
        return LOOKALIKE
    if any(ch.isdigit() for ch in token):
        return OTHER
    if not _has_vowel(token):
        return OTHER
    if scripts == {"latin"} and not latin_context and all(
        ch in LOOKALIKE_LATIN for ch in token if _script(ch) == "latin"
    ):
        return LOOKALIKE
    return VALID
