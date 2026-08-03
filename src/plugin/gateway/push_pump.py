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

# Lifecycle subscriptions are global. ``pane.agent_status_changed``,
# ``pane.scroll_changed`` and ``pane.output_matched`` require a concrete
# ``pane_id`` and are attached dynamically for observed panes — including them
# without ``pane_id`` makes Herdr reject the *entire* events.subscribe batch.
DEFAULT_SUBSCRIPTIONS: list[dict[str, str]] = [
    {"type": "workspace.created"},
    {"type": "workspace.closed"},
    {"type": "pane.created"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.moved"},
]

# Edge-triggered output match (herdr ``pane.output_matched``): Herdr pushes a
# single event when a pane's recent buffer *transitions* from non-matching to
# matching (never during a continuous match), carrying a full read snapshot.
# We use it only as a wake-up signal so the observe loop reads immediately
# instead of waiting out the longer poll interval — the actual pane.read still
# happens in the observe loop, keeping one writer per pane.
OUTPUT_MATCH_REGEX = r"\d+\s+tasks?\s*\(|Task\s+\d+|…\s*\+\d+\s+\w+"


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
        poll_interval: float = 1.0,
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
        self._pane_scroll: dict[str, dict[str, Any]] = {}
        self._match_events: dict[str, asyncio.Event] = {}
        self._shutdown = asyncio.Event()
        self._resubscribe = asyncio.Event()
        self._subscriber_min_backoff = 0.5
        self._subscriber_max_backoff = 30.0

    def _build_subscriptions(self) -> list[dict[str, Any]]:
        """Lifecycle globals + per-observed-pane dynamic subscriptions."""
        subs: list[dict[str, Any]] = list(self._base_subscriptions)
        for pane_id in sorted(self._observed_panes):
            subs.append({"type": "pane.agent_status_changed", "pane_id": pane_id})
            subs.append({"type": "pane.scroll_changed", "pane_id": pane_id})
            subs.append(
                {
                    "type": "pane.output_matched",
                    "pane_id": pane_id,
                    "source": "recent",
                    "lines": self.max_lines,
                    "strip_ansi": False,
                    "match": {"type": "regex", "value": OUTPUT_MATCH_REGEX},
                }
            )
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
                        await self._handle_event(recv_task.result())
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

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Route Herdr push events: scroll/match are Gateway-local signals; the
        rest are forwarded verbatim to push clients."""
        name = event.get("event")
        data = event.get("data") or {}
        if name == "pane.scroll_changed":
            pane_id = str(data.get("pane_id") or "")
            self._pane_scroll[pane_id] = data.get("scroll") or {}
        elif name == "pane.output_matched":
            pane_id = str(data.get("pane_id") or "")
            self._match_events.setdefault(pane_id, asyncio.Event()).set()
        else:
            await self._push_hub.broadcast(event)

    def _is_scrolled(self, pane_id: str) -> bool:
        """True while the terminal user is scrolled up away from the bottom.

        The ``recent`` read source is a scrollback snapshot, so scrolling does
        not change what we read — this only gates *pushing*: while the user is
        browsing history the latest snapshot is held (pending) and flushed the
        moment they return to the bottom.
        """
        scroll = self._pane_scroll.get(pane_id) or {}
        return int(scroll.get("offset_from_bottom") or 0) > 0

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
        self._pane_scroll.pop(pane_id, None)
        self._match_events.pop(pane_id, None)
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

    async def _sleep_or_match(self, pane_id: str, duration: float | None = None) -> None:
        """Sleep for ``duration`` (default the poll interval), or wake immediately
        on an output match.

        ``pane.output_matched`` is edge-triggered on the Herdr side, so this
        stays quiet during a continuous match; the low-frequency poll remains
        the reliable fallback for output that never matches.
        """
        delay = self.poll_interval if duration is None else duration
        ev = self._match_events.setdefault(pane_id, asyncio.Event())
        sleep_task = asyncio.create_task(asyncio.sleep(delay))
        match_task = asyncio.create_task(ev.wait())
        try:
            await asyncio.wait(
                {sleep_task, match_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sleep_task, match_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep_task, match_task, return_exceptions=True)
        ev.clear()

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
                scrolled = self._is_scrolled(pane_id)

                if changed:
                    if scrolled:
                        # User is browsing history: hold the newest snapshot,
                        # replacing any older held one. Nothing is lost — the
                        # moment they return to the bottom it is flushed.
                        pending = (revision, text, truncated)
                        last_revision = revision
                        last_text = text
                    elif now - last_push >= self.push_cooldown:
                        await self._emit_terminal_output(pane_id, revision, text, truncated)
                        last_revision = revision
                        last_text = text
                        last_push = now
                        pending = None
                    else:
                        pending = (revision, text, truncated)

                if pending is not None and not scrolled and now - last_push >= self.push_cooldown:
                    rev, txt, trunc = pending
                    await self._emit_terminal_output(pane_id, rev, txt, trunc)
                    last_revision = rev
                    last_text = txt
                    last_push = time.monotonic()
                    pending = None

                # A held (pending) update must flush as soon as the push
                # cooldown lapses — never wait out a long poll interval.
                delay: float | None = None
                if pending is not None and not scrolled:
                    remaining = self.push_cooldown - (time.monotonic() - last_push)
                    if remaining > 0:
                        delay = min(self.poll_interval, remaining)
                await self._sleep_or_match(pane_id, delay)
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
