"""Dual TLS client for Gateway control RPC and push events."""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("src.bot.gateway_client")

from src.bot.registry import RemoteRecord
from src.shared.fingerprint import cert_sha256_fingerprint, fingerprints_match
from src.shared.ndjson import (
    HerdrProtocolError,
    decode_line,
    encode_line,
    make_request,
    unwrap_result,
)


class TlsFingerprintError(Exception):
    """Peer certificate fingerprint does not match the pinned remote value."""


class GatewayClient:
    """Maintain control + push TLS sessions to a Gateway remote.

    ``on_control_ready`` runs after every successful control connection, so
    callers can re-issue ``observe_pane`` for mapped panes. Observe state is
    intentionally not retained by the Gateway client itself.
    """

    def __init__(
        self,
        remote: RemoteRecord,
        on_event: Callable[[dict], Awaitable[None]],
        *,
        on_control_ready: Callable[[], Awaitable[None]] | None = None,
        min_backoff: float = 0.5,
        max_backoff: float = 30.0,
        control_heartbeat: float = 15.0,
        push_heartbeat: float = 15.0,
    ) -> None:
        self._remote = remote
        self._on_event = on_event
        self._on_control_ready = on_control_ready
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._control_heartbeat = control_heartbeat
        # The gateway sends a `{"type":"ping"}` frame on every push session every
        # ``push_heartbeat`` seconds. A read stall of >2.5x that interval means the
        # peer is gone (half-open TCP) and the loop must reconnect — previously a
        # half-open push session blocked ``readline()`` forever and terminal
        # updates silently stopped reaching the bot.
        self._push_read_timeout = push_heartbeat * 2.5

        self._stopped = asyncio.Event()
        self._control_ready = asyncio.Event()
        self._control_lost = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._control_reader: asyncio.StreamReader | None = None
        self._control_writer: asyncio.StreamWriter | None = None
        self._push_reader: asyncio.StreamReader | None = None
        self._push_writer: asyncio.StreamWriter | None = None
        self._control_task: asyncio.Task[None] | None = None
        self._push_task: asyncio.Task[None] | None = None
        self._fingerprint_error: TlsFingerprintError | None = None

    async def start(self) -> None:
        """Open control and push connections, authenticate, and run reconnect loops."""
        self._stopped.clear()
        self._fingerprint_error = None
        self._control_task = asyncio.create_task(
            self._control_reconnect_loop(),
            name=f"gateway-control-{self._remote.id}",
        )
        self._push_task = asyncio.create_task(
            self._push_reconnect_loop(),
            name=f"gateway-push-{self._remote.id}",
        )

    async def stop(self) -> None:
        """Stop reconnect loops and close both connections."""
        self._stopped.set()
        self._control_lost.set()
        tasks = [t for t in (self._control_task, self._push_task) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._control_task = None
        self._push_task = None
        await self._close_control()
        await self._close_push()
        self._control_ready.clear()

    async def request(self, method: str, params: dict | None = None) -> Any:
        """Send an NDJSON RPC on the control connection and return the result."""
        await self._wait_control_ready(timeout=30.0)
        async with self._control_lock:
            if self._control_writer is None or self._control_writer.is_closing():
                raise ConnectionError("control connection not available")
            if self._control_reader is None:
                raise ConnectionError("control connection not available")
            req = make_request(method, params)
            try:
                self._control_writer.write(encode_line(req))
                await self._control_writer.drain()
                # Bound the RPC response wait: a half-open control connection
                # (TCP up, gateway handler gone) would otherwise block
                # ``readline()`` forever and wedge the whole reconcile loop.
                line = await asyncio.wait_for(
                    self._control_reader.readline(),
                    timeout=self._control_heartbeat * 2,
                )
                if not line:
                    self._mark_control_lost()
                    raise ConnectionError("control connection closed")
                return unwrap_result(decode_line(line))
            except asyncio.TimeoutError:
                log.warning("control RPC %s timed out; marking control lost", method)
                self._mark_control_lost()
                raise ConnectionError(f"control RPC {method} timed out")
            except (ConnectionError, OSError, HerdrProtocolError):
                self._mark_control_lost()
                raise
            except Exception:
                self._mark_control_lost()
                raise

    async def observe_pane(self, pane_id: str, enable: bool) -> Any:
        """Enable or disable Gateway-local terminal observe for a mapped pane."""
        return await self.request(
            "bridge.observe_pane",
            {"pane_id": pane_id, "enable": enable},
        )

    async def send_input(self, pane_id: str, text: str, *, keys: list[str] | None = None) -> Any:
        """Type text into a Herdr Pane, then submit with Enter.

        On Herdr 0.7:
        - trailing ``\\n`` only inserts a line break (does not run the command)
        - ``submit: true`` is accepted but does not execute
        - reliable path: ``pane.send_text`` then ``pane.send_keys`` ``Enter``
        """
        keys = list(keys or [])
        wants_enter = any(
            str(key).lower() in {"enter", "return", "\r", "\n", "c-m"} for key in keys
        ) or not keys
        extra_keys = [
            key
            for key in keys
            if str(key).lower() not in {"enter", "return", "\r", "\n", "c-m"}
        ]
        text = text.rstrip("\r\n")
        result: Any = None
        if text:
            result = await self.request("pane.send_text", {"pane_id": pane_id, "text": text})
        if extra_keys:
            result = await self.request(
                "pane.send_keys",
                {"pane_id": pane_id, "keys": extra_keys},
            )
        if wants_enter:
            result = await self.request(
                "pane.send_keys",
                {"pane_id": pane_id, "keys": ["Enter"]},
            )
        return result

    async def send_interrupt(self, pane_id: str) -> Any:
        """Cancel the current agent/shell turn in a Pane.

        Prefers ``agent.send_keys`` (``esc``) when an agent owns the pane; falls
        back to raw ``pane.send_keys`` with ``esc`` then ``ctrl+c``.
        """
        try:
            return await self.request(
                "agent.send_keys",
                {"target": pane_id, "keys": ["esc"]},
            )
        except Exception:  # noqa: BLE001
            pass
        result = await self.request(
            "pane.send_keys",
            {"pane_id": pane_id, "keys": ["esc"]},
        )
        try:
            result = await self.request(
                "pane.send_keys",
                {"pane_id": pane_id, "keys": ["ctrl+c"]},
            )
        except Exception:  # noqa: BLE001
            pass
        return result

    def _mark_control_lost(self) -> None:
        self._control_ready.clear()
        self._control_lost.set()

    async def _wait_control_ready(self, *, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._fingerprint_error is not None:
                raise self._fingerprint_error
            if self._control_ready.is_set():
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for control connection")
            await asyncio.sleep(min(0.05, remaining))

    async def _control_reconnect_loop(self) -> None:
        backoff = self._min_backoff
        while not self._stopped.is_set():
            self._control_lost.clear()
            try:
                reader, writer = await self._connect("control")
                self._control_reader = reader
                self._control_writer = writer
                self._control_ready.set()
                if self._on_control_ready is not None:
                    try:
                        await self._on_control_ready()
                    except Exception:  # noqa: BLE001
                        # Connection is healthy even if a restore RPC fails; Runtime
                        # logs individual observe failures and the caller may retry.
                        pass
                backoff = self._min_backoff
                await self._monitor_control_connection()
            except TlsFingerprintError as exc:
                self._fingerprint_error = exc
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            finally:
                await self._close_control()
                self._control_ready.clear()

            if self._stopped.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _monitor_control_connection(self) -> None:
        """Detect an idle control disconnect with periodic Gateway pings."""
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._control_lost.wait(),
                    timeout=self._control_heartbeat,
                )
                return
            except TimeoutError:
                pass

            # A write/read round trip detects a peer that closed while the Bot
            # had no user RPCs to send. request() marks the connection lost on
            # every transport or protocol failure.
            await self.request("ping")

    async def _push_reconnect_loop(self) -> None:
        backoff = self._min_backoff
        while not self._stopped.is_set():
            try:
                reader, writer = await self._connect("push")
                self._push_reader = reader
                self._push_writer = writer
                backoff = self._min_backoff
                log.info(
                    "push session connected for %s (heartbeat %.0fs, stall timeout %.0fs)",
                    self._remote.id,
                    self._push_read_timeout / 2.5,
                    self._push_read_timeout,
                )
                await self._read_push_events(reader)
            except TlsFingerprintError as exc:
                self._fingerprint_error = exc
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("push session failed for %s: %s", self._remote.id, exc)
            finally:
                await self._close_push()

            if self._stopped.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _read_push_events(self, reader: asyncio.StreamReader) -> None:
        while not self._stopped.is_set():
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=self._push_read_timeout,
                )
            except asyncio.TimeoutError:
                # No heartbeat frame arrived — the gateway died or the connection
                # went half-open. Bail so the reconnect loop opens a fresh one.
                log.warning(
                    "push session stalled %.0fs for %s — reconnecting",
                    self._push_read_timeout,
                    self._remote.id,
                )
                break
            if not line:
                break
            try:
                event = decode_line(line)
            except HerdrProtocolError:
                continue
            if event.get("type") == "ping":
                continue
            try:
                await self._on_event(event)
            except Exception:  # noqa: BLE001
                continue

    async def _connect(self, role: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self._tls_connect()
        try:
            await self._authenticate(reader, writer, role)
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            raise
        return reader, writer

    async def _tls_connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        reader, writer = await asyncio.open_connection(
            self._remote.host,
            self._remote.port,
            ssl=ctx,
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        if ssl_obj is None:
            writer.close()
            await writer.wait_closed()
            raise ConnectionError("TLS handshake did not produce ssl_object")

        der = ssl_obj.getpeercert(binary_form=True)
        if der is None:
            writer.close()
            await writer.wait_closed()
            raise ConnectionError("no peer certificate")

        actual = cert_sha256_fingerprint(der)
        if not fingerprints_match(self._remote.fingerprint, actual):
            writer.close()
            await writer.wait_closed()
            raise TlsFingerprintError(
                f"certificate fingerprint mismatch for remote {self._remote.id!r}",
            )
        return reader, writer

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        role: str,
    ) -> Any:
        writer.write(
            encode_line(
                make_request(
                    "bridge.auth",
                    {"token": self._remote.token, "role": role},
                ),
            ),
        )
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise ConnectionError("connection closed during auth")
        return unwrap_result(decode_line(line))

    async def _close_control(self) -> None:
        writer = self._control_writer
        self._control_reader = None
        self._control_writer = None
        if writer is not None and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _close_push(self) -> None:
        writer = self._push_writer
        self._push_reader = None
        self._push_writer = None
        if writer is not None and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
