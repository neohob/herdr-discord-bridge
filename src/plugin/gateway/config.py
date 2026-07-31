"""Gateway plugin configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
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
class GatewayConfig:
    listen_host: str
    listen_port: int
    token: str
    herdr_socket: str
    cert_path: Path
    key_path: Path


def load_gateway_config(path: str | Path) -> GatewayConfig:
    """Load gateway settings from a YAML file (top-level ``gateway:`` section)."""
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"gateway config not found: {cfg_path}")

    raw = _walk_expand(yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {})
    gw = raw.get("gateway") or raw

    token = gw.get("token") or os.environ.get("GATEWAY_TOKEN", "")
    if not token:
        raise ValueError("gateway.token / GATEWAY_TOKEN is required")

    cert_raw = gw.get("cert_path") or gw.get("tls_cert")
    key_raw = gw.get("key_path") or gw.get("tls_key")
    if not cert_raw or not key_raw:
        raise ValueError("gateway.cert_path and gateway.key_path are required")

    return GatewayConfig(
        listen_host=str(gw.get("listen_host", "127.0.0.1")),
        listen_port=int(gw.get("listen_port", 9876)),
        token=str(token),
        herdr_socket=str(gw.get("herdr_socket", "~/.config/herdr/herdr.sock")),
        cert_path=_expand_path(str(cert_raw)),
        key_path=_expand_path(str(key_raw)),
    )
