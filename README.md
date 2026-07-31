# Herdr Discord Bridge

Discord Bot + Herdr Gateway 插件，通过 **TLS NDJSON** 把远程 Herdr 主机映射进 Discord 服务器（Remote → 频道，Pane → Thread）。

## 文档

- 设计说明：[docs/superpowers/specs/2026-07-31-herdr-discord-bridge-design.md](docs/superpowers/specs/2026-07-31-herdr-discord-bridge-design.md)
- 领域用语：[CONTEXT.md](CONTEXT.md)

## 远程 Gateway 插件

在运行 Herdr 的主机上，将插件目录 link 到 Herdr：

```bash
herdr plugin link /path/to/herdr-discord-bridge/src/plugin
herdr plugin action invoke setup --plugin herdr-discord-bridge
herdr plugin action invoke start --plugin herdr-discord-bridge
```

记下 setup 输出的 host、port、token、TLS 指纹，供 Discord 内 `/herdr register` 使用。

## 启动 Bot（Docker Compose）

```bash
cp config.example.yaml config.yaml   # 填写 guild_id、operators 等
cp .env.example .env                 # 填写 DISCORD_TOKEN
docker compose up -d --build
```

挂载卷：`config.yaml`、`cache/`、`logs/`。
