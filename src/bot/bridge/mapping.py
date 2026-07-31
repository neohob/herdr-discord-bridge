"""Persist Discord category/channel ↔ remote/pane mapping."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PaneMapping:
    remote_id: str
    pane_id: str
    channel_id: int
    terminal_message_id: int | None = None
    label: str = ""
    agent_status: str = "unknown"


@dataclass
class RemoteMapping:
    remote_id: str
    category_id: int | None = None
    panes: dict[str, PaneMapping] = field(default_factory=dict)


class MappingStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.remotes: dict[str, RemoteMapping] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self.remotes = {}
                return
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            remotes: dict[str, RemoteMapping] = {}
            for rid, data in (raw.get("remotes") or {}).items():
                panes: dict[str, PaneMapping] = {}
                for pid, pdata in (data.get("panes") or {}).items():
                    panes[pid] = PaneMapping(
                        remote_id=rid,
                        pane_id=pid,
                        channel_id=int(pdata["channel_id"]),
                        terminal_message_id=(
                            int(pdata["terminal_message_id"])
                            if pdata.get("terminal_message_id") is not None
                            else None
                        ),
                        label=str(pdata.get("label") or ""),
                        agent_status=str(pdata.get("agent_status") or "unknown"),
                    )
                remotes[rid] = RemoteMapping(
                    remote_id=rid,
                    category_id=int(data["category_id"]) if data.get("category_id") else None,
                    panes=panes,
                )
            self.remotes = remotes

    def save(self) -> None:
        with self._lock:
            payload: dict[str, Any] = {"remotes": {}}
            for rid, rm in self.remotes.items():
                payload["remotes"][rid] = {
                    "category_id": rm.category_id,
                    "panes": {
                        pid: asdict(pm)
                        for pid, pm in rm.panes.items()
                    },
                }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def ensure_remote(self, remote_id: str) -> RemoteMapping:
        with self._lock:
            if remote_id not in self.remotes:
                self.remotes[remote_id] = RemoteMapping(remote_id=remote_id)
            return self.remotes[remote_id]

    def set_category(self, remote_id: str, category_id: int) -> None:
        with self._lock:
            rm = self.ensure_remote(remote_id)
            rm.category_id = category_id
            self.save()

    def upsert_pane(self, mapping: PaneMapping) -> None:
        with self._lock:
            rm = self.ensure_remote(mapping.remote_id)
            rm.panes[mapping.pane_id] = mapping
            self.save()

    def remove_pane(self, remote_id: str, pane_id: str) -> None:
        with self._lock:
            rm = self.remotes.get(remote_id)
            if not rm:
                return
            rm.panes.pop(pane_id, None)
            self.save()

    def get_pane(self, remote_id: str, pane_id: str) -> PaneMapping | None:
        rm = self.remotes.get(remote_id)
        if not rm:
            return None
        return rm.panes.get(pane_id)

    def find_by_channel(self, channel_id: int) -> PaneMapping | None:
        for rm in self.remotes.values():
            for pm in rm.panes.values():
                if pm.channel_id == channel_id:
                    return pm
        return None

    def all_panes(self, remote_id: str | None = None) -> list[PaneMapping]:
        if remote_id:
            rm = self.remotes.get(remote_id)
            return list(rm.panes.values()) if rm else []
        out: list[PaneMapping] = []
        for rm in self.remotes.values():
            out.extend(rm.panes.values())
        return out
