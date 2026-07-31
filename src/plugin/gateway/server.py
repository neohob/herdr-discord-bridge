"""TLS gateway server: auth, control passthrough, push hub registration."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable
from typing import Any

from src.plugin.gateway.config import GatewayConfig
from src.plugin.gateway.tls_util import load_ssl_context_server
from src.shared.ndjson import (
    HerdrApiError,
    HerdrProtocolError,
    decode_line,
    encode_line,
)


class PushHub:
    """Minimal registry of push-session writers for downstream broadcast (Task 5)."""

    def __init__(self) -> None:
        self._writers: set[asyncio.StreamWriter] = set()

    def add(self, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)

    def remove(self, writer: asyncio.StreamWriter) -> None:
        self._writers.discard(writer)

    @property
    def count(self) -> int:
        return len(self._writers)

    async def broadcast(self, obj: dict[str, Any]) -> None:
        line = encode_line(obj)
        dead: list[asyncio.StreamWriter] = []
        for writer in self._writers:
            try:
                writer.write(line)
                await writer.drain()
            except Exception:  # noqa: BLE001
                dead.append(writer)
        for writer in dead:
            self.remove(writer)


def _error_response(req_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": req_id, "error": {"code": code, "message": message}}


async def _send_and_close(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(encode_line(payload))
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass


def _handle_bridge_method(method: str, params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    if method == "bridge.observe_pane":
        # Task 5 will implement observe; keep explicit hook here.
        return _error_response(req_id, "not_implemented", "bridge.observe_pane not yet implemented")
    return _error_response(req_id, "not_implemented", f"bridge method {method!r} not implemented")


async def _handle_control_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    herdr_factory: Callable[[], Any],
) -> None:
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            req = decode_line(line)
        except HerdrProtocolError as exc:
            writer.write(encode_line(_error_response(None, "invalid_request", str(exc))))
            await writer.drain()
            continue

        method = str(req.get("method") or "")
        req_id = req.get("id")
        params = req.get("params") or {}

        if method.startswith("bridge."):
            resp = _handle_bridge_method(method, params, req_id)
        else:
            herdr = herdr_factory()
            try:
                result = await herdr.request(method, params)
                resp = {"id": req_id, "result": result}
            except HerdrApiError as exc:
                resp = _error_response(req_id, exc.code, exc.message)
            except HerdrProtocolError as exc:
                resp = _error_response(req_id, "protocol_error", str(exc))
            except Exception as exc:  # noqa: BLE001
                resp = _error_response(req_id, "internal_error", str(exc))
            finally:
                close = getattr(herdr, "close", None)
                if close is not None:
                    maybe = close()
                    if asyncio.iscoroutine(maybe):
                        await maybe

        writer.write(encode_line(resp))
        await writer.drain()


async def _handle_push_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    push_hub: PushHub,
) -> None:
    push_hub.add(writer)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
    finally:
        push_hub.remove(writer)


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cfg: GatewayConfig,
    herdr_factory: Callable[[], Any],
    push_hub: PushHub,
) -> None:
    try:
        line = await reader.readline()
        if not line:
            return

        try:
            req = decode_line(line)
        except HerdrProtocolError:
            await _send_and_close(writer, _error_response(None, "invalid_request", "invalid auth frame"))
            return

        if req.get("method") != "bridge.auth":
            await _send_and_close(
                writer,
                _error_response(req.get("id"), "auth_required", "first frame must be bridge.auth"),
            )
            return

        params = req.get("params") or {}
        token = params.get("token")
        role = params.get("role")
        req_id = req.get("id")

        if token != cfg.token:
            await _send_and_close(writer, _error_response(req_id, "auth_failed", "invalid token"))
            return

        if role not in ("control", "push"):
            await _send_and_close(writer, _error_response(req_id, "auth_failed", "invalid role"))
            return

        writer.write(encode_line({"id": req_id, "result": {"type": "ok", "protocol": 1}}))
        await writer.drain()

        if role == "push":
            await _handle_push_session(reader, writer, push_hub)
        else:
            await _handle_control_session(reader, writer, herdr_factory)
    finally:
        if not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


async def serve_gateway(
    cfg: GatewayConfig,
    herdr_factory: Callable[[], Any],
    *,
    push_hub: PushHub | None = None,
) -> None:
    """Run the TLS gateway until cancelled."""
    hub = push_hub or PushHub()
    ssl_ctx = load_ssl_context_server(cfg.cert_path, cfg.key_path)

    async def client_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await _handle_client(reader, writer, cfg, herdr_factory, hub)

    server = await asyncio.start_server(
        client_handler,
        host=cfg.listen_host,
        port=cfg.listen_port,
        ssl=ssl_ctx,
    )

    async with server:
        await server.serve_forever()
