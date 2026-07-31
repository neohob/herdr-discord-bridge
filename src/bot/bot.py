"""Discord bot entrypoint for the Gateway-backed Herdr Discord Bridge."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

from src.bot.chat_input import on_message as forward_chat_input
from src.bot.choice_ui import PersistentChoiceButton
from src.bot.commands import register_commands
from src.bot.config import AppConfig, load_config
from src.bot.gateway_client import GatewayClient
from src.bot.lifecycle import (
    on_guild_channel_delete as handle_channel_delete,
    on_raw_thread_delete as handle_raw_thread_delete,
    on_thread_delete as handle_thread_delete,
)
from src.bot.mapping import MappingStore
from src.bot.registry import RemoteRegistry
from src.bot.runtime import Runtime

log = logging.getLogger(__name__)


class BridgeBot(commands.Bot):
    def __init__(self, config: AppConfig):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.mapping = MappingStore(config.mapping_path)
        self.registry = RemoteRegistry(config.registry_path)
        self.runtime: Runtime | None = None
        self.add_listener(self._forward_chat_input, "on_message")
        self.add_listener(self._handle_channel_delete, "on_guild_channel_delete")
        self.add_listener(self._handle_thread_delete, "on_thread_delete")
        self.add_listener(self._handle_raw_thread_delete, "on_raw_thread_delete")

    def require_client(self, remote_id: str) -> GatewayClient:
        client = self.runtime.clients.get(remote_id) if self.runtime else None
        if client is None:
            raise RuntimeError(f"remote `{remote_id}` is not connected")
        return client

    async def _forward_chat_input(self, message: discord.Message) -> None:
        await forward_chat_input(self, message)

    async def _handle_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await handle_channel_delete(self, channel)

    async def _handle_thread_delete(self, thread: discord.Thread) -> None:
        await handle_thread_delete(self, thread)

    async def _handle_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        await handle_raw_thread_delete(self, payload)

    async def setup_hook(self) -> None:
        # Dynamic persistent components recover blocked-choice callbacks from
        # their encoded remote/pane IDs after a process restart.
        self.add_dynamic_items(PersistentChoiceButton)
        register_commands(self.tree, self)
        if self.config.discord.guild_id:
            guild = discord.Object(id=self.config.discord.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)
        guild = self.get_guild(self.config.discord.guild_id) if self.config.discord.guild_id else None
        if guild is None and self.guilds:
            guild = self.guilds[0]
            log.warning("guild_id unset/mismatch; using first guild %s", guild.id)
        if guild is None:
            log.error("no guild available; invite the bot and set discord.guild_id")
            return

        if self.runtime is None:
            self.runtime = Runtime(
                self.config,
                guild,
                registry=self.registry,
                mapping=self.mapping,
            )
        try:
            await self.runtime.start()
        except Exception:  # noqa: BLE001
            log.exception("failed to start Gateway runtime")
            await self._alert("Gateway runtime failed to start; see bridge logs.")
            return

        online = ", ".join(f"`{rid}`" for rid in self.runtime.clients) or "_none_"
        await self._alert(f"Herdr Discord Bridge ready. Gateway remotes: {online}")

    async def _alert(self, message: str) -> None:
        log.info(message)
        cid = self.config.discord.home_channel_id
        if not cid:
            return
        channel = self.get_channel(cid)
        if channel is None:
            try:
                channel = await self.fetch_channel(cid)
            except Exception:  # noqa: BLE001
                return
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(message)
            except Exception:  # noqa: BLE001
                log.exception("alert send failed")

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.stop()
        await super().close()


def _configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "bridge.log", encoding="utf-8"),
        ],
    )


def main() -> None:
    config = load_config()
    _configure_logging(config.log_dir)
    bot = BridgeBot(config)
    bot.run(config.discord.token, log_handler=None)


if __name__ == "__main__":
    main()
