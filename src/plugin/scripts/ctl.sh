#!/usr/bin/env bash
# Herdr Discord Bridge — host plugin control (English user-facing output).
# Spec: https://herdr.dev/docs/plugins/
#
# Config  → HERDR_PLUGIN_CONFIG_DIR  (token, certs, config.yaml)
# State   → HERDR_PLUGIN_STATE_DIR   (pid, logs)
# Socket  → HERDR_SOCKET_PATH        (injected by Herdr)
# Binary  → HERDR_BIN_PATH           (preferred when calling `herdr` CLI)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Monorepo checkout: src/plugin → repo root. Standalone install: empty.
REPO_ROOT=""
if [[ -f "${PLUGIN_ROOT}/../../pyproject.toml" && -d "${PLUGIN_ROOT}/../../src/shared" ]]; then
  REPO_ROOT="$(cd "${PLUGIN_ROOT}/../.." && pwd)"
fi

DEFAULT_LISTEN_HOST="0.0.0.0"
DEFAULT_LISTEN_PORT="9876"
DEFAULT_SOCKET="${HOME}/.config/herdr/herdr.sock"

PID_FILE_NAME="gateway.pid"
LOG_FILE_NAME="gateway.log"

config_dir() {
  if [[ -n "${HERDR_PLUGIN_CONFIG_DIR:-}" ]]; then
    printf '%s\n' "${HERDR_PLUGIN_CONFIG_DIR}"
    return
  fi
  # Match Herdr's per-plugin config layout when env is missing (manual runs).
  printf '%s\n' "${HOME}/.config/herdr/plugins/config/herdr-discord-bridge"
}

state_dir() {
  if [[ -n "${HERDR_PLUGIN_STATE_DIR:-}" ]]; then
    printf '%s\n' "${HERDR_PLUGIN_STATE_DIR}"
    return
  fi
  printf '%s\n' "${HOME}/.config/herdr/plugins/state/herdr-discord-bridge"
}

pid_file() {
  printf '%s\n' "$(state_dir)/${PID_FILE_NAME}"
}

log_file() {
  printf '%s\n' "$(state_dir)/${LOG_FILE_NAME}"
}

config_file() {
  printf '%s\n' "$(config_dir)/config.yaml"
}

cert_file() {
  printf '%s\n' "$(config_dir)/gateway.crt"
}

key_file() {
  printf '%s\n' "$(config_dir)/gateway.key"
}

pythonpath_value() {
  # Plugin root first so `gateway` + vendored `shared` resolve for GitHub installs.
  if [[ -n "${REPO_ROOT}" ]]; then
    printf '%s:%s\n' "${PLUGIN_ROOT}" "${REPO_ROOT}"
  else
    printf '%s\n' "${PLUGIN_ROOT}"
  fi
}

run_python() {
  PYTHONPATH="$(pythonpath_value)${PYTHONPATH:+:${PYTHONPATH}}" "$(python_bin)" "$@"
}

detect_host() {
  local h
  h="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo localhost)"
  if [[ "${h}" == "localhost" || "${h}" == *.local ]]; then
    local ip
    ip="$(iproute_ip || true)"
    if [[ -n "${ip}" ]]; then
      h="${ip}"
    fi
  fi
  printf '%s\n' "${h}"
}

iproute_ip() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
    return
  fi
  if command -v ifconfig >/dev/null 2>&1; then
    ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" {print $2; exit}'
  fi
}

herdr_socket() {
  if [[ -n "${HERDR_SOCKET_PATH:-}" ]]; then
    printf '%s\n' "${HERDR_SOCKET_PATH}"
    return
  fi
  if [[ -S "${DEFAULT_SOCKET}" ]]; then
    printf '%s\n' "${DEFAULT_SOCKET}"
    return
  fi
  if [[ -n "${XDG_RUNTIME_DIR:-}" && -S "${XDG_RUNTIME_DIR}/herdr.sock" ]]; then
    printf '%s\n' "${XDG_RUNTIME_DIR}/herdr.sock"
    return
  fi
  printf '%s\n' "${DEFAULT_SOCKET}"
}

herdr_bin() {
  printf '%s\n' "${HERDR_BIN_PATH:-herdr}"
}

remote_id_suggestion() {
  local h
  h="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo remote)"
  printf '%s\n' "${h}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/^$/remote/'
}

python_bin() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "${PYTHON}"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return
  fi
  printf '%s\n' "python"
}

generate_token() {
  run_python -c 'import secrets; print(secrets.token_urlsafe(32))'
}

generate_tls() {
  local cert key
  cert="$(cert_file)"
  key="$(key_file)"
  run_python -c "
from pathlib import Path
from gateway.tls_util import generate_self_signed
print(generate_self_signed(Path('${cert}'), Path('${key}')))
"
}

read_fingerprint() {
  local cert
  cert="$(cert_file)"
  run_python -c "
from pathlib import Path
from gateway.tls_util import fingerprint_from_cert_file
print(fingerprint_from_cert_file(Path('${cert}')))
"
}

write_config() {
  local dir cfg token socket cert key listen_host listen_port
  dir="$(config_dir)"
  mkdir -p "${dir}"
  chmod 700 "${dir}" 2>/dev/null || true

  cfg="$(config_file)"
  token="$1"
  socket="$(herdr_socket)"
  cert="$(cert_file)"
  key="$(key_file)"
  listen_host="${GATEWAY_LISTEN_HOST:-${DEFAULT_LISTEN_HOST}}"
  listen_port="${GATEWAY_LISTEN_PORT:-${DEFAULT_LISTEN_PORT}}"

  cat >"${cfg}" <<EOF
gateway:
  listen_host: ${listen_host}
  listen_port: ${listen_port}
  token: ${token}
  herdr_socket: ${socket}
  cert_path: ${cert}
  key_path: ${key}
EOF
  chmod 600 "${cfg}" 2>/dev/null || true
}

print_register_snippet() {
  local host port token fingerprint rid cfg
  host="${GATEWAY_PUBLIC_HOST:-$(detect_host)}"
  port="${GATEWAY_LISTEN_PORT:-${DEFAULT_LISTEN_PORT}}"
  token="$1"
  fingerprint="$2"
  rid="$(remote_id_suggestion)"
  cfg="$(config_file)"

  cat <<EOF

════════════════════════════════════════════════════════════
 Discord Bridge — Gateway setup complete
════════════════════════════════════════════════════════════

Plugin config (HERDR_PLUGIN_CONFIG_DIR):
  $(config_dir)

Plugin state (HERDR_PLUGIN_STATE_DIR):
  $(state_dir)

Gateway config:
  ${cfg}

TLS certificate / key:
  $(cert_file)
  $(key_file)

Register this Remote in the Discord Bot (/herdr register or bot config):

remotes:
  - id: ${rid}
    host: ${host}
    port: ${port}
    token: ${token}
    fingerprint: ${fingerprint}

Herdr socket: $(herdr_socket)
Herdr binary: $(herdr_bin)

Start:
  herdr plugin action invoke start --plugin herdr-discord-bridge

Or:
  bash ${SCRIPT_DIR}/ctl.sh start
════════════════════════════════════════════════════════════

EOF
}

cmd_setup() {
  local dir token fingerprint
  dir="$(config_dir)"
  mkdir -p "${dir}" "$(state_dir)"

  token="$(generate_token)"
  fingerprint="$(generate_tls)"
  write_config "${token}"
  print_register_snippet "${token}" "${fingerprint}"
}

is_running() {
  local pf pid
  pf="$(pid_file)"
  if [[ ! -f "${pf}" ]]; then
    return 1
  fi
  pid="$(cat "${pf}")"
  if [[ -z "${pid}" ]]; then
    return 1
  fi
  kill -0 "${pid}" 2>/dev/null
}

cmd_start() {
  local pf lf cfg
  pf="$(pid_file)"
  lf="$(log_file)"
  cfg="$(config_file)"

  if [[ ! -f "${cfg}" ]]; then
    echo "error: config not found at ${cfg}" >&2
    echo "Run setup first: herdr plugin action invoke setup --plugin herdr-discord-bridge" >&2
    exit 1
  fi

  if is_running; then
    echo "Gateway already running (pid $(cat "${pf}"))."
    exit 0
  fi

  export HERDR_PLUGIN_CONFIG_DIR="$(config_dir)"
  export HERDR_PLUGIN_STATE_DIR="$(state_dir)"
  mkdir -p "$(config_dir)" "$(state_dir)"

  # Long-lived process: start action backgrounds it (Herdr [[startup]] must exit).
  nohup env \
    HERDR_PLUGIN_CONFIG_DIR="$(config_dir)" \
    HERDR_PLUGIN_STATE_DIR="$(state_dir)" \
    HERDR_SOCKET_PATH="$(herdr_socket)" \
    PYTHONPATH="$(pythonpath_value)${PYTHONPATH:+:${PYTHONPATH}}" \
    "$(python_bin)" -m gateway >>"${lf}" 2>&1 &
  echo $! >"${pf}"
  sleep 0.5

  if is_running; then
    echo "Gateway started (pid $(cat "${pf}"))."
    echo "Log file: ${lf}"
    echo "Listening per ${cfg}"
  else
    echo "error: gateway failed to start; see ${lf}" >&2
    rm -f "${pf}"
    exit 1
  fi
}

cmd_stop() {
  local pf pid
  pf="$(pid_file)"

  if ! is_running; then
    echo "Gateway is not running."
    rm -f "${pf}"
    exit 0
  fi

  pid="$(cat "${pf}")"
  kill "${pid}" 2>/dev/null || true

  local i
  for i in $(seq 1 20); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f "${pf}"
      echo "Gateway stopped."
      return 0
    fi
    sleep 0.25
  done

  kill -9 "${pid}" 2>/dev/null || true
  rm -f "${pf}"
  echo "Gateway stopped (forced)."
}

cmd_status() {
  local pf lf cfg running="no"
  pf="$(pid_file)"
  lf="$(log_file)"
  cfg="$(config_file)"

  if is_running; then
    running="yes (pid $(cat "${pf}"))"
  fi

  cat <<EOF
plugin_id:      herdr-discord-bridge
plugin_root:    ${PLUGIN_ROOT}
config_dir:     $(config_dir)
state_dir:      $(state_dir)
config_file:    ${cfg} $([ -f "${cfg}" ] && echo '[ok]' || echo '[missing]')
pid_file:       ${pf}
running:        ${running}
log_file:       ${lf}
host_hint:      ${GATEWAY_PUBLIC_HOST:-$(detect_host)}
herdr_socket:   $(herdr_socket)
herdr_bin:      $(herdr_bin)
gateway_cmd:    PYTHONPATH=$(pythonpath_value) python -m gateway
EOF

  if [[ -f "${cfg}" ]]; then
    echo ""
    echo "gateway settings:"
    grep -E '^(  )?(listen_host|listen_port|herdr_socket):' "${cfg}" 2>/dev/null || true
    if [[ -f "$(cert_file)" ]]; then
      echo "fingerprint:    $(read_fingerprint)"
    fi
  fi
}

cmd_teardown() {
  if is_running; then
    cmd_stop
  fi

  echo "Teardown complete. Config/TLS remain in:"
  echo "  $(config_dir)"
  echo "Runtime state remains in:"
  echo "  $(state_dir)"
  echo "Delete those directories manually for a full reset."
}

usage() {
  cat <<EOF
usage: ctl.sh <setup|start|stop|status|teardown>

Herdr Discord Bridge host plugin control.
Docs: https://herdr.dev/docs/plugins/

Environment (injected by Herdr when using plugin actions):
  HERDR_PLUGIN_CONFIG_DIR  User config (default under ~/.config/herdr/plugins/config/)
  HERDR_PLUGIN_STATE_DIR   Runtime state (default under ~/.config/herdr/plugins/state/)
  HERDR_SOCKET_PATH        Herdr Unix socket / named pipe
  HERDR_BIN_PATH           Herdr CLI binary (portable)
  HERDR_PLUGIN_ROOT        Linked/installed plugin directory
  GATEWAY_LISTEN_HOST      Listen address (default: ${DEFAULT_LISTEN_HOST})
  GATEWAY_LISTEN_PORT      Listen port (default: ${DEFAULT_LISTEN_PORT})
  GATEWAY_PUBLIC_HOST      Host printed for Discord register
  PYTHON                   Python interpreter (default: python3)
EOF
}

main() {
  local cmd="${1:-setup}"
  case "${cmd}" in
    setup) cmd_setup ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    teardown) cmd_teardown ;;
    -h|--help|help) usage ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
