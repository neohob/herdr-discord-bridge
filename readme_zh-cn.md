# Herdr Discord Bridge

[English](README.md) · 中文

把多台机器上的 [Herdr](https://herdr.dev) Pane 映射进 Discord：在频道/线程里对话即可向对应 Pane 发送命令，并接收终端画面推送。

- **机器人（Discord Bot）**：连接 Discord；经网络访问各主机上的 TLS 插件。可用 Docker 部署（本机 / Unraid），便于迁移。
- **主机插件（Herdr Plugin）**：装在**每一台运行 Herdr 的机器**上，桥接本机 `herdr.sock` 与 TLS 端口。必须与 Herdr 同机（Unix socket，不能跨网挂 sock）。

> 本文「机器人」= Discord 进程；「主机插件」= 各 Herdr 主机上的插件。勿与「一台中心 Gateway 直连所有 herdr.sock」混淆。

## 架构

```text
Discord 用户
    ↕ Discord API
机器人（Docker，可放 Unraid；建议与 Tailscale 容器共用网络）
    ↕ Tailscale / 内网  TLS NDJSON :9876
主机插件（每台 Herdr 主机各一个）
    ↕ Unix
herdr.sock → Pane / Workspace / Tab
```

| Discord 概念 | 含义 |
|--------------|------|
| Remote 频道 | 一台已注册主机（一个主机插件端点） |
| Pane 线程 | 该主机上的一个 Herdr Pane |
| 线程内聊天 | 转发为 `pane.send_text` + `pane.send_keys Enter` |
| Terminal Message | 线程内由推送更新的终端画面 |

跨 Tailscale 主机：每台装主机插件；在 Discord `/herdr register` 时填该机的 **Tailscale IP**、端口、token、证书指纹。

## 仓库结构

```text
src/
  bot/                 # Discord 机器人
  plugin/              # Herdr 主机插件（herdr-plugin.toml + gateway + ctl.sh）
  shared/              # NDJSON / TLS 指纹等共用逻辑
docker/
  gateway-entrypoint.sh
  unraid-herdr-gateway.xml   # Unraid DockerMan 模板（主机插件镜像，可选）
Dockerfile             # 机器人镜像
Dockerfile.gateway     # 主机插件镜像（可选，无 Herdr 的机器勿单独用）
docker-compose.yml     # 通用机器人 Compose
docker-compose.unraid.yml  # Unraid：机器人 network_mode=container:Tailscale-Docker
config.example.yaml
docs/                  # 设计 / ADR / CONTEXT
```

## 1. 主机插件（每台 Herdr 机器）

遵循 [Herdr plugins](https://herdr.dev/docs/plugins/) 规范：`herdr-plugin.toml` + argv actions；配置在 `HERDR_PLUGIN_CONFIG_DIR`，运行时状态在 `HERDR_PLUGIN_STATE_DIR`。TLS Gateway 是长驻进程，用 **`start` action** 拉起（不要用 `[[startup]]`，规范要求 startup 必须退出）。

### 要求

- Herdr ≥ 0.7
- 本机存在 `herdr.sock`（常见：`~/.config/herdr/herdr.sock`）
- Python 3.11+，依赖 `cryptography` / `PyYAML`（GitHub `plugin install` 时由 `[[build]]` 安装）
- 防火墙 / Tailscale 放行监听端口（默认 `9876`）

### 安装

**GitHub（发布 / 终端用户）：**

```bash
herdr plugin install neohob/herdr-discord-bridge/src/plugin
herdr plugin config-dir herdr-discord-bridge
```

**本地开发（本仓库）：**

```bash
herdr plugin link "$(pwd)/src/plugin"
```

### 启停

```bash
herdr plugin action invoke setup  --plugin herdr-discord-bridge
herdr plugin action invoke start  --plugin herdr-discord-bridge
herdr plugin action invoke status --plugin herdr-discord-bridge
# stop / teardown 同理
```

`setup` 会生成 token、自签证书，并打印供 Discord 注册用的信息。

| 路径 | 用途 |
|------|------|
| `HERDR_PLUGIN_CONFIG_DIR` | `config.yaml`、TLS 证书（通常 `~/.config/herdr/plugins/config/herdr-discord-bridge/`） |
| `HERDR_PLUGIN_STATE_DIR` | `gateway.pid`、`gateway.log`（通常 `~/.config/herdr/plugins/state/herdr-discord-bridge/`） |

可选环境变量：`GATEWAY_LISTEN_PORT`、`GATEWAY_PUBLIC_HOST`、`HERDR_SOCKET_PATH`、`HERDR_BIN_PATH`。

### 上架 Marketplace

给本仓库加上 GitHub topic **`herdr-plugin`**，即可进入 Herdr marketplace 索引（约 30 分钟刷新）。用户安装路径：`neohob/herdr-discord-bridge/src/plugin`。

### 注册时建议填写

| 字段 | 说明 |
|------|------|
| host | 该机 Tailscale IP（推荐）或可达内网 IP；机器人与插件同机可用 `127.0.0.1` |
| port | 默认 `9876` |
| token | setup 输出 |
| fingerprint | 证书 SHA-256 十六进制（64 字符） |

## 2. 机器人（Discord Bot）

### Discord 开发者后台

1. 创建 Application → Bot，复制 Token  
2. 打开 **Message Content Intent**  
3. **OAuth2 → Requires OAuth2 Code Grant** 关闭  
4. 邀请链接（替换 `CLIENT_ID`）：

```text
https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=8&scope=bot%20applications.commands
```

5. 邀请到目标服务器后，复制**服务器 ID**（开发者模式）写入配置 `guild_id`

### 配置文件

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

`.env`：

```bash
DISCORD_TOKEN=你的BotToken
LOG_LEVEL=info
```

`config.yaml` 要点：

| 字段 | 说明 |
|------|------|
| `discord.guild_id` | 运行机器人的服务器 ID |
| `discord.token` | 可用 `"${DISCORD_TOKEN}"` |
| `operators.require_manage_guild` | `true` 时需 Manage Guild（或匹配 role） |
| `operators.user_ids` / `role_ids` | 可选限制操作者 |
| `bridge.terminal.max_lines` | 终端画面行数 |
| `bridge.terminal.edit_cooldown` | Discord 消息编辑冷却（秒） |
| `seed_remotes` | 可选；首次写入 `cache/remotes.json`，之后以 Discord 注册为准 |

数据目录：

- `cache/remotes.json` — Remote 注册表（token/指纹等）  
- `cache/mapping.json` — 频道 / 线程映射  
- `logs/` — 日志  

**不要把 `.env` / `config.yaml` / `cache/remotes.json` 提交进 git。**

### 本机直接跑

```bash
export $(grep -v '^#' .env | xargs)
export BRIDGE_CONFIG="$PWD/config.yaml" PYTHONPATH=.
python -m src.bot
```

### Docker Compose（通用）

```bash
docker compose up -d --build
```

挂载：`config.yaml`、`cache/`、`logs/`。

### Unraid + Tailscale（推荐）

机器人与 `Tailscale-Docker` **共用网络命名空间**，才能访问各机 `100.x:9876`：

```bash
# 见 docker-compose.unraid.yml
# network_mode: "container:Tailscale-Docker"
```

注意：Unraid 若无有 `docker compose` v2，可用等价 `docker run --network container:Tailscale-Docker ...`。  
镜像构建用仓库根目录 `Dockerfile`。配置建议放在：

```text
/mnt/user/appdata/herdr-discord-bridge/{config.yaml,.env,cache,logs}
```

同一 Bot Token **只能跑一个机器人实例**。

## 3. Discord 用法

在目标服务器的**文字频道**中（需 Operator 权限）：

### 注册一台主机并开频道

```text
/herdr register
```

| 选项 | 示例 |
|------|------|
| host | `100.x.x.x`（Tailscale） |
| port | `9876` |
| token / fingerprint | 主机插件 setup 输出 |
| id | 可选，如 `macbook` |
| create_channel | `True` → 自动建 Remote 频道 |

### 同步 Pane → 线程

进入 Remote 频道：

```text
/herdr sync
```

会按 Herdr **实时** `workspace.list` / `tab.list` / `pane.list` 建或重命名线程，格式类似：

```text
🟢 JinAn-MAP › cursor · Cursor Agent [wB:p6]
```

（工作区名 › Tab label · 显示名 [pane_id]，无静态对照表。）

### 日常

| 操作 | 方式 |
|------|------|
| 向 Pane 发命令 | **聊天模式**：发一条 →「思考中…」；输出**只追加不删**，同条消息刷新；装满后封存再开「（续）」下一条，频道里可翻完整历史 |
| Approve / 选择 | 仅在真正确认提示 / `blocked` 时出 Yes/No/Custom |
| 看输出 | 纯文本 Bot 气泡；没有输入时不刷屏 |
| 列表 / 读写 | `/herdr pane list\|read\|…` |
| 状态 | `/herdr status` |
| 换绑频道 | 在新频道 `/herdr rebind` |
| 帮助 | `/herdr help` |

斜杠命令若未出现：等 1～2 分钟或重开客户端；确认机器人在线且 `guild_id` 正确。

## 4. 常见问题

| 现象 | 处理 |
|------|------|
| 「应用程序未响应」 | 多为 sync 建线程较慢；已 defer。稍后重试 `/herdr sync` |
| 文字进了 Pane 但不执行 | 需 `send_keys Enter`（勿只发 `\n`）。请使用当前版本机器人 |
| 只能看到 NVBOT 联系人 | 需用含 `bot` scope 的链接邀请进**服务器** |
| Unraid 机器人连不上 `100.x` | 使用 `--network container:Tailscale-Docker`（或宿主机已装 Tailscale） |
| 删 Remote 频道 | 凭证保留，可在新频道 `rebind`；真正删主机用 `/herdr remove` |

## 5. 开发与测试

```bash
python -m pip install -r requirements.txt   # 或 pip install -e ".[dev]"
PYTHONPATH=. pytest -q
```

更细的设计与术语见：

- [docs/superpowers/specs/2026-07-31-herdr-discord-bridge-design.md](docs/superpowers/specs/2026-07-31-herdr-discord-bridge-design.md)
- [CONTEXT.md](CONTEXT.md)
- [docs/adr/](docs/adr/)
