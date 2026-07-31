"""Interactive approval choices for blocked Herdr agents."""

from __future__ import annotations

import base64
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
        super().__init__(timeout=None)
        self.remote_id = remote_id
        self.pane_id = pane_id
        self.add_item(_ChoiceButton(remote_id, pane_id, "yes", "✅ Yes", discord.ButtonStyle.success))
        self.add_item(_ChoiceButton(remote_id, pane_id, "no", "❌ No", discord.ButtonStyle.danger))
        self.add_item(_ChoiceButton(remote_id, pane_id, "custom", "📝 Custom", discord.ButtonStyle.secondary))

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        return await _check_operator(interaction)


async def _check_operator(interaction: discord.Interaction[Any]) -> bool:
    """Reject choice interactions from Discord users without operator access."""
    config = getattr(getattr(interaction, "client", None), "config", None)
    member = getattr(interaction, "user", None)
    if config is not None and member is not None and is_operator(member, config.operators):
        return True
    await interaction.response.send_message("Operator permission is required.", ephemeral=True)
    return False


class _ChoiceButton(discord.ui.Button[BlockedChoiceView]):
    def __init__(
        self,
        remote_id: str,
        pane_id: str,
        action: str,
        label: str,
        style: discord.ButtonStyle,
    ) -> None:
        super().__init__(
            label=label,
            style=style,
            custom_id=_choice_custom_id(remote_id, pane_id, action),
        )
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


def _encode_component(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_component(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _choice_custom_id(remote_id: str, pane_id: str, action: str) -> str:
    return f"herdr-choice:{_encode_component(remote_id)}:{_encode_component(pane_id)}:{action}"


class PersistentChoiceButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"herdr-choice:(?P<remote>[A-Za-z0-9_-]+):(?P<pane>[A-Za-z0-9_-]+):(?P<action>yes|no|custom)",
):
    """Route persistent choice buttons after a Bot process restart."""

    def __init__(self, remote_id: str, pane_id: str, action: str) -> None:
        label, style = {
            "yes": ("✅ Yes", discord.ButtonStyle.success),
            "no": ("❌ No", discord.ButtonStyle.danger),
            "custom": ("📝 Custom", discord.ButtonStyle.secondary),
        }[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=_choice_custom_id(remote_id, pane_id, action),
            ),
        )
        self.remote_id = remote_id
        self.pane_id = pane_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction[Any], item: Any, match: Any) -> PersistentChoiceButton:
        return cls(
            _decode_component(match["remote"]),
            _decode_component(match["pane"]),
            match["action"],
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        if not await _check_operator(interaction):
            return
        if self.action == "custom":
            await interaction.response.send_modal(_CustomInputModal(self.remote_id, self.pane_id))
            return
        await _send_choice(
            interaction,
            self.remote_id,
            self.pane_id,
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
