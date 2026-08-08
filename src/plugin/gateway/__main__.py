"""Gateway plugin entry point: ``python -m src.plugin.gateway``."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from pathlib import Path

from .config import load_gateway_config
from .herdr_unix import HerdrUnixClient
from .push_pump import PushPump
from .server import PushHub, serve_gateway


def _config_path() -> Path:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "config.yaml"
    return Path.home() / ".config" / "herdr-discord-bridge" / "config.yaml"


def _herdr_factory(socket_path: str) -> Callable[[], HerdrUnixClient]:
    def factory() -> HerdrUnixClient:
        client = HerdrUnixClient()
        client._path = socket_path  # noqa: SLF001
        return client

    return factory


async def _run() -> None:
    cfg = load_gateway_config(_config_path())
    hub = PushHub()
    factory = _herdr_factory(cfg.herdr_socket)
    pump = PushPump(hub, cfg.herdr_socket, herdr_factory=factory)

    pump_task = asyncio.create_task(pump.run())
    try:
        await serve_gateway(cfg, factory, push_hub=hub, push_pump=pump)
    finally:
        pump_task.cancel()
        await pump.shutdown()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task


def main() -> None:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
