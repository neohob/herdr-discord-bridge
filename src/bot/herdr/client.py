"""Async Herdr NDJSON client over an SSH-relayed Unix socket stream."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.bot.herdr.models import PaneInfo, WorkspaceInfo, extract_list
from src.bot.herdr.protocol import (
    HerdrProtocolError,
    decode_line,
    encode_line,
    make_request,
    unwrap_result,
)
from src.bot.ssh.manager import RemoteSession

log = logging.getLogger(__name__)


class HerdrClient:
    """One request/response connection per call (matches herdr ApiClient style)."""

    def __init__(self, session: RemoteSession, timeout: float = 30.0):
        self.session = session
        self.timeout = timeout

    @property
    def remote_id(self) -> str:
        return self.session.id

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        req = make_request(method, params)
        proc = await self.session.open_herdr_channel()
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None
            proc.stdin.write(encode_line(req))
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            if not line:
                stderr = b""
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                raise HerdrProtocolError(
                    f"empty response for {method} on {self.remote_id}: {stderr.decode(errors='replace')}"
                )
            payload = decode_line(line)
            return unwrap_result(payload)
        finally:
            proc.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass

    async def ping(self) -> Any:
        return await self.request("ping")

    async def session_snapshot(self) -> Any:
        return await self.request("session.snapshot")

    async def workspace_list(self) -> list[WorkspaceInfo]:
        result = await self.request("workspace.list")
        return [WorkspaceInfo.from_dict(x) for x in extract_list(result, "workspaces", "workspace_list")]

    async def pane_list(self, workspace_id: str | None = None) -> list[PaneInfo]:
        params: dict[str, Any] = {}
        if workspace_id:
            params["workspace_id"] = workspace_id
        result = await self.request("pane.list", params)
        return [PaneInfo.from_dict(x) for x in extract_list(result, "panes", "pane_list")]

    async def pane_get(self, pane_id: str) -> PaneInfo:
        result = await self.request("pane.get", {"pane_id": pane_id})
        if isinstance(result, dict):
            pane = result.get("pane") or result.get("workspace") or result
            if isinstance(pane, dict) and "pane_id" in pane:
                return PaneInfo.from_dict(pane)
            if isinstance(pane, dict):
                return PaneInfo.from_dict(pane)
        raise HerdrProtocolError(f"unexpected pane.get result: {result!r}")

    async def pane_read(
        self,
        pane_id: str,
        *,
        source: str = "recent",
        lines: int = 80,
        strip_ansi: bool = True,
    ) -> dict[str, Any]:
        result = await self.request(
            "pane.read",
            {
                "pane_id": pane_id,
                "source": source,
                "lines": lines,
                "strip_ansi": strip_ansi,
            },
        )
        if isinstance(result, dict):
            return result.get("read") or result
        raise HerdrProtocolError(f"unexpected pane.read result: {result!r}")

    async def pane_send_text(self, pane_id: str, text: str) -> Any:
        return await self.request("pane.send_text", {"pane_id": pane_id, "text": text})

    async def pane_send_keys(self, pane_id: str, keys: list[str]) -> Any:
        return await self.request("pane.send_keys", {"pane_id": pane_id, "keys": keys})

    async def pane_send_input(
        self,
        pane_id: str,
        text: str = "",
        keys: list[str] | None = None,
    ) -> Any:
        params: dict[str, Any] = {"pane_id": pane_id}
        if text:
            params["text"] = text
        if keys:
            params["keys"] = keys
        return await self.request("pane.send_input", params)

    async def pane_close(self, pane_id: str) -> Any:
        return await self.request("pane.close", {"pane_id": pane_id})

    async def pane_split(
        self,
        target_pane_id: str,
        direction: str = "right",
        *,
        focus: bool = False,
    ) -> PaneInfo:
        result = await self.request(
            "pane.split",
            {"target_pane_id": target_pane_id, "direction": direction, "focus": focus},
        )
        if isinstance(result, dict):
            pane = result.get("pane") or result
            if isinstance(pane, dict):
                return PaneInfo.from_dict(pane)
        raise HerdrProtocolError(f"unexpected pane.split result: {result!r}")

    async def workspace_create(self, cwd: str | None = None, focus: bool = False) -> Any:
        params: dict[str, Any] = {"focus": focus}
        if cwd:
            params["cwd"] = cwd
        return await self.request("workspace.create", params)

    async def agent_prompt(self, agent_name: str, text: str, **extra: Any) -> Any:
        params: dict[str, Any] = {"agent": agent_name, "text": text, **extra}
        # Newer herdr may use different param names; raw request stays flexible.
        return await self.request("agent.prompt", params)

    async def agent_list(self, workspace_id: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if workspace_id:
            params["workspace_id"] = workspace_id
        return await self.request("agent.list", params)
