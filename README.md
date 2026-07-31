# Herdr Discord Bridge

[中文文档](readme_zh-cn.md)

Map [Herdr](https://herdr.dev) panes from multiple hosts into Discord: chat in a channel/thread to drive the matching pane, and receive live terminal output.

- **Bot (Discord Bot):** connects to Discord and reaches each host’s TLS plugin over the network. Deploy with Docker (local or Unraid) so it can move with you.
- **Host plugin (Herdr Plugin):** runs on **every machine that runs Herdr**, bridging local `herdr.sock` to a TLS port. It must be co-located with Herdr (Unix socket; do not expose the sock across the network).

> In this doc, **Bot** = the Discord process; **host plugin** = the plugin on each Herdr host. Do not confuse this with “one central gateway talking to every `herdr.sock`.”

## Architecture

```text
Discord user
    ↕ Discord API
Bot (Docker; Unraid OK; prefer sharing Tailscale container network)
    ↕ Tailscale / LAN  TLS NDJSON :9876
Host plugin (one per Herdr host)
    ↕ Unix
herdr.sock → Pane / Workspace / Tab
```

| Discord concept | Meaning |
|-----------------|--------|
| Remote channel | One registered host (one host-plugin endpoint) |
| Pane thread | One Herdr Pane on that host |
| Chat in the thread | Forwarded as `pane.send_text` + `pane.send_keys Enter` |
| Terminal message | Bot bubble updated from push events |

Across Tailscale hosts: install the host plugin on each machine; in Discord `/herdr register`, use that host’s **Tailscale IP**, port, token, and cert fingerprint.

## Repository layout

```text
src/
  bot/                 # Discord bot
  plugin/              # Herdr host plugin (herdr-plugin.toml + gateway + ctl.sh)
  shared/              # Shared NDJSON / TLS fingerprint helpers
docker/
  gateway-entrypoint.sh
  unraid-herdr-discord-bridge.xml  # Unraid DockerMan template (Discord bot)
  unraid-herdr-gateway.xml         # Optional: host-plugin image ONLY on a machine that has herdr.sock
Dockerfile             # Bot image
Dockerfile.gateway     # Host plugin image (optional; do not run alone without Herdr)
docker-compose.yml     # Generic bot Compose
docker-compose.unraid.yml  # Unraid: bot network_mode=container:Tailscale-Docker
config.example.yaml
docs/                  # Design / ADR / CONTEXT
readme_zh-cn.md        # Chinese documentation
```

## 1. Host plugin (each Herdr machine)

Follows the [Herdr plugins](https://herdr.dev/docs/plugins/) contract: `herdr-plugin.toml` + argv actions, config under `HERDR_PLUGIN_CONFIG_DIR`, runtime state under `HERDR_PLUGIN_STATE_DIR`. The TLS gateway is a long-lived process started by the **`start` action** (not a `[[startup]]` hook — those must exit).

### Requirements

- Herdr ≥ 0.7
- Local `herdr.sock` (often `~/.config/herdr/herdr.sock`)
- Python 3.11+ with `cryptography` and `PyYAML` (installed automatically on GitHub `plugin install` via `[[build]]`)
- Firewall / Tailscale allows the listen port (default `9876`)

### Install

**From GitHub (publish / end users):**

```bash
herdr plugin install neohob/herdr-discord-bridge/src/plugin
# optional pin: herdr plugin install neohob/herdr-discord-bridge/src/plugin --ref main
herdr plugin config-dir herdr-discord-bridge
```

**Local development (this repo):**

```bash
herdr plugin link "$(pwd)/src/plugin"
```

### Lifecycle

```bash
herdr plugin action invoke setup  --plugin herdr-discord-bridge
herdr plugin action invoke start  --plugin herdr-discord-bridge
herdr plugin action invoke status --plugin herdr-discord-bridge
# stop / teardown likewise
```

`setup` creates a token and self-signed cert, and prints values for Discord registration.

| Path | Purpose |
|------|---------|
| `HERDR_PLUGIN_CONFIG_DIR` | `config.yaml`, TLS cert/key (typically `~/.config/herdr/plugins/config/herdr-discord-bridge/`) |
| `HERDR_PLUGIN_STATE_DIR` | `gateway.pid`, `gateway.log` (typically `~/.config/herdr/plugins/state/herdr-discord-bridge/`) |

Optional env: `GATEWAY_LISTEN_PORT` (default `9876`), `GATEWAY_PUBLIC_HOST` (host printed for Discord register), `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`.

### Marketplace listing

Add the GitHub topic **`herdr-plugin`** on this repository so it appears in the [Herdr marketplace](https://herdr.dev/docs/plugins/) index (refreshes about every 30 minutes). Install path for users: `neohob/herdr-discord-bridge/src/plugin`.

### Fields to register

| Field | Notes |
|-------|--------|
| host | That machine’s Tailscale IP (preferred) or reachable LAN IP; use `127.0.0.1` if bot and plugin share a host |
| port | Default `9876` |
| token | From setup |
| fingerprint | Cert SHA-256 hex (64 chars) |

## 2. Bot (Discord Bot)

### Discord developer portal

1. Create an Application → Bot, copy the token  
2. Enable **Message Content Intent**  
3. Turn **OAuth2 → Requires OAuth2 Code Grant** off  
4. Invite URL (replace `CLIENT_ID`):

```text
https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=8&scope=bot%20applications.commands
```

5. After inviting to the target guild, copy the **guild ID** (Developer Mode) into `guild_id`

### Config

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

`.env`:

```bash
DISCORD_TOKEN=your-bot-token
LOG_LEVEL=info
```

`config.yaml` highlights:

| Key | Notes |
|-----|--------|
| `discord.guild_id` | Guild where the bot runs |
| `discord.token` | May be `"${DISCORD_TOKEN}"` |
| `operators.require_manage_guild` | When `true`, need Manage Guild (or a matching role) |
| `operators.user_ids` / `role_ids` | Optional operator allowlists |
| `bridge.terminal.max_lines` | Terminal view line count |
| `bridge.terminal.edit_cooldown` | Discord message edit cooldown (seconds) |
| `seed_remotes` | Optional; seeds `cache/remotes.json` once, then Discord registration wins |

Data dirs:

- `cache/remotes.json` — remote registry (tokens / fingerprints)  
- `cache/mapping.json` — channel / thread mapping  
- `logs/` — logs  

**Do not commit `.env`, `config.yaml`, or `cache/remotes.json`.**

### Run locally

```bash
export $(grep -v '^#' .env | xargs)
export BRIDGE_CONFIG="$PWD/config.yaml" PYTHONPATH=.
python -m src.bot
```

### Docker Compose (generic)

```bash
docker compose up -d --build
```

Mounts: `config.yaml`, `cache/`, `logs/`.

### Unraid + Tailscale (recommended)

Share the Bot’s network namespace with `Tailscale-Docker` so it can reach each host’s `100.x:9876`:

```bash
# See docker-compose.unraid.yml
# network_mode: "container:Tailscale-Docker"
```

If Unraid lacks `docker compose` v2, use an equivalent `docker run --network container:Tailscale-Docker ...`.  
Build from the repo-root `Dockerfile`. Suggested appdata layout:

```text
/mnt/user/appdata/herdr-discord-bridge/{config.yaml,.env,cache,logs}
```

Only **one** bot process may use a given Bot Token.

## 3. Discord usage

In a text channel of the target guild (Operator permission required):

### Register a host and open a channel

```text
/herdr register
```

| Option | Example |
|--------|---------|
| host | `100.x.x.x` (Tailscale) |
| port | `9876` |
| token / fingerprint | From host-plugin setup |
| id | Optional, e.g. `macbook` |
| create_channel | `True` → create the Remote channel |

### Sync panes → threads

In the Remote channel:

```text
/herdr sync
```

Builds or renames threads from live Herdr `workspace.list` / `tab.list` / `pane.list`, e.g.:

```text
🟢 JinAn-MAP › cursor · Cursor Agent [wB:p6]
```

(workspace › tab label · display name `[pane_id]` — not a static lookup table.)

### Day-to-day

| Action | How |
|--------|-----|
| Send to a Pane | **Chat mode:** type normally in the Pane thread. Output streams under your message. |
| Agent skills | Prefer slash **`/agent`**: `command` = `/grilling` (etc.), optional `text` = trailing content → Pane receives `command` + `text`. Plain-message `/xxx …` still forwards if you can send it. |
| Stop current task | Slash **`/stop`** in the Pane thread (sends Esc, then Ctrl+C fallback) |
| Discord ops | Slash menu **`/herdr …`** (not forwarded as pane text) |
| Approve / choose | Yes/No/Custom only on real confirm prompts / `blocked` |
| Read output | Plain-text bot bubbles; spinner/typing-echo noise is coalesced |
| List / read / … | `/herdr pane list\|read\|…` (slash command UI) |
| Status | `/herdr status` |
| Rebind channel | `/herdr rebind` in the new channel |
| Help | `/herdr help` |

**Slash vs skills:** `/herdr` = bridge ops; `/agent` = send into the current Pane thread; `/stop` = interrupt the current turn. Normal thread messages (including `/…` for the agent) still forward when Discord lets you send them as plain text.
If slash commands are missing: wait 1–2 minutes or restart the client; confirm the bot is online and `guild_id` is correct.

## 4. Troubleshooting

| Symptom | What to try |
|---------|-------------|
| “Application did not respond” | Sync creating threads can be slow (deferred). Retry `/herdr sync` |
| Text reaches the Pane but does not run | Needs `send_keys Enter` (a bare `\n` is not enough). Use a current bot build |
| You only see the bot as a DM contact | Invite with a URL that includes the `bot` scope into a **server** |
| Unraid bot cannot reach `100.x` | Use `--network container:Tailscale-Docker` (or Tailscale on the host) |
| Deleted Remote channel | Credentials remain; `rebind` in a new channel. To drop the host use `/herdr remove` |

## 5. Development and tests

```bash
python -m pip install -r requirements.txt   # or pip install -e ".[dev]"
PYTHONPATH=. pytest -q
```

More design and terminology:

- [docs/superpowers/specs/2026-07-31-herdr-discord-bridge-design.md](docs/superpowers/specs/2026-07-31-herdr-discord-bridge-design.md)
- [CONTEXT.md](CONTEXT.md)
- [docs/adr/](docs/adr/)
