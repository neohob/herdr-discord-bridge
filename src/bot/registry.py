"""Remote Registry — runtime source of truth for Gateway remotes."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class RemoteRecord:
    id: str
    host: str
    port: int
    token: str
    fingerprint: str
    channel_id: int | None = None


class RemoteRegistry:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._records: dict[str, RemoteRecord] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self._records = {}
                return
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            records: dict[str, RemoteRecord] = {}
            for item in raw.get("remotes") or []:
                record = RemoteRecord(
                    id=str(item["id"]),
                    host=str(item["host"]),
                    port=int(item["port"]),
                    token=str(item["token"]),
                    fingerprint=str(item["fingerprint"]),
                    channel_id=(
                        int(item["channel_id"]) if item.get("channel_id") is not None else None
                    ),
                )
                records[record.id] = record
            self._records = records

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"remotes": [asdict(r) for r in self._records.values()]}
            self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def upsert(self, record: RemoteRecord) -> None:
        with self._lock:
            self._records[record.id] = record
            self.save()

    def get(self, remote_id: str) -> RemoteRecord | None:
        with self._lock:
            return self._records.get(remote_id)

    def list_unbound(self) -> list[RemoteRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.channel_id is None]

    def bind_channel(self, remote_id: str, channel_id: int) -> None:
        with self._lock:
            record = self._records.get(remote_id)
            if record is None:
                raise KeyError(f"remote `{remote_id}` not found")
            record.channel_id = channel_id
            self.save()

    def unbind_channel(self, remote_id: str) -> None:
        with self._lock:
            record = self._records.get(remote_id)
            if record is None:
                raise KeyError(f"remote `{remote_id}` not found")
            record.channel_id = None
            self.save()

    def remove(self, remote_id: str) -> None:
        with self._lock:
            if remote_id not in self._records:
                return
            del self._records[remote_id]
            self.save()
