"""Interactive Discord components for pane approvals and navigation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import ui

if TYPE_CHECKING:
    from src.bot.bot import BridgeBot

log = logging.getLogger(__name__)


def blocked_view(remote_id: str, pane_id: str) -> ui.View:
    view = ui.View(timeout=3600)
    view.add_item(_ApproveButton(remote_id, pane_id, "yes", "✅ Yes", discord.ButtonStyle.success))
    view.add_item(_ApproveButton(remote_id, pane_id, "no", "❌ No", discord.ButtonStyle.danger))
    view.add_item(_ApproveButton(remote_id, pane_id, "custom", "📝 Custom", discord.ButtonStyle.secondary))
    return view


class _ApproveButton(ui.Button["ui.View"]):
    def __init__(
        self,
        remote_id: str,
        pane_id: str,
        action: str,
        label: str,
        style: discord.ButtonStyle,
    ):
        super().__init__(
            label=label,
            style=style,
            custom_id=f"approve:{remote_id}:{pane_id}:{action}",
        )
        self.remote_id = remote_id
        self.pane_id = pane_id
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if self.action == "custom":
            await interaction.response.send_modal(_CustomInputModal(self.remote_id, self.pane_id))
            return
        text = "y" if self.action == "yes" else "n"
        await _send_to_pane(bot, interaction, self.remote_id, self.pane_id, text)


class _CustomInputModal(ui.Modal, title="Send to pane"):
    def __init__(self, remote_id: str, pane_id: str):
        super().__init__()
        self.remote_id = remote_id
        self.pane_id = pane_id
        self.text = ui.TextInput(label="Text", style=discord.TextStyle.paragraph, required=True)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _send_to_pane(interaction.client, interaction, self.remote_id, self.pane_id, str(self.text.value))


async def _send_to_pane(
    bot: discord.Client,
    interaction: discord.Interaction,
    remote_id: str,
    pane_id: str,
    text: str,
) -> None:
    from src.bot.bot import BridgeBot

    if not isinstance(bot, BridgeBot):
        await interaction.response.send_message("Bot not ready", ephemeral=True)
        return
    client = bot.herdr_clients.get(remote_id)
    if client is None:
        await interaction.response.send_message(f"Remote `{remote_id}` offline", ephemeral=True)
        return
    try:
        await client.pane_send_input(pane_id, text=text, keys=["enter"])
    except Exception as exc:  # noqa: BLE001
        log.exception("approve send failed")
        await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)
        return
    if interaction.response.is_done():
        await interaction.followup.send(f"Sent to `{remote_id}:{pane_id}`", ephemeral=True)
    else:
        await interaction.response.edit_message(
            content=f"✅ Sent `{text}` → `{remote_id}:{pane_id}`",
            view=None,
        )
