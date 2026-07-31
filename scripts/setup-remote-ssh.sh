#!/usr/bin/env bash
# Run once on each host that runs Herdr.
# Generates a dedicated SSH key, authorizes it, and prints the private key path
# for you to copy onto the Discord bot host (Docker Compose ./keys).
set -euo pipefail

MARKER="HERDR_DISCORD_BRIDGE"
KEY_NAME="id_ed25519_discord_bridge"
DEFAULT_SOCKET="${HOME}/.config/herdr/herdr.sock"
DEFAULT_CONFIG_DIR="${HOME}/.config/herdr-discord-bridge"

config_dir() {
  printf '%s\n' "${HERDR_DISCORD_BRIDGE_CONFIG_DIR:-${DEFAULT_CONFIG_DIR}}"
}

key_paths() {
  local dir
  dir="$(config_dir)"
  mkdir -p "${dir}"
  chmod 700 "${dir}" 2>/dev/null || true
  PRIVATE_KEY="${dir}/${KEY_NAME}"
  PUBLIC_KEY="${PRIVATE_KEY}.pub"
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

ensure_ssh_dir() {
  mkdir -p "${HOME}/.ssh"
  chmod 700 "${HOME}/.ssh"
  touch "${HOME}/.ssh/authorized_keys"
  chmod 600 "${HOME}/.ssh/authorized_keys"
}

pubkey_line() {
  local pub
  pub="$(tr -d '\n' <"${PUBLIC_KEY}")"
  printf '%s %s\n' "${pub}" "${MARKER}"
}

authorize_key() {
  ensure_ssh_dir
  local line
  line="$(pubkey_line)"
  if grep -F "${MARKER}" "${HOME}/.ssh/authorized_keys" >/dev/null 2>&1; then
    local tmp
    tmp="$(mktemp)"
    grep -vF "${MARKER}" "${HOME}/.ssh/authorized_keys" >"${tmp}" || true
    printf '%s\n' "${line}" >>"${tmp}"
    mv "${tmp}" "${HOME}/.ssh/authorized_keys"
    chmod 600 "${HOME}/.ssh/authorized_keys"
  else
    printf '%s\n' "${line}" >>"${HOME}/.ssh/authorized_keys"
  fi
}

generate_key_if_needed() {
  key_paths
  if [[ -f "${PRIVATE_KEY}" && -f "${PUBLIC_KEY}" ]]; then
    return 0
  fi
  if ! command -v ssh-keygen >/dev/null 2>&1; then
    echo "error: ssh-keygen not found" >&2
    exit 1
  fi
  ssh-keygen -t ed25519 -f "${PRIVATE_KEY}" -N "" -C "herdr-discord-bridge@$(hostname 2>/dev/null || echo host)" >/dev/null
  chmod 600 "${PRIVATE_KEY}"
  chmod 644 "${PUBLIC_KEY}"
}

remote_id_suggestion() {
  local h
  h="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo remote)"
  printf '%s\n' "${h}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/^$/remote/'
}

print_snippet() {
  local host user socket rid key
  host="$(detect_host)"
  user="$(id -un)"
  socket="$(herdr_socket)"
  rid="$(remote_id_suggestion)"
  key_paths
  key="${PRIVATE_KEY}"

  cat <<EOF

════════════════════════════════════════════════════════════
 Discord Bridge — SSH setup complete
════════════════════════════════════════════════════════════

Private key path (copy this file to the Discord bot host yourself):
  ${key}

Public key:
  ${PUBLIC_KEY}

Suggested bot config.yaml remote entry (Docker Compose mounts ./keys -> /app/keys):

remotes:
  - id: ${rid}
    host: ${host}
    port: 22
    user: ${user}
    ssh_key: /app/keys/id_ed25519_discord_bridge_${rid}
    herdr_socket: ${socket}

authorized_keys marker: ${MARKER}
Setup is idempotent. Use teardown to remove the marked authorized_keys line.
════════════════════════════════════════════════════════════

EOF
}

cmd_setup() {
  generate_key_if_needed
  authorize_key
  print_snippet
}

cmd_status() {
  key_paths
  local authorized="no"
  if [[ -f "${HOME}/.ssh/authorized_keys" ]] && grep -F "${MARKER}" "${HOME}/.ssh/authorized_keys" >/dev/null 2>&1; then
    authorized="yes"
  fi
  cat <<EOF
config_dir:     $(config_dir)
private_key:    ${PRIVATE_KEY} $([ -f "${PRIVATE_KEY}" ] && echo '[ok]' || echo '[missing]')
public_key:     ${PUBLIC_KEY} $([ -f "${PUBLIC_KEY}" ] && echo '[ok]' || echo '[missing]')
authorized:     ${authorized}
user:           $(id -un)
host_hint:      $(detect_host)
herdr_socket:   $(herdr_socket)
EOF
}

cmd_show_config() {
  key_paths
  if [[ ! -f "${PRIVATE_KEY}" ]]; then
    echo "No key yet. Run: ./scripts/setup-remote-ssh.sh setup" >&2
    exit 1
  fi
  print_snippet
}

cmd_teardown() {
  ensure_ssh_dir
  if [[ -f "${HOME}/.ssh/authorized_keys" ]]; then
    local tmp
    tmp="$(mktemp)"
    grep -vF "${MARKER}" "${HOME}/.ssh/authorized_keys" >"${tmp}" || true
    mv "${tmp}" "${HOME}/.ssh/authorized_keys"
    chmod 600 "${HOME}/.ssh/authorized_keys"
  fi
  key_paths
  echo "Removed ${MARKER} from authorized_keys."
  echo "Key files left in place (delete manually if desired):"
  echo "  ${PRIVATE_KEY}"
  echo "  ${PUBLIC_KEY}"
}

usage() {
  cat <<EOF
usage: setup-remote-ssh.sh <setup|status|show-config|teardown>

Run on each Herdr host. Does not talk to Herdr at runtime — only prepares SSH access
for the Discord bot.
EOF
}

main() {
  local cmd="${1:-setup}"
  case "${cmd}" in
    setup) cmd_setup ;;
    status) cmd_status ;;
    show-config) cmd_show_config ;;
    teardown) cmd_teardown ;;
    -h|--help|help) usage ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
