"""Gateway push pump: Herdr events + Gateway-local terminal observe → PushHub."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from .ansi import strip_ansi
from .herdr_unix import HerdrUnixClient, HerdrUnixSubscriber
from .server import PushHub

DEFAULT_SUBSCRIPTIONS: list[dict[str, str]] = [
    {"type": "workspace.created"},
    {"type": "workspace.closed"},
    {"type": "pane.created"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.agent_status_changed"},
]


class PushPump:
    """Fan out Herdr events and per-pane Terminal Views to push clients."""

    def __init__(
        self,
        push_hub: PushHub,
        herdr_socket: str,
        *,
        herdr_factory: Callable[[], Any] | None = None,
        subscriber_factory: Callable[[], Any] | None = None,
        max_lines: int = 50,
        push_cooldown: float = 1.0,
        poll_interval: float = 0.25,
        subscriptions: list[dict[str, str]] | None = None,
    ) -> None:
        self._push_hub = push_hub
        self._herdr_socket = herdr_socket
        self._herdr_factory = herdr_factory or HerdrUnixClient
        self._subscriber_factory = subscriber_factory or HerdrUnixSubscriber
        self.max_lines = max_lines
        self.push_cooldown = push_cooldown
        self.poll_interval = poll_interval
        self._subscriptions = subscriptions or DEFAULT_SUBSCRIPTIONS
        self._observe_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown = asyncio.Event()
        self._subscriber_min_backoff = 0.5
        self._subscriber_max_backoff = 30.0

    async def run(self) -> None:
        """Subscribe to Herdr events and reconnect after subscriber failures."""
        backoff = self._subscriber_min_backoff
        while not self._shutdown.is_set():
            subscriber = self._subscriber_factory()
            try:
                await subscriber.start(self._herdr_socket, self._subscriptions)
                backoff = self._subscriber_min_backoff
                while not self._shutdown.is_set():
                    payload = await subscriber.recv_event()
                    await self._push_hub.broadcast(payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if not self._shutdown.is_set():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._subscriber_max_backoff)
            finally:
                close = getattr(subscriber, "close", None)
                if close is not None:
                    maybe = close()
                    if asyncio.iscoroutine(maybe):
                        await maybe

    async def set_observe(self, pane_id: str, enable: bool) -> None:
        """Start or stop Gateway-local terminal observe for *pane_id*."""
        if enable:
            existing = self._observe_tasks.get(pane_id)
            if existing is not None and not existing.done():
                return
            self._observe_tasks[pane_id] = asyncio.create_task(self._observe_loop(pane_id))
            return

        task = self._observe_tasks.pop(pane_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        """Stop all observe loops and the event subscription loop."""
        self._shutdown.set()
        pane_ids = list(self._observe_tasks)
        for pane_id in pane_ids:
            await self.set_observe(pane_id, False)

    async def _observe_loop(self, pane_id: str) -> None:
        # Gateway-local pane.read polling — NOT exposed to Bot; replace with
        # Herdr terminal observe stream when available.
        herdr = self._herdr_factory()
        last_revision: int | None = None
        last_text: str | None = None
        last_push = 0.0
        pending: tuple[int, str, bool] | None = None

        try:
            connect = getattr(herdr, "connect", None)
            if connect is not None:
                maybe = connect(self._herdr_socket)
                if asyncio.iscoroutine(maybe):
                    await maybe

            while True:
                read = await herdr.request(
                    "pane.read",
                    {
                        "pane_id": pane_id,
                        "source": "recent",
                        "lines": self.max_lines,
                        "strip_ansi": False,
                    },
                )
                if isinstance(read, dict) and "read" in read:
                    read = read["read"]
                if not isinstance(read, dict):
                    read = {}

                text = strip_ansi(str(read.get("text") or ""))
                revision = int(read.get("revision") or 0)
                truncated = bool(read.get("truncated", False))

                changed = revision != last_revision or text != last_text
                now = time.monotonic()

                if changed:
                    if now - last_push >= self.push_cooldown:
                        await self._emit_terminal_output(pane_id, revision, text, truncated)
                        last_revision = revision
                        last_text = text
                        last_push = now
                        pending = None
                    else:
                        pending = (revision, text, truncated)

                if pending is not None and now - last_push >= self.push_cooldown:
                    rev, txt, trunc = pending
                    await self._emit_terminal_output(pane_id, rev, txt, trunc)
                    last_revision = rev
                    last_text = txt
                    last_push = time.monotonic()
                    pending = None

                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            if pending is not None:
                rev, txt, trunc = pending
                await self._emit_terminal_output(pane_id, rev, txt, trunc)
            raise
        finally:
            close = getattr(herdr, "close", None)
            if close is not None:
                maybe = close()
                if asyncio.iscoroutine(maybe):
                    await maybe

    async def _emit_terminal_output(
        self,
        pane_id: str,
        revision: int,
        text: str,
        truncated: bool,
    ) -> None:
        await self._push_hub.broadcast(
            {
                "event": "bridge.terminal_output",
                "data": {
                    "pane_id": pane_id,
                    "revision": revision,
                    "text": text,
                    "truncated": truncated,
                },
            },
        )
