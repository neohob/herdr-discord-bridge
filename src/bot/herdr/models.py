"""Lightweight views of herdr API objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaneInfo:
        pane_id = str(data.get("pane_id") or data.get("id") or "")
        label = str(data.get("label") or data.get("title") or data.get("agent") or pane_id)
        return cls(
            pane_id=pane_id,
            workspace_id=str(data.get("workspace_id") or ""),
            tab_id=str(data.get("tab_id") or ""),
            label=label,
            agent=str(data.get("agent") or ""),
            agent_status=str(data.get("agent_status") or "unknown"),
            cwd=str(data.get("cwd") or ""),
            focused=bool(data.get("focused", False)),
            revision=int(data.get("revision") or 0),
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
