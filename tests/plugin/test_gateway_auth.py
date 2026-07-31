from __future__ import annotations

import asyncio
import os
import socket
import ssl
import uuid

import pytest

from src.plugin.gateway.config import GatewayConfig, load_gateway_config
from src.plugin.gateway.server import PushHub, serve_gateway
from src.plugin.gateway.tls_util import generate_self_signed
from src.shared.fingerprint import cert_sha256_fingerprint, fingerprints_match
from src.shared.ndjson import decode_line, encode_line, make_request, unwrap_result


@pytest.fixture
def unix_sock_path() -> str:
    path = f"/tmp/hg{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
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


async def _tls_connect(port: int, expected_fp: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    ssl_obj = writer.get_extra_info("ssl_object")
    assert ssl_obj is not None
    der = ssl_obj.getpeercert(binary_form=True)
    assert der is not None
    actual = cert_sha256_fingerprint(der)
    assert fingerprints_match(expected_fp, actual)
    return reader, writer


async def _auth_control(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    token: str,
    *,
    req_id: str = "auth_1",
) -> dict:
    writer.write(
        encode_line(make_request("bridge.auth", {"token": token, "role": "control"}, req_id)),
    )
    await writer.drain()
    line = await reader.readline()
    return decode_line(line)


@pytest.mark.asyncio
async def test_control_auth_and_ping(tmp_path, unix_sock_path):
    from src.plugin.gateway.herdr_unix import HerdrUnixClient

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    expected_fp = generate_self_signed(cert_path, key_path)

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

    try:
        reader, writer = await _tls_connect(port, expected_fp)
        auth_resp = await _auth_control(reader, writer, "test-secret")
        assert unwrap_result(auth_resp) == {"type": "ok", "protocol": 1}

        writer.write(encode_line(make_request("ping")))
        await writer.drain()
        ping_line = await reader.readline()
        ping_resp = decode_line(ping_line)
        assert unwrap_result(ping_resp) == {"type": "pong"}

        writer.close()
        await writer.wait_closed()
    finally:
        gateway_task.cancel()
        try:
            await gateway_task
        except asyncio.CancelledError:
            pass
        unix_server.close()
        await unix_server.wait_closed()


@pytest.mark.asyncio
async def test_wrong_token_closes_with_error(tmp_path, unix_sock_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    expected_fp = generate_self_signed(cert_path, key_path)

    port = _free_port()
    cfg = GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=port,
        token="correct-token",
        herdr_socket=unix_sock_path,
        cert_path=cert_path,
        key_path=key_path,
    )

    def herdr_factory():
        raise AssertionError("herdr should not be called on failed auth")

    gateway_task = asyncio.create_task(serve_gateway(cfg, herdr_factory))
    await asyncio.sleep(0.05)

    try:
        reader, writer = await _tls_connect(port, expected_fp)
        line = await _auth_control(reader, writer, "wrong-token")
        assert "error" in line
        assert line["error"]["code"] == "auth_failed"

        eof_line = await reader.readline()
        assert not eof_line
    finally:
        gateway_task.cancel()
        try:
            await gateway_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_push_session_registers_in_hub(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    expected_fp = generate_self_signed(cert_path, key_path)

    port = _free_port()
    cfg = GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=port,
        token="push-token",
        herdr_socket="/tmp/unused.sock",
        cert_path=cert_path,
        key_path=key_path,
    )

    hub = PushHub()
    gateway_task = asyncio.create_task(serve_gateway(cfg, lambda: None, push_hub=hub))
    await asyncio.sleep(0.05)

    try:
        reader, writer = await _tls_connect(port, expected_fp)
        writer.write(
            encode_line(make_request("bridge.auth", {"token": "push-token", "role": "push"}, "auth_1")),
        )
        await writer.drain()
        auth_line = await reader.readline()
        assert unwrap_result(decode_line(auth_line)) == {"type": "ok", "protocol": 1}

        await asyncio.sleep(0.05)
        assert hub.count == 1

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        assert hub.count == 0
    finally:
        gateway_task.cancel()
        try:
            await gateway_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_observe_pane_with_push_pump(tmp_path):
    from plugin.test_push_pump import EventCollectingHub, FakeHerdrClient, FakeHerdrSubscriber
    from src.plugin.gateway.push_pump import PushPump

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    expected_fp = generate_self_signed(cert_path, key_path)

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

    try:
        reader, writer = await _tls_connect(port, expected_fp)
        await _auth_control(reader, writer, "observe-token")

        writer.write(
            encode_line(
                make_request("bridge.observe_pane", {"pane_id": "w1:p1", "enable": True}),
            ),
        )
        await writer.drain()
        line = await reader.readline()
        resp = decode_line(line)
        assert unwrap_result(resp) == {"type": "ok"}

        ev = await hub.wait_event("bridge.terminal_output", timeout=2)
        assert ev["data"]["pane_id"] == "w1:p1"
        assert ev["data"]["text"] == "hello"

        writer.close()
        await writer.wait_closed()
    finally:
        gateway_task.cancel()
        pump_task.cancel()
        for task in (gateway_task, pump_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_bridge_method_returns_not_implemented(tmp_path, unix_sock_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    expected_fp = generate_self_signed(cert_path, key_path)

    port = _free_port()
    cfg = GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=port,
        token="bridge-token",
        herdr_socket=unix_sock_path,
        cert_path=cert_path,
        key_path=key_path,
    )

    gateway_task = asyncio.create_task(serve_gateway(cfg, lambda: None))
    await asyncio.sleep(0.05)

    try:
        reader, writer = await _tls_connect(port, expected_fp)
        await _auth_control(reader, writer, "bridge-token")

        writer.write(encode_line(make_request("bridge.foo", {})))
        await writer.drain()
        line = await reader.readline()
        resp = decode_line(line)
        assert resp["error"]["code"] == "not_implemented"
        assert "bridge.foo" in resp["error"]["message"]

        writer.close()
        await writer.wait_closed()
    finally:
        gateway_task.cancel()
        try:
            await gateway_task
        except asyncio.CancelledError:
            pass


def test_load_gateway_config(tmp_path):
    cfg_file = tmp_path / "gateway.yaml"
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("cert")
    key.write_text("key")
    cfg_file.write_text(
        f"""
gateway:
  listen_host: 0.0.0.0
  listen_port: 4242
  token: my-token
  herdr_socket: /tmp/herdr.sock
  cert_path: {cert}
  key_path: {key}
"""
    )
    cfg = load_gateway_config(cfg_file)
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 4242
    assert cfg.token == "my-token"
    assert cfg.herdr_socket == "/tmp/herdr.sock"
    assert cfg.cert_path == cert.resolve()
    assert cfg.key_path == key.resolve()
