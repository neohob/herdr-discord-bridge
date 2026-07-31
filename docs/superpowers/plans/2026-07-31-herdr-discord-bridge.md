# Herdr Discord Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a TLS NDJSON Gateway (Herdr plugin) plus a Discord Bot that maps Remotes→channels and Panes→Threads with push-based Terminal Views and `/herdr` ops—matching [docs/superpowers/specs/2026-07-31-herdr-discord-bridge-design.md](../specs/2026-07-31-herdr-discord-bridge-design.md).

**Architecture:** Gateway owns Herdr subscriptions and fans out on a Push TLS socket; Control TLS socket passthroughs Herdr RPC plus `bridge.observe_pane`. Bot holds Remote Registry, Discord mapping, Chat Input, Choice UI; no SSH, no HTTP, no Bot-side `pane.read` polling for live view.

**Tech Stack:** Python 3.13+, asyncio, discord.py 2.x, PyYAML, stdlib `ssl`/`asyncio` streams (no asyncssh), pytest + pytest-asyncio, Docker Compose for Bot.

## Global Constraints

- Spec + [CONTEXT.md](../../../CONTEXT.md) + ADRs 0001–0021 are authoritative; do not reintroduce SSH or Category=Remote.
- Plugin user-facing setup/status text: **English only**.
- Single Discord top-level slash: `/herdr` (+ subcommands/groups only).
- Gateway listen: TLS required; Bot verifies SHA-256 cert fingerprint pin.
- Two connections per Remote: `bridge.auth` with `role: "control" | "push"`.
- Delete obsolete `src/bot/ssh/`, `scripts/setup-remote-ssh.sh`, `keys/` volume, `asyncssh` dependency as part of Bot cutover.
- Domain terms: Remote, Gateway, Bot, Remote Channel, Pane Channel (Thread), Terminal View, Operator, Remote Registry, Rebind, Sync.

## File structure (target)

```
src/
  shared/
    __init__.py
    ndjson.py              # encode/decode line, make_request, unwrap_result
    fingerprint.py         # cert SHA-256 pin helpers
  plugin/                  # Herdr plugin root (link this directory)
    herdr-plugin.toml
    scripts/ctl.sh         # setup|start|stop|status|teardown (English)
    gateway/
      __init__.py
      __main__.py          # python -m src.plugin.gateway
      config.py
      herdr_unix.py        # AF_UNIX NDJSON client + subscribe reader
      tls_util.py          # self-signed cert generate + load
      server.py            # TLS accept, auth, control/push sessions
      push_pump.py         # events + terminal observe → Terminal View
      ansi.py              # strip ANSI for Terminal View
  bot/
    __init__.py
    __main__.py
    bot.py
    config.py              # discord/guild/operators only (+ optional seed)
    registry.py            # Remote Registry persistence
    mapping.py             # pane↔thread mapping
    operators.py           # Manage Guild + allowlist
    gateway_client.py      # dual TLS client (control RPC + push reader)
    discord_map.py         # Remote Channel / Thread ensure, unbind hooks
    terminal_view.py       # Terminal Message edit coalescing
    chat_input.py          # on_message → pane.send_input
    choice_ui.py           # blocked Buttons/Modal
    commands.py            # /herdr tree
    lifecycle.py           # channel/thread delete handlers
tests/
  shared/...
  plugin/...
  bot/...
```

Remove after cutover: `src/bot/ssh/`, `src/bot/herdr/` (replaced by gateway_client + shared), old `bridge/*` SSH-era modules, `scripts/setup-remote-ssh.sh`.

---

### Task 1: Shared NDJSON + fingerprint helpers

**Files:**
- Create: `src/shared/__init__.py`
- Create: `src/shared/ndjson.py`
- Create: `src/shared/fingerprint.py`
- Create: `tests/shared/test_ndjson.py`
- Create: `tests/shared/test_fingerprint.py`
- Modify: `pyproject.toml` (pytest deps, packages)
- Modify: `requirements.txt` (drop asyncssh; add pytest/pytest-asyncio for dev—or use optional `[dev]`)

**Interfaces:**
- Produces:
  - `make_request(method: str, params: dict | None = None, req_id: str | None = None) -> dict`
  - `encode_line(obj: dict) -> bytes`
  - `decode_line(line: str | bytes) -> dict`
  - `unwrap_result(payload: dict) -> Any`  # raises `HerdrApiError`
  - `cert_sha256_fingerprint(cert_pem_or_der: bytes) -> str`  # lowercase hex with optional `sha256:` prefix normalized to bare hex
  - `fingerprints_match(expected: str, actual: str) -> bool`

- [ ] **Step 1: Write failing tests**

```python
# tests/shared/test_ndjson.py
from src.shared.ndjson import make_request, encode_line, decode_line, unwrap_result, HerdrApiError
import pytest

def test_roundtrip_request_line():
    req = make_request("ping", {}, req_id="req_1")
    assert req["method"] == "ping" and req["id"] == "req_1"
    line = encode_line(req)
    assert line.endswith(b"\n")
    assert decode_line(line)["method"] == "ping"

def test_unwrap_error():
    with pytest.raises(HerdrApiError) as ei:
        unwrap_result({"id": "1", "error": {"code": "x", "message": "nope"}})
    assert ei.value.code == "x"
```

```python
# tests/shared/test_fingerprint.py
from src.shared.fingerprint import fingerprints_match

def test_fingerprint_match_normalizes_prefix():
    bare = "ab" * 32
    assert fingerprints_match(bare, f"sha256:{bare}")
    assert not fingerprints_match(bare, "cd" * 32)
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd /path/to/herdr-discord-bridge
python -m pip install -e ".[dev]"  # or pytest pytest-asyncio
pytest tests/shared/test_ndjson.py tests/shared/test_fingerprint.py -v
```

Expected: import/module missing failures.

- [ ] **Step 3: Implement `src/shared/ndjson.py` and `fingerprint.py`**

Implement the interfaces above (copy patterns from old `src/bot/herdr/protocol.py` where useful; keep zero Discord deps).

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/shared/ -v
```

- [ ] **Step 5: Commit** (if git available)

```bash
git add src/shared tests/shared pyproject.toml requirements.txt
git commit -m "feat(shared): NDJSON protocol and TLS fingerprint helpers"
```

---

### Task 2: Gateway Herdr Unix client (request + subscribe)

**Files:**
- Create: `src/plugin/gateway/herdr_unix.py`
- Create: `tests/plugin/test_herdr_unix.py` (use a temp AF_UNIX echo stub server)

**Interfaces:**
- Consumes: `src.shared.ndjson`
- Produces:
  - `class HerdrUnixClient:`
    - `async def connect(self, path: str) -> None`
    - `async def request(self, method: str, params: dict | None = None) -> Any`
    - `async def close(self) -> None`
  - `class HerdrUnixSubscriber:`
    - `async def start(self, path: str, subscriptions: list[dict]) -> None`  # sends events.subscribe, yields events
    - `async def __aiter__` / `async def recv_event(self) -> dict`  # frames with `event` key
    - `async def close(self) -> None`

- [ ] **Step 1: Write failing test with local Unix stub**

Stub accepts one connection, reads a JSON line, if method `ping` replies `{"id":..., "result":{"type":"pong"}}`.

```python
@pytest.mark.asyncio
async def test_unix_ping(tmp_path):
    sock = str(tmp_path / "h.sock")
    # start stub task...
    client = HerdrUnixClient()
    await client.connect(sock)
    result = await client.request("ping")
    assert result["type"] == "pong"
    await client.close()
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/plugin/test_herdr_unix.py::test_unix_ping -v
```

- [ ] **Step 3: Implement `HerdrUnixClient` / `HerdrUnixSubscriber`**

Use `asyncio.open_unix_connection`. One connection per request for client (match herdr ApiClient), dedicated connection for subscriber that stays open after subscribe ack.

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/plugin/test_herdr_unix.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/plugin/gateway/herdr_unix.py tests/plugin/test_herdr_unix.py
git commit -m "feat(plugin): Herdr Unix NDJSON client and subscriber"
```

---

### Task 3: Gateway TLS cert generation + fingerprint

**Files:**
- Create: `src/plugin/gateway/tls_util.py`
- Create: `tests/plugin/test_tls_util.py`
- Modify: depend on nothing heavy—use `cryptography` **or** shell out to `openssl` in setup script. **Decision locked for plan:** use stdlib + `openssl` CLI in `ctl.sh` for cert gen; Python loads PEM via `ssl`. If `openssl` missing in tests, generate via `cryptography` package.

**Add to requirements:** `cryptography>=42` (used by Gateway setup and tests).

**Interfaces:**
- Produces:
  - `generate_self_signed(cert_path: Path, key_path: Path, common_name: str = "herdr-discord-bridge") -> str`  # returns bare hex fingerprint
  - `load_ssl_context_server(cert_path: Path, key_path: Path) -> ssl.SSLContext`
  - `fingerprint_from_cert_file(cert_path: Path) -> str`

- [ ] **Step 1: Failing test**

```python
def test_generate_writes_files_and_fingerprint(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    fp = generate_self_signed(cert, key)
    assert cert.is_file() and key.is_file()
    assert len(fp) == 64
    assert fingerprint_from_cert_file(cert) == fp
```

- [ ] **Step 2–4: Implement with `cryptography` X.509 self-signed; pytest PASS; commit**

```bash
git commit -m "feat(plugin): self-signed TLS cert generation and fingerprint"
```

---

### Task 4: Gateway TLS server — auth + control passthrough

**Files:**
- Create: `src/plugin/gateway/config.py`  # load yaml: listen_host, listen_port, token, herdr_socket, cert/key paths
- Create: `src/plugin/gateway/server.py`
- Create: `tests/plugin/test_gateway_auth.py`

**Interfaces:**
- Produces:
  - `async def serve_gateway(cfg: GatewayConfig, herdr_factory) -> None`
  - On connect: TLS handshake → read first NDJSON → require `bridge.auth` with matching token and role ∈ {control,push}
  - Control session: for each request line, if method starts with `bridge.` handle locally later; else `herdr.request` and write result line
  - Push session: after auth, register writer in PushHub (Task 5); ignore client RPC except optional future noop

- [ ] **Step 1: Integration-style test**

Use `ssl` client connecting to `127.0.0.1:port` with generated cert, pin fingerprint, send auth control, then `ping` passthrough against Unix stub.

```python
@pytest.mark.asyncio
async def test_control_auth_and_ping(...):
    # start gateway + unix stub
    # tls connect, bridge.auth role=control
    # send ping, expect pong
```

Wrong token → connection closed / error then EOF.

- [ ] **Step 2–4: Implement until PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(plugin): TLS gateway auth and control-plane passthrough"
```

---

### Task 5: Gateway push pump — events + Terminal View

**Files:**
- Create: `src/plugin/gateway/ansi.py`
- Create: `src/plugin/gateway/push_pump.py`
- Create: `tests/plugin/test_ansi.py`
- Create: `tests/plugin/test_push_pump.py`

**Interfaces:**
- Consumes: `HerdrUnixSubscriber`, PushHub from server
- Produces:
  - `strip_ansi(text: str) -> str`
  - `class PushPump:`
    - `async def run(self) -> None`  # lifecycle subscribe loop
    - `async def set_observe(self, pane_id: str, enable: bool) -> None`
    - Emits to all push clients:
      - herdr events as `{"event": "...", "data": {...}}`
      - `{"event":"bridge.terminal_output","data":{"pane_id","revision","text","truncated"}}`
  - Terminal View: maintain last N lines (config `max_lines`, default 50); coalesce ≥ `push_cooldown` seconds (default 1.0) per pane
  - Observe implementation for this plan: on enable, spawn task that periodically calls `pane.read` **inside the Gateway only** (source=recent, lines=N) and pushes when text/revision changes — until/unless `terminal session observe` stream is wired. Document in code comment that this is Gateway-local, not Bot polling. Prefer observe stream in a follow-up task if Herdr CLI stream is easier via subprocess; **do not** expose polling to Bot.

**Spec note:** Design prefers terminal observe stream; Gateway-local read loop is an allowed implementation detail that still satisfies “Bot must not poll.”

- [ ] **Step 1: Tests for strip_ansi and coalesce emit**

```python
def test_strip_ansi_removes_csi():
    assert "hi" in strip_ansi("\x1b[31mhi\x1b[0m")
```

```python
@pytest.mark.asyncio
async def test_observe_emits_terminal_output(fake_herdr, push_hub):
    pump = PushPump(...)
    await pump.set_observe("w1:p1", True)
    # fake_herdr returns changing text
    ev = await push_hub.wait_event("bridge.terminal_output", timeout=2)
    assert ev["data"]["pane_id"] == "w1:p1"
```

- [ ] **Step 2–4: Implement; wire `bridge.observe_pane` on control session; PASS; commit**

```bash
git commit -m "feat(plugin): push pump for events and terminal views"
```

---

### Task 6: Plugin packaging — ctl.sh + herdr-plugin.toml + `__main__`

**Files:**
- Create: `src/plugin/herdr-plugin.toml`
- Create: `src/plugin/scripts/ctl.sh` (English output)
- Create: `src/plugin/gateway/__main__.py`
- Create: `src/plugin/gateway/config.py` (if not done)
- Delete: `scripts/setup-remote-ssh.sh`

**Interfaces:**
- `setup`: generate token (secrets.token_urlsafe), cert/key, write `$HERDR_PLUGIN_CONFIG_DIR/config.yaml`, print English snippet for Bot register (host hint, port, token, fingerprint)—**no scp**
- `start`: `python -m src.plugin.gateway` (ensure PYTHONPATH=repo root) or `python gateway/__main__.py` with path hacks documented in ctl.sh
- Actions in toml: setup, start, stop, status, teardown

- [ ] **Step 1: Manual dry-run script**

```bash
HERDR_PLUGIN_CONFIG_DIR=/tmp/hdb-cfg bash src/plugin/scripts/ctl.sh setup
# expect English paths + fingerprint
```

- [ ] **Step 2: Implement ctl + toml + __main__ entry that loads config and `asyncio.run(serve_gateway(...))`**

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(plugin): Herdr plugin manifest and English setup/start ctl"
```

---

### Task 7: Bot config + Remote Registry + Operators

**Files:**
- Replace/rewrite: `src/bot/config.py`
- Create: `src/bot/registry.py`
- Create: `src/bot/operators.py`
- Create: `tests/bot/test_registry.py`
- Create: `tests/bot/test_operators.py`
- Modify: `config.example.yaml` (no ssh_key remotes; operators allowlist; optional seed_remotes)

**Interfaces:**
- `load_config(path) -> AppConfig` with `discord`, `bridge.terminal`, `operators: {require_manage_guild: bool, user_ids: list[int], role_ids: list[int]}`, `seed_remotes: list[RemoteSeed] | None`
- `class RemoteRecord`: id, host, port, token, fingerprint, channel_id: int | None
- `class RemoteRegistry`: load/save `cache/remotes.json`; `upsert`, `get`, `list_unbound`, `bind_channel`, `unbind_channel` (clears channel_id, keeps secrets), `remove`
- `def is_operator(member: discord.Member, cfg: OperatorsConfig) -> bool`

- [ ] **Step 1: Registry tests for unbind keeps token**

```python
def test_unbind_keeps_credentials(tmp_path):
    reg = RemoteRegistry(tmp_path / "r.json")
    reg.upsert(RemoteRecord(id="r1", host="1.2.3.4", port=8787, token="t", fingerprint="a"*64, channel_id=1))
    reg.unbind_channel("r1")
    r = reg.get("r1")
    assert r.channel_id is None and r.token == "t"
    assert "r1" in [x.id for x in reg.list_unbound()]
```

- [ ] **Step 2–4: Implement; PASS; commit**

```bash
git commit -m "feat(bot): Remote Registry and operator checks"
```

---

### Task 8: Bot GatewayClient (dual TLS)

**Files:**
- Create: `src/bot/gateway_client.py`
- Create: `tests/bot/test_gateway_client.py` (against Task 4–5 gateway or a fake TLS server)

**Interfaces:**
- `class GatewayClient:`
  - `def __init__(self, remote: RemoteRecord, on_event: Callable[[dict], Awaitable[None]])`
  - `async def start(self) -> None`  # open control+push, auth both, start push reader + reconnect loop
  - `async def stop(self) -> None`
  - `async def request(self, method: str, params: dict | None = None) -> Any`  # control connection
  - `async def observe_pane(self, pane_id: str, enable: bool) -> Any`
  - Verify TLS with `ssl.SSLContext` + custom pin check using `fingerprints_match`
  - Reconnect with exponential backoff; on control reconnect, caller re-enables observes

- [ ] **Step 1: Test auth + ping against running test gateway fixture**

- [ ] **Step 2–4: Implement; PASS; commit**

```bash
git commit -m "feat(bot): dual TLS GatewayClient with fingerprint pin"
```

---

### Task 9: Discord mapping — Remote Channel, Threads, Terminal Message

**Files:**
- Create: `src/bot/mapping.py` (pane_id → thread_id, terminal_message_id, remote_id)
- Create: `src/bot/discord_map.py`
- Create: `src/bot/terminal_view.py`
- Rewrite/remove old: `src/bot/bridge/channel_manager.py`, `mapping.py`, `terminal_sim.py`

**Interfaces:**
- `async def ensure_remote_channel(guild, remote_id, name) -> TextChannel`
- `async def ensure_pane_thread(remote_channel, pane: PaneInfo) -> Thread`
- `async def apply_terminal_view(thread, pane_id, text, status, bridge_cfg) -> message_id`  # coalesce edits
- MappingStore persist `cache/mapping.json`

- [ ] **Step 1: Unit-test Terminal View coalesce without Discord** (fake clock / last_edit timestamp)

- [ ] **Step 2–4: Implement discord_map + terminal_view; commit**

```bash
git commit -m "feat(bot): Remote Channel, Pane Threads, Terminal Message views"
```

---

### Task 10: Bot core — connect registry remotes, handle push events

**Files:**
- Rewrite: `src/bot/bot.py`
- Create: `src/bot/runtime.py`  # owns dict[remote_id, GatewayClient]

**Interfaces:**
- On ready: load registry (+ import seeds once); for each bound Remote start GatewayClient
- Push handler:
  - `bridge.terminal_output` → terminal_view
  - `pane.agent_status_changed` → update thread name emoji; if blocked → choice_ui (Task 12)
  - `pane.created` / closed → optional notify in Remote Channel (do not auto-thread; Sync/create does)
- Delete `src/bot/ssh/`, stop using old event_loop poller

- [ ] **Step 1: Smoke test runtime with mock GatewayClient**

- [ ] **Step 2–4: Implement; commit**

```bash
git commit -m "feat(bot): runtime wiring for gateway push events"
```

---

### Task 11: `/herdr` commands — register, rebind, sync, pane close, passthrough groups

**Files:**
- Rewrite: `src/bot/commands.py`
- Delete obsolete command helpers as needed

**Interfaces:**
- Register slash command `herdr` with subcommands at minimum:
  - `register` (host, port, token, fingerprint, id?, create_channel?)
  - `rebind` (remote id optional → Select unbound)
  - `remove` / `status`
  - `sync` (remote from channel context)
  - `pane` group: `split`, `close`, `list`, `read`, …
  - `workspace` group: `list`, `create`, …
  - `help`
- Operator check on structural subcommands; ephemeral for secrets
- Context: if in Remote Channel, default remote_id; if in Pane Thread, default pane_id
- `pane close`: control `pane.close` then delete/archive thread
- After mapping a pane (sync/create): `observe_pane(id, True)`

- [ ] **Step 1: Test context resolution pure functions**

```python
def test_resolve_remote_from_channel(registry):
    ...
```

- [ ] **Step 2–4: Implement command tree; commit**

```bash
git commit -m "feat(bot): /herdr register rebind sync and pane ops"
```

---

### Task 12: Chat Input + Choice UI + Discord lifecycle events

**Files:**
- Create: `src/bot/chat_input.py`
- Create: `src/bot/choice_ui.py`
- Create: `src/bot/lifecycle.py`

**Interfaces:**
- `on_message`: if message in mapped Pane Thread, author not bot, not command → `pane.send_input(text, keys=["enter"])`
- `on_guild_channel_delete`: if channel_id bound → `registry.unbind_channel` + stop GatewayClient
- `on_thread_delete` / `on_raw_thread_delete`: unmap pane, `observe_pane(False)`
- Choice UI: on blocked, send View with Yes/No/Custom; `interaction_check` → `is_operator`; success → send_input + edit message remove view

- [ ] **Step 1: Unit-test operator check on fake interaction; test unbind on channel delete with mocks**

- [ ] **Step 2–4: Implement; commit**

```bash
git commit -m "feat(bot): chat input, choice UI, channel/thread lifecycle"
```

---

### Task 13: Compose / config / Dockerfile cleanup + README stub

**Files:**
- Modify: `Dockerfile`, `docker-compose.yml`, `.env.example`, `config.example.yaml`, `.gitignore`
- Remove: `keys/` mount and `keys/.gitkeep` (or leave empty unused—prefer remove mount)
- Create: `README.md` short pointer to spec + plugin link + compose up (Chinese OK for README)

**Compose volumes:** `config.yaml`, `cache/`, `logs/` only.

- [ ] **Step 1: Update files**

- [ ] **Step 2: `docker compose build` succeeds**

```bash
docker compose build
```

Expected: image build OK.

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: compose and config for TLS gateway bot"
```

---

### Task 14: End-to-end manual checklist (no Discord automation required)

**Files:** none (checklist in this plan)

- [ ] **Step 1: On a machine with Herdr**

```bash
herdr plugin link /path/to/herdr-discord-bridge/src/plugin
herdr plugin action invoke setup --plugin herdr.discord-bridge
herdr plugin action invoke start --plugin herdr.discord-bridge
# note host port token fingerprint
```

- [ ] **Step 2: Run Bot**

```bash
cp config.example.yaml config.yaml   # guild_id, token, operators
docker compose up -d --build
```

- [ ] **Step 3: In Discord**

1. `/herdr register` with values from setup  
2. Confirm Remote Channel + TLS connected (`/herdr status`)  
3. `/herdr sync` → Threads appear; Terminal Message updates without Bot pane.read polling  
4. Type in Thread → appears in Herdr pane  
5. Delete Thread → sync restores; `/herdr pane close` removes pane  
6. Delete Remote Channel → rebind in new channel without re-token  

- [ ] **Step 4: Commit any bugfixes from checklist**

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| TLS + fingerprint | 3, 4, 8 |
| Dual control/push + auth role | 4, 8 |
| Gateway owns push; Terminal View plain sliding window | 5 |
| Bot no live pane.read poll | 5, 10 |
| Remote=channel, Pane=Thread | 9 |
| Registry source of truth; unbind/rebind | 7, 11, 12 |
| `/herdr` single slash | 11 |
| Chat Input | 12 |
| Choice UI Operators-only + edit after click | 12 |
| Sync explicit | 11 |
| Plugin English setup | 6 |
| Compose Bot deploy | 13 |
| Remove SSH | 10, 13 |

**Placeholder scan:** Gateway-local `pane.read` loop is explicit interim for Terminal View; Task 5 documents it. No TBD steps.

**Type consistency:** `RemoteRecord`, `GatewayClient.request/observe_pane`, `bridge.auth` roles used uniformly across tasks.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-31-herdr-discord-bridge.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — execute tasks in this session with checkpoints  

Which approach?
