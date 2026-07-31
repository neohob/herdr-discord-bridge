"""Edit a Discord message to simulate a live terminal pane.

Legacy text-channel model — TODO(Task 10): use src.bot.terminal_view in Pane Threads.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from src.bot.config import BridgeConfig

log = logging.getLogger(__name__)

DISCORD_MSG_LIMIT = 1900  # leave room under 2000


class TerminalSimulator:
    def __init__(
        self,
        channel: discord.TextChannel,
        *,
        remote_id: str,
        pane_id: str,
        bridge: BridgeConfig,
        message_id: int | None = None,
    ):
        self.channel = channel
        self.remote_id = remote_id
        self.pane_id = pane_id
        self.bridge = bridge
        self.message_id = message_id
        self.terminal_msg: discord.Message | None = None
        self.output_buffer: list[str] = []
        self.current_status = "unknown"
        self.last_update = 0.0
        self._pending = False

    @property
    def max_lines(self) -> int:
        return self.bridge.terminal.max_lines

    @property
    def edit_cooldown(self) -> float:
        return self.bridge.terminal.edit_cooldown

    async def ensure_message(self) -> discord.Message:
        if self.terminal_msg is not None:
            return self.terminal_msg
        if self.message_id:
            try:
                self.terminal_msg = await self.channel.fetch_message(self.message_id)
                return self.terminal_msg
            except discord.NotFound:
                self.message_id = None
        content = self._render("Waiting for output...")
        self.terminal_msg = await self.channel.send(content)
        self.message_id = self.terminal_msg.id
        self.last_update = time.time()
        return self.terminal_msg

    async def set_output(self, text: str, *, force: bool = False) -> None:
        lines = text.splitlines()
        if len(lines) > self.max_lines:
            lines = lines[-self.max_lines :]
        self.output_buffer = lines
        await self._maybe_flush(force=force)

    async def append_output(self, text: str) -> None:
        self.output_buffer.extend(text.splitlines())
        if len(self.output_buffer) > self.max_lines:
            self.output_buffer = self.output_buffer[-self.max_lines :]
        await self._maybe_flush()

    async def update_status(self, status: str, *, force: bool = True) -> None:
        self.current_status = status or "unknown"
        await self._maybe_flush(force=force)

    def _emoji(self) -> str:
        return self.bridge.status_emoji.get(self.current_status, self.bridge.status_emoji.get("unknown", "❓"))

    def _render(self, extra: str | None = None) -> str:
        body_lines = list(self.output_buffer)
        if extra:
            body_lines.append(extra)
        body = "\n".join(body_lines)
        header = f"{self._emoji()} [{self.remote_id}:{self.pane_id}] {self.current_status}"
        content = f"```\n{header}\n{'─' * 40}\n{body}\n```"
        if len(content) > DISCORD_MSG_LIMIT:
            # Trim body until it fits.
            while body_lines and len(content) > DISCORD_MSG_LIMIT:
                body_lines = body_lines[1:]
                body = "\n".join(body_lines)
                content = f"```\n{header}\n{'─' * 40}\n{body}\n```"
        return content

    async def _maybe_flush(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_update) < self.edit_cooldown:
            self._pending = True
            return
        await self._flush()

    async def _flush(self) -> None:
        self._pending = False
        msg = await self.ensure_message()
        content = self._render()
        try:
            await msg.edit(content=content)
            self.last_update = time.time()
        except discord.HTTPException as exc:
            log.warning("terminal edit failed %s/%s: %s", self.remote_id, self.pane_id, exc)

    async def flush_if_pending(self) -> None:
        if self._pending:
            await self._flush()
