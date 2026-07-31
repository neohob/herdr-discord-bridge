"""Lightweight views of herdr API objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


def _cwd_basename(cwd: str) -> str:
    text = (cwd or "").strip().rstrip("/\\")
    if not text:
        return ""
    if "\\" in text and not text.startswith("/"):
        return PureWindowsPath(text).name
    return PurePosixPath(text).name


def _pane_display_label(data: dict[str, Any], pane_id: str) -> str:
    """Prefer human titles from Herdr over raw pane ids."""
    for key in (
        "label",
        "title",
        "terminal_title_stripped",
        "terminal_title",
        "agent",
    ):
        value = str(data.get(key) or "").strip()
        if value and value != pane_id:
            # Prefer last path segment when title is "user@host:~/long/path".
            if ":~/" in value or ":/" in value:
                tail = value.split(":")[-1].strip()
                base = _cwd_basename(tail.replace("~", ""))
                if base:
                    return base
            return value
    cwd = str(data.get("foreground_cwd") or data.get("cwd") or "").strip()
    base = _cwd_basename(cwd)
    if base:
        return base
    return pane_id


@dataclass(slots=True)
class PaneInfo:
    pane_id: str
    workspace_id: str
    tab_id: str = ""
    label: str = ""
    agent: str = ""
    agent_status: str = "unknown"
    cwd: str = ""
    focused: bool = False
    revision: int = 0
    workspace_label: str = ""
    tab_label: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaneInfo:
        pane_id = str(data.get("pane_id") or data.get("id") or "")
        return cls(
            pane_id=pane_id,
            workspace_id=str(data.get("workspace_id") or ""),
            tab_id=str(data.get("tab_id") or ""),
            label=_pane_display_label(data, pane_id),
            agent=str(data.get("agent") or ""),
            agent_status=str(data.get("agent_status") or "unknown"),
            cwd=str(data.get("foreground_cwd") or data.get("cwd") or ""),
            focused=bool(data.get("focused", False)),
            revision=int(data.get("revision") or 0),
            workspace_label=str(data.get("workspace_label") or ""),
            tab_label=str(data.get("tab_label") or ""),
        )


@dataclass(slots=True)
class WorkspaceInfo:
    workspace_id: str
    label: str = ""
    number: int = 0
    agent_status: str = "unknown"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceInfo:
        return cls(
            workspace_id=str(data.get("workspace_id") or data.get("id") or ""),
            label=str(data.get("label") or ""),
            number=int(data.get("number") or 0),
            agent_status=str(data.get("agent_status") or "unknown"),
        )


def extract_list(result: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull a list of objects from heterogeneous herdr list results."""
    if result is None:
        return []
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        for key in keys:
            value = result.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        # Some responses nest under type wrappers.
        for value in result.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [x for x in value if isinstance(x, dict)]
    return []
