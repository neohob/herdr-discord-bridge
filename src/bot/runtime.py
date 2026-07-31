"""Gateway-backed runtime for Discord Remote Channels and Pane Threads."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import discord

from src.bot.choice_detect import choice_fingerprint, is_blocked_status
from src.bot.choice_ui import clear_choice_message, ensure_choice_message
from src.bot.config import AppConfig
from src.bot.discord_map import ensure_pane_thread, thread_name_for
from src.bot.gateway_client import GatewayClient
from src.bot.herdr.models import PaneInfo
from src.bot.mapping import MappingStore, PaneMapping
from src.bot.pane_lifecycle import is_pane_missing_error, retire_mapped_pane
from src.bot.registry import RemoteRecord, RemoteRegistry
from src.bot.terminal_view import apply_terminal_view, get_terminal_state

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
ControlReadyHandler = Callable[[], Awaitable[None]]


class GatewayClientFactory(Protocol):
    def __call__(
        self,
        remote: RemoteRecord,
        on_event: EventHandler,
        *,
        on_control_ready: ControlReadyHandler | None = None,
    ) -> GatewayClient: ...


class Runtime:
    """Own Gateway clients and route their push events to Discord."""

    def __init__(
        self,
        config: AppConfig,
        guild: discord.Guild | Any,
        *,
        registry: RemoteRegistry | None = None,
        mapping: MappingStore | None = None,
        client_factory: GatewayClientFactory = GatewayClient,
    ) -> None:
        self.config = config
        self.guild = guild
        self.registry = registry or RemoteRegistry(config.registry_path)
        self.mapping = mapping or MappingStore(config.mapping_path)
        self._client_factory = client_factory
        self.clients: dict[str, GatewayClient] = {}

    def load_registry(self) -> None:
        """Import previously unseen configured seed remotes into the registry."""
        self.registry.load()
        for seed in self.config.seed_remotes or []:
            if self.registry.get(seed.id) is None:
                self.registry.upsert(
                    RemoteRecord(
                        id=seed.id,
                        host=seed.host,
                        port=seed.port,
                        token=seed.token,
                        fingerprint=seed.fingerprint,
                    ),
                )

    async def start(self) -> None:
        """Start one GatewayClient per Remote that has a bound Remote Channel."""
        self.load_registry()
        for remote in self.registry.list():
            if remote.channel_id is None:
                continue
            await self.start_remote(remote)

    async def start_remote(self, remote: RemoteRecord) -> GatewayClient:
        """Start a bound remote once and return its client."""
        existing = self.clients.get(remote.id)
        if existing is not None:
            return existing

        async def on_event(event: dict[str, Any]) -> None:
            await self.handle_push(remote.id, event)

        async def on_control_ready() -> None:
            await self.reenable_observe(remote.id)

        client = self._client_factory(
            remote,
            on_event,
            on_control_ready=on_control_ready,
        )
        self.clients[remote.id] = client
        try:
            await client.start()
        except Exception:
            self.clients.pop(remote.id, None)
            raise
        return client

    async def stop(self) -> None:
        """Stop every running Gateway client."""
        clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                log.exception("failed stopping Gateway client")

    async def handle_push(self, remote_id: str, event: dict[str, Any]) -> None:
        """Route a Gateway push envelope for ``remote_id``."""
        event_name = _normalize_event_name(str(event.get("event") or ""))
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}

        if event_name == "bridge.terminal_output":
            await self._handle_terminal_output(remote_id, data)
        elif event_name == "pane.agent_status_changed":
            await self._handle_status_change(remote_id, data)
        elif event_name in {"pane.created", "pane.closed", "pane.exited"}:
            await self._handle_lifecycle(remote_id, event_name, data)
        elif event_name == "pane.moved":
            await self._handle_pane_moved(remote_id, data)
        else:
            log.debug("ignored Gateway event %s for %s", event_name, remote_id)

    async def reenable_observe(self, remote_id: str) -> None:
        """Re-enable terminal observation after a Gateway control reconnect."""
        client = self.clients.get(remote_id)
        if client is None:
            return
        for pane in list(self.mapping.all_panes(remote_id)):
            try:
                await client.observe_pane(pane.pane_id, True)
            except Exception as exc:  # noqa: BLE001
                if is_pane_missing_error(exc):
                    await retire_mapped_pane(
                        guild=self.guild,
                        mapping=self.mapping,
                        client=client,
                        remote_id=remote_id,
                        pane_id=pane.pane_id,
                        reason=f"Herdr pane {pane.pane_id} not found on reconnect",
                    )
                    log.info("retired stale mapping %s:%s after observe failed", remote_id, pane.pane_id)
                    continue
                log.exception("failed re-enabling observe for %s:%s", remote_id, pane.pane_id)

    async def _handle_terminal_output(self, remote_id: str, data: dict[str, Any]) -> None:
        pane_id = _pane_id(data)
        if not pane_id:
            return
        pane = self.mapping.get_pane(remote_id, pane_id)
        if pane is None:
            log.debug("terminal output for unmapped pane %s:%s", remote_id, pane_id)
            return
        thread = await self._fetch_thread(pane.thread_id)
        if thread is None:
            return
        text = str(data.get("text") or "")
        status = str(data.get("agent_status") or pane.agent_status or "unknown")
        message_id = await apply_terminal_view(
            thread,
            pane_id,
            text,
            status,
            self.config.bridge,
            remote_id=remote_id,
            message_id=pane.terminal_message_id,
        )
        if message_id is not None:
            self.mapping.set_terminal_message(remote_id, pane_id, message_id)
        await self._sync_choice_ui(
            thread,
            remote_id=remote_id,
            pane_id=pane_id,
            status=status,
            text=text,
            revision=data.get("revision"),
        )

    async def _handle_status_change(self, remote_id: str, data: dict[str, Any]) -> None:
        pane_id = _pane_id(data)
        if not pane_id:
            return
        pane = self.mapping.get_pane(remote_id, pane_id)
        if pane is None:
            log.debug("status update for unmapped pane %s:%s", remote_id, pane_id)
            return

        status = str(data.get("agent_status") or data.get("status") or "unknown")
        pane.agent_status = status
        self.mapping.upsert_pane(pane)

        thread = await self._fetch_thread(pane.thread_id)
        if thread is not None:
            pane_info = PaneInfo(
                pane_id=pane_id,
                workspace_id="",
                label=str(data.get("label") or pane.label or pane_id),
                agent=str(data.get("agent") or ""),
                agent_status=status,
            )
            try:
                await thread.edit(name=thread_name_for(pane_info, self.config.bridge))
            except discord.HTTPException:
                log.exception("failed renaming pane thread %s:%s", remote_id, pane_id)

            state = get_terminal_state(thread, pane_id)
            await self._sync_choice_ui(
                thread,
                remote_id=remote_id,
                pane_id=pane_id,
                status=status,
                text=state.text,
                revision=None,
            )

    async def _sync_choice_ui(
        self,
        thread: Any,
        *,
        remote_id: str,
        pane_id: str,
        status: str,
        text: str,
        revision: Any,
    ) -> None:
        fp = choice_fingerprint(status=status, text=text, revision=revision)
        if fp is None:
            state = get_terminal_state(thread, pane_id)
            if state.choice_message_id is not None and not is_blocked_status(status):
                await clear_choice_message(thread, pane_id, note="_(no longer waiting)_")
            return
        reason = "agent blocked" if is_blocked_status(status) else "检测到确认提示"
        await ensure_choice_message(
            thread,
            remote_id=remote_id,
            pane_id=pane_id,
            fingerprint=fp,
            content=(
                f"🔘 **请选择**（点这里，不是打字）— `{remote_id}:{pane_id}`\n"
                f"{reason}。Yes=`y` / No=`n` / Custom=自定义"
            ),
        )

    async def _handle_lifecycle(
        self,
        remote_id: str,
        event_name: str,
        data: dict[str, Any],
    ) -> None:
        remote = self.registry.get(remote_id)
        if remote is None or remote.channel_id is None:
            return
        channel = await self._fetch_channel(remote.channel_id)
        pane_id = _pane_id(data) or "unknown"
        client = self.clients.get(remote_id)

        if event_name in {"pane.closed", "pane.exited"}:
            await retire_mapped_pane(
                guild=self.guild,
                mapping=self.mapping,
                client=client,
                remote_id=remote_id,
                pane_id=pane_id,
                reason=f"Herdr pane {pane_id} {event_name.split('.')[-1]}",
            )
            note = f"Pane `{pane_id}` closed — Discord thread retired."
        else:
            mapped = await self._auto_map_created_pane(remote_id, remote, channel, data)
            note = (
                f"Pane `{pane_id}` created — thread ready."
                if mapped
                else f"Pane `{pane_id}` created. Run `/herdr sync` if no thread appeared."
            )

        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(note)
            except discord.HTTPException:
                log.exception("failed sending lifecycle notification for %s", remote_id)

    async def _handle_pane_moved(self, remote_id: str, data: dict[str, Any]) -> None:
        """Retire the old Pane id and map the destination after a Herdr move."""
        previous = str(data.get("previous_pane_id") or "")
        client = self.clients.get(remote_id)
        if previous:
            await retire_mapped_pane(
                guild=self.guild,
                mapping=self.mapping,
                client=client,
                remote_id=remote_id,
                pane_id=previous,
                reason=f"Herdr pane moved away from {previous}",
            )
        remote = self.registry.get(remote_id)
        if remote is None or remote.channel_id is None:
            return
        channel = await self._fetch_channel(remote.channel_id)
        await self._auto_map_created_pane(remote_id, remote, channel, data)

    async def _auto_map_created_pane(
        self,
        remote_id: str,
        remote: RemoteRecord,
        channel: Any,
        data: dict[str, Any],
    ) -> bool:
        """Create/bind a Discord thread for a newly created Herdr Pane."""
        if self.config.bridge.read_only or channel is None:
            return False
        pane = PaneInfo.from_dict(_pane_payload(data))
        if not pane.pane_id:
            # Some pushes only carry pane_id; enrich from live pane.get.
            pane_id = _pane_id(data)
            if not pane_id:
                return False
            client = self.clients.get(remote_id)
            if client is not None:
                try:
                    details = await client.request("pane.get", {"pane_id": pane_id})
                    if isinstance(details, dict):
                        pane = PaneInfo.from_dict(_pane_payload(details))
                except Exception:  # noqa: BLE001
                    pane = PaneInfo(pane_id=pane_id, workspace_id="")
            else:
                pane = PaneInfo(pane_id=pane_id, workspace_id="")
        if not pane.pane_id:
            return False
        try:
            await ensure_pane_thread(
                channel,
                pane,
                remote_id=remote_id,
                mapping=self.mapping,
                bridge_cfg=self.config.bridge,
            )
            client = self.clients.get(remote_id)
            if client is not None:
                await client.observe_pane(pane.pane_id, True)
            return True
        except Exception:  # noqa: BLE001
            log.exception("failed auto-mapping created pane %s:%s", remote_id, pane.pane_id)
            return False

    async def _fetch_thread(self, thread_id: int) -> discord.Thread | Any | None:
        thread = self.guild.get_thread(thread_id)
        if isinstance(thread, discord.Thread) or _is_thread_like(thread):
            return thread
        try:
            thread = await self.guild.fetch_channel(thread_id)
        except discord.NotFound:
            log.warning("mapped Discord thread %s no longer exists", thread_id)
            return None
        except discord.HTTPException:
            log.exception("failed fetching mapped Discord thread %s", thread_id)
            return None
        return thread if isinstance(thread, discord.Thread) or _is_thread_like(thread) else None

    async def _fetch_channel(self, channel_id: int) -> discord.abc.GuildChannel | Any | None:
        channel = self.guild.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.guild.fetch_channel(channel_id)
        except discord.HTTPException:
            log.exception("failed fetching Remote Channel %s", channel_id)
            return None

    async def _notify_thread(self, thread: Any | None, message: str) -> None:
        if thread is None:
            return
        try:
            await thread.send(message)
        except discord.HTTPException:
            log.exception("failed sending pane notification")


_EVENT_NAME_ALIASES = {
    "pane_created": "pane.created",
    "pane_closed": "pane.closed",
    "pane_exited": "pane.exited",
    "pane_moved": "pane.moved",
    "pane_updated": "pane.updated",
    "pane_focused": "pane.focused",
    "pane_agent_status_changed": "pane.agent_status_changed",
    "pane_agent_detected": "pane.agent_detected",
    "workspace_created": "workspace.created",
    "workspace_closed": "workspace.closed",
}


def _normalize_event_name(name: str) -> str:
    """Herdr may emit ``pane_created`` while we subscribe as ``pane.created``."""
    text = (name or "").strip()
    if not text or "." in text:
        return text
    return _EVENT_NAME_ALIASES.get(text, text)


def _pane_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested ``{pane: {...}}`` event payloads for ``PaneInfo.from_dict``."""
    pane = data.get("pane")
    if isinstance(pane, dict):
        merged = dict(pane)
        for key, value in data.items():
            if key == "pane":
                continue
            merged.setdefault(key, value)
        return merged
    return data


def _pane_id(data: dict[str, Any]) -> str:
    payload = _pane_payload(data)
    return str(payload.get("pane_id") or payload.get("id") or "")


def _is_thread_like(value: Any) -> bool:
    """Permit small Discord fakes in runtime tests without weakening production checks."""
    return value is not None and hasattr(value, "edit") and hasattr(value, "send")
