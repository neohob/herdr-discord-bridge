"""Temporary command registration placeholder.

Task 11 owns the Gateway-backed slash-command implementation.  Keeping this
module importable lets the bot start without exposing the removed SSH commands.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import app_commands

if TYPE_CHECKING:
    from src.bot.bot import BridgeBot

log = logging.getLogger(__name__)


def register_commands(tree: app_commands.CommandTree, bot: BridgeBot) -> None:
    """Register no commands until Task 11 replaces the SSH-era command tree."""
    del tree, bot
    log.warning("slash commands are disabled until Task 11")
