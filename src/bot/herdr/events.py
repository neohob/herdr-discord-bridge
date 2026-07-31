"""Long-lived herdr events.subscribe stream over SSH relay."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from src.bot.herdr.protocol import decode_line, encode_line, make_request, unwrap_result
from src.bot.ssh.manager import RemoteSession

log = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]

DEFAULT_SUBSCRIPTIONS: list[dict[str, Any]] = [
    {"type": "workspace.created"},
    {"type": "workspace.closed"},
    {"type": "pane.created"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.agent_detected"},
]


class HerdrEventStream:
    def __init__(
        self,
        session: RemoteSession,
        *,
        subscriptions: Iterable[dict[str, Any]] | None = None,
        on_event: EventHandler | None = None,
        reconnect_delay: float = 2.0,
    ):
        self.session = session
        self.subscriptions = list(subscriptions or DEFAULT_SUBSCRIPTIONS)
        self.on_event = on_event
        self.reconnect_delay = reconnect_delay
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"herdr-events-{self.session.id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def add_pane_status_subscription(self, pane_id: str) -> None:
        entry = {"type": "pane.agent_status_changed", "pane_id": pane_id}
        if entry not in self.subscriptions:
            self.subscriptions.append(entry)

    async def _run(self) -> None:
        delay = self.reconnect_delay
        while not self._stop.is_set():
            try:
                await self._subscribe_once()
                delay = self.reconnect_delay
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("event stream error on %s; reconnecting", self.session.id)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 60)

    async def _subscribe_once(self) -> None:
        proc = await self.session.open_herdr_channel()
        assert proc.stdin is not None
        assert proc.stdout is not None
        req = make_request("events.subscribe", {"subscriptions": self.subscriptions})
        proc.stdin.write(encode_line(req))
        await proc.stdin.drain()

        # Ack line
        ack_line = await proc.stdout.readline()
        if not ack_line:
            raise RuntimeError("subscription closed before ack")
        unwrap_result(decode_line(ack_line))
        log.info("subscribed on remote %s (%d filters)", self.session.id, len(self.subscriptions))

        try:
            while not self._stop.is_set():
                line = await proc.stdout.readline()
                if not line:
                    raise RuntimeError("subscription EOF")
                payload = decode_line(line)
                event_name = str(payload.get("event") or "")
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                if self.on_event and event_name:
                    result = self.on_event(event_name, data)  # type: ignore[arg-type]
                    if asyncio.iscoroutine(result):
                        await result
        finally:
            proc.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
