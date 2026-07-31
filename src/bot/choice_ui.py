"""Interactive approval choices for blocked Herdr agents."""

from __future__ import annotations

import base64
import logging
from typing import Any

import discord

from src.bot.operators import is_operator
from src.bot.terminal_view import get_terminal_state

log = logging.getLogger(__name__)


def blocked_view(remote_id: str, pane_id: str) -> discord.ui.View:
    """Create the approval view shown when a Pane agent is blocked."""
    return BlockedChoiceView(remote_id, pane_id)


async def clear_choice_message(thread: Any, pane_id: str, *, note: str | None = None) -> None:
    """Remove components from the pending choice message, if any."""
    state = get_terminal_state(thread, pane_id)
    mid = state.choice_message_id
    state.choice_message_id = None
    state.choice_fingerprint = None
    if mid is None:
        return
    try:
        msg = await thread.fetch_message(mid)
        await msg.edit(content=note or "_(choice dismissed)_", view=None)
    except Exception:  # noqa: BLE001
        log.debug("failed clearing choice message %s", mid, exc_info=True)


async def ensure_choice_message(
    thread: Any,
    *,
    remote_id: str,
    pane_id: str,
    fingerprint: str,
    content: str,
) -> int | None:
    """Post Yes/No/Custom once per fingerprint under the current live session."""
    state = get_terminal_state(thread, pane_id)
    if state.choice_fingerprint == fingerprint and state.choice_message_id is not None:
        return state.choice_message_id
    if state.choice_message_id is not None:
        await clear_choice_message(thread, pane_id)
    try:
        msg = await thread.send(content, view=blocked_view(remote_id, pane_id))
    except discord.HTTPException:
        log.exception("failed sending choice UI for %s:%s", remote_id, pane_id)
        return None
    state.choice_message_id = int(msg.id)
    state.choice_fingerprint = fingerprint
    return state.choice_message_id


class BlockedChoiceView(discord.ui.View):
    """Layout-only view. Clicks are handled solely by PersistentChoiceButton.

    Having both View button callbacks and DynamicItem callbacks caused Yes to
    send ``y`` twice (appearing as ``yy`` in the Pane).
    """

    def __init__(self, remote_id: str, pane_id: str) -> None:
        super().__init__(timeout=None)
        self.remote_id = remote_id
        self.pane_id = pane_id
        for action, label, style in (
            ("yes", "✅ Yes", discord.ButtonStyle.success),
            ("no", "❌ No", discord.ButtonStyle.danger),
            ("custom", "📝 Custom", discord.ButtonStyle.secondary),
        ):
            self.add_item(
                discord.ui.Button(
                    label=label,
                    style=style,
                    custom_id=_choice_custom_id(remote_id, pane_id, action),
                ),
            )


async def _check_operator(interaction: discord.Interaction[Any]) -> bool:
    """Reject choice interactions from Discord users without operator access."""
    config = getattr(getattr(interaction, "client", None), "config", None)
    member = getattr(interaction, "user", None)
    if config is not None and member is not None and is_operator(member, config.operators):
        return True
    await interaction.response.send_message("Operator permission is required.", ephemeral=True)
    return False


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
    """Sole click handler for choice buttons (including after Bot restart)."""

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
        if not interaction.response.is_done():
            await interaction.response.send_message("Could not send input to the Pane.", ephemeral=True)
        return

    channel = getattr(interaction, "channel", None)
    if channel is not None:
        state = get_terminal_state(channel, pane_id)
        state.choice_message_id = None
        state.choice_fingerprint = None

    if getattr(interaction, "message", None) is not None:
        await interaction.response.edit_message(
            content=f"✅ Sent `{text}` → `{remote_id}:{pane_id}`",
            view=None,
        )
    else:
        await interaction.response.send_message(f"Sent to `{remote_id}:{pane_id}`.", ephemeral=True)
