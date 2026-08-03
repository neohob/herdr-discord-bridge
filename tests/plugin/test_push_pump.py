from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from src.plugin.gateway.push_pump import PushPump
from src.plugin.gateway.server import PushHub


class FakeHerdrClient:
    """Injectable Herdr client for PushPump tests."""

    def __init__(self) -> None:
        self.read_results: dict[str, list[dict[str, Any]]] = {}
        self.read_calls: list[str] = []
        self._last_read: dict[str, dict[str, Any]] = {}

    def set_reads(self, pane_id: str, reads: list[dict[str, Any]]) -> None:
        self.read_results[pane_id] = list(reads)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "pane.read":
            pane_id = str((params or {}).get("pane_id", ""))
            self.read_calls.append(pane_id)
            queue = self.read_results.get(pane_id, [])
            if queue:
                result = queue.pop(0)
                self._last_read[pane_id] = result
                return result
            if pane_id in self._last_read:
                return self._last_read[pane_id]
            return {"text": "", "revision": 0, "truncated": False}
        raise AssertionError(f"unexpected method {method!r}")

    async def close(self) -> None:
        pass


class FakeHerdrSubscriber:
    """Emits queued Herdr events; ``feed`` can inject more while running."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in events:
            self._queue.put_nowait(event)
        self.started = False
        self.subscriptions: list[dict] | None = None
        self._closed = asyncio.Event()

    def feed(self, event: dict[str, Any]) -> None:
        self._queue.put_nowait(event)

    async def start(self, path: str, subscriptions: list[dict]) -> None:
        self.started = True
        self.subscriptions = subscriptions
        self._closed = asyncio.Event()

    async def recv_event(self) -> dict:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=10)
        except TimeoutError:
            raise ConnectionError("subscriber closed") from None

    async def close(self) -> None:
        self._closed.set()


class EventCollectingHub(PushHub):
    """PushHub that records broadcasts and supports wait_event."""

    def __init__(self) -> None:
        super().__init__()
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def broadcast(self, obj: dict[str, Any]) -> None:
        await self.events.put(obj)
        await super().broadcast(obj)

    async def wait_event(self, event_name: str, *, timeout: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for event {event_name!r}")
            obj = await asyncio.wait_for(self.events.get(), timeout=remaining)
            if obj.get("event") == event_name:
                return obj


@pytest.fixture
def push_hub() -> EventCollectingHub:
    return EventCollectingHub()


@pytest.fixture
def fake_herdr() -> FakeHerdrClient:
    return FakeHerdrClient()


@pytest.fixture
def fake_subscriber() -> FakeHerdrSubscriber:
    return FakeHerdrSubscriber(
        events=[
            {"event": "pane.created", "data": {"pane_id": "w1:p1"}},
        ],
    )


def _make_pump(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
    *,
    push_cooldown: float = 0.05,
    poll_interval: float = 0.02,
) -> PushPump:
    return PushPump(
        push_hub,
        herdr_socket="/tmp/fake.sock",
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=lambda: fake_subscriber,
        push_cooldown=push_cooldown,
        poll_interval=poll_interval,
    )


@pytest.mark.asyncio
async def test_run_broadcasts_herdr_events(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    run_task = asyncio.create_task(pump.run())
    try:
        ev = await push_hub.wait_event("pane.created", timeout=2)
        assert ev["data"]["pane_id"] == "w1:p1"
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_run_recreates_subscriber_after_start_failure(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
):
    class FailingSubscriber(FakeHerdrSubscriber):
        async def start(self, path: str, subscriptions: list[dict]) -> None:
            raise ConnectionError("socket unavailable")

    recovered = FakeHerdrSubscriber([{"event": "pane.created", "data": {"pane_id": "w2:p2"}}])
    attempts = iter([FailingSubscriber([]), recovered])
    pump = PushPump(
        push_hub,
        herdr_socket="/tmp/fake.sock",
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=lambda: next(attempts),
    )
    pump._subscriber_min_backoff = 0.01  # noqa: SLF001
    pump._subscriber_max_backoff = 0.02  # noqa: SLF001
    run_task = asyncio.create_task(pump.run())
    try:
        event = await push_hub.wait_event("pane.created", timeout=1)
        assert event["data"]["pane_id"] == "w2:p2"
        assert recovered.started
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_observe_emits_terminal_output(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [
            {"text": "line one", "revision": 1, "truncated": False},
            {"text": "line two", "revision": 2, "truncated": False},
        ],
    )
    # Observe does not require the Herdr event subscriber to be running.
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    await pump.set_observe("w1:p1", True)
    try:
        ev = await push_hub.wait_event("bridge.terminal_output", timeout=2)
        assert ev["data"]["pane_id"] == "w1:p1"
        assert ev["data"]["text"] == "line one"
        assert ev["data"]["revision"] == 1
    finally:
        await pump.shutdown()


@pytest.mark.asyncio
async def test_observe_strips_ansi_locally(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [{"text": "\x1b[31mred\x1b[0m", "revision": 1, "truncated": False}],
    )
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    await pump.set_observe("w1:p1", True)
    try:
        ev = await push_hub.wait_event("bridge.terminal_output", timeout=2)
        assert ev["data"]["text"] == "red"
    finally:
        await pump.shutdown()


@pytest.mark.asyncio
async def test_observe_coalesces_rapid_updates(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [
            {"text": "a", "revision": 1, "truncated": False},
            {"text": "b", "revision": 2, "truncated": False},
            {"text": "c", "revision": 3, "truncated": False},
        ],
    )
    pump = PushPump(
        push_hub,
        herdr_socket="/tmp/fake.sock",
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=lambda: fake_subscriber,
        push_cooldown=0.2,
        poll_interval=0.02,
    )
    collected: list[dict[str, Any]] = []

    async def collect() -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                obj = await asyncio.wait_for(push_hub.events.get(), timeout=0.1)
            except TimeoutError:
                continue
            if obj.get("event") == "bridge.terminal_output":
                collected.append(obj)

    collector = asyncio.create_task(collect())
    try:
        await pump.set_observe("w1:p1", True)
        await asyncio.sleep(0.5)
        terminal_events = [e for e in collected if e.get("event") == "bridge.terminal_output"]
        assert len(terminal_events) <= 2
        assert terminal_events[-1]["data"]["text"] == "c"
    finally:
        collector.cancel()
        try:
            await collector
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_set_observe_disable_stops_polling(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [{"text": "once", "revision": 1, "truncated": False}],
    )
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    try:
        await pump.set_observe("w1:p1", True)
        await push_hub.wait_event("bridge.terminal_output", timeout=2)
        calls_before = len(fake_herdr.read_calls)
        await pump.set_observe("w1:p1", False)
        await asyncio.sleep(0.15)
        calls_after_disable = len(fake_herdr.read_calls)
        assert calls_after_disable == calls_before
    finally:
        await pump.shutdown()


def test_default_subscriptions_omit_bare_agent_status():
    from src.plugin.gateway.push_pump import DEFAULT_SUBSCRIPTIONS

    assert not any(
        s.get("type") == "pane.agent_status_changed" and "pane_id" not in s
        for s in DEFAULT_SUBSCRIPTIONS
    )


@pytest.mark.asyncio
async def test_observe_adds_per_pane_agent_status_subscription(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
):
    """Herdr rejects global agent_status without pane_id; observe must attach it."""
    started: list[list[dict]] = []

    class RecordingSubscriber(FakeHerdrSubscriber):
        def __init__(self) -> None:
            super().__init__([])

        async def start(self, path: str, subscriptions: list[dict]) -> None:
            started.append(list(subscriptions))
            await super().start(path, subscriptions)

    pump = PushPump(
        push_hub,
        herdr_socket="/tmp/fake.sock",
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=RecordingSubscriber,
    )
    run_task = asyncio.create_task(pump.run())
    try:
        deadline = time.monotonic() + 2
        while not started and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert started
        assert {"type": "pane.agent_status_changed", "pane_id": "w1:p1"} not in started[0]
        await pump.set_observe("w1:p1", True)
        deadline = time.monotonic() + 2
        while len(started) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert len(started) >= 2
        assert {"type": "pane.agent_status_changed", "pane_id": "w1:p1"} in started[-1]
    finally:
        await pump.shutdown()
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass


def test_build_subscriptions_includes_scroll_and_match():
    """Per-pane subscriptions now include scroll_changed (pause while the user
    browses history) and output_matched (event-driven wake-up)."""
    pump = _make_pump(EventCollectingHub(), FakeHerdrClient(), FakeHerdrSubscriber([]))
    pump._observed_panes.add("w1:p1")  # noqa: SLF001
    subs = pump._build_subscriptions()
    per_pane = [s for s in subs if "pane_id" in s]
    assert any(s["type"] == "pane.scroll_changed" for s in per_pane)
    matches = [s for s in per_pane if s["type"] == "pane.output_matched"]
    assert len(matches) == 1
    assert matches[0]["match"]["type"] == "regex"
    assert "Task" in matches[0]["match"]["value"]


@pytest.mark.asyncio
async def test_scroll_changed_holds_pushes_until_bottom(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
):
    """While the terminal user is scrolled up, terminal output is held (not
    broadcast); returning to the bottom flushes the newest snapshot."""
    fake_herdr.set_reads(
        "w1:p1",
        [
            {"text": "first", "revision": 1, "truncated": False},
            {"text": "second", "revision": 2, "truncated": False},
        ],
    )
    fake_sub = FakeHerdrSubscriber(
        [
            {"event": "pane.created", "data": {"pane_id": "w1:p1"}},
        ]
    )
    pump = _make_pump(push_hub, fake_herdr, fake_sub, poll_interval=0.01)
    run_task = asyncio.create_task(pump.run())
    try:
        # Scroll state is established *before* observing so no race exists
        # between the observe loop's first read and the scroll event.
        fake_sub.feed(
            {
                "event": "pane.scroll_changed",
                "data": {
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "scroll": {"offset_from_bottom": 5, "max_offset_from_bottom": 40, "viewport_rows": 24},
                },
            }
        )
        await push_hub.wait_event("pane.created", timeout=2)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if (pump._pane_scroll.get("w1:p1") or {}).get("offset_from_bottom", 0) > 0:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("scroll state never applied")
        while not push_hub.events.empty():
            await push_hub.events.get()

        await pump.set_observe("w1:p1", True)
        # While scrolled, every read (revisions 1 and 2) is held, never pushed.
        await asyncio.sleep(0.05)
        assert push_hub.events.empty(), "scrolled output must be held, not pushed"

        # Back to the bottom: the newest snapshot is flushed.
        fake_sub.feed(
            {
                "event": "pane.scroll_changed",
                "data": {
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 40, "viewport_rows": 24},
                },
            }
        )
        ev = await push_hub.wait_event("bridge.terminal_output", timeout=2)
        assert ev["data"]["text"] == "second"
        assert ev["data"]["revision"] == 2
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_output_matched_wakes_observe_loop(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
):
    """A pane.output_matched event wakes the observe loop immediately instead
    of waiting out the poll interval."""
    fake_herdr.set_reads(
        "w1:p1",
        [
            {"text": "line one", "revision": 1, "truncated": False},
            {"text": "10 tasks (3 running)", "revision": 2, "truncated": False},
        ],
    )
    fake_sub = FakeHerdrSubscriber(
        [
            {"event": "pane.created", "data": {"pane_id": "w1:p1"}},
            {
                "event": "pane.output_matched",
                "data": {"pane_id": "w1:p1", "matched_line": "10 tasks (3 running)"},
            },
        ]
    )
    pump = _make_pump(push_hub, fake_herdr, fake_sub, poll_interval=5.0)
    run_task = asyncio.create_task(pump.run())
    try:
        await pump.set_observe("w1:p1", True)
        # First read broadcasts immediately.
        first = await push_hub.wait_event("bridge.terminal_output", timeout=2)
        assert first["data"]["text"] == "line one"
        # The 5s poll interval alone would never deliver this in time; the
        # output_matched wake-up must fire the read immediately.
        ev = await push_hub.wait_event("bridge.terminal_output", timeout=2)
        assert ev["data"]["text"] == "10 tasks (3 running)"
        assert ev["data"]["revision"] == 2
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()
