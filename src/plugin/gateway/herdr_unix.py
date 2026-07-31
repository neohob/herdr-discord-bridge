"""Herdr NDJSON client and event subscriber over a local Unix socket."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ._lib import (
    HerdrProtocolError,
    decode_line,
    encode_line,
    make_request,
    unwrap_result,
)


class HerdrUnixClient:
    """One request/response connection per call (matches herdr ApiClient style)."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._path: str | None = None
        self.timeout = timeout

    async def connect(self, path: str) -> None:
        self._path = path

    async def request(self, method: str, params: dict | None = None) -> Any:
        if not self._path:
            raise RuntimeError("HerdrUnixClient is not connected")
        req = make_request(method, params)
        reader, writer = await asyncio.open_unix_connection(self._path)
        try:
            writer.write(encode_line(req))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line:
                raise HerdrProtocolError(f"empty response for {method}")
            return unwrap_result(decode_line(line))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        self._path = None


class HerdrUnixSubscriber:
    """Long-lived connection for events.subscribe stream."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.timeout = timeout

    async def start(self, path: str, subscriptions: list[dict]) -> None:
        reader, writer = await asyncio.open_unix_connection(path)
        self._reader = reader
        self._writer = writer
        req = make_request("events.subscribe", {"subscriptions": subscriptions})
        writer.write(encode_line(req))
        await writer.drain()
        ack_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        if not ack_line:
            raise HerdrProtocolError("subscription closed before ack")
        unwrap_result(decode_line(ack_line))

    async def recv_event(self) -> dict:
        if self._reader is None:
            raise RuntimeError("HerdrUnixSubscriber is not started")
        line = await asyncio.wait_for(self._reader.readline(), timeout=self.timeout)
        if not line:
            raise HerdrProtocolError("subscription EOF")
        payload = decode_line(line)
        if "event" not in payload:
            raise HerdrProtocolError("event frame missing 'event' key")
        return payload

    def __aiter__(self) -> AsyncIterator[dict]:
        return self

    async def __anext__(self) -> dict:
        try:
            return await self.recv_event()
        except HerdrProtocolError as exc:
            if str(exc) == "subscription EOF":
                raise StopAsyncIteration from exc
            raise

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._reader = None
        self._writer = None
