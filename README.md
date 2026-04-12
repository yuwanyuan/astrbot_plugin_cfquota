# astrbot_plugin_cfquota

> AstrBot 插件 — 查询 Cloudflare Workers 计算额度使用情况，支持多账号管理和定时推送

## ✨ 功能

- 📊 查询 Workers 请求次数和 CPU 时间使用情况
- 👥 支持添加多个 Cloudflare 账号
- ⭐ 支持设置默认账号
- 🔍 添加账号时自动验证有效性
- ⏰ 定时推送额度信息（支持自定义时间和账号）
- 📡 立即推送功能
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

### 定时推送

| 命令 | 说明 | 示例 |
|------|------|------|
| `/cfpush status` | 查看推送状态 | `/cfpush` |
| `/cfpush on [小时...]` | 开启推送（默认 8 20） | `/cfpush on 8 20` |
| `/cfpush off` | 关闭推送 | `/cfpush off` |
| `/cfpush hours 小时...` | 修改推送时间 | `/cfpush hours 9 18 21` |
| `/cfpush accounts 名称...` | 指定推送账号 | `/cfpush accounts 主账号` |
| `/cfpush now` | 立即推送一次 | `/cfpush now` |
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

       ⚡ 今日请求次数:
         已用: 12,345
         限制: 100,000/天
         使用率: 12.35%

       ⏱ CPU 时间: 10ms/请求（免费版）
```

### 开启定时推送
```
用户: /cfpush on 8 20
机器人: ✅ 定时推送已开启！

         ⏰ 推送时间: 08:00, 20:00
         📊 推送账号: 全部账号
         🎯 推送目标: 当前会话
```

### 立即推送
```
用户: /cfpush now
机器人: 🔍 正在查询额度...
       （随后收到推送消息）
```

## ⏰ 定时推送原理

1. 插件启动时注册一个后台异步任务，每 60 秒检查一次
2. 到达指定整点时间时，自动查询所有（或指定）账号的额度
3. 通过 `self.context.send_message()` 主动推送到绑定的会话
4. 使用 KV 存储防止同一小时重复推送

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

## 📜 许可证

MIT
