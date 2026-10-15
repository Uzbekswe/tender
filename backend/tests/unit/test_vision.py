"""Stage 3 vision read with a fake LLM (no network). Async code runs via asyncio.run."""

import asyncio

import pytest

from engine import vision
from engine.vision import read_page
from llm.client import CallMeta, LLMError
from llm.schemas import PageRead

GOOD = "Сертификат соответствия выдан обществу с ограниченной ответственностью для поставки оборудования."
GARBLE = "zxcv bnmq wrtp sdfg hjkl zxcv bnmq wrtp sdfg hjkl zxcv bnmq wrtp"


class FakeLLM:
    """Replies are popped in order: a PageRead, or an exception to raise."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def complete(self, task, prompt, variables, schema, images=None, tier=None):
        self.calls.append({"task": task, "prompt": prompt, "variables": variables,
                           "schema": schema, "images": images})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        meta = CallMeta(task=task, model_requested="m", model_answered="m",
                        prompt_version=f"{prompt}@v1", cache_hit=False)
        return reply, meta


def _render_log():
    renders: list[int] = []

    def render(rotation: int) -> bytes:
        renders.append(rotation)
        return f"png{rotation}".encode()

    return renders, render


def test_rotation_reread_happens_once_and_is_kept_when_better():
    llm = FakeLLM(PageRead(text=GARBLE, rotation=90), PageRead(text=GOOD, rotation=0))
    renders, render = _render_log()

    res = asyncio.run(read_page(llm, "DOC-07 p.41", render))

    assert renders == [0, 90]
    assert len(llm.calls) == 2
    assert llm.calls[0]["images"][0].png_bytes == b"png0"
    assert llm.calls[1]["images"][0].png_bytes == b"png90"
    assert llm.calls[0]["variables"] == {"label": "DOC-07 p.41"}
    assert res.text == GOOD
    assert res.rotation == 90
    assert res.unreadable is False
    assert len(res.metas) == 2


def test_rotation_reread_is_never_repeated():
    llm = FakeLLM(PageRead(text=GARBLE, rotation=90), PageRead(text=GARBLE, rotation=180))
    renders, render = _render_log()

    res = asyncio.run(read_page(llm, "L", render))

    assert len(llm.calls) == 2
    assert renders == [0, 90]
    assert res.rotation == 0  # both reads are equally poor, so the first is kept
    assert any("rotation still reported as 180" in w for w in res.warnings)


def test_worse_reread_is_discarded():
    llm = FakeLLM(PageRead(text=GOOD, rotation=90), PageRead(text=GARBLE, rotation=0))
    renders, render = _render_log()

    res = asyncio.run(read_page(llm, "L", render))

    assert len(llm.calls) == 2
    assert res.text == GOOD
    assert res.rotation == 0
    assert res.read_quality == pytest.approx(1.0)


def test_llm_failure_twice_is_unreadable_and_does_not_raise():
    llm = FakeLLM(LLMError("provider down"), LLMError("still down"))
    renders, render = _render_log()

    res = asyncio.run(read_page(llm, "L", render))

    assert res.unreadable is True
    assert res.read_quality == 0.0
    assert res.text == ""
    assert res.rotation == 0
    assert len(llm.calls) == 2
    assert renders == [0]  # rendered once, the PNG is reused on the retry
    assert any("still down" in w for w in res.warnings)
    assert res.metas == []


def test_single_llm_failure_is_retried_once():
    llm = FakeLLM(LLMError("timeout"), PageRead(text=GOOD))

    res = asyncio.run(read_page(llm, "L", _render_log()[1]))

    assert len(llm.calls) == 2
    assert res.unreadable is False
    assert res.text == GOOD
    assert any("timeout" in w for w in res.warnings)


def test_failed_rotated_reread_keeps_first_read():
    llm = FakeLLM(PageRead(text=GOOD, rotation=90), LLMError("nope"), LLMError("nope"))

    res = asyncio.run(read_page(llm, "L", _render_log()[1]))

    assert res.unreadable is False
    assert res.text == GOOD
    assert res.rotation == 0
    assert len(llm.calls) == 3


def test_blank_page_is_not_unreadable_and_scores_one():
    llm = FakeLLM(PageRead(text="", illegible_fragments=0))

    res = asyncio.run(read_page(llm, "L", _render_log()[1]))

    assert res.unreadable is False
    assert res.read_quality == 1.0
    assert "blank page" in res.warnings


def test_empty_text_with_illegible_fragments_is_unreadable():
    llm = FakeLLM(PageRead(text="  ", illegible_fragments=3))

    res = asyncio.run(read_page(llm, "L", _render_log()[1]))

    assert res.unreadable is True
    assert res.read_quality == 0.0


def test_illegible_fragments_lower_quality_by_0_05_each():
    llm = FakeLLM(PageRead(text=GOOD, illegible_fragments=2))

    res = asyncio.run(read_page(llm, "L", _render_log()[1]))

    assert res.unreadable is False
    assert res.read_quality == pytest.approx(0.9)


def test_illegible_penalty_is_capped_at_0_3():
    llm = FakeLLM(PageRead(text=GOOD, illegible_fragments=10))

    res = asyncio.run(read_page(llm, "L", _render_log()[1]))

    assert res.read_quality == pytest.approx(0.7)


def test_text_layer_agreement_blends_in(monkeypatch):
    pytest.importorskip("rapidfuzz")
    llm = FakeLLM(PageRead(text=GOOD, illegible_fragments=2))

    res = asyncio.run(read_page(llm, "L", _render_log()[1], text_layer=GOOD))

    assert res.read_quality == pytest.approx(0.7 * 0.9 + 0.3 * 1.0)


def test_no_agreement_blend_without_rapidfuzz(monkeypatch):
    monkeypatch.setattr(vision, "_agreement", lambda text, layer: None)
    llm = FakeLLM(PageRead(text=GOOD, illegible_fragments=2))

    res = asyncio.run(read_page(llm, "L", _render_log()[1], text_layer=GOOD))

    assert res.read_quality == pytest.approx(0.9)


def test_text_layer_ignored_when_it_is_garbage(monkeypatch):
    calls: list[tuple] = []

    def spy(text, layer):
        calls.append((text, layer))
        return 0.0

    monkeypatch.setattr(vision, "_agreement", spy)
    llm = FakeLLM(PageRead(text=GOOD))

    res = asyncio.run(read_page(llm, "L", _render_log()[1], text_layer=GARBLE))

    assert calls == []  # text layer score is below 0.5, so no blend
    assert res.read_quality == pytest.approx(1.0)


def test_stamp_signature_qr_flags_come_from_the_kept_read():
    llm = FakeLLM(PageRead(text=GARBLE, rotation=90, has_stamp="no"),
                  PageRead(text=GOOD, rotation=0, has_stamp="yes", has_signature="yes", has_qr="no"))

    res = asyncio.run(read_page(llm, "L", _render_log()[1]))

    assert (res.has_stamp, res.has_signature, res.has_qr) == ("yes", "yes", "no")
