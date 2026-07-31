"""Detect real interactive approval prompts — avoid false Yes/No spam."""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Only patterns that look like an interactive prompt, not prose mentioning
# "approve" / "permission" in agent chatter.
_APPROVAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in (
        r"do you want to proceed\b",
        r"do you want to continue\b",
        r"allow this (action|edit|change|tool|command)\b",
        r"\(y/n\)\s*$",
        r"\[y/n\]\s*$",
        r"\(yes/no\)\s*$",
        r"\[yes/no\]\s*$",
        r"press enter to (continue|confirm)\b",
        r"^\s*(continue|proceed|confirm)\?\s*\[?y/n\]?\s*$",
        r"❯\s*1\.\s*yes\b",  # Claude Code numbered choice
        r"❯\s*yes\b",
    )
)

# Herdr "waiting" / "needs_input" fire constantly and are NOT Yes/No prompts.
_BLOCKED_STATUSES = frozenset({"blocked"})


def is_blocked_status(status: str) -> bool:
    return str(status or "").strip().lower() in _BLOCKED_STATUSES


def detect_approval_prompt(text: str, *, lookback_lines: int = 8) -> str | None:
    """Return a fingerprint snippet only for a likely interactive prompt at the tip."""
    lines = [ln.rstrip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    # Only the very end of the viewport — older "approve" words must not retrigger.
    window = lines[-lookback_lines:]
    blob = "\n".join(window)
    for pattern in _APPROVAL_PATTERNS:
        match = pattern.search(blob)
        if match:
            start = max(0, match.start() - 20)
            end = min(len(blob), match.end() + 20)
            return blob[start:end]
    return None


def choice_fingerprint(*, status: str, text: str, revision: Any = None) -> str | None:
    """Stable fingerprint for de-duplicating choice UI posts."""
    del revision
    status_key = str(status or "").strip().lower()
    if is_blocked_status(status_key):
        seed = f"status:{status_key}"
        return hashlib.sha256(seed.encode()).hexdigest()[:24]
    snippet = detect_approval_prompt(text)
    if snippet:
        seed = f"text:{snippet}"
        return hashlib.sha256(seed.encode()).hexdigest()[:24]
    return None
