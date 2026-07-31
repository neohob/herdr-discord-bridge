"""Tests for approval-prompt detection."""

from __future__ import annotations

from src.bot.choice_detect import choice_fingerprint, detect_approval_prompt, is_blocked_status


def test_detect_do_you_want_to_proceed():
    text = "Running tool\nDo you want to proceed?\n> "
    assert detect_approval_prompt(text) is not None


def test_detect_yn():
    assert detect_approval_prompt("Overwrite file? (y/n)") is not None


def test_no_false_positive_on_normal_output():
    assert detect_approval_prompt("compiled successfully\nidle") is None


def test_fingerprint_stable_across_revisions():
    text = "Allow this action? (y/n)"
    a = choice_fingerprint(status="working", text=text, revision=1)
    b = choice_fingerprint(status="working", text=text, revision=99)
    assert a is not None and a == b


def test_blocked_status_fingerprint():
    assert is_blocked_status("blocked")
    assert choice_fingerprint(status="blocked", text="", revision=1) is not None
