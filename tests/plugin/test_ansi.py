from __future__ import annotations

from src.plugin.gateway.ansi import strip_ansi


def test_strip_ansi_removes_csi():
    assert "hi" in strip_ansi("\x1b[31mhi\x1b[0m")
    assert "\x1b" not in strip_ansi("\x1b[31mhi\x1b[0m")


def test_strip_ansi_plain_text_unchanged():
    assert strip_ansi("hello world") == "hello world"


def test_strip_ansi_empty():
    assert strip_ansi("") == ""
