"""Multi-remote SSH connection pool."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from src.bot.config import RemoteConfig
from src.bot.ssh.relay import relay_remote_command

log = logging.getLogger(__name__)


@dataclass
class RemoteSession:
    config: RemoteConfig
    conn: asyncssh.SSHClientConnection | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def online(self) -> bool:
        return self.conn is not None and not self.conn.is_closing()

    async def connect(self) -> None:
        async with self._lock:
            if self.online:
                return
            key = self.config.ssh_key
            if not key.is_file():
                raise FileNotFoundError(f"ssh_key not found for remote {self.id}: {key}")
            log.info("ssh connect %s@%s:%s (%s)", self.config.user, self.config.host, self.config.port, self.id)
            self.conn = await asyncssh.connect(
                self.config.host,
                port=self.config.port,
                username=self.config.user,
                client_keys=[str(key)],
                known_hosts=None,
                # Dedicated bridge keys; BatchMode-like behavior.
                preferred_auth=["publickey"],
            )

    async def close(self) -> None:
        async with self._lock:
            if self.conn is not None:
                self.conn.close()
                try:
                    await self.conn.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                self.conn = None

    async def ensure(self) -> asyncssh.SSHClientConnection:
        if not self.online:
            await self.connect()
        assert self.conn is not None
        return self.conn

    async def open_herdr_channel(self) -> asyncssh.SSHClientProcess:
        """Open an SSH process that relays stdio to the remote herdr socket."""
        conn = await self.ensure()
        cmd = relay_remote_command(self.config.herdr_socket)
        return await conn.create_process(cmd, encoding=None)


class SshManager:
    def __init__(self, remotes: list[RemoteConfig]):
        self._sessions: dict[str, RemoteSession] = {
            r.id: RemoteSession(config=r) for r in remotes
        }

    def get(self, remote_id: str) -> RemoteSession:
        try:
            return self._sessions[remote_id]
        except KeyError as exc:
            raise KeyError(f"unknown remote: {remote_id}") from exc

    def all(self) -> list[RemoteSession]:
        return list(self._sessions.values())

    async def connect_all(self) -> dict[str, Exception | None]:
        results: dict[str, Exception | None] = {}

        async def one(session: RemoteSession) -> None:
            try:
                await session.connect()
                results[session.id] = None
            except Exception as exc:  # noqa: BLE001
                log.exception("ssh failed for %s", session.id)
                results[session.id] = exc

        await asyncio.gather(*(one(s) for s in self._sessions.values()))
        return results

    async def close_all(self) -> None:
        await asyncio.gather(*(s.close() for s in self._sessions.values()))

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "host": s.config.host,
                "user": s.config.user,
                "online": s.online,
            }
            for s in self._sessions.values()
        ]
