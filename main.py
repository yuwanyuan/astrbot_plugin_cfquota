"""
AstrBot 插件：CF Workers 额度查询
查询 Cloudflare Workers 计算额度使用情况，支持多账号管理和定时推送

命令列表：
  /cf额度 [账号别名]   - 查询 Workers 额度（不指定则查询默认账号）
  /cfadd 名称 AccountID ApiToken  - 添加 Cloudflare 账号
  /cflist             - 列出所有已添加的账号
  /cfdel 名称         - 删除指定账号
  /cfdefault 名称     - 设置默认账号
  /cfpush             - 管理定时推送
  /cfhelp             - 显示帮助信息
"""

import asyncio
import aiohttp
import logging
from datetime import datetime, time as dt_time, timezone
from astrbot.api.star import Context, Star, register
from astrbot.api import filter
from astrbot.api.event import AstrMessageEvent, MessageChain

logger = logging.getLogger("astrbot_plugin_cfquota")

# Cloudflare API 基地址
CF_API_BASE = "https://api.cloudflare.com/client/v4"

# 免费版 Workers 限额（用于估算）
FREE_PLAN_LIMITS = {
    "requests_per_day": 100000,
    "cpu_time_per_request_ms": 10,
}


# ============ Cloudflare API 调用 ============

async def cf_get_account_info(api_token: str, account_id: str) -> dict:
    """获取 Cloudflare 账户基本信息"""
    url = f"{CF_API_BASE}/accounts/{account_id}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if not data.get("success"):
                errors = data.get("errors", [{}])
                msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
                raise Exception(msg)
            return data["result"]


async def cf_get_workers_usage(api_token: str, account_id: str) -> dict:
    """查询 Workers 使用量，依次尝试多个 API"""
    async with aiohttp.ClientSession() as session:
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

        # 尝试 1: 订阅使用量接口
        try:
            url = f"{CF_API_BASE}/accounts/{account_id}/workers/subscriptions/usage"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        return data.get("result", {})
        except Exception as e:
            logger.debug(f"Subscription usage API failed: {e}")

        # 尝试 2: GraphQL Analytics API
        try:
            return await cf_get_workers_analytics(api_token, account_id, session)
        except Exception as e:
            logger.debug(f"GraphQL analytics API failed: {e}")

        # 所有方式都失败
        return {"source": "unavailable"}


async def cf_get_workers_analytics(api_token: str, account_id: str, session: aiohttp.ClientSession) -> dict:
    """通过 GraphQL Analytics API 查询 Workers 使用量（请求次数 + CPU 时间）"""
    graphql_url = f"{CF_API_BASE}/graphql"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_str = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 查询请求次数和 CPU 时间 (wallTime, 单位: 纳秒)
    query = """
    query {
      viewer {
        accounts(filter: {accountTag: "%s"}) {
          workersInvocationsAdaptive(limit: 100, filter: {
            datetime_geq: "%s",
            datetime_lt: "%s"
          }) {
            sum {
              requests
              errors
              wallTime
              subrequests
            }
            dimensions {
              scriptName
              status
            }
          }
        }
      }
    }
    """ % (account_id, today_str, now_str)

    async with session.post(graphql_url, headers=headers, json={"query": query}) as resp:
        data = await resp.json()

    # 检查 GraphQL 错误
    if data.get("errors"):
        error_msg = data["errors"][0].get("message", "Unknown GraphQL error")
        raise Exception(f"GraphQL: {error_msg}")

    accounts = data.get("data", {}).get("viewer", {}).get("accounts", [])
    if not accounts:
        return {"source": "unavailable"}

    invocations = accounts[0].get("workersInvocationsAdaptive", [])
    if not invocations:
        return {
            "source": "graphql_analytics",
            "requests_today": 0,
            "wall_time_ms": 0,
            "workers": [],
        }

    # 按脚本名聚合数据
    worker_map = {}
    total_requests = 0
    total_wall_ns = 0

    for item in invocations:
        dims = item.get("dimensions", {})
        script_name = dims.get("scriptName", "unknown")
        status = dims.get("status", "unknown")
        s = item.get("sum", {})
        reqs = s.get("requests", 0)
        errs = s.get("errors", 0)
        wall_ns = s.get("wallTime", 0)

        total_requests += reqs
        total_wall_ns += wall_ns

        if script_name not in worker_map:
            worker_map[script_name] = {"requests": 0, "errors": 0, "wall_ns": 0}
        worker_map[script_name]["requests"] += reqs
        worker_map[script_name]["errors"] += errs
        worker_map[script_name]["wall_ns"] += wall_ns

    # 转为列表
    workers = []
    for name, stats in worker_map.items():
        workers.append({
            "name": name,
            "requests": stats["requests"],
            "errors": stats["errors"],
            "wall_ms": stats["wall_ns"] / 1_000_000 if stats["wall_ns"] else 0,
        })

    return {
        "source": "graphql_analytics",
        "requests_today": total_requests,
        "wall_time_ms": total_wall_ns / 1_000_000 if total_wall_ns else 0,
        "workers": workers,
    }


def _today_str() -> str:
    """返回今天的日期字符串 YYYY-MM-DD"""
    from datetime import date
    return date.today().isoformat()


async def cf_validate_account(api_token: str, account_id: str) -> tuple:
    """验证账号是否有效，返回 (valid: bool, account_name_or_error: str)
    
    依次尝试:
    1. Account Info API（需要 Account Read 权限）
    2. Token Verify + GraphQL 试查（需要 Analytics Read 权限）
    """
    # 方式 1: Account Info
    try:
        info = await cf_get_account_info(api_token, account_id)
        return True, info.get("name", "Unknown")
    except Exception:
        pass

    # 方式 2: GraphQL 试查
    try:
        async with aiohttp.ClientSession() as session:
            usage = await cf_get_workers_analytics(api_token, account_id, session)
            if usage.get("source") != "unavailable":
                return True, f"Account {account_id[:8]}..."
    except Exception:
        pass

    return False, "无法验证账号（API Token 权限不足，需要 Workers Scripts Read 或 Analytics Read 权限）"


# ============ 额度信息格式化 ============

def _safe_mask_id(account_id: str) -> str:
    """安全地截断 Account ID 用于显示，防止 ID 过短时出错"""
    if len(account_id) <= 12:
        return account_id[:4] + "..." + account_id[-4:] if len(account_id) >= 8 else account_id
    return account_id[:8] + "..." + account_id[-4:]


def format_quota_text(account_name: str, account_alias: str, usage_data: dict) -> str:
    """格式化额度查询结果为文本"""
    lines = [
        "📊 Cloudflare Workers 额度查询",
        "",
        f"🏢 账户: {account_name}（{account_alias}）",
    ]

    if usage_data.get("source") == "unavailable":
        lines.extend([
            "",
            "⚠️ 无法获取详细使用量数据",
            "请确认 API Token 具有 Workers 读取权限",
            "",
            "📋 免费版限额参考:",
            "  ⚡ 请求次数: 100,000/天",
            "  ⏱ CPU 时间: 10ms/请求",
        ])
    elif usage_data.get("source") == "graphql_analytics":
        requests_today = usage_data.get("requests_today", 0)
        wall_time_ms = usage_data.get("wall_time_ms", 0)
        workers = usage_data.get("workers", [])
        limit = FREE_PLAN_LIMITS["requests_per_day"]
        percentage = (requests_today / limit * 100) if limit > 0 else 0
        lines.extend([
            "",
            "⚡ 今日请求次数:",
            f"  已用: {requests_today:,}",
            f"  限制: {limit:,}/天",
            f"  使用率: {percentage:.2f}%",
            "",
            f"⏱ 今日 CPU 时间: {wall_time_ms:.2f}ms",
            f"  免费版限制: 10ms/请求",
        ])

        # 按 Worker 分组显示
        if workers:
            lines.extend(["", "📋 各 Worker 详情:"])
            for w in workers:
                wname = w.get("name", "unknown")
                wreqs = w.get("requests", 0)
                werrs = w.get("errors", 0)
                wwall = w.get("wall_ms", 0)
                err_str = f", 错误: {werrs}" if werrs > 0 else ""
                lines.append(f"  • {wname}: {wreqs} 请求{err_str}, CPU {wwall:.2f}ms")

        lines.extend(["", "📈 数据来源: Cloudflare Analytics API"])
    else:
        # subscription usage 接口返回的完整数据
        plan = usage_data.get("plan", {})
        plan_name = plan.get("name", "Free") if isinstance(plan, dict) else "Free"
        req_usage = usage_data.get("usage", {}).get("requests", {})
        dur_usage = usage_data.get("usage", {}).get("duration", {})

        lines.append(f"📦 套餐: {plan_name}")
        lines.append("")

        if req_usage:
            used = req_usage.get("used", 0)
            limit = req_usage.get("limit", FREE_PLAN_LIMITS["requests_per_day"])
            pct = (used / limit * 100) if limit > 0 else 0
            lines.extend([
                "⚡ 请求次数:",
                f"  已用: {used:,}",
                f"  限制: {limit:,}",
                f"  使用率: {pct:.2f}%",
                "",
            ])

        if dur_usage:
            used = dur_usage.get("used", 0)
            limit = dur_usage.get("limit", FREE_PLAN_LIMITS["cpu_time_per_request_ms"])
            pct = (used / limit * 100) if limit > 0 else 0
            lines.extend([
                "⏱ CPU 时间:",
                f"  已用: {used}",
                f"  限制: {limit}",
                f"  使用率: {pct:.2f}%",
            ])

        # 如果既没有 req_usage 也没有 dur_usage，给出提示
        if not req_usage and not dur_usage:
            lines.extend([
                "⚠️ 未获取到详细使用量数据",
                "",
                "📋 免费版限额参考:",
                "  ⚡ 请求次数: 100,000/天",
                "  ⏱ CPU 时间: 10ms/请求",
            ])

    return "\n".join(lines)


def format_accounts_list(accounts: list, default_name: str = "") -> str:
    """格式化账号列表"""
    if not accounts:
        return "📋 暂无已添加的 Cloudflare 账号\n\n使用 /cfadd 名称 AccountID ApiToken 添加账号"

    lines = ["📋 已添加的 Cloudflare 账号:", ""]
    for i, acc in enumerate(accounts, 1):
        name = acc.get("name", "未命名")
        account_id = acc.get("account_id", "")
        account_name = acc.get("account_name", "未知")
        is_default = " ⭐默认" if name == default_name else ""
        masked_id = _safe_mask_id(account_id) if account_id else "N/A"
        lines.append(f"  {i}. {name} ({account_name}){is_default}")
        lines.append(f"     Account ID: {masked_id}")
    lines.append(f"\n共 {len(accounts)} 个账号")
    return "\n".join(lines)


# ============ 定时推送相关 ============

# 支持的推送时间点（整点）
VALID_PUSH_HOURS = list(range(0, 24))

def format_push_status(push_config: dict) -> str:
    """格式化定时推送状态"""
    if not push_config.get("enabled"):
        return "📡 定时推送: 未开启"

    hours = push_config.get("hours", [])
    accounts = push_config.get("accounts", [])  # 空=全部
    umo = push_config.get("umo", "")

    hour_strs = [f"{h:02d}:00" for h in sorted(hours)] if hours else ["未设置"]
    account_str = "全部账号" if not accounts else "、".join(accounts)
    target_info = f"会话 {umo[:20]}..." if umo else "未绑定会话"

    return (
        f"📡 定时推送: ✅ 已开启\n\n"
        f"  ⏰ 推送时间: {', '.join(hour_strs)}\n"
        f"  📊 推送账号: {account_str}\n"
        f"  🎯 推送目标: {target_info}"
    )


# ============ 插件主类 ============

@register("astrbot_plugin_cfquota", "yuwanyuan", "CF Workers 额度查询", "v1.0.0", "https://github.com/yuwanyuan/astrbot_plugin_cfquota")
class CFQuotaPlugin(Star):
    """
    Cloudflare Workers 额度查询插件
    支持多账号管理和定时推送
    """

    def __init__(self, context: Context):
        super().__init__(context)
        # 从配置读取账号数据
        self._accounts: list = []
        self._default_account: str = ""
        self._load_config()

        # 定时推送配置
        self._push_config: dict = {
            "enabled": False,
            "hours": [],        # 推送时间（小时列表，如 [8, 20]）
            "accounts": [],     # 推送哪些账号（空=全部）
            "umo": "",          # 推送目标会话
        }

        # 启动后台定时推送任务
        self._push_task = asyncio.create_task(self._push_loop())

    async def _push_loop(self):
        """后台定时推送循环"""
        # 先等一小段时间，让插件完全初始化
        await asyncio.sleep(10)

        while True:
            try:
                # 从 KV 加载最新推送配置
                await self._load_push_config()

                if self._push_config.get("enabled"):
                    now = datetime.now()
                    current_hour = now.hour
                    current_minute = now.minute

                    # 在每小时的第 0 分钟执行推送
                    # 为了避免时间偏差，允许 ±1 分钟的窗口
                    if current_minute <= 1 and current_hour in self._push_config.get("hours", []):
                        # 检查是否已经推送过（避免重复）
                        last_push_key = f"cf_last_push_{current_hour}"
                        last_push = await self.context.get_kv_data(last_push_key)
                        today_str = now.strftime("%Y-%m-%d")

                        if last_push != today_str:
                            # 标记已推送
                            await self.context.put_kv_data(last_push_key, today_str)
                            # 执行推送
                            await self._do_push()

                # 每 60 秒检查一次
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                logger.info("CFQuotaPlugin push loop cancelled")
                break
            except Exception as e:
                logger.error(f"Push loop error: {e}")
                await asyncio.sleep(60)

    async def _do_push(self):
        """执行一次定时推送"""
        push_config = self._push_config
        umo = push_config.get("umo", "")
        if not umo:
            logger.warning("Push target UMO is empty, skip")
            return

        # 加载最新账号数据
        await self._load_accounts_from_kv()

        if not self._accounts:
            logger.warning("No accounts configured, skip push")
            return

        # 确定要推送的账号
        push_account_names = push_config.get("accounts", [])
        accounts_to_push = []
        if push_account_names:
            for acc in self._accounts:
                if acc.get("name") in push_account_names:
                    accounts_to_push.append(acc)
        else:
            accounts_to_push = self._accounts

        if not accounts_to_push:
            logger.warning("No matching accounts for push")
            return

        # 逐个查询并推送
        all_results = []
        for acc in accounts_to_push:
            alias = acc.get("name", "未命名")
            api_token = acc.get("api_token", "")
            account_id = acc.get("account_id", "")
            account_name = acc.get("account_name", "未知")

            if not api_token or not account_id:
                all_results.append(f"⚠️ {alias}: 配置不完整")
                continue

            try:
                usage = await cf_get_workers_usage(api_token, account_id)
                # 尝试获取账户名
                try:
                    info = await cf_get_account_info(api_token, account_id)
                    account_name = info.get("name", account_name)
                except Exception:
                    pass
                result = format_quota_text(account_name, alias, usage)
                all_results.append(result)
            except Exception as e:
                all_results.append(f"❌ {alias}: 查询失败 - {str(e)}")

        if not all_results:
            return

        # 拼接推送消息
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = f"⏰ CF Workers 额度定时推送\n🕐 {now_str}\n{'─' * 30}"
        body = f"\n\n{'─' * 30}\n\n".join(all_results)
        full_message = f"{header}\n\n{body}"

        # 发送主动消息
        try:
            message_chain = MessageChain().message(full_message)
            await self.context.send_message(umo, message_chain)
            logger.info(f"Push sent to {umo[:20]}...")
        except Exception as e:
            logger.error(f"Failed to send push message: {e}")

    async def _save_push_config(self):
        """保存推送配置到 KV"""
        await self.context.put_kv_data("cf_push_config", self._push_config)

    async def _load_push_config(self):
        """从 KV 加载推送配置"""
        kv_config = await self.context.get_kv_data("cf_push_config")
        if kv_config is not None and isinstance(kv_config, dict):
            self._push_config = kv_config

    # ============ 配置加载 ============

    def _load_config(self):
        """从 AstrBot 配置加载账号数据"""
        config = self.context.get_config()
        if not config:
            return

        # 读取账号列表
        accounts_data = config.get("accounts", [])
        if isinstance(accounts_data, list):
            self._accounts = [acc for acc in accounts_data if isinstance(acc, dict)]
        elif isinstance(accounts_data, dict):
            # template_list 格式: {cloudflare: [{...}, {...}]}
            for key, val in accounts_data.items():
                if isinstance(val, list):
                    self._accounts.extend([acc for acc in val if isinstance(acc, dict)])
                elif isinstance(val, dict):
                    self._accounts.append(val)

        # 读取默认账号
        self._default_account = config.get("default_account", "")

        # 如果没有默认账号但有账号，设第一个为默认
        if not self._default_account and self._accounts:
            self._default_account = self._accounts[0].get("name", "")

        logger.info(f"CFQuotaPlugin loaded with {len(self._accounts)} account(s)")

    def _get_account(self, name: str = "") -> dict | None:
        """根据别名获取账号，不指定则返回默认账号"""
        if not self._accounts:
            return None

        if name:
            for acc in self._accounts:
                if acc.get("name") == name:
                    return acc
            return None

        # 返回默认账号
        if self._default_account:
            for acc in self._accounts:
                if acc.get("name") == self._default_account:
                    return acc

        # 返回第一个
        return self._accounts[0] if self._accounts else None

    async def _save_accounts(self):
        """保存账号数据到 KV 存储"""
        await self.context.put_kv_data("cf_accounts", self._accounts)
        await self.context.put_kv_data("cf_default_account", self._default_account)

    async def _load_accounts_from_kv(self):
        """从 KV 存储加载账号数据（优先级高于配置文件）"""
        kv_accounts = await self.context.get_kv_data("cf_accounts")
        if kv_accounts is not None:
            if isinstance(kv_accounts, list):
                self._accounts = kv_accounts
            # KV 有数据就不再读配置文件

        kv_default = await self.context.get_kv_data("cf_default_account")
        if kv_default is not None and isinstance(kv_default, str):
            self._default_account = kv_default

    # ============ 命令处理 ============

    @filter.command("/cf额度")
    async def query_quota(self, event: AstrMessageEvent):
        """查询 Workers 额度: /cf额度 [账号别名]"""
        await self._load_accounts_from_kv()

        # 解析参数
        args = event.message_str.strip().split()
        alias = args[0] if args else ""

        account = self._get_account(alias)
        if not account:
            if alias:
                result = f"❌ 未找到账号「{alias}」\n\n使用 /cflist 查看已有账号\n使用 /cfadd 添加新账号"
            else:
                result = "❌ 尚未添加任何 Cloudflare 账号\n\n使用 /cfadd 名称 AccountID ApiToken 添加账号\n或在 AstrBot 管理面板中配置"
            yield event.plain_result(result)
            return

        api_token = account.get("api_token", "")
        account_id = account.get("account_id", "")
        alias_name = account.get("name", "未命名")
        account_name = account.get("account_name", "未知")

        if not api_token or not account_id:
            yield event.plain_result(f"❌ 账号「{alias_name}」配置不完整，缺少 API Token 或 Account ID")
            return

        try:
            # 获取使用量（GraphQL）
            usage = await cf_get_workers_usage(api_token, account_id)

            # 获取账户名（尝试 Account Info，失败则用 KV 中缓存的名称）
            account_name = account.get("account_name", "未知")
            try:
                info = await cf_get_account_info(api_token, account_id)
                account_name = info.get("name", account_name)
            except Exception:
                pass  # Account Info 权限不足时使用缓存的名称

            result = format_quota_text(account_name, alias_name, usage)
            yield event.plain_result(result)

        except Exception as e:
            logger.error(f"Failed to query quota for {alias_name}: {e}")
            yield event.plain_result(f"❌ 查询失败: {str(e)}")

    @filter.command("/cfadd")
    async def add_account(self, event: AstrMessageEvent):
        """添加 Cloudflare 账号: /cfadd 名称 AccountID ApiToken"""
        await self._load_accounts_from_kv()

        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result(
                "⚠️ 参数不足\n\n"
                "用法: /cfadd 名称 AccountID ApiToken\n\n"
                "示例: /cfadd 主账号 abc123def456 your_api_token_here\n\n"
                "📌 获取方式:\n"
                "  • Account ID: Cloudflare Dashboard → 右侧 API 区域\n"
                "  • API Token: https://dash.cloudflare.com/profile/api-tokens\n"
                "    需要权限: Account > Workers Scripts > Read"
            )
            return

        name, account_id, api_token = args[0], args[1], args[2]

        # 检查重名
        for acc in self._accounts:
            if acc.get("name") == name:
                yield event.plain_result(f"❌ 账号「{name}」已存在，请使用其他名称\n使用 /cfdel {name} 先删除旧账号")
                return

        # 验证账号
        valid, account_name = await cf_validate_account(api_token, account_id)
        if not valid:
            yield event.plain_result(f"❌ 账号验证失败: {account_name}\n请检查 Account ID 和 API Token 是否正确")
            return

        # 添加账号
        new_account = {
            "name": name,
            "account_id": account_id,
            "api_token": api_token,
            "account_name": account_name,
        }
        self._accounts.append(new_account)

        # 如果是第一个账号，自动设为默认
        if len(self._accounts) == 1:
            self._default_account = name

        await self._save_accounts()

        result = f"✅ 账号添加成功！\n\n"
        result += f"  名称: {name}\n"
        result += f"  账户: {account_name}\n"
        result += f"  Account ID: {_safe_mask_id(account_id)}\n"
        if self._default_account == name:
            result += f"\n⭐ 已设为默认账号"
        result += f"\n\n使用 /cf额度 {name} 查询额度"

        yield event.plain_result(result)

    @filter.command("/cflist")
    async def list_accounts(self, event: AstrMessageEvent):
        """列出所有已添加的账号: /cflist"""
        await self._load_accounts_from_kv()
        result = format_accounts_list(self._accounts, self._default_account)
        yield event.plain_result(result)

    @filter.command("/cfdel")
    async def delete_account(self, event: AstrMessageEvent):
        """删除指定账号: /cfdel 名称"""
        await self._load_accounts_from_kv()

        args = event.message_str.strip().split()
        if not args:
            yield event.plain_result("⚠️ 请指定要删除的账号名称\n\n用法: /cfdel 名称\n使用 /cflist 查看已有账号")
            return

        name = args[0]
        original_len = len(self._accounts)
        self._accounts = [acc for acc in self._accounts if acc.get("name") != name]

        if len(self._accounts) == original_len:
            yield event.plain_result(f"❌ 未找到账号「{name}」\n使用 /cflist 查看已有账号")
            return

        # 如果删除的是默认账号，重新设定
        if self._default_account == name:
            self._default_account = self._accounts[0].get("name", "") if self._accounts else ""

        await self._save_accounts()

        result = f"✅ 已删除账号「{name}」"
        if not self._accounts:
            result += "\n\n📋 已无剩余账号"
        elif self._default_account:
            result += f"\n⭐ 当前默认账号: {self._default_account}"

        yield event.plain_result(result)

    @filter.command("/cfdefault")
    async def set_default_account(self, event: AstrMessageEvent):
        """设置默认账号: /cfdefault 名称"""
        await self._load_accounts_from_kv()

        args = event.message_str.strip().split()
        if not args:
            current = self._default_account or "未设置"
            yield event.plain_result(f"当前默认账号: {current}\n\n用法: /cfdefault 名称\n使用 /cflist 查看已有账号")
            return

        name = args[0]
        found = False
        for acc in self._accounts:
            if acc.get("name") == name:
                found = True
                break

        if not found:
            yield event.plain_result(f"❌ 未找到账号「{name}」\n使用 /cflist 查看已有账号")
            return

        self._default_account = name
        await self._save_accounts()

        yield event.plain_result(f"✅ 已将「{name}」设为默认账号\n\n使用 /cf额度 即可查询此账号的额度")

    @filter.command("/cfpush")
    async def manage_push(self, event: AstrMessageEvent):
        """管理定时推送: /cfpush [子命令] [参数]"""
        await self._load_push_config()
        await self._load_accounts_from_kv()

        args = event.message_str.strip().split()
        sub_cmd = args[0].lower() if args else ""

        if not sub_cmd or sub_cmd == "status":
            # 查看推送状态
            result = format_push_status(self._push_config)
            yield event.plain_result(result)

        elif sub_cmd == "on":
            # 开启定时推送，同时绑定当前会话为推送目标
            if not self._accounts:
                yield event.plain_result("❌ 请先添加 Cloudflare 账号\n使用 /cfadd 名称 AccountID ApiToken 添加")
                return

            # 解析推送时间
            hours = []
            for arg in args[1:]:
                try:
                    h = int(arg)
                    if 0 <= h <= 23:
                        hours.append(h)
                except ValueError:
                    pass

            if not hours:
                hours = [8, 20]  # 默认早8晚8

            self._push_config["enabled"] = True
            self._push_config["hours"] = sorted(list(set(hours)))
            self._push_config["umo"] = event.unified_msg_origin

            await self._save_push_config()

            hour_strs = [f"{h:02d}:00" for h in sorted(hours)]
            result = (
                f"✅ 定时推送已开启！\n\n"
                f"  ⏰ 推送时间: {', '.join(hour_strs)}\n"
                f"  📊 推送账号: 全部账号\n"
                f"  🎯 推送目标: 当前会话\n\n"
                f"💡 其他操作:\n"
                f"  /cfpush accounts 名称1 名称2  → 指定推送账号\n"
                f"  /cfpush off                   → 关闭推送\n"
                f"  /cfpush now                   → 立即推送一次"
            )
            yield event.plain_result(result)

        elif sub_cmd == "off":
            # 关闭定时推送
            self._push_config["enabled"] = False
            await self._save_push_config()
            yield event.plain_result("✅ 定时推送已关闭")

        elif sub_cmd == "hours":
            # 修改推送时间
            hours = []
            for arg in args[1:]:
                try:
                    h = int(arg)
                    if 0 <= h <= 23:
                        hours.append(h)
                except ValueError:
                    pass

            if not hours:
                yield event.plain_result(
                    "⚠️ 请指定推送时间（小时，0-23）\n\n"
                    "用法: /cfpush hours 8 12 20\n\n"
                    "示例: 每天 8点、12点、20点 推送"
                )
                return

            self._push_config["hours"] = sorted(list(set(hours)))
            # 如果之前未开启，自动开启
            if not self._push_config.get("enabled"):
                self._push_config["enabled"] = True
                if not self._push_config.get("umo"):
                    self._push_config["umo"] = event.unified_msg_origin

            await self._save_push_config()

            hour_strs = [f"{h:02d}:00" for h in sorted(set(hours))]
            yield event.plain_result(f"✅ 推送时间已更新: {', '.join(hour_strs)}")

        elif sub_cmd == "accounts":
            # 指定推送哪些账号
            account_names = args[1:]
            if not account_names:
                # 清空 = 推送全部
                self._push_config["accounts"] = []
                await self._save_push_config()
                yield event.plain_result("✅ 已重置为推送全部账号")
                return

            # 验证账号名是否存在
            invalid = []
            valid_names = []
            for name in account_names:
                found = any(acc.get("name") == name for acc in self._accounts)
                if found:
                    valid_names.append(name)
                else:
                    invalid.append(name)

            self._push_config["accounts"] = valid_names
            await self._save_push_config()

            result = f"✅ 推送账号已更新: {', '.join(valid_names) if valid_names else '全部'}"
            if invalid:
                result += f"\n\n⚠️ 以下账号不存在: {', '.join(invalid)}\n使用 /cflist 查看已有账号"
            yield event.plain_result(result)

        elif sub_cmd == "now":
            # 立即推送一次
            if not self._accounts:
                yield event.plain_result("❌ 请先添加 Cloudflare 账号")
                return

            # 确保有推送目标
            self._push_config["umo"] = event.unified_msg_origin
            yield event.plain_result("🔍 正在查询额度...")

            # 执行推送
            await self._do_push()
            # 注意：_do_push 是通过 send_message 发送的，不需要再 yield

        else:
            # 未知子命令，显示帮助
            yield event.plain_result(
                "📡 定时推送管理\n\n"
                "用法: /cfpush [子命令] [参数]\n\n"
                "子命令:\n"
                "  status              - 查看推送状态\n"
                "  on [小时...]        - 开启推送（默认 8 20）\n"
                "  off                 - 关闭推送\n"
                "  hours 小时...       - 修改推送时间\n"
                "  accounts 名称...    - 指定推送账号\n"
                "  now                 - 立即推送一次\n\n"
                "示例:\n"
                "  /cfpush on 8 20       → 每天 8:00、20:00 推送\n"
                "  /cfpush hours 9 18    → 改为 9:00、18:00\n"
                "  /cfpush accounts 主账号 → 只推送主账号\n"
                "  /cfpush now           → 立即推送一次"
            )

    @filter.command("/cfhelp")
    async def show_help(self, event: AstrMessageEvent):
        """显示帮助信息: /cfhelp"""
        help_text = (
            "📖 CF Workers 额度查询 - 帮助\n\n"
            "🔧 命令列表:\n"
            "  /cf额度 [名称]    - 查询额度（不指定查默认账号）\n"
            "  /cfadd 名称 ID Token - 添加账号\n"
            "  /cflist          - 列出所有账号\n"
            "  /cfdel 名称      - 删除账号\n"
            "  /cfdefault 名称  - 设置默认账号\n"
            "  /cfpush on [小时...] - 开启定时推送\n"
            "  /cfpush off      - 关闭定时推送\n"
            "  /cfpush now      - 立即推送一次\n"
            "  /cfhelp          - 显示此帮助\n\n"
            "📝 示例:\n"
            "  /cf额度           → 查询默认账号额度\n"
            "  /cf额度 工作账号   → 查询指定账号额度\n"
            "  /cfadd 主账号 abc123 token456\n"
            "  /cfpush on 8 20   → 每天 8:00 和 20:00 推送\n\n"
            "🔑 获取凭证:\n"
            "  • Account ID: Cloudflare Dashboard → API 区域\n"
            "  • API Token: https://dash.cloudflare.com/profile/api-tokens\n"
            "    权限: Account > Workers Scripts > Read\n\n"
            "💡 也可以在 AstrBot 管理面板中配置账号"
        )
        yield event.plain_result(help_text)
