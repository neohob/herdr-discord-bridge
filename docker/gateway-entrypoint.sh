#!/usr/bin/env bash
# Gateway container entrypoint: auto-setup once, then run the TLS server.
set -euo pipefail

CONFIG_DIR="${HERDR_PLUGIN_CONFIG_DIR:-/config}"
CFG="${CONFIG_DIR}/config.yaml"
CERT="${CONFIG_DIR}/gateway.crt"
KEY="${CONFIG_DIR}/gateway.key"
LISTEN_HOST="${GATEWAY_LISTEN_HOST:-0.0.0.0}"
LISTEN_PORT="${GATEWAY_LISTEN_PORT:-9876}"
HERDR_SOCKET="${HERDR_SOCKET:-/herdr/herdr.sock}"
PUBLIC_HOST="${GATEWAY_PUBLIC_HOST:-}"

mkdir -p "${CONFIG_DIR}"

cmd="${1:-start}"

do_setup() {
  python - <<'PY'
import os
import secrets
from pathlib import Path

from src.plugin.gateway.tls_util import generate_self_signed, fingerprint_from_cert_file

config_dir = Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"])
cert = config_dir / "gateway.crt"
key = config_dir / "gateway.key"
cfg = config_dir / "config.yaml"
listen_host = os.environ.get("GATEWAY_LISTEN_HOST", "0.0.0.0")
listen_port = os.environ.get("GATEWAY_LISTEN_PORT", "9876")
herdr_socket = os.environ.get("HERDR_SOCKET", "/herdr/herdr.sock")
public_host = os.environ.get("GATEWAY_PUBLIC_HOST") or "SET_ME_TO_UNRAID_IP"
token = secrets.token_urlsafe(32)
fp = generate_self_signed(cert, key)

cfg.write_text(
    f"""gateway:
  listen_host: {listen_host}
  listen_port: {listen_port}
  token: {token}
  herdr_socket: {herdr_socket}
  cert_path: {cert}
  key_path: {key}
""",
    encoding="utf-8",
)

print("")
print("=" * 60)
print(" Discord Bridge Gateway — setup complete")
print("=" * 60)
print(f"Config: {cfg}")
print(f"Cert:   {cert}")
print(f"Key:    {key}")
print("")
print("Register in Discord with /herdr register:")
print(f"  host:        {public_host}")
print(f"  port:        {listen_port}")
print(f"  token:       {token}")
print(f"  fingerprint: {fp}")
print("=" * 60)
print("")
PY
}

case "${cmd}" in
  setup)
    do_setup
    ;;
  start)
    if [[ ! -f "${CFG}" || ! -f "${CERT}" || ! -f "${KEY}" ]]; then
      echo "No gateway config found — running first-time setup..."
      do_setup
    fi
    if [[ ! -S "${HERDR_SOCKET}" ]]; then
      echo "warning: Herdr socket not found at ${HERDR_SOCKET}" >&2
      echo "warning: mount the host herdr.sock to ${HERDR_SOCKET} (DockerMan path mapping)" >&2
    fi
    echo "Starting gateway on ${LISTEN_HOST}:${LISTEN_PORT} (herdr=${HERDR_SOCKET})"
    exec python -m src.plugin.gateway
    ;;
  credentials)
    if [[ ! -f "${CFG}" ]]; then
      echo "error: no config; start the container once to generate credentials" >&2
      exit 1
    fi
    python - <<'PY'
import os
from pathlib import Path
import yaml
from src.plugin.gateway.tls_util import fingerprint_from_cert_file

cfg_path = Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"]) / "config.yaml"
cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["gateway"]
fp = fingerprint_from_cert_file(Path(cfg["cert_path"]))
host = os.environ.get("GATEWAY_PUBLIC_HOST") or "192.168.100.3"
print(f"host={host}")
print(f"port={cfg['listen_port']}")
print(f"token={cfg['token']}")
print(f"fingerprint={fp}")
PY
    ;;
  *)
    exec "$@"
    ;;
esac
