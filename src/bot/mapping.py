"""Persist Discord Remote Channel / Pane Thread mapping."""

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
    thread_id: int
    terminal_message_id: int | None = None
    label: str = ""
    agent_status: str = "unknown"


@dataclass
class RemoteMapping:
    remote_id: str
    channel_id: int | None = None
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
                    thread_id = pdata.get("thread_id")
                    if thread_id is None and pdata.get("channel_id") is not None:
                        thread_id = pdata["channel_id"]
                    if thread_id is None:
                        continue
                    panes[pid] = PaneMapping(
                        remote_id=rid,
                        pane_id=pid,
                        thread_id=int(thread_id),
                        terminal_message_id=(
                            int(pdata["terminal_message_id"])
                            if pdata.get("terminal_message_id") is not None
                            else None
                        ),
                        label=str(pdata.get("label") or ""),
                        agent_status=str(pdata.get("agent_status") or "unknown"),
                    )
                channel_id = data.get("channel_id")
                if channel_id is None and data.get("category_id") is not None:
                    channel_id = data["category_id"]
                remotes[rid] = RemoteMapping(
                    remote_id=rid,
                    channel_id=int(channel_id) if channel_id else None,
                    panes=panes,
                )
            self.remotes = remotes

    def save(self) -> None:
        with self._lock:
            payload: dict[str, Any] = {"remotes": {}}
            for rid, rm in self.remotes.items():
                payload["remotes"][rid] = {
                    "channel_id": rm.channel_id,
                    "panes": {pid: asdict(pm) for pid, pm in rm.panes.items()},
                }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)

    def ensure_remote(self, remote_id: str) -> RemoteMapping:
        with self._lock:
            if remote_id not in self.remotes:
                self.remotes[remote_id] = RemoteMapping(remote_id=remote_id)
            return self.remotes[remote_id]

    def set_remote_channel(self, remote_id: str, channel_id: int) -> None:
        with self._lock:
            rm = self.ensure_remote(remote_id)
            rm.channel_id = channel_id
            self.save()

    def upsert_pane(self, mapping: PaneMapping) -> None:
        with self._lock:
            rm = self.ensure_remote(mapping.remote_id)
            rm.panes[mapping.pane_id] = mapping
            self.save()

    def set_terminal_message(self, remote_id: str, pane_id: str, message_id: int) -> None:
        with self._lock:
            pm = self.get_pane(remote_id, pane_id)
            if pm is None:
                return
            pm.terminal_message_id = message_id
            self.save()

    def remove_pane(self, remote_id: str, pane_id: str) -> None:
        with self._lock:
            rm = self.remotes.get(remote_id)
            if not rm:
                return
            rm.panes.pop(pane_id, None)
            self.save()

    def remove_remote(self, remote_id: str) -> None:
        """Remove a Remote Channel and all of its Pane Thread mappings."""
        with self._lock:
            if remote_id not in self.remotes:
                return
            del self.remotes[remote_id]
            self.save()

    def get_pane(self, remote_id: str, pane_id: str) -> PaneMapping | None:
        rm = self.remotes.get(remote_id)
        if not rm:
            return None
        return rm.panes.get(pane_id)

    def find_by_thread(self, thread_id: int) -> PaneMapping | None:
        for rm in self.remotes.values():
            for pm in rm.panes.values():
                if pm.thread_id == thread_id:
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
