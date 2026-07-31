from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from src.shared.ndjson import decode_line, encode_line


@pytest.fixture
def unix_sock_path() -> str:
    """Short AF_UNIX path (macOS limit ~104 bytes; pytest tmp dirs exceed it)."""
    path = f"/tmp/hu{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
    yield path
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


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


async def _subscribe_stub_handler(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    events: list[dict] | None = None,
) -> None:
    line = await reader.readline()
    req = decode_line(line)
    assert req.get("method") == "events.subscribe"
    ack = {"id": req["id"], "result": {"subscribed": True}}
    writer.write(encode_line(ack))
    await writer.drain()
    for event in events or []:
        writer.write(encode_line(event))
        await writer.drain()
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_unix_ping(unix_sock_path):
    from src.plugin.gateway.herdr_unix import HerdrUnixClient

    server = await asyncio.start_unix_server(_ping_stub_handler, path=unix_sock_path)
    try:
        client = HerdrUnixClient()
        await client.connect(unix_sock_path)
        result = await client.request("ping")
        assert result["type"] == "pong"
        await client.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unix_request_one_connection_per_call(unix_sock_path):
    from src.plugin.gateway.herdr_unix import HerdrUnixClient

    connections: list[int] = []

    async def counting_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        connections.append(1)
        await _ping_stub_handler(reader, writer)

    server = await asyncio.start_unix_server(counting_handler, path=unix_sock_path)
    try:
        client = HerdrUnixClient()
        await client.connect(unix_sock_path)
        await client.request("ping")
        await client.request("ping")
        assert len(connections) == 2
        await client.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unix_subscriber_recv_event(unix_sock_path):
    from src.plugin.gateway.herdr_unix import HerdrUnixSubscriber

    events = [
        {"event": "pane.created", "data": {"pane_id": "p1"}},
        {"event": "pane.closed", "data": {"pane_id": "p1"}},
    ]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _subscribe_stub_handler(reader, writer, events=events)

    server = await asyncio.start_unix_server(handler, path=unix_sock_path)
    try:
        sub = HerdrUnixSubscriber()
        await sub.start(unix_sock_path, [{"type": "pane.created"}, {"type": "pane.closed"}])
        first = await sub.recv_event()
        assert first["event"] == "pane.created"
        assert first["data"]["pane_id"] == "p1"
        second = await sub.recv_event()
        assert second["event"] == "pane.closed"
        await sub.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unix_subscriber_aiter(unix_sock_path):
    from src.plugin.gateway.herdr_unix import HerdrUnixSubscriber

    events = [
        {"event": "workspace.created", "data": {"workspace_id": "w1"}},
    ]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _subscribe_stub_handler(reader, writer, events=events)

    server = await asyncio.start_unix_server(handler, path=unix_sock_path)
    try:
        sub = HerdrUnixSubscriber()
        await sub.start(unix_sock_path, [{"type": "workspace.created"}])
        received = [event async for event in sub]
        assert len(received) == 1
        assert received[0]["event"] == "workspace.created"
        await sub.close()
    finally:
        server.close()
        await server.wait_closed()
