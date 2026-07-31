"""Detect CLI / agent approval prompts from terminal text or status."""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Common approval / confirmation prompts in agent CLIs.
_APPROVAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"do you want to proceed",
        r"do you want to continue",
        r"allow this\b",
        r"approve\b",
        r"permission.*(required|needed|to)",
        r"\(y/n\)",
        r"\[y/n\]",
        r"\(yes/no\)",
        r"\[yes/no\]",
        r"press enter to continue",
        r"are you sure\b",
        r"confirm\b.*(y/n|yes/no|\?)",
    )
)

_BLOCKED_STATUSES = frozenset({"blocked", "waiting", "needs_input", "need_input"})


def is_blocked_status(status: str) -> bool:
    return str(status or "").strip().lower() in _BLOCKED_STATUSES


def detect_approval_prompt(text: str, *, lookback_lines: int = 20) -> str | None:
    """Return a fingerprint snippet if *text* looks like an approval prompt."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    window = lines[-lookback_lines:]
    blob = "\n".join(window)
    for pattern in _APPROVAL_PATTERNS:
        match = pattern.search(blob)
        if match:
            start = max(0, match.start() - 40)
            end = min(len(blob), match.end() + 40)
            return blob[start:end]
    return None


def choice_fingerprint(*, status: str, text: str, revision: Any = None) -> str | None:
    """Build a stable fingerprint for de-duplicating choice UI posts.

    Revision is ignored so continuous terminal refreshes of the same prompt
    do not re-post buttons.
    """
    del revision  # reserved for future use; must not bust de-dupe
    status_key = str(status or "").strip().lower()
    if is_blocked_status(status_key):
        seed = f"status:{status_key}"
        return hashlib.sha256(seed.encode()).hexdigest()[:24]
    snippet = detect_approval_prompt(text)
    if snippet:
        seed = f"text:{snippet}"
        return hashlib.sha256(seed.encode()).hexdigest()[:24]
    return None
