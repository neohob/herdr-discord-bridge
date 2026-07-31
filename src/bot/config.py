"""Load and resolve bridge configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_PATTERN.sub(repl, value)


def _expand_path(value: str) -> Path:
    return Path(_expand_env(value)).expanduser().resolve()


def _walk_expand(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, list):
        return [_walk_expand(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _walk_expand(v) for k, v in obj.items()}
    return obj


@dataclass(slots=True)
class DiscordConfig:
    token: str
    guild_id: int
    home_channel_id: int = 0


@dataclass(slots=True)
class TerminalConfig:
    max_lines: int = 50
    edit_cooldown: float = 2.0
    poll_interval: float = 2.0


@dataclass(slots=True)
class BridgeConfig:
    category_prefix: str = "remote:"
    terminal: TerminalConfig = field(default_factory=TerminalConfig)
    status_emoji: dict[str, str] = field(
        default_factory=lambda: {
            "idle": "🟢",
            "working": "🔵",
            "blocked": "🔴",
            "done": "✅",
            "shell": "⚪",
            "unknown": "❓",
        }
    )
    sync_interval: int = 300
    read_only: bool = False


@dataclass(slots=True)
class OperatorsConfig:
    require_manage_guild: bool = True
    user_ids: list[int] = field(default_factory=list)
    role_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class RemoteSeed:
    id: str
    host: str
    port: int
    token: str
    fingerprint: str


@dataclass(slots=True)
class AppConfig:
    discord: DiscordConfig
    bridge: BridgeConfig
    operators: OperatorsConfig
    seed_remotes: list[RemoteSeed] | None
    registry_path: Path
    mapping_path: Path
    log_dir: Path


def _parse_operators(raw: dict[str, Any]) -> OperatorsConfig:
    ops = raw.get("operators") or {}
    return OperatorsConfig(
        require_manage_guild=bool(ops.get("require_manage_guild", True)),
        user_ids=[int(x) for x in ops.get("user_ids") or []],
        role_ids=[int(x) for x in ops.get("role_ids") or []],
    )


def _parse_seed_remotes(raw: dict[str, Any]) -> list[RemoteSeed] | None:
    items = raw.get("seed_remotes")
    if items is None:
        return None
    seeds: list[RemoteSeed] = []
    for item in items:
        seeds.append(
            RemoteSeed(
                id=str(item["id"]),
                host=str(item["host"]),
                port=int(item.get("port", 8787)),
                token=str(item["token"]),
                fingerprint=str(item["fingerprint"]),
            )
        )
    return seeds


def load_config(path: str | Path | None = None) -> AppConfig:
    root = Path(__file__).resolve().parent.parent.parent
    cfg_path = Path(path) if path else Path(os.environ.get("BRIDGE_CONFIG", root / "config.yaml"))
    if not cfg_path.is_file():
        example = root / "config.example.yaml"
        raise FileNotFoundError(
            f"config not found: {cfg_path}. Copy {example} to config.yaml and configure the bot."
        )

    raw = _walk_expand(yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {})
    discord_raw = raw.get("discord") or {}
    token = discord_raw.get("token") or os.environ.get("DISCORD_TOKEN", "")
    if not token:
        raise ValueError("discord.token / DISCORD_TOKEN is required")

    bridge_raw = raw.get("bridge") or {}
    term_raw = bridge_raw.get("terminal") or {}
    terminal = TerminalConfig(
        max_lines=int(term_raw.get("max_lines", 50)),
        edit_cooldown=float(term_raw.get("edit_cooldown", 2.0)),
        poll_interval=float(term_raw.get("poll_interval", 2.0)),
    )
    bridge = BridgeConfig(
        category_prefix=str(bridge_raw.get("category_prefix", "remote:")),
        terminal=terminal,
        status_emoji={
            **BridgeConfig().status_emoji,
            **(bridge_raw.get("status_emoji") or {}),
        },
        sync_interval=int(bridge_raw.get("sync_interval", 300)),
        read_only=bool(bridge_raw.get("read_only", False)),
    )

    registry_path = root / "cache" / "remotes.json"
    mapping_path = root / "cache" / "mapping.json"
    log_dir = root / "logs"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        discord=DiscordConfig(
            token=token,
            guild_id=int(discord_raw.get("guild_id") or 0),
            home_channel_id=int(discord_raw.get("home_channel_id") or 0),
        ),
        bridge=bridge,
        operators=_parse_operators(raw),
        seed_remotes=_parse_seed_remotes(raw),
        registry_path=registry_path,
        mapping_path=mapping_path,
        log_dir=log_dir,
    )
