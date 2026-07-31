"""Interactive approval choices for blocked Herdr agents."""

from __future__ import annotations

import logging
from typing import Any

import discord

from src.bot.operators import is_operator

log = logging.getLogger(__name__)


def blocked_view(remote_id: str, pane_id: str) -> discord.ui.View:
    """Create the approval view shown when a Pane agent is blocked."""
    return BlockedChoiceView(remote_id, pane_id)


class BlockedChoiceView(discord.ui.View):
    def __init__(self, remote_id: str, pane_id: str) -> None:
        super().__init__(timeout=3600)
        self.remote_id = remote_id
        self.pane_id = pane_id
        self.add_item(_ChoiceButton("yes", "✅ Yes", discord.ButtonStyle.success))
        self.add_item(_ChoiceButton("no", "❌ No", discord.ButtonStyle.danger))
        self.add_item(_ChoiceButton("custom", "📝 Custom", discord.ButtonStyle.secondary))

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        config = getattr(getattr(interaction, "client", None), "config", None)
        member = getattr(interaction, "user", None)
        if config is not None and member is not None and is_operator(member, config.operators):
            return True
        await interaction.response.send_message("Operator permission is required.", ephemeral=True)
        return False


class _ChoiceButton(discord.ui.Button[BlockedChoiceView]):
    def __init__(self, action: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"herdr-choice:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if not isinstance(view, BlockedChoiceView):
            return
        if self.action == "custom":
            await interaction.response.send_modal(_CustomInputModal(view.remote_id, view.pane_id))
            return
        await _send_choice(
            interaction,
            view.remote_id,
            view.pane_id,
            "y" if self.action == "yes" else "n",
        )


class _CustomInputModal(discord.ui.Modal, title="Send to pane"):
    def __init__(self, remote_id: str, pane_id: str) -> None:
        super().__init__()
        self.remote_id = remote_id
        self.pane_id = pane_id
        self.text = discord.ui.TextInput(label="Text", style=discord.TextStyle.paragraph, required=True)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction[Any]) -> None:
        await _send_choice(interaction, self.remote_id, self.pane_id, str(self.text.value))


async def _send_choice(
    interaction: discord.Interaction[Any],
    remote_id: str,
    pane_id: str,
    text: str,
) -> None:
    bot = interaction.client
    try:
        client = bot.require_client(remote_id)
        await client.send_input(pane_id, text, keys=["enter"])
    except Exception:  # noqa: BLE001
        log.exception("failed sending approval to %s:%s", remote_id, pane_id)
        await interaction.response.send_message("Could not send input to the Pane.", ephemeral=True)
        return

    if getattr(interaction, "message", None) is not None:
        await interaction.response.edit_message(
            content=f"✅ Sent `{text}` → `{remote_id}:{pane_id}`",
            view=None,
        )
    else:
        await interaction.response.send_message(f"Sent to `{remote_id}:{pane_id}`.", ephemeral=True)
