# syncodex

> Sync Codex config, skills & model catalog from your machine to remote servers over SSH — with a web UI, and each server keeps its own model provider.
> 把本地的 Codex 配置 / skills / 模型目录通过 SSH 同步到一台或多台远端服务器，网页可视化操作；每台服务器保留自己的 model provider，互不干扰。

![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-0a0c10) ![license](https://img.shields.io/badge/license-MIT-2dd4bf) ![deps](https://img.shields.io/badge/deps-stdlib%20%2B%20optional%20paramiko-8a92a6)

---

## 它解决什么问题 / Problem

在本地用 ccswitch（或直接改 `config.toml`）切换模型后，远端服务器上的 Codex 不会自动跟着变。
手工 `scp` / `rsync` 又很麻烦，而且**本地和服务器路径不一致、provider 定义也不一样**，直接整份复制会报
`Model provider "codex" not found` 之类的错。

syncodex 是一个**本地网页控制台**：

- 在浏览器里添加/管理 SSH 服务器（主机、用户名、密钥或密码、远端 `.codex` 路径）
- 一键提取本地可移植配置 → 预览差异 → 同步到一台或多台服务器
- 可选重启服务器上的 Codex app-server，让模型列表立即刷新
- 服务器各自保留自己的 provider 与机器相关配置，本地怎么切都不影响远端

## 特性 / Features

- 🖥️ **纯网页操作**：双击启动脚本 → 浏览器打开控制台，不需要安装 App
- 🔐 **SSH 认证管理**：主机/端口/用户名 + 密钥文件或密码（和 Codex 连 SSH 一样录入）
- 📂 **路径各自独立**：本地选 `~/.codex`，每台服务器填各自的远端 `.codex` 路径
- 🔁 **多台或单台**：勾选任意台（≥1）批量同步
- 👁 **同步前预览差异**：共享配置、skills 文件、模型目录的 +/− 一目了然
- 🧩 **provider 永不覆盖**：`model_provider` 和 `[model_providers.*]` 不进共享段
- 🔄 **镜像模式可选**：默认远端多余文件会被删除（与本地完全一致），可关闭
- ♻️ **一键重启远端 app-server**：同步后让远端模型列表立即生效
- 🚫 **零依赖可用**：密钥认证直接用系统自带 OpenSSH（Win/mac/Linux 都有）；只有「密码登录」才需要可选安装 paramiko

## 快速开始 / Quickstart

> 需要 Python 3.9+（Windows 安装时勾选 *Add to PATH*）。

### Windows

```bat
:: 第一次：进入目录双击即可
start.bat
```

### macOS / Linux

```bash
chmod +x start.sh
./start.sh
# 停止：./stop.sh
```

浏览器会自动打开 `http://127.0.0.1:8765`。

**首次使用流程**：

1. 点「＋ 添加服务器」，录入 SSH 信息：名称、主机、端口、用户名、认证方式（密钥路径 / 密码）、远端 `.codex` 目录
2. 在「本地与同步设置」确认本地 Codex 文件夹（可点「自动检测」），保存
3. 勾选服务器 → 「① 提取共享配置」→「② 预览差异」→「③ 同步到所选」
4. 同步完成后点「重启进程」，远端模型列表立即刷新

## 认证方式 / Authentication

| 方式 | 是否需要安装 | 说明 |
|---|---|---|
| 密钥文件 / ssh-agent | ❌ 零依赖 | 填私钥路径（如 `~/.ssh/id_ed25519`），或留空走 `~/.ssh/config` / agent |
| 密码登录 | ✅ `pip install -r requirements.txt` | 需要 paramiko，网页里直接填服务器密码 |
| 加密私钥（带口令） | ✅ 建议 paramiko | 或先把密钥加载进 ssh-agent |

> 密码 / 口令会**明文**保存在本地 `config.json`（已被 `.gitignore` 忽略），请不要提交该文件。

## 同步逻辑 / How it works

- **共享段 `config.shared.toml`**：只含可移植顶层键（默认 `model` / `model_reasoning_effort` / `disable_response_storage` / `model_catalog_json` / `auto_review_model_override`），可在设置里改
- **provider 永不进共享段**：服务器第一次同步时自动生成 `config.server.tail.toml`（从原 `config.toml` 剥离共享键、保留 provider 与机器相关段，原文件备份为 `.bak-pre-sync`）
- **合并**：远端 `config.toml = 共享头 + 本机尾`
- **skills**：排除桌面内置的 `.system`，只同步用户技能；默认镜像模式（远端多余文件删除）
- **模型目录**：推送 `cc-switch-model-catalog.json`，预览会显示新增/移除的模型 slug
- **永不同步** `auth.json` / 任何密钥文件；服务只监听 `127.0.0.1`

## 安全说明 / Security

- 服务只绑定 `127.0.0.1`，不暴露到局域网/公网
- 不传输、不读取 `auth.json`、API Key 等凭据
- 密码等敏感字段在 `/api/status` 中一律打码，不会回显到前端
- 首次连接使用 `StrictHostKeyChecking=accept-new`（自动记录主机指纹）；如需更严格可在 `server.py` 中调整

## 与 codex-provider-sync 的关系 / Related projects

我们和 [Dailin521/codex-provider-sync](https://github.com/Dailin521/codex-provider-sync) 是**互补**的关系，推荐搭配使用：

- **codex-provider-sync**：解决「本地切换 provider 之后，历史会话在 Codex 里看不到/丢上下文」的问题 —— 同步会话文件与 SQLite 索引，写入前自动备份
- **syncodex**：解决「把本地最新配置 / skills / 模型目录分发到远程 SSH 服务器」的问题 —— 多服务器批量同步 + 网页可视化 + 保留各自 provider

一句话：**codex-provider-sync 管“会话找回”，syncodex 管“配置/技能下发”**，两者互不冲突。

## 常见问题 / FAQ

- **端口被占用**：改 `config.json` 的 `port`，并同步修改 `start.bat` / `stop.bat` / `start.sh` / `stop.sh` 里的端口号
- **没有 python**：先装 Python 3（https://www.python.org），勾选 *Add to PATH*
- **提示需安装 paramiko**：该服务器用的是「密码认证」，执行 `pip install -r requirements.txt`；密钥认证不需要
- **同步后远端模型列表没变**：点「重启进程」，app-server 在启动时才加载模型目录
- **首次同步自动生成 tail 后，服务器配置会不会被弄坏**：原 `config.toml` 会先备份为 `config.toml.bak-pre-sync`，可随时回滚

## 项目结构 / Layout

```
syncodex/
├── server.py            # 本地后端（stdlib HTTP + 系统 ssh/scp，paramiko 可选）
├── index.html           # 网页控制台（单文件，无外部依赖，离线可用）
├── config.example.json  # 配置示例（不包含任何真实凭据）
├── config.json          # 本地配置（含服务器信息，已被 .gitignore 忽略）
├── requirements.txt     # 可选依赖：仅密码认证需要 paramiko
├── start.bat / stop.bat # Windows 启动/停止
├── start.sh  / stop.sh  # macOS / Linux 启动/停止
└── LICENSE              # MIT
```

## License

[MIT](./LICENSE)
