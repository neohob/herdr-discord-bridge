"""Gateway push pump: Herdr events + Gateway-local terminal observe → PushHub."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from ._lib import HerdrApiError
from .ansi import strip_ansi
from .herdr_unix import HerdrUnixClient, HerdrUnixSubscriber
from .server import PushHub

log = logging.getLogger(__name__)

# Lifecycle subscriptions are global. ``pane.agent_status_changed`` requires a
# concrete ``pane_id`` and is attached dynamically for observed panes — including
# it without ``pane_id`` makes Herdr reject the *entire* events.subscribe batch.
DEFAULT_SUBSCRIPTIONS: list[dict[str, str]] = [
    {"type": "workspace.created"},
    {"type": "workspace.closed"},
    {"type": "pane.created"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.moved"},
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
        self._base_subscriptions = list(subscriptions or DEFAULT_SUBSCRIPTIONS)
        self._observe_tasks: dict[str, asyncio.Task[None]] = {}
        self._observed_panes: set[str] = set()
        self._shutdown = asyncio.Event()
        self._resubscribe = asyncio.Event()
        self._subscriber_min_backoff = 0.5
        self._subscriber_max_backoff = 30.0

    def _build_subscriptions(self) -> list[dict[str, str]]:
        """Lifecycle globals + per-observed-pane agent status subscriptions."""
        subs = list(self._base_subscriptions)
        for pane_id in sorted(self._observed_panes):
            subs.append({"type": "pane.agent_status_changed", "pane_id": pane_id})
        return subs

    def _request_resubscribe(self) -> None:
        self._resubscribe.set()

    async def run(self) -> None:
        """Subscribe to Herdr events and reconnect after subscriber failures."""
        backoff = self._subscriber_min_backoff
        while not self._shutdown.is_set():
            subscriber = self._subscriber_factory()
            self._resubscribe.clear()
            try:
                subscriptions = self._build_subscriptions()
                await subscriber.start(self._herdr_socket, subscriptions)
                log.info("Herdr events.subscribe ok (%d subscription(s))", len(subscriptions))
                backoff = self._subscriber_min_backoff
                while not self._shutdown.is_set() and not self._resubscribe.is_set():
                    recv_task = asyncio.create_task(subscriber.recv_event())
                    resub_task = asyncio.create_task(self._resubscribe.wait())
                    try:
                        done, pending = await asyncio.wait(
                            {recv_task, resub_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        for task in (recv_task, resub_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(recv_task, resub_task, return_exceptions=True)
                    if self._shutdown.is_set() or self._resubscribe.is_set():
                        if self._resubscribe.is_set():
                            log.info("resubscribing Herdr events after observe set change")
                        break
                    if recv_task in done and not recv_task.cancelled():
                        exc = recv_task.exception()
                        if exc is not None:
                            raise exc
                        await self._push_hub.broadcast(recv_task.result())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Herdr event subscriber failed; retrying in %.1fs", backoff)
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
            changed = pane_id not in self._observed_panes
            self._observed_panes.add(pane_id)
            existing = self._observe_tasks.get(pane_id)
            if existing is None or existing.done():
                self._observe_tasks[pane_id] = asyncio.create_task(self._observe_loop(pane_id))
            if changed:
                self._request_resubscribe()
            return

        self._observed_panes.discard(pane_id)
        task = self._observe_tasks.pop(pane_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._request_resubscribe()

    async def shutdown(self) -> None:
        """Stop all observe loops and the event subscription loop."""
        self._shutdown.set()
        self._request_resubscribe()
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
                try:
                    read = await herdr.request(
                        "pane.read",
                        {
                            "pane_id": pane_id,
                            "source": "recent",
                            "lines": self.max_lines,
                            "strip_ansi": False,
                        },
                    )
                except HerdrApiError as exc:
                    if str(exc.code) == "pane_not_found" or "pane_not_found" in str(exc):
                        log.warning("observe stopped; pane %s gone (%s)", pane_id, exc)
                        self._observed_panes.discard(pane_id)
                        self._observe_tasks.pop(pane_id, None)
                        self._request_resubscribe()
                        return
                    raise
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
