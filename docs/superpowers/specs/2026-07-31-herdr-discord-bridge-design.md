# Herdr Discord Bridge — 设计说明

**日期：** 2026-07-31  
**状态：** 待审阅  
**领域用语：** [CONTEXT.md](../../../CONTEXT.md)  
**决策记录：** [docs/adr/](../../adr/)

---

## 1. 目标

用**一个** Discord Bot，把一台或多台远程 Herdr 主机映射进私有服务器：

- 每台主机对应一个文字频道（Remote Channel）
- 每个 Herdr Pane 对应该频道下的一个 Thread（Pane Channel）
- 在 Thread 里打字即驱动对应 Pane
- 结构化操作统一走 `/herdr`

远程侧只通过 Herdr 插件 Gateway，使用 **TLS + NDJSON 长连接** 通讯。  
不做：SSH、HTTP、Bot 侧输出轮询。

---

## 2. Discord 映射

| 概念 | Discord | 说明 |
|------|---------|------|
| Remote（主机 + Gateway） | 一个**文字频道**（Remote Channel） | Operator 登记主机时创建/绑定 |
| Pane | 该频道下的 **Thread**（Pane Channel） | 放 Terminal Message；支持 Chat Input |
| Workspace / Tab | 仅体现在命名/标签 | 不单独占 Discord 层级 |

**主流程：**

1. Operator 用 `/herdr` 登记 Remote（host、port、token、TLS 指纹）→ Bot 创建/绑定 Remote Channel → 对该 Gateway 建立 Control + Push 两条 TLS 连接。
2. 在 Remote Channel 内：用 `/herdr` 新建 Pane（Herdr + Thread），或对已有 Pane 执行 Sync。
3. 在 Pane Thread 内：人类每条消息 → `pane.send_input`（文本 + Enter）。Bot 根据推送编辑一条 Terminal Message。
4. Agent 需要有限选择时：在该 Pane Thread 发 Button/Select/Modal，仅 Operator 可点；成功后编辑消息去掉组件。

### 2.1 Discord 对象生命周期

| 事件 | 行为 |
|------|------|
| 删除 Remote Channel（或显式解绑） | 断开该 Remote 的 TLS；清除频道/Thread 映射；**Registry 保留** host/port/token/指纹 |
| 重新绑定 | 在目标新频道内 `/herdr rebind`，Select 选择「未绑定」Remote，绑到**当前频道**并重连；需要 Thread 时再 Sync |
| 删除 Pane Thread | 停止 observe、清除该 Pane 的 Discord 映射；**不**关闭 Herdr Pane；`/herdr sync` 可再建 Thread |
| 真正移除 Pane | `/herdr` → pane close（控制面透传 Herdr）；成功后 Bot 删除或归档对应 Thread |
| Choice UI 点选成功 | 编辑原消息记录结果并移除按钮；失败 ephemeral，可保留按钮重试 |

---

## 3. 架构

```
Discord 服务器
  Remote Channel ── Threads（各 Pane）
         ▲
         │ discord.py
         │
      Bot（Docker Compose）
         │  每 Remote：TLS ×2（control + push）
         ▼
  Gateway（主机上的 Herdr 插件）
         │  AF_UNIX
         ▼
    herdr.sock
```

| 组件 | 职责 |
|------|------|
| **Bot** | Discord API、Remote Registry、映射、Terminal Message 编辑、`/herdr`、Chat Input、Choice UI、解绑/重绑、Operator 校验 |
| **Gateway** | TLS 监听、鉴权、连接本机 herdr.sock、**拥有 push plane**、产出 Terminal View、控制面透传 |

不做：Web 面板、HTTP API、SSH 通道、双 transport、完整 VT100、依赖社区 HTTP 类插件。

---

## 4. 线协议

### 4.1 连接

每个 Remote，Bot 与 `host:port` 保持两条 **TLS** 长连接：

| 连接 | `bridge.auth` 的 role | 流量 |
|------|----------------------|------|
| Control Connection | `"control"` | 请求/响应（多为 Herdr 透传） |
| Push Connection | `"push"` | 仅 Gateway → Bot 的事件 |

每条连接首帧：

```json
{"id":"auth_1","method":"bridge.auth","params":{"token":"<token>","role":"control"}}
```

成功：`{"id":"auth_1","result":{"type":"ok","protocol":1}}`。失败或缺少/错误 role → 断开。  
Bot 用 Remote Registry 中的 **SHA-256 证书指纹** 校验 Gateway 证书（允许自签）。  
断线：指数退避重连 → 双连接重新鉴权 → 为已 Mapped Pane 重新开启 observe。

### 4.2 控制面

鉴权后，Control Connection 承载与 herdr 同形的 NDJSON RPC（透传到 `herdr.sock`），以及 bridge 方法：

- `bridge.observe_pane` `{pane_id, enable}` — 为 Mapped Pane 开启/停止 Terminal View 推送
- 按需透传：`ping`、`session.snapshot`、`workspace.*`、`pane.*`、`agent.*` 等

信封：`{id, method, params}` → `{id, result}` 或 `{id, error}`。

### 4.3 推送面（Gateway 拥有）

仅 Gateway 持有 Herdr 的 `events.subscribe` 与 terminal observe，并向各 Push Connection 扇出：

**生命周期 / agent**（保持 herdr 事件形）：

```json
{"event":"pane.agent_status_changed","data":{...}}
{"event":"pane.created","data":{...}}
```

至少订阅：workspace 创建/关闭，pane 创建/关闭/退出，`pane.agent_status_changed`（按需按 pane 动态加订）。

**Terminal View**（bridge 形：纯文本滑动窗口——不是全量历史，不是增量 delta）：

```json
{
  "event": "bridge.terminal_output",
  "data": {
    "pane_id": "w1:p1",
    "revision": 12,
    "text": "纯文本 Terminal View",
    "truncated": false
  }
}
```

Gateway 负责去 ANSI、合并节流后再推。Bot 替换缓冲，并在 Discord 限流下编辑 Terminal Message。  
**Bot 不得用定时 `pane.read` 做实时画面。** 一次性查询（如 `/herdr` read）仍可走 Control Connection 的 `pane.read`。

只对 **Mapped Pane** 做 observe；Bot 在 Thread 映射就绪后通过 Control Connection 开启。

---

## 5. Slash 与 Operator

- 顶层命令只有一条：**`/herdr`**，动作用 subcommand（可用 Discord 的 subcommand group 对应 `workspace` / `pane` 等）。不发明顶层 `/herdr-pane` 等，除非 Herdr 本身提供独立的 `herdr-*` 命令。
- **Operator：** 需 Manage Guild（或配置的角色），并可叠加 user id 白名单。
- 含密钥的回复：ephemeral（避免以后加人协作时 token 留在频道历史里）。
- **Remote Registry**（`cache/`）：host、port、token、指纹、Remote Channel id 的运行时权威来源。`config.yaml` 放 Discord token、guild、operator 配置；remotes 仅可作一次性种子。
- **Sync：** 显式 `/herdr sync` 才为已有 Herdr Pane 建 Thread；连接成功时不自动铺满。
- **Chat Input：** Pane Thread 内所有非 Bot 用户消息都送入对应 pane（ADR-0013）。

代表性 subcommand（非穷尽，以实现覆盖 Herdr socket 为准）：

| 区域 | 示例 |
|------|------|
| Remote | register / update / remove / status / rebind（未绑定列表） |
| Sync | sync |
| Workspace | list / create / close / rename / focus |
| Pane | list / split / close / rename / focus / read |
| Agent | list / prompt / status / wait |
| Help | help |

具体参数受 Discord 限制与 Herdr 参数约束；在 Remote Channel / Pane Thread 内可省略 `remote` / `pane`（从上下文反查）。

---

## 6. Gateway 插件（远程主机）

Herdr 插件 id：`herdr.discord-bridge`（最终以 manifest 为准）。

| 动作 | 行为 |
|------|------|
| `setup` | 生成 token、自签 TLS 证书、写配置；**英文**打印 host/port/token/指纹及 Bot 登记提示（不打印 scp） |
| `start` | TLS 监听、连接 `herdr.sock`、接受 Bot、运行推送泵 |
| `stop` / `status` | 生命周期 / 健康 |
| `teardown` | 可选：停止监听、清理鉴权材料 |

插件配置目录：`listen_host`、`listen_port`、`token`、`herdr_socket`、证书路径。

插件是薄 Gateway + 推送泵，不是第二个 Discord Bot。插件面向用户的安装/setup 提示使用英文。

---

## 7. Bot 部署

Docker Compose：出站访问 Discord API，以及对各 Remote Gateway 的 TLS。  
挂载：`config.yaml`、`cache/`、`logs/`。不挂 SSH 私钥，不挂 `herdr.sock`。

```bash
docker compose up -d --build
```

---

## 8. 容错

| 场景 | 处理 |
|------|------|
| TLS / 指纹不匹配 | 拒绝连接；向 Operator 告警（若配置了告警频道） |
| 鉴权失败 | 断开；告警 |
| TCP 断开 | 退避重连；重新鉴权；为 Mapped Pane 重新 observe |
| herdr.sock 不可用 | Gateway 重连 sock；可发健康事件；结构漂移时 Bot Sync |
| Discord 编辑限流 | 合并 Terminal Message 编辑（`edit_cooldown`） |
| Thread 丢失 | Sync 或按需重建 |

---

## 9. 目标仓库结构

```
docs/
  adr/                          # ADR 0001–0015+
  superpowers/specs/            # 本设计文档
CONTEXT.md
src/
  bot/                          # Discord Bot（Compose）
  plugin/                       # Herdr Gateway 插件
Dockerfile / docker-compose.yml
config.example.yaml
```

---

## 10. 决策索引

| ADR | 摘要 |
|-----|------|
| 0001 | Gateway 拥有 push plane |
| 0002 | Gateway 产出纯文本终端输出 |
| 0003 | Terminal View 为滑动窗口快照 |
| 0004 | Control / Push 分两条 TCP |
| 0005 | `bridge.auth` 声明 role |
| 0006 | 仅观察 Mapped Pane |
| 0007 | 单一 `/herdr` slash 命令 |
| 0008 | Remote Channel 承载 Pane 子频道 |
| 0009 | Pane 为 Discord Thread |
| 0010 | Operator = 管理员 + 可选白名单 |
| 0011 | Remote Registry 为权威来源 |
| 0012 | 已有 Pane 靠显式 Sync |
| 0013 | Chat Input 发送全部用户消息 |
| 0014 | Gateway 必须 TLS |
| 0015 | 以证书指纹钉扎信任 |
| 0016 | 解绑保留凭据；重绑恢复频道 |
| 0017 | 在目标频道内执行 rebind |
| 0018 | 删 Thread 只解映射；关 Pane 走 Herdr |
| 0019 | 有限选择优先 Discord 组件 |
| 0020 | 抉择发在 Pane Thread，仅 Operator |
| 0021 | 点选成功后编辑消息并去按钮 |

---

## 11. 成功标准

- Operator 能在 Discord 用 host/port/token/指纹登记 Remote，并得到 Remote Channel。
- 误删 Remote Channel 后，可在新频道 `rebind` 而不必重贴 token/指纹。
- Sync 或新建 Pane 后出现 Thread，Terminal View 靠推送更新，Bot 不轮询刷屏。
- 删 Thread 不杀 Pane；`pane close` 才真正移除 Pane。
- 在 Thread 里打字能驱动对应 Herdr Pane；blocked 等有限选择可用按钮，仅 Operator。
- `/herdr` 覆盖结构化 Herdr 操作，不堆大量顶层 slash 命令。
- 无 SSH / HTTP 控制通路；Control / Push 均为 TLS NDJSON 长连接。
