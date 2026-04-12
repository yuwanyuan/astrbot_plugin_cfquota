<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_cfquota?name=astrbot_plugin_cfquota&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_cfquota

_📊 Cloudflare Workers 额度查询插件（AstrBot） 📊_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.9.2%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Cloudflare](https://img.shields.io/badge/Cloudflare-Workers-F38020?logo=cloudflare&logoColor=white)](https://workers.cloudflare.com/)

</div>

> AstrBot 插件 — 查询 Cloudflare Workers 计算额度使用情况，支持多账号管理、后台数据采集和定时推送

## ✨ 功能

- 📊 查询 Workers 请求次数和 CPU 时间使用情况
- 👥 支持添加多个 Cloudflare 账号
- ⭐ 支持设置默认账号
- 🔍 添加账号时自动验证有效性
- ⏱ 后台数据采集（每 30/60 分钟从 CF API 拉取数据并缓存）
- ⏰ 定时推送缓存数据（自定义整点时间，推送即时无延迟）
- 📡 手动采集和立即推送功能
- 🤖 通过 AstrBot 跨平台使用（QQ / Telegram / 飞书 / 钉钉 / Slack 等）

## 📦 安装

### 方式一：通过 AstrBot 插件市场安装

在 AstrBot 管理面板 → 插件市场 中搜索 `cfquota` 并安装。

### 方式二：手动安装

1. 将本目录复制到 AstrBot 的 `data/plugins/` 目录下
2. 重启 AstrBot 或在管理面板中刷新插件

### 方式三：从 Git 安装

在 AstrBot 管理面板 → 插件管理 → 从 Git 仓库安装，填入仓库地址。

## 🔧 配置

### 方式一：命令行配置（推荐）

直接在聊天中发送命令即可管理账号，数据保存在 KV 存储中：

```
/cfadd 主账号 your_account_id your_api_token
```

### 方式二：AstrBot 管理面板配置

在 AstrBot 管理面板 → 插件配置 中找到「CF Workers 额度查询」，添加账号：

| 字段 | 说明 |
|------|------|
| **账号别名** | 自定义名称，用于区分多个账号 |
| **Account ID** | Cloudflare Account ID |
| **API Token** | Cloudflare API Token |

## 📋 命令列表

### 账号管理

| 命令 | 说明 | 示例 |
|------|------|------|
| `/cf额度 [名称]` | 查询额度（不指定则查默认账号） | `/cf额度` 或 `/cf额度 工作账号` |
| `/cfadd 名称 ID Token` | 添加 Cloudflare 账号 | `/cfadd 主账号 abc123 token456` |
| `/cflist` | 列出所有已添加的账号 | `/cflist` |
| `/cfdel 名称` | 删除指定账号 | `/cfdel 主账号` |
| `/cfdefault 名称` | 设置默认账号 | `/cfdefault 主账号` |

### 用量监控

| 命令 | 说明 | 示例 |
|------|------|------|
| `/cfpush status` | 查看监控状态 | `/cfpush` |
| `/cfpush on [小时...]` | 开启监控（默认 8 20） | `/cfpush on 8 20` |
| `/cfpush off` | 关闭监控 | `/cfpush off` |
| `/cfpush interval 30\|60` | 数据采集间隔（分钟） | `/cfpush interval 30` |
| `/cfpush hours 小时...` | 修改推送时间 | `/cfpush hours 9 18 21` |
| `/cfpush accounts 名称...` | 指定监控账号 | `/cfpush accounts 主账号` |
| `/cfpush now` | 采集并立即推送一次 | `/cfpush now` |
| `/cfpush fetch` | 手动触发一次数据采集 | `/cfpush fetch` |
| `/cfhelp` | 显示帮助信息 | `/cfhelp` |

## 🔑 获取 Cloudflare 凭证

### Account ID
1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com)
2. 选择你的账户
3. 右侧 **API** 区域找到 Account ID

### API Token
1. 访问 [API Tokens](https://dash.cloudflare.com/profile/api-tokens)
2. 点击 **Create Token**
3. 选择自定义权限：
   - **Account** → **Workers Scripts** → **Read**
4. 复制生成的 Token

## 🖼️ 使用示例

### 添加账号
```
用户: /cfadd 主账号 abc123def456 v4Kx8mP2qR7nT5wY
机器人: ✅ 账号添加成功！
         名称: 主账号
         账户: My Cloudflare Account
         Account ID: abc123de...f456
         ⭐ 已设为默认账号
```

### 查询额度
```
用户: /cf额度
机器人: 📊 Cloudflare Workers 额度查询

       🏢 账户: My Cloudflare Account（主账号）
       🕐 数据时间: 2026-04-13 08:30:00

       ⚡ 今日请求次数:
         已用: 12,345
         限制: 100,000/天
         使用率: 12.35%

       ⏱ CPU 时间: 10ms/请求（免费版）
```

### 开启用量监控
```
用户: /cfpush on 8 20
机器人: ✅ 用量监控已开启！

         ⏱ 数据采集: 每 60 分钟
         ⏰ 推送时间: 08:00, 20:00
         📊 监控账号: 全部账号
         🎯 推送目标: 当前会话
```

### 修改采集间隔
```
用户: /cfpush interval 30
机器人: ✅ 数据采集间隔已更新: 每 30 分钟
```

### 手动采集数据
```
用户: /cfpush fetch
机器人: ✅ 数据采集完成，已缓存 2 个账号的数据
         • 主账号: 采集于 2026-04-13 08:30:00
         • 备用账号: 采集于 2026-04-13 08:30:05
```

### 立即推送
```
用户: /cfpush now
       （先自动采集数据，再推送缓存结果）
```

## 📡 用量监控架构

```
┌──────────────────────────────────────────────┐
│              插件启动                          │
│                                               │
│  ┌─────────────────┐  ┌─────────────────────┐ │
│  │   数据采集循环    │  │    推送循环          │ │
│  │                  │  │                     │ │
│  │  每 30/60 分钟    │  │  每 60 秒检查        │ │
│  │       ↓          │  │       ↓             │ │
│  │  调用 CF API     │  │  当前时间 == 整点?    │ │
│  │       ↓          │  │    ↓         ↓      │ │
│  │  写入内存缓存     │  │   Yes       No      │ │
│  │  _usage_cache    │  │    ↓         ↓      │ │
│  │                  │  │  推送缓存   等待      │ │
│  └─────────────────┘  │  数据到会话           │ │
│                        └─────────────────────┘ │
└──────────────────────────────────────────────┘
```

**优势**：
- 📤 **推送即时**：推送时使用缓存数据，无需等待 API 响应
- 🔄 **数据新鲜**：采集间隔可配置 30/60 分钟，数据始终保持更新
- 🛡️ **容错降级**：API 临时故障时仍可推送最近的缓存数据
- ⚡ **手动查询优先使用缓存**：`/cf额度` 命令优先返回缓存数据，缓存过期才实时查询

**推送配置持久化**：推送设置保存在 KV 存储中，重启 AstrBot 后自动恢复。

## 🏗️ 项目结构

```
astrbot_plugin_cfquota/
├── main.py              # 插件主文件（所有逻辑）
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置 Schema（管理面板用）
├── requirements.txt     # Python 依赖
└── README.md            # 说明文档
```

## 🧩 依赖

- `aiohttp` — 异步 HTTP 请求（调用 Cloudflare API）

> AstrBot 运行环境已内置 `aiohttp`，通常无需额外安装。

## ⚠️ 注意事项

- API Token 仅需要 **Workers Scripts Read** 权限，请遵循最小权限原则
- 额度数据来自 Cloudflare API，可能有 5-15 分钟延迟
- 免费版 Workers 的限额：每天 100,000 请求、每次 10ms CPU 时间
- KV 存储中的账号数据仅保存在本地，不会上传
- 定时推送的精度为 ±1 分钟，非精确到秒
- 数据采集间隔建议 60 分钟（30 分钟更实时但 API 调用更频繁）

## 📜 许可证

MIT
