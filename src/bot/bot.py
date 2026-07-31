"""Discord bot entrypoint for Herdr Discord Bridge."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

from src.bot.bridge.channel_manager import ChannelManager
from src.bot.bridge.commands import register_commands
from src.bot.bridge.event_loop import RemoteBridgeLoop
from src.bot.bridge.mapping import MappingStore
from src.bot.config import AppConfig, load_config
from src.bot.herdr.client import HerdrClient
from src.bot.ssh.manager import SshManager

log = logging.getLogger(__name__)


class BridgeBot(commands.Bot):
    def __init__(self, config: AppConfig):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = False
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.ssh = SshManager(config.remotes)
        self.mapping = MappingStore(config.mapping_path)
        self.channels: ChannelManager | None = None
        self.herdr_clients: dict[str, HerdrClient] = {}
        self.loops: dict[str, RemoteBridgeLoop] = {}

    def require_client(self, remote_id: str) -> HerdrClient:
        client = self.herdr_clients.get(remote_id)
        if client is None:
            raise RuntimeError(f"remote `{remote_id}` is not connected")
        return client

    async def setup_hook(self) -> None:
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

        self.channels = ChannelManager(guild, self.config, self.mapping)
        results = await self.ssh.connect_all()
        for session in self.ssh.all():
            err = results.get(session.id)
            if err is not None:
                await self._alert(f"SSH failed for `{session.id}`: `{err}`")
                continue
            client = HerdrClient(session)
            try:
                pong = await client.ping()
                log.info("herdr ping %s -> %s", session.id, pong)
            except Exception as exc:  # noqa: BLE001
                await self._alert(f"Herdr ping failed for `{session.id}`: `{exc}`")
                continue
            self.herdr_clients[session.id] = client
            loop = RemoteBridgeLoop(session, config=self.config, channels=self.channels)
            self.loops[session.id] = loop
            try:
                await loop.start()
            except Exception as exc:  # noqa: BLE001
                log.exception("bridge loop start failed %s", session.id)
                await self._alert(f"Bridge start failed `{session.id}`: `{exc}`")

        online = ", ".join(f"`{rid}`" for rid in self.herdr_clients) or "_none_"
        await self._alert(f"Herdr Discord Bridge ready. Remotes: {online}")

        if self.config.bridge.sync_interval > 0:
            self.loop.create_task(self._periodic_sync())

    async def _periodic_sync(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(self.config.bridge.sync_interval)
            if self.channels is None:
                continue
            for rid, client in list(self.herdr_clients.items()):
                try:
                    await self.channels.sync_remote(client)
                except Exception:  # noqa: BLE001
                    log.exception("periodic sync failed %s", rid)

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
        for loop in self.loops.values():
            await loop.stop()
        await self.ssh.close_all()
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
