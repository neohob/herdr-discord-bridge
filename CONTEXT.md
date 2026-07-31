# Herdr Discord Bridge

Domain language for bridging remote Herdr sessions into a Discord guild via a long-lived gateway.

## Language

### Topology

**Remote**:
A Herdr host (Gateway endpoint: IP/host + port + token) treated as one unit of connectivity. In Discord it is represented by one Remote Channel. Operators can register host/key via `/herdr` (not only via Bot config file).
_Avoid_: Server, machine, node (unless speaking infra), SSH host; equating Remote with a Discord Category

**Remote Channel**:
The Discord text channel that stands for one Remote. Structural commands for that host (e.g. create Pane) run here. (ADR-0009)
_Avoid_: Category-as-Remote (obsolete); requiring a separate guild-wide Control Channel for host ops

**Gateway**:
The Herdr plugin process on a Remote that listens on TCP, authenticates the Bot, talks to local `herdr.sock`, and owns all push subscriptions.
_Avoid_: Bridge (reserved for the whole system / Discord Bot side), proxy, relay, HTTP server

**Bot**:
The Discord application process (Compose) that maps Remotes/Panes into Discord and is the sole Discord API client.
_Avoid_: Bridge bot as a second identity; client (too vague)

**Bridge**:
The overall system: Bot + Gateways + Discord mapping. Not a running process name.
_Avoid_: Using "bridge" for the Gateway alone

### Herdr surface

**Pane**:
A real Herdr terminal pane; the unit mapped to one Pane Channel under its Remote Channel.
_Avoid_: Tab, terminal (generic); top-level guild channel for every Pane

**Pane Channel**:
A Discord **Thread** under the Remote Channel for one Mapped Pane. Holds the Terminal Message; Chat Input here is sent to the Pane. (ADR-0009)
_Avoid_: Top-level text channel per Pane; Category-under-Remote nesting

**Workspace** / **Tab**:
Herdr grouping concepts. Visible only in naming/labels on Discord; not their own Discord Category/Channel tier.
_Avoid_: Mapping Workspace→Category (superseded)

### Connections & push

**Control Connection**:
The Bot↔Gateway long-lived **TLS** TCP link used only for control-plane request/response NDJSON (mostly Herdr passthrough). First frame: `bridge.auth` with `role: "control"`. (ADR-0004, ADR-0005, ADR-0014)
_Avoid_: Multiplexing push on this socket; HTTP API; REST; separate listen ports per role; plaintext TCP

**Push Connection**:
The Bot↔Gateway long-lived **TLS** TCP link used only for the push plane. First frame: `bridge.auth` with `role: "push"`. (ADR-0004, ADR-0005, ADR-0014)
_Avoid_: Sending pane.send_* on the push socket; Bot-owned events.subscribe; dual listen ports; plaintext TCP

**Connection Role**:
Either `control` or `push`, declared in `bridge.auth` params. Wrong or missing role → Gateway closes the socket. (ADR-0005)
_Avoid_: Inferring role from traffic; post-auth bind frame; port-number-as-role

**Control plane**:
Logical request/response traffic carried on the Control Connection.
_Avoid_: HTTP API, REST

**Push plane**:
Events and terminal output the Gateway originates and fans out on Push Connections. The Gateway is the sole subscriber to Herdr `events.subscribe` / terminal observe. (ADR-0001)
_Avoid_: Bot-side polling, passthrough `events.subscribe` owned by the Bot

**Terminal Message**:
The single Discord message per Pane channel that the Bot edits to simulate a live terminal view.
_Avoid_: Log dump, webhook message

**Terminal Output Event**:
A push-plane frame (`bridge.terminal_output`) produced by the Gateway: Discord-ready plain text for one Pane (ANSI already stripped), plus a revision. The Bot does not strip ANSI or own the observe stream. (ADR-0002)
_Avoid_: Raw terminal frame, ANSI blob, Bot-side pane.read poll

**Terminal View**:
The latest complete visible window of a Pane as plain text — a sliding recent slice (e.g. last N lines / viewport), not the full scrollback history from session start. Each Terminal Output Event replaces the prior Terminal View. (ADR-0003)
_Avoid_: Full transcript, entire scrollback, append-only delta log

**Mapped Pane**:
A Pane that has a Pane Channel (Thread) and an entry in the Bot mapping store. Only Mapped Panes receive Terminal View pushes; the Bot enables observe on the Control Connection after mapping. (ADR-0006)
_Avoid_: Observing every Herdr pane by default

**Chat Input**:
Every non-bot user message in a Pane Channel (Thread) is forwarded to the Mapped Pane via `pane.send_input` (text + Enter). Bot messages ignored. Slash commands remain for structured ops. (ADR-0013)
_Avoid_: Requiring `/herdr send` for ordinary typing; prefix-gated send as default

**Slash Surface**:
A single Discord top-level command `/herdr` whose subcommands expose Herdr operations and Remote registration (host/ip/token). No separate top-level `/herdr-*` unless Herdr ships distinct `herdr-*` commands. (ADR-0007)
_Avoid_: One Discord top-level command per Herdr argv; inventing `/herdr-*` families Herdr does not have

**Operator**:
A Discord user allowed to register Remotes and run structural `/herdr` ops. Must pass admin permission (e.g. Manage Guild or configured role) and, when set, an operator user-id allowlist. (ADR-0010)
_Avoid_: Treating every guild member as able to set Gateway tokens

**Remote Registry**:
The Bot’s persisted store of Remotes (host, port, token, TLS cert fingerprint, Discord Remote Channel id). Runtime source of truth for connectivity; not the Compose `config.yaml` remotes list. Optional yaml seeds import once. (ADR-0011, ADR-0015)
_Avoid_: Requiring config.yaml as the only way to add hosts; dual-writing yaml and registry without a single winner

**Gateway Identity**:
The Gateway’s TLS certificate; Bot trusts it by pinned SHA-256 fingerprint recorded at Remote registration / plugin setup—not by public CA and not by skipping verify. (ADR-0015)
_Avoid_: insecure_skip_verify as default; requiring Let’s Encrypt for private hosts

**Sync**:
An explicit `/herdr` action that lists Panes on a Remote and creates/updates Pane Channels (Threads) + observe for Mapped Panes. Not implicit on every Gateway connect. (ADR-0012)
_Avoid_: Auto-creating a Thread for every existing Pane on connect

**Remote Unbind**:
When a Remote Channel is deleted (or explicitly unbound), the Bot drops TLS connections and clears Discord channel/thread mappings for that Remote, but keeps Gateway credentials in the Remote Registry so the Operator can Rebind without re-entering token/fingerprint. (ADR-0016)
_Avoid_: Deleting Registry credentials on every channel delete; leaving live TLS to a missing channel

**Rebind**:
An Operator `/herdr` action run **inside** the target Discord text channel: pick an unbound Registry Remote (Select Menu) and bind it to the current channel, then reconnect Control/Push. Sync Threads separately if needed. (ADR-0016, ADR-0017)
_Avoid_: Forcing full re-register of token/fingerprint after accidental channel delete; auto-creating the channel on rebind

**Thread Unmap**:
When a Pane Thread is deleted, the Bot stops observe and clears that Pane’s Discord mapping; the Herdr Pane itself is unchanged. `/herdr sync` can recreate the Thread. (ADR-0018)
_Avoid_: Treating Thread delete as `pane.close`

**Pane Close**:
Removing a Pane in Herdr (via `/herdr` → pane close / equivalent control-plane call). On success the Bot archives or deletes the Pane Thread. (ADR-0018)
_Avoid_: Closing Panes only by deleting Discord Threads

**Choice UI**:
Bounded choices (e.g. agent blocked yes/no, pick Remote/Pane) use Discord Components—Buttons, Select Menus, and Modals for short custom text. Unbounded input falls back to Chat Input in the Pane Thread. Agent decision prompts are posted in the Pane Thread and only Operators may click. After a successful choice, the Bot edits that message to record the outcome and removes the components. (ADR-0019, ADR-0020, ADR-0021)
_Avoid_: Requiring free-text parsing for yes/no when Buttons suffice; posting decisions only in the Remote Channel; allowing any guild member to click; leaving clickable buttons after success
