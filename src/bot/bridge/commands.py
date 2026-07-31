"""Slash command router: /herdr <action>."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from src.bot.bot import BridgeBot

log = logging.getLogger(__name__)


def register_commands(tree: app_commands.CommandTree, bot: BridgeBot) -> None:
    @tree.command(name="herdr", description="Herdr Discord Bridge operations")
    @app_commands.describe(
        action="Action to run",
        remote="Remote id (optional if used inside a pane channel)",
        pane="Pane id (optional if used inside a pane channel)",
        text="Text for send / prompt",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="status", value="status"),
            app_commands.Choice(name="sync", value="sync"),
            app_commands.Choice(name="send", value="send"),
            app_commands.Choice(name="read", value="read"),
            app_commands.Choice(name="close", value="close"),
            app_commands.Choice(name="help", value="help"),
        ]
    )
    async def herdr(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        remote: str | None = None,
        pane: str | None = None,
        text: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        act = action.value
        try:
            if act == "help":
                await interaction.followup.send(_help_text(), ephemeral=True)
                return
            if act == "status":
                await interaction.followup.send(await _cmd_status(bot), ephemeral=True)
                return
            if act == "sync":
                await interaction.followup.send(await _cmd_sync(bot, remote), ephemeral=True)
                return

            remote_id, pane_id = _resolve_target(bot, interaction, remote, pane)
            if act == "send":
                if not text:
                    await interaction.followup.send("`text` is required for send", ephemeral=True)
                    return
                client = bot.require_client(remote_id)
                await client.pane_send_input(pane_id, text=text, keys=["enter"])
                await interaction.followup.send(f"Sent to `{remote_id}:{pane_id}`", ephemeral=True)
                return
            if act == "read":
                client = bot.require_client(remote_id)
                read = await client.pane_read(pane_id, lines=40)
                body = str(read.get("text") or "")[-1500:]
                await interaction.followup.send(f"```\n{body}\n```", ephemeral=True)
                return
            if act == "close":
                client = bot.require_client(remote_id)
                await client.pane_close(pane_id)
                await interaction.followup.send(f"Closed `{remote_id}:{pane_id}`", ephemeral=True)
                return
            await interaction.followup.send(f"Unknown action: {act}", ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("/herdr %s failed", act)
            await interaction.followup.send(f"Error: {exc}", ephemeral=True)


def _help_text() -> str:
    return (
        "**Herdr Discord Bridge**\n"
        "`/herdr status` — remotes + pane mapping\n"
        "`/herdr sync [remote]` — resync Discord channels from Herdr\n"
        "`/herdr send pane:/remote text:` — send text+Enter\n"
        "`/herdr read` — read recent pane output\n"
        "`/herdr close` — close pane\n\n"
        "Inside a pane channel, `remote`/`pane` are inferred.\n"
        "Remote setup: run `scripts/setup-remote-ssh.sh` on each Herdr host"
    )


async def _cmd_status(bot: BridgeBot) -> str:
    lines = ["**Remotes**"]
    for item in bot.ssh.status():
        mark = "online" if item["online"] else "offline"
        lines.append(f"- `{item['id']}` {item['user']}@{item['host']} — {mark}")
    lines.append("")
    lines.append("**Mapped panes**")
    panes = bot.mapping.all_panes()
    if not panes:
        lines.append("_none yet — run /herdr sync_")
    for pm in panes[:40]:
        lines.append(f"- `{pm.remote_id}:{pm.pane_id}` → <#{pm.channel_id}> [{pm.agent_status}]")
    if len(panes) > 40:
        lines.append(f"... +{len(panes) - 40} more")
    return "\n".join(lines)


async def _cmd_sync(bot: BridgeBot, remote: str | None) -> str:
    if bot.channels is None:
        raise RuntimeError("channel manager not ready")
    targets = [remote] if remote else [s.id for s in bot.ssh.all()]
    done = []
    for rid in targets:
        client = bot.require_client(rid)
        await bot.channels.sync_remote(client)
        loop = bot.loops.get(rid)
        if loop and loop._stream:
            for pm in bot.mapping.all_panes(rid):
                loop._stream.add_pane_status_subscription(pm.pane_id)
        done.append(rid)
    return "Synced: " + ", ".join(f"`{x}`" for x in done)


def _resolve_target(
    bot: BridgeBot,
    interaction: discord.Interaction,
    remote: str | None,
    pane: str | None,
) -> tuple[str, str]:
    if remote and pane:
        return remote, pane
    channel_id = interaction.channel_id
    if channel_id:
        pm = bot.mapping.find_by_channel(channel_id)
        if pm:
            return pm.remote_id, pm.pane_id
    if remote and not pane:
        raise ValueError("pane is required when not in a mapped channel")
    raise ValueError("Provide remote+pane, or run inside a mapped pane channel")
