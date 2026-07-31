"""ANSI escape sequence stripping for terminal text."""

from __future__ import annotations

import re

_CSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Remove CSI ANSI escape sequences from *text*."""
    return _CSI_PATTERN.sub("", text)
