"""Catch a file uploaded into the wrong box before any model call is spent.

A bid placed in the tender box makes the checklist out of the bidder's own promises; a tender
placed in the bid box checks the tender against itself. Both happen easily with long Russian or
Uzbek file names. This check is code only: the file name first, then the title area of the first
page. It answers "tender", "bid" or None (cannot tell), and never blocks on its own; the screen
asks the user to confirm.
"""

from __future__ import annotations

import re
from pathlib import Path

TITLE_CHARS = 600  # the title area of page 1; deeper text mentions both sides (a tender explains the offer)

_BID_WORDS = re.compile(
    r"тендерн\w* предложени|коммерческ\w* предложени|ценов\w* предложени|техническ\w* предложени"
    r"|конкурсн\w* предложени|предложени\w* участник|заявк\w* на участие|оферт"
    r"|tender taklif|tijorat taklif|narx taklif|texnik taklif|taklifnoma|ishtirok etish uchun ariza"
    r"|\bbid\b|\boffer\b|\bproposal\b",
)
_TENDER_WORDS = re.compile(
    r"тендерн\w* документаци|конкурсн\w* документаци|закупочн\w* документаци|документаци\w* о закупк"
    r"|техническ\w* задани|приглашени\w* к участию|извещени\w* о проведении"
    r"|tender hujjat|xarid hujjat|tanlov hujjat|texnik topshiriq"
    r"|tender documentation|bidding documents|terms of reference|invitation to bid",
)


def _norm(text: str) -> str:
    return re.sub(r"[_\-.\s]+", " ", (text or "").lower().replace("ё", "е"))


def _vote(text: str) -> str | None:
    text = _norm(text)
    bid, tender = len(_BID_WORDS.findall(text)), len(_TENDER_WORDS.findall(text))
    if bid > tender:
        return "bid"
    if tender > bid:
        return "tender"
    return None


def guess_role(file_name: str, first_page_text: str = "") -> str | None:
    """"tender", "bid" or None. The file name decides when it is clear; otherwise the page title."""
    return _vote(Path(file_name).stem) or _vote((first_page_text or "")[:TITLE_CHARS])


def first_page_text(path: Path) -> str:
    """Text layer of page 1 of a PDF; empty for scans, other formats or unreadable files."""
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc[0].get_text() if doc.page_count else ""
    except Exception:  # a bad or non-PDF file simply gives no hint
        return ""


def misplaced(file_name: str, path: Path | None, box: str) -> bool:
    """True when the file looks like it belongs in the other box ("tender" or "bid")."""
    guess = guess_role(file_name, first_page_text(path) if path is not None else "")
    return guess is not None and guess != box
