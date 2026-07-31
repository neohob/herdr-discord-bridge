from __future__ import annotations

import asyncio
import os
import socket
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from plugin.test_push_pump import EventCollectingHub, FakeHerdrClient, FakeHerdrSubscriber
from src.bot.gateway_client import GatewayClient, TlsFingerprintError
from src.bot.registry import RemoteRecord
from src.plugin.gateway.config import GatewayConfig
from src.plugin.gateway.herdr_unix import HerdrUnixClient
from src.plugin.gateway.push_pump import PushPump
from src.plugin.gateway.server import serve_gateway
from src.plugin.gateway.tls_util import generate_self_signed
from src.shared.ndjson import decode_line, encode_line, make_request, unwrap_result


@pytest.fixture
def unix_sock_path() -> str:
    path = f"/tmp/hgc{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
    yield path
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _ping_stub_handler(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    line = await reader.readline()
    req = decode_line(line)
    if req.get("method") == "ping":
        resp = {"id": req["id"], "result": {"type": "pong"}}
    else:
        resp = {
            "id": req["id"],
            "error": {"code": "unknown", "message": f"unknown method {req.get('method')!r}"},
        }
    writer.write(encode_line(resp))
    await writer.drain()
    writer.close()
    await writer.wait_closed()


@pytest_asyncio.fixture
async def gateway_fixture(tmp_path, unix_sock_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    fingerprint = generate_self_signed(cert_path, key_path)

    port = _free_port()
    cfg = GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=port,
        token="test-secret",
        herdr_socket=unix_sock_path,
        cert_path=cert_path,
        key_path=key_path,
    )

    unix_server = await asyncio.start_unix_server(_ping_stub_handler, path=unix_sock_path)

    def herdr_factory() -> HerdrUnixClient:
        client = HerdrUnixClient()
        client._path = unix_sock_path  # noqa: SLF001
        return client

    gateway_task = asyncio.create_task(serve_gateway(cfg, herdr_factory))
    await asyncio.sleep(0.05)

    remote = RemoteRecord(
        id="r1",
        host="127.0.0.1",
        port=port,
        token="test-secret",
        fingerprint=fingerprint,
    )

    yield remote, gateway_task, unix_server

    gateway_task.cancel()
    try:
        await gateway_task
    except asyncio.CancelledError:
        pass
    unix_server.close()
    await unix_server.wait_closed()


@pytest.mark.asyncio
async def test_gateway_client_auth_and_ping(gateway_fixture):
    remote, _, _ = gateway_fixture
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    client = GatewayClient(remote, on_event, min_backoff=0.05, max_backoff=0.2)
    await client.start()
    try:
        result = await client.request("ping")
        assert result == {"type": "pong"}
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_gateway_client_fingerprint_mismatch(tmp_path, unix_sock_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    generate_self_signed(cert_path, key_path)

    port = _free_port()
    cfg = GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=port,
        token="test-secret",
        herdr_socket=unix_sock_path,
        cert_path=cert_path,
        key_path=key_path,
    )

    unix_server = await asyncio.start_unix_server(_ping_stub_handler, path=unix_sock_path)

    def herdr_factory() -> HerdrUnixClient:
        client = HerdrUnixClient()
        client._path = unix_sock_path  # noqa: SLF001
        return client

    gateway_task = asyncio.create_task(serve_gateway(cfg, herdr_factory))
    await asyncio.sleep(0.05)

    remote = RemoteRecord(
        id="r1",
        host="127.0.0.1",
        port=port,
        token="test-secret",
        fingerprint="0" * 64,
    )

    client = GatewayClient(remote, lambda _e: asyncio.sleep(0), min_backoff=0.05, max_backoff=0.2)
    await client.start()
    try:
        with pytest.raises(TlsFingerprintError):
            await client.request("ping")
    finally:
        await client.stop()
        gateway_task.cancel()
        try:
            await gateway_task
        except asyncio.CancelledError:
            pass
        unix_server.close()
        await unix_server.wait_closed()


@pytest.mark.asyncio
async def test_gateway_client_observe_pane_and_push(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    fingerprint = generate_self_signed(cert_path, key_path)

    port = _free_port()
    cfg = GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=port,
        token="observe-token",
        herdr_socket="/tmp/unused.sock",
        cert_path=cert_path,
        key_path=key_path,
    )

    hub = EventCollectingHub()
    fake_herdr = FakeHerdrClient()
    fake_herdr.set_reads(
        "w1:p1",
        [{"text": "hello", "revision": 1, "truncated": False}],
    )
    fake_subscriber = FakeHerdrSubscriber(events=[])
    pump = PushPump(
        hub,
        cfg.herdr_socket,
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=lambda: fake_subscriber,
        push_cooldown=0.05,
        poll_interval=0.02,
    )

    gateway_task = asyncio.create_task(
        serve_gateway(cfg, lambda: fake_herdr, push_hub=hub, push_pump=pump),
    )
    pump_task = asyncio.create_task(pump.run())
    await asyncio.sleep(0.05)

    received: asyncio.Queue[dict] = asyncio.Queue()

    async def on_event(event: dict) -> None:
        await received.put(event)

    remote = RemoteRecord(
        id="r1",
        host="127.0.0.1",
        port=port,
        token="observe-token",
        fingerprint=fingerprint,
    )
    client = GatewayClient(remote, on_event, min_backoff=0.05, max_backoff=0.2)
    await client.start()
    try:
        result = await client.observe_pane("w1:p1", True)
        assert result == {"type": "ok"}

        event = await asyncio.wait_for(received.get(), timeout=2.0)
        assert event["event"] == "bridge.terminal_output"
        assert event["data"]["pane_id"] == "w1:p1"
        assert event["data"]["text"] == "hello"
    finally:
        await client.stop()
        gateway_task.cancel()
        pump_task.cancel()
        for task in (gateway_task, pump_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_idle_control_eof_reconnects_and_restores_observes() -> None:
    """A control EOF is detected by heartbeat without a user RPC."""
    remote = RemoteRecord("r1", "unused", 1, "token", "fingerprint")
    ready_calls: list[None] = []

    async def on_ready() -> None:
        ready_calls.append(None)

    def connection(lines: list[bytes]) -> tuple[SimpleNamespace, MagicMock]:
        reader = SimpleNamespace(readline=AsyncMock(side_effect=lines))
        writer = MagicMock()
        writer.is_closing.return_value = False
        writer.wait_closed = AsyncMock()
        return reader, writer

    pong = encode_line({"id": "heartbeat", "result": {"type": "pong"}})
    first = connection([pong, b""])
    second = connection([pong] * 20)

    async def fake_connect(role: str):
        if role == "control":
            return next(control_connections)
        await asyncio.Event().wait()

    control_connections = iter([first, second])
    client = GatewayClient(
        remote,
        lambda _event: asyncio.sleep(0),
        on_control_ready=on_ready,
        min_backoff=0.01,
        max_backoff=0.02,
        control_heartbeat=0.01,
    )
    client._connect = fake_connect  # type: ignore[method-assign]  # noqa: SLF001
    await client.start()
    try:
        async def restored() -> bool:
            while len(ready_calls) < 2:
                await asyncio.sleep(0.005)
            return True

        await asyncio.wait_for(restored(), timeout=0.5)
    finally:
        await client.stop()
