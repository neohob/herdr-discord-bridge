"""Tests for tight approval-prompt detection."""

from __future__ import annotations

from src.bot.choice_detect import choice_fingerprint, detect_approval_prompt, is_blocked_status


def test_detect_real_proceed_prompt():
    text = "Running tool X\nDo you want to proceed?\n"
    assert detect_approval_prompt(text) is not None


def test_detect_yn_at_eol():
    assert detect_approval_prompt("Overwrite file? (y/n)") is not None


def test_no_false_positive_on_approve_prose():
    text = "I will approve the design and continue coding.\npermission to edit is granted in the repo."
    assert detect_approval_prompt(text) is None


def test_no_false_positive_on_are_you_sure_in_docs():
    assert detect_approval_prompt("Docs say: are you sure about this approach?\nnext line") is None


def test_waiting_status_is_not_blocked():
    assert not is_blocked_status("waiting")
    assert not is_blocked_status("needs_input")
    assert is_blocked_status("blocked")
    assert choice_fingerprint(status="waiting", text="hello") is None


def test_old_approve_word_deeper_in_scrollback_ignored():
    lines = ["approve this PR someday"] + [f"line {i}" for i in range(20)]
    assert detect_approval_prompt("\n".join(lines)) is None
