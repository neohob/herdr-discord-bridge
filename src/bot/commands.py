"""Discord slash-command surface: ``/herdr`` (ops) and ``/agent`` (Pane input)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands

from src.bot.chat_input import format_agent_anchor, forward_pane_input
from src.bot.discord_map import ensure_pane_thread, ensure_remote_channel
from src.bot.herdr.models import PaneInfo, extract_list
from src.bot.mapping import MappingStore
from src.bot.operators import is_operator
from src.bot.pane_lifecycle import retire_mapped_pane
from src.bot.registry import RemoteRecord, RemoteRegistry

if TYPE_CHECKING:
    from src.bot.bot import BridgeBot
    from src.bot.runtime import Runtime


def resolve_remote_from_channel(
    channel_id: int | None,
    registry: RemoteRegistry,
    mapping: MappingStore,
) -> str | None:
    """Return the Remote represented by a Discord Remote Channel."""
    if channel_id is None:
        return None
    for remote in registry.list():
        if remote.channel_id == channel_id:
            return remote.id
    for remote_id, remote_mapping in mapping.remotes.items():
        if remote_mapping.channel_id == channel_id:
            return remote_id
    return None


def resolve_pane_from_thread(
    thread_id: int | None,
    mapping: MappingStore,
) -> tuple[str, str] | None:
    """Return ``(remote_id, pane_id)`` for a mapped Pane Thread."""
    if thread_id is None:
        return None
    pane = mapping.find_by_thread(thread_id)
    return (pane.remote_id, pane.pane_id) if pane else None


def _interaction_context(
    interaction: discord.Interaction[Any],
    registry: RemoteRegistry,
    mapping: MappingStore,
) -> tuple[str | None, str | None]:
    """Resolve Remote and Pane defaults from the command's channel."""
    channel_id = getattr(interaction.channel, "id", None)
    pane = resolve_pane_from_thread(channel_id, mapping)
    if pane:
        return pane
    return resolve_remote_from_channel(channel_id, registry, mapping), None


async def _respond(interaction: discord.Interaction[Any], message: str, *, ephemeral: bool = False) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(message, ephemeral=ephemeral)


def _operator(bot: BridgeBot, interaction: discord.Interaction[Any]) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and is_operator(member, bot.config.operators)


async def _require_operator(bot: BridgeBot, interaction: discord.Interaction[Any]) -> bool:
    if _operator(bot, interaction):
        return True
    await _respond(interaction, "Operator permission is required.", ephemeral=True)
    return False


def _runtime(bot: BridgeBot) -> Runtime:
    if bot.runtime is None:
        raise RuntimeError("the Bridge runtime is not ready")
    return bot.runtime


async def _remote_channel(
    interaction: discord.Interaction[Any],
    remote: RemoteRecord,
    mapping: MappingStore,
) -> discord.TextChannel:
    channel = interaction.guild.get_channel(remote.channel_id) if interaction.guild and remote.channel_id else None
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError("run this command in the Remote Channel")
    mapping.set_remote_channel(remote.id, channel.id)
    return channel


def _result_text(result: Any) -> str:
    text = str(result)
    return text if len(text) <= 1800 else f"{text[:1797]}..."


async def _stop_remote(runtime: Runtime, remote_id: str) -> None:
    client = runtime.clients.pop(remote_id, None)
    if client is not None:
        await client.stop()


async def _ensure_client(bot: BridgeBot, remote_id: str) -> Any:
    """Return a live Gateway client, starting its bound Remote when needed."""
    remote = bot.registry.get(remote_id)
    if remote is None:
        raise RuntimeError(f"remote `{remote_id}` is not registered")
    runtime = _runtime(bot)
    client = runtime.clients.get(remote_id)
    return client if client is not None else await runtime.start_remote(remote)


async def _workspace_labels(client: Any) -> dict[str, str]:
    try:
        result = await client.request("workspace.list")
    except Exception:  # noqa: BLE001
        return {}
    labels: dict[str, str] = {}
    for item in extract_list(result, "workspaces", "items"):
        workspace_id = str(item.get("workspace_id") or item.get("id") or "")
        label = str(item.get("label") or "").strip()
        if workspace_id and label:
            labels[workspace_id] = label
    return labels


async def _tab_labels(client: Any) -> dict[str, str]:
    """Map tab_id → label from live ``tab.list`` (not a static table)."""
    try:
        result = await client.request("tab.list")
    except Exception:  # noqa: BLE001
        return {}
    labels: dict[str, str] = {}
    for item in extract_list(result, "tabs", "items"):
        tab_id = str(item.get("tab_id") or item.get("id") or "")
        label = str(item.get("label") or "").strip()
        if tab_id and label:
            labels[tab_id] = label
    return labels


async def _prune_stale_panes(
    *,
    bot: BridgeBot,
    remote: RemoteRecord,
    live_pane_ids: set[str],
    guild: discord.Guild | None,
) -> int:
    """Archive/delete Discord threads for Pane ids that no longer exist on Herdr."""
    pruned = 0
    runtime = getattr(bot, "runtime", None)
    client = runtime.clients.get(remote.id) if runtime is not None else None
    for pane in list(bot.mapping.all_panes(remote.id)):
        if pane.pane_id in live_pane_ids:
            continue
        retired = await retire_mapped_pane(
            guild=guild,
            mapping=bot.mapping,
            client=client,
            remote_id=remote.id,
            pane_id=pane.pane_id,
            reason=f"Herdr pane {pane.pane_id} no longer exists",
        )
        if retired is not None:
            pruned += 1
    return pruned


async def _map_panes(
    *,
    interaction: discord.Interaction[Any],
    bot: BridgeBot,
    remote: RemoteRecord,
    panes: list[dict[str, Any]],
) -> int:
    channel = await _remote_channel(interaction, remote, bot.mapping)
    client = await _ensure_client(bot, remote.id)
    workspace_labels = await _workspace_labels(client)
    tab_labels = await _tab_labels(client)
    mapped = 0
    for pane_data in panes:
        pane = PaneInfo.from_dict(pane_data)
        if not pane.pane_id:
            continue
        pane.workspace_label = workspace_labels.get(pane.workspace_id, pane.workspace_label)
        pane.tab_label = tab_labels.get(pane.tab_id, pane.tab_label)
        await ensure_pane_thread(
            channel,
            pane,
            remote_id=remote.id,
            mapping=bot.mapping,
            bridge_cfg=bot.config.bridge,
        )
        await client.observe_pane(pane.pane_id, True)
        mapped += 1
        # Avoid Discord thread-create 429s that stretch past interaction timeouts.
        await asyncio.sleep(1.0)
    return mapped


class _RebindSelect(discord.ui.Select):
    def __init__(
        self,
        remotes: list[RemoteRecord],
        bind: Callable[[discord.Interaction[Any], str], Awaitable[None]],
    ) -> None:
        super().__init__(
            placeholder="Select an unbound Remote",
            options=[discord.SelectOption(label=remote.id, value=remote.id) for remote in remotes[:25]],
            min_values=1,
            max_values=1,
        )
        self._bind = bind

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        await self._bind(interaction, self.values[0])
        self.view.stop()


class _RebindView(discord.ui.View):
    def __init__(
        self,
        remotes: list[RemoteRecord],
        bind: Callable[[discord.Interaction[Any], str], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=120)
        self.add_item(_RebindSelect(remotes, bind))


def compose_agent_payload(command: str, text: str | None = None) -> str:
    """Join ``/agent`` command + optional trailing text for Pane send."""
    head = str(command or "").strip()
    tail = str(text or "").strip()
    if head and tail:
        return f"{head} {tail}"
    return head or tail


async def handle_agent_command(
    bot: BridgeBot,
    interaction: discord.Interaction[Any],
    command: str,
    text: str | None = None,
) -> None:
    """``/agent``: forward command (+ optional trailing text) into the Pane thread."""
    payload = compose_agent_payload(command, text)
    if not payload:
        await _respond(interaction, "command must not be empty.", ephemeral=True)
        return

    channel = interaction.channel
    thread_id = getattr(channel, "id", None)
    pane = bot.mapping.find_by_thread(thread_id)
    if pane is None:
        await _respond(
            interaction,
            "Run `/agent` inside a mapped Pane thread.",
            ephemeral=True,
        )
        return

    runtime = getattr(bot, "runtime", None)
    client = runtime.clients.get(pane.remote_id) if runtime is not None else None
    if client is None:
        await _respond(
            interaction,
            f"Remote `{pane.remote_id}` is offline.",
            ephemeral=True,
        )
        return

    # Ack Discord's 3s interaction deadline before slower Pane work.
    await _respond(interaction, "已发送", ephemeral=True)

    try:
        anchor = await channel.send(format_agent_anchor(interaction.user, payload))
    except Exception as exc:  # noqa: BLE001
        await _respond(interaction, f"Could not post anchor message: {exc}", ephemeral=True)
        return

    ok = await forward_pane_input(bot, channel, pane, payload, anchor)
    if not ok:
        await _respond(
            interaction,
            f"Failed to send to `{pane.remote_id}:{pane.pane_id}`.",
            ephemeral=True,
        )


def register_commands(tree: app_commands.CommandTree, bot: BridgeBot) -> None:
    """Register top-level ``/herdr`` and ``/agent`` slash commands."""
    root = app_commands.Group(name="herdr", description="Manage Herdr Remotes and Panes")
    pane_group = app_commands.Group(name="pane", description="Pane operations", parent=root)
    workspace_group = app_commands.Group(name="workspace", description="Workspace operations", parent=root)

    @tree.command(name="agent", description="Send an agent skill/text into this Pane thread")
    @app_commands.describe(
        command="Skill or first token, e.g. /grilling or /compact (forwarded as-is)",
        text="Optional extra text after the skill (appended with a space)",
    )
    async def agent_command(
        interaction: discord.Interaction[Any],
        command: app_commands.Range[str, 1, 6000],
        text: app_commands.Range[str, 1, 6000] | None = None,
    ) -> None:
        await handle_agent_command(
            bot,
            interaction,
            str(command),
            None if text is None else str(text),
        )

    async def bind(interaction: discord.Interaction[Any], remote_id: str) -> None:
        if not await _require_operator(bot, interaction):
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await _respond(interaction, "Rebind must be run in the target Remote Channel.", ephemeral=True)
            return
        if resolve_remote_from_channel(interaction.channel.id, bot.registry, bot.mapping):
            await _respond(interaction, "This channel is already bound to a Remote.", ephemeral=True)
            return
        remote = bot.registry.get(remote_id)
        if remote is None or remote.channel_id is not None:
            await _respond(interaction, "That Remote is no longer available for rebind.", ephemeral=True)
            return
        bot.registry.bind_channel(remote_id, interaction.channel.id)
        bot.mapping.set_remote_channel(remote_id, interaction.channel.id)
        await _runtime(bot).start_remote(remote)
        await _respond(interaction, f"Remote `{remote_id}` rebound to this channel.", ephemeral=True)

    @root.command(name="register", description="Register a Gateway Remote")
    @app_commands.describe(
        host="Gateway host or IP",
        port="Gateway TLS port",
        token="Gateway authentication token",
        fingerprint="Gateway certificate SHA-256 fingerprint",
        id="Optional stable Remote id",
        create_channel="Create a Remote Channel",
    )
    async def register(
        interaction: discord.Interaction[Any],
        host: str,
        port: app_commands.Range[int, 1, 65535],
        token: str,
        fingerprint: str,
        id: str | None = None,
        create_channel: bool = True,
    ) -> None:
        if not await _require_operator(bot, interaction):
            return
        remote_id = (id or host).strip().lower().replace(" ", "-")
        if not remote_id or not token or not fingerprint:
            await _respond(interaction, "id, token, and fingerprint must not be empty.", ephemeral=True)
            return
        remote = RemoteRecord(remote_id, host.strip(), int(port), token, fingerprint)
        bot.registry.upsert(remote)
        try:
            if create_channel:
                if interaction.guild is None:
                    raise RuntimeError("register requires a guild")
                channel = await ensure_remote_channel(
                    interaction.guild,
                    remote_id,
                    remote_id,
                    mapping=bot.mapping,
                    bridge_cfg=bot.config.bridge,
                )
            elif isinstance(interaction.channel, discord.TextChannel):
                channel = interaction.channel
            else:
                raise RuntimeError("create_channel=false requires a text channel")
            bot.registry.bind_channel(remote_id, channel.id)
            bot.mapping.set_remote_channel(remote_id, channel.id)
            remote = bot.registry.get(remote_id)
            assert remote is not None
            await _runtime(bot).start_remote(remote)
        except Exception as exc:  # credentials must never be included in an error reply
            await _respond(interaction, f"Remote registration failed: {exc}", ephemeral=True)
            return
        await _respond(interaction, f"Registered Remote `{remote_id}` in {channel.mention}.", ephemeral=True)

    @root.command(name="rebind", description="Bind an unbound Remote to this channel")
    async def rebind(interaction: discord.Interaction[Any], remote_id: str | None = None) -> None:
        if remote_id:
            await bind(interaction, remote_id)
            return
        if not await _require_operator(bot, interaction):
            return
        remotes = bot.registry.list_unbound()
        if not remotes:
            await _respond(interaction, "There are no unbound Remotes.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose the Remote to bind to this channel:",
            ephemeral=True,
            view=_RebindView(remotes, bind),
        )

    @root.command(name="remove", description="Remove a registered Remote")
    async def remove(interaction: discord.Interaction[Any], remote_id: str | None = None) -> None:
        if not await _require_operator(bot, interaction):
            return
        context_remote, _ = _interaction_context(interaction, bot.registry, bot.mapping)
        target = remote_id or context_remote
        if not target or bot.registry.get(target) is None:
            await _respond(interaction, "Specify a registered Remote or run this in its Remote Channel.", ephemeral=True)
            return
        try:
            if bot.runtime is not None:
                await _stop_remote(bot.runtime, target)
            bot.registry.remove(target)
            bot.mapping.remove_remote(target)
            await _respond(interaction, f"Removed Remote `{target}`.", ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Could not remove Remote: {exc}", ephemeral=True)

    @root.command(name="status", description="Show registered Remote status")
    async def status(interaction: discord.Interaction[Any]) -> None:
        remotes = bot.registry.list()
        if not remotes:
            await _respond(interaction, "No Remotes are registered.", ephemeral=True)
            return
        clients = bot.runtime.clients if bot.runtime else {}
        lines = [
            f"`{remote.id}` — {'connected' if remote.id in clients else 'disconnected'}, "
            f"{'bound' if remote.channel_id else 'unbound'}"
            for remote in remotes
        ]
        await _respond(interaction, "\n".join(lines), ephemeral=True)

    @root.command(name="sync", description="Map existing Herdr Panes into Threads")
    async def sync(interaction: discord.Interaction[Any]) -> None:
        if not await _require_operator(bot, interaction):
            return
        remote_id, _ = _interaction_context(interaction, bot.registry, bot.mapping)
        remote = bot.registry.get(remote_id) if remote_id else None
        if remote is None:
            await _respond(interaction, "Run Sync in a Remote Channel.", ephemeral=True)
            return
        # Sync can create many Threads and hit Discord 429s; defer before work.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            client = await _ensure_client(bot, remote.id)
            result = await client.request("pane.list")
            panes = extract_list(result, "panes", "items")
            live_ids = {
                str(item.get("pane_id") or item.get("id") or "")
                for item in panes
                if item.get("pane_id") or item.get("id")
            }
            pruned = await _prune_stale_panes(
                bot=bot,
                remote=remote,
                live_pane_ids=live_ids,
                guild=interaction.guild,
            )
            count = await _map_panes(interaction=interaction, bot=bot, remote=remote, panes=panes)
            msg = f"Synced {count} Pane(s) for `{remote.id}`."
            if pruned:
                msg += f" Pruned {pruned} stale thread(s)."
            await _respond(interaction, msg, ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Sync failed: {exc}", ephemeral=True)

    @pane_group.command(name="list", description="List Panes on this Remote")
    async def pane_list(interaction: discord.Interaction[Any]) -> None:
        remote_id, _ = _interaction_context(interaction, bot.registry, bot.mapping)
        if not remote_id:
            await _respond(interaction, "Run this in a Remote Channel or Pane Thread.", ephemeral=True)
            return
        try:
            result = await (await _ensure_client(bot, remote_id)).request("pane.list")
            await _respond(interaction, _result_text(result), ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Pane list failed: {exc}", ephemeral=True)

    @pane_group.command(name="split", description="Create a Pane by splitting the current Pane")
    async def pane_split(interaction: discord.Interaction[Any], direction: str = "horizontal") -> None:
        if not await _require_operator(bot, interaction):
            return
        remote_id, pane_id = _interaction_context(interaction, bot.registry, bot.mapping)
        if not remote_id or not pane_id:
            await _respond(interaction, "Run pane split inside a Pane Thread.", ephemeral=True)
            return
        try:
            result = await (await _ensure_client(bot, remote_id)).request(
                "pane.split", {"pane_id": pane_id, "direction": direction}
            )
            pane_data = result if isinstance(result, dict) else {}
            if pane_data.get("pane_id") or pane_data.get("id"):
                remote = bot.registry.get(remote_id)
                if remote is None:
                    raise RuntimeError(f"remote `{remote_id}` is not registered")
                await _map_panes(interaction=interaction, bot=bot, remote=remote, panes=[pane_data])
            await _respond(interaction, "Pane split.", ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Pane split failed: {exc}", ephemeral=True)

    @pane_group.command(name="close", description="Close the current Herdr Pane")
    async def pane_close(interaction: discord.Interaction[Any], pane_id: str | None = None) -> None:
        if not await _require_operator(bot, interaction):
            return
        remote_id, context_pane_id = _interaction_context(interaction, bot.registry, bot.mapping)
        target = pane_id or context_pane_id
        if not remote_id or not target:
            await _respond(interaction, "Run pane close in a Pane Thread or supply a Pane id.", ephemeral=True)
            return
        try:
            client = await _ensure_client(bot, remote_id)
            await client.request("pane.close", {"pane_id": target})
            await retire_mapped_pane(
                guild=interaction.guild,
                mapping=bot.mapping,
                client=client,
                remote_id=remote_id,
                pane_id=target,
                reason=f"Herdr pane {target} closed",
            )
            await _respond(interaction, f"Closed Pane `{target}`.", ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Pane close failed: {exc}", ephemeral=True)

    @pane_group.command(name="read", description="Read the current Pane once")
    async def pane_read(interaction: discord.Interaction[Any], pane_id: str | None = None) -> None:
        remote_id, context_pane_id = _interaction_context(interaction, bot.registry, bot.mapping)
        target = pane_id or context_pane_id
        if not remote_id or not target:
            await _respond(interaction, "Run pane read in a Pane Thread or supply a Pane id.", ephemeral=True)
            return
        try:
            result = await (await _ensure_client(bot, remote_id)).request(
                "pane.read", {"pane_id": target}
            )
            await _respond(interaction, _result_text(result), ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Pane read failed: {exc}", ephemeral=True)

    @workspace_group.command(name="list", description="List workspaces")
    async def workspace_list(interaction: discord.Interaction[Any]) -> None:
        remote_id, _ = _interaction_context(interaction, bot.registry, bot.mapping)
        if not remote_id:
            await _respond(interaction, "Run this in a Remote Channel or Pane Thread.", ephemeral=True)
            return
        try:
            result = await (await _ensure_client(bot, remote_id)).request("workspace.list")
            await _respond(interaction, _result_text(result), ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Workspace list failed: {exc}", ephemeral=True)

    @workspace_group.command(name="create", description="Create a workspace")
    async def workspace_create(interaction: discord.Interaction[Any], label: str) -> None:
        if not await _require_operator(bot, interaction):
            return
        remote_id, _ = _interaction_context(interaction, bot.registry, bot.mapping)
        if not remote_id:
            await _respond(interaction, "Run this in a Remote Channel or Pane Thread.", ephemeral=True)
            return
        try:
            result = await (await _ensure_client(bot, remote_id)).request(
                "workspace.create", {"label": label}
            )
            await _respond(interaction, _result_text(result), ephemeral=True)
        except Exception as exc:
            await _respond(interaction, f"Workspace create failed: {exc}", ephemeral=True)

    @root.command(name="help", description="Show Herdr command help")
    async def help_command(interaction: discord.Interaction[Any]) -> None:
        await _respond(
            interaction,
            "Use `/herdr register`, `rebind`, `status`, `sync`, `/herdr pane …`, "
            "or `/herdr workspace …`. In a Pane thread, `/agent` with `command` "
            "(e.g. `/grilling`) and optional `text` forwards the joined line to the Pane. "
            "Run structural operations in the Remote Channel or Pane Thread to use its context.",
            ephemeral=True,
        )

    tree.add_command(root)
