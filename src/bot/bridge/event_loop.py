"""Per-remote herdr event handling + pane output polling."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from src.bot.bridge.buttons import blocked_view
from src.bot.herdr.client import HerdrClient
from src.bot.herdr.events import HerdrEventStream
from src.bot.herdr.models import PaneInfo

if TYPE_CHECKING:
    from src.bot.bridge.channel_manager import ChannelManager
    from src.bot.config import AppConfig
    from src.bot.ssh.manager import RemoteSession

log = logging.getLogger(__name__)


class RemoteBridgeLoop:
    def __init__(
        self,
        session: RemoteSession,
        *,
        config: AppConfig,
        channels: ChannelManager,
    ):
        self.session = session
        self.config = config
        self.channels = channels
        self.client = HerdrClient(session)
        self._poll_task: asyncio.Task[None] | None = None
        self._revisions: dict[str, int] = {}
        self._stream: HerdrEventStream | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        await self.client.ping()
        await self.channels.sync_remote(self.client)
        # Subscribe to lifecycle; add status filters for known panes.
        subs = None
        stream = HerdrEventStream(
            self.session,
            subscriptions=subs,
            on_event=self._on_event,
        )
        for pm in self.channels.mapping.all_panes(self.session.id):
            stream.add_pane_status_subscription(pm.pane_id)
        self._stream = stream
        stream.start()
        self._poll_task = asyncio.create_task(self._poll_outputs(), name=f"poll-{self.session.id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._stream:
            await self._stream.stop()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def _on_event(self, event: str, data: dict[str, Any]) -> None:
        remote_id = self.session.id
        log.debug("event %s %s %s", remote_id, event, data.keys())
        if event in {"pane_created", "pane.created"}:
            pane_data = data.get("pane") if isinstance(data.get("pane"), dict) else data
            pane = PaneInfo.from_dict(pane_data)
            await self.channels.ensure_pane_channel(remote_id, pane)
            if self._stream:
                self._stream.add_pane_status_subscription(pane.pane_id)
            return

        if event in {"pane_closed", "pane.closed", "pane_exited", "pane.exited"}:
            pane_id = str(data.get("pane_id") or (data.get("pane") or {}).get("pane_id") or "")
            if pane_id:
                await self.channels.on_pane_closed(remote_id, pane_id)
            return

        if event in {"pane.agent_status_changed", "pane_agent_status_changed"}:
            pane_id = str(data.get("pane_id") or "")
            status = str(data.get("agent_status") or "unknown")
            if not pane_id:
                return
            try:
                pane = await self.client.pane_get(pane_id)
                pane.agent_status = status
            except Exception:  # noqa: BLE001
                pane = PaneInfo(pane_id=pane_id, workspace_id="", agent_status=status, label=pane_id)
            await self.channels.ensure_pane_channel(remote_id, pane)
            sim = self.channels.get_terminal(remote_id, pane_id)
            if sim:
                await sim.update_status(status, force=True)
            if status == "blocked":
                await self._notify_blocked(remote_id, pane_id)
            return

    async def _notify_blocked(self, remote_id: str, pane_id: str) -> None:
        pm = self.channels.mapping.get_pane(remote_id, pane_id)
        if not pm:
            return
        channel = self.channels.guild.get_channel(pm.channel_id)
        if channel is None:
            return
        view = blocked_view(remote_id, pane_id)
        await channel.send(
            f"🔴 **blocked** — `{remote_id}:{pane_id}` needs a decision",
            view=view,
        )

    async def _poll_outputs(self) -> None:
        interval = self.config.bridge.terminal.poll_interval
        while not self._stop.is_set():
            try:
                panes = await self.client.pane_list()
                for pane in panes:
                    await self._refresh_pane_output(pane)
                for sim in list(self.channels.terminals.values()):
                    if sim.remote_id == self.session.id:
                        await sim.flush_if_pending()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("output poll failed on %s", self.session.id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _refresh_pane_output(self, pane: PaneInfo) -> None:
        try:
            read = await self.client.pane_read(pane.pane_id, source="recent", lines=self.config.bridge.terminal.max_lines)
        except Exception:  # noqa: BLE001
            return
        text = str(read.get("text") or "")
        revision = int(read.get("revision") or pane.revision or 0)
        prev = self._revisions.get(pane.pane_id)
        if prev is not None and revision == prev:
            return
        self._revisions[pane.pane_id] = revision
        sim = self.channels.get_terminal(self.session.id, pane.pane_id)
        if sim is None:
            await self.channels.ensure_pane_channel(self.session.id, pane)
            sim = self.channels.get_terminal(self.session.id, pane.pane_id)
        if sim is None:
            return
        if pane.agent_status:
            sim.current_status = pane.agent_status
        await sim.set_output(text)
