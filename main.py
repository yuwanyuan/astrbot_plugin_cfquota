"""
AstrBot 插件：CF Workers 额度查询
查询 Cloudflare Workers 计算额度使用情况，支持多账号管理和定时推送

架构：
  - 数据采集层：每 30/60 分钟从 CF API 拉取用量数据，缓存到内存
  - 推送层：在用户自定义的整点时间，推送缓存数据（快速、可靠）

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
from datetime import datetime, timezone
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.core.message.components import Plain
from astrbot.api import logger

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


def format_quota_text(account_name: str, account_alias: str, usage_data: dict, cache_time: str = "") -> str:
    """格式化额度查询结果为文本
    
    Args:
        cache_time: 缓存时间字符串，为空表示实时查询
    """
    lines = [
        "📊 Cloudflare Workers 额度查询",
        "",
        f"🏢 账户: {account_name}（{account_alias}）",
    ]

    if cache_time:
        lines.append(f"🕐 数据时间: {cache_time}")

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


# ============ 用量监控相关 ============

# 支持的数据采集间隔（分钟）
VALID_FETCH_INTERVALS = [30, 60]

def format_push_status(push_config: dict) -> str:
    """格式化用量监控 + 定时推送状态"""
    if not push_config.get("enabled"):
        return "📡 用量监控: 未开启"

    fetch_interval = push_config.get("fetch_interval", 60)
    hours = push_config.get("hours", [])
    accounts = push_config.get("accounts", [])  # 空=全部
    umo = push_config.get("umo", "")

    hour_strs = [f"{h:02d}:00" for h in sorted(hours)] if hours else ["未设置"]
    account_str = "全部账号" if not accounts else "、".join(accounts)
    target_info = f"会话 {umo[:20]}..." if umo else "未绑定会话"

    return (
        f"📡 用量监控: ✅ 已开启\n\n"
        f"  ⏱ 数据采集: 每 {fetch_interval} 分钟\n"
        f"  ⏰ 推送时间: {', '.join(hour_strs)}\n"
        f"  📊 监控账号: {account_str}\n"
        f"  🎯 推送目标: {target_info}"
    )


# ============ 插件主类 ============

@register("astrbot_plugin_cfquota", "yuwanyuan", "CF Workers 额度查询", "v1.0.0", "https://github.com/yuwanyuan/astrbot_plugin_cfquota")
class CFQuotaPlugin(Star):
    """
    Cloudflare Workers 额度查询插件
    支持多账号管理、后台数据采集和定时推送
    
    架构：
    - 采集循环：每 30/60 分钟从 CF API 拉取数据 → 缓存到 _usage_cache
    - 推送循环：每 60 秒检查，到达整点时推送缓存数据
    - 手动查询：优先使用缓存，缓存过期则实时查询
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # 保存插件配置（AstrBot 框架实例化时传入的 AstrBotConfig 对象）
        # Star 基类 __init__ 不会保存 config 参数，需要我们自己保存
        self.config = config if config is not None else {}
        # 从配置读取账号数据
        self._accounts: list = []
        self._default_account: str = ""
        self._load_config()

        # 用量监控 + 推送配置
        self._push_config: dict = {
            "enabled": False,
            "fetch_interval": 60,   # 数据采集间隔（分钟），30 或 60
            "hours": [],            # 推送时间（小时列表，如 [8, 20]）
            "accounts": [],         # 监控哪些账号（空=全部）
            "umo": "",              # 推送目标会话
        }

        # 用量数据缓存: { 账号别名: {"usage": dict, "account_name": str, "fetched_at": str} }
        self._usage_cache: dict = {}

        # 启动后台任务
        self._fetch_task = asyncio.create_task(self._fetch_loop())
        self._push_task = asyncio.create_task(self._push_loop())

    async def terminate(self):
        """插件卸载时取消后台任务"""
        if self._fetch_task and not self._fetch_task.done():
            self._fetch_task.cancel()
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
        logger.info("CFQuotaPlugin terminated")

    # ============ 数据采集循环 ============

    async def _fetch_loop(self):
        """后台数据采集循环：每隔 fetch_interval 分钟从 CF API 拉取数据
        
        监控开启时：按用户设置的间隔（30/60分钟）采集
        监控关闭时：每 2 小时采集一次（保证手动查询有可用缓存）
        """
        # 先等一小段时间，让插件完全初始化
        await asyncio.sleep(15)

        while True:
            try:
                # 从 KV 加载最新配置
                await self._load_push_config()
                await self._load_accounts_from_kv()

                if self._push_config.get("enabled"):
                    # 监控开启：按配置的间隔采集
                    interval = self._push_config.get("fetch_interval", 60)
                    if interval not in VALID_FETCH_INTERVALS:
                        interval = 60
                    await self._fetch_all_usage()
                    await asyncio.sleep(interval * 60)
                else:
                    # 监控关闭：每 2 小时采集一次（保证手动查询有缓存）
                    await self._fetch_all_usage()
                    await asyncio.sleep(120 * 60)

            except asyncio.CancelledError:
                logger.info("CFQuotaPlugin fetch loop cancelled")
                break
            except Exception as e:
                logger.error(f"Fetch loop error: {e}")
                await asyncio.sleep(60)

    async def _fetch_all_usage(self):
        """采集所有账号的用量数据"""
        if not self._accounts:
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Fetching usage data for {len(self._accounts)} account(s)...")

        for acc in self._accounts:
            alias = acc.get("name", "未命名")
            api_token = acc.get("api_token", "")
            account_id = acc.get("account_id", "")

            if not api_token or not account_id:
                continue

            try:
                usage = await cf_get_workers_usage(api_token, account_id)
                # 尝试获取账户名
                account_name = acc.get("account_name", "未知")
                name_updated = False
                try:
                    info = await cf_get_account_info(api_token, account_id)
                    account_name = info.get("name", account_name)
                    # 更新账号中的账户名
                    acc["account_name"] = account_name
                    name_updated = True
                except Exception:
                    pass

                self._usage_cache[alias] = {
                    "usage": usage,
                    "account_name": account_name,
                    "fetched_at": now_str,
                    "fetch_error": None,  # 清除旧的错误标记
                }
                logger.debug(f"Fetched usage for {alias}")

                # 如果账户名有更新，保存到 KV
                if name_updated:
                    await self._save_accounts()

            except Exception as e:
                logger.error(f"Failed to fetch usage for {alias}: {e}")
                if alias in self._usage_cache:
                    # 保留旧的缓存数据，标记为采集失败
                    self._usage_cache[alias]["fetch_error"] = str(e)
                else:
                    # 首次采集就失败，也要创建缓存条目，否则推送和查询无法正确处理
                    self._usage_cache[alias] = {
                        "usage": None,
                        "account_name": acc.get("account_name", "未知"),
                        "fetched_at": now_str,
                        "fetch_error": str(e),
                    }

        logger.info(f"Usage data fetch completed, cached {len(self._usage_cache)} account(s)")

    # ============ 推送循环 ============

    async def _push_loop(self):
        """后台推送循环：每 60 秒检查，到达整点时推送缓存数据"""
        # 先等一小段时间，让首次数据采集完成
        await asyncio.sleep(20)

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
                        last_push = await self.get_kv_data(last_push_key, None)
                        today_str = now.strftime("%Y-%m-%d")

                        if last_push != today_str:
                            # 标记已推送
                            await self.put_kv_data(last_push_key, today_str)
                            # 执行推送（使用缓存数据）
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
        """执行一次定时推送（使用缓存数据）"""
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

        # 逐个格式化推送（优先使用缓存）
        all_results = []
        for acc in accounts_to_push:
            alias = acc.get("name", "未命名")
            account_name = acc.get("account_name", "未知")

            cached = self._usage_cache.get(alias)
            if cached and cached.get("usage"):
                # 使用缓存数据
                result = format_quota_text(
                    cached.get("account_name", account_name),
                    alias,
                    cached["usage"],
                    cache_time=cached.get("fetched_at", ""),
                )
                # 如果采集时出错，附加提示
                if cached.get("fetch_error"):
                    result += f"\n\n⚠️ 上次采集出错: {cached['fetch_error']}"
                all_results.append(result)
            elif cached and cached.get("fetch_error"):
                # 缓存存在但采集失败，优先显示错误而不是再实时查询
                all_results.append(f"❌ {alias}: 上次采集失败 - {cached['fetch_error']}")
            else:
                # 缓存无数据，实时查询（降级方案）
                api_token = acc.get("api_token", "")
                account_id = acc.get("account_id", "")
                if not api_token or not account_id:
                    all_results.append(f"⚠️ {alias}: 配置不完整")
                    continue

                try:
                    usage = await cf_get_workers_usage(api_token, account_id)
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
            message_chain = MessageChain([Plain(full_message)])
            await self.context.send_message(umo, message_chain)
            logger.info(f"Push sent to {umo[:20]}...")
        except Exception as e:
            logger.error(f"Failed to send push message: {e}")

    async def _save_push_config(self):
        """保存推送配置到 KV"""
        await self.put_kv_data("cf_push_config", self._push_config)

    async def _load_push_config(self):
        """从 KV 加载推送配置"""
        kv_config = await self.get_kv_data("cf_push_config", None)
        if kv_config is not None and isinstance(kv_config, dict):
            self._push_config = kv_config

    # ============ 配置加载 ============

    def _load_config(self):
        """从 AstrBot 配置加载账号数据
        
        template_list 返回格式: list[dict]，每个 dict 含 __template_key 和模板字段
        例: [{"__template_key": "cloudflare", "name": "主账号", "account_id": "xxx", "api_token": "yyy"}]
        """
        # 调试：输出配置对象的类型和内容
        if self.config:
            logger.info(f"CFQuotaPlugin config type: {type(self.config).__name__}")
            logger.debug(f"CFQuotaPlugin config content: {dict(self.config) if hasattr(self.config, 'items') else self.config}")
        else:
            logger.warning("CFQuotaPlugin config is empty! Plugin config from admin panel not loaded.")
        
        accounts_data = self.config.get("accounts", []) if self.config else []
        
        if isinstance(accounts_data, list):
            # template_list 返回的就是 list[dict]，直接使用
            self._accounts = [acc for acc in accounts_data if isinstance(acc, dict)]
        elif isinstance(accounts_data, dict):
            # 兼容旧格式: {模板名: [条目1, 条目2]}
            for key, val in accounts_data.items():
                if isinstance(val, list):
                    self._accounts.extend([acc for acc in val if isinstance(acc, dict)])
                elif isinstance(val, dict):
                    self._accounts.append(val)

        # 读取默认账号
        self._default_account = self.config.get("default_account", "") if self.config else ""

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
        await self.put_kv_data("cf_accounts", self._accounts)
        await self.put_kv_data("cf_default_account", self._default_account)

    async def _load_accounts_from_kv(self):
        """从 KV 存储加载账号数据（优先级高于配置文件）"""
        kv_accounts = await self.get_kv_data("cf_accounts", None)
        if kv_accounts is not None:
            if isinstance(kv_accounts, list):
                self._accounts = kv_accounts
            # KV 有数据就不再读配置文件

        kv_default = await self.get_kv_data("cf_default_account", None)
        if kv_default is not None and isinstance(kv_default, str):
            self._default_account = kv_default

    # ============ 命令处理 ============

    @filter.command("cf额度")
    async def query_quota(self, event: AstrMessageEvent):
        """查询 Workers 额度: /cf额度 [账号别名]"""
        await self._load_accounts_from_kv()

        # 解析参数（跳过命令名本身）
        args = event.message_str.strip().split()[1:]
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

        # 优先使用缓存数据
        cached = self._usage_cache.get(alias_name)
        if cached and cached.get("usage"):
            # 检查缓存是否是今天的数据
            fetched_at = cached.get("fetched_at", "")
            is_stale = False
            if fetched_at:
                try:
                    fetched_date = fetched_at.split(" ")[0]
                    today_date = datetime.now().strftime("%Y-%m-%d")
                    is_stale = fetched_date != today_date
                except Exception:
                    pass

            result = format_quota_text(
                cached.get("account_name", account_name),
                alias_name,
                cached["usage"],
                cache_time=fetched_at,
            )
            if is_stale:
                result += "\n\n⚠️ 缓存数据非今日，建议使用 /cfpush fetch 重新采集"
            yield event.plain_result(result)
        else:
            # 缓存无数据，实时查询
            try:
                usage = await cf_get_workers_usage(api_token, account_id)

                # 获取账户名
                try:
                    info = await cf_get_account_info(api_token, account_id)
                    account_name = info.get("name", account_name)
                except Exception:
                    pass

                result = format_quota_text(account_name, alias_name, usage)
                yield event.plain_result(result)

            except Exception as e:
                logger.error(f"Failed to query quota for {alias_name}: {e}")
                yield event.plain_result(f"❌ 查询失败: {str(e)}")

    @filter.command("cfadd")
    async def add_account(self, event: AstrMessageEvent):
        """添加 Cloudflare 账号: /cfadd 名称 AccountID ApiToken"""
        await self._load_accounts_from_kv()

        # 解析参数（跳过命令名本身）
        args = event.message_str.strip().split()[1:]
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

    @filter.command("cflist")
    async def list_accounts(self, event: AstrMessageEvent):
        """列出所有已添加的账号: /cflist"""
        await self._load_accounts_from_kv()
        result = format_accounts_list(self._accounts, self._default_account)
        yield event.plain_result(result)

    @filter.command("cfdel")
    async def delete_account(self, event: AstrMessageEvent):
        """删除指定账号: /cfdel 名称"""
        await self._load_accounts_from_kv()

        # 解析参数（跳过命令名本身）
        args = event.message_str.strip().split()[1:]
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

        # 同时清除缓存
        self._usage_cache.pop(name, None)

        await self._save_accounts()

        result = f"✅ 已删除账号「{name}」"
        if not self._accounts:
            result += "\n\n📋 已无剩余账号"
        elif self._default_account:
            result += f"\n⭐ 当前默认账号: {self._default_account}"

        yield event.plain_result(result)

    @filter.command("cfdefault")
    async def set_default_account(self, event: AstrMessageEvent):
        """设置默认账号: /cfdefault 名称"""
        await self._load_accounts_from_kv()

        # 解析参数（跳过命令名本身）
        args = event.message_str.strip().split()[1:]
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

    @filter.command("cfpush")
    async def manage_push(self, event: AstrMessageEvent):
        """管理定时推送: /cfpush [子命令] [参数]"""
        await self._load_push_config()
        await self._load_accounts_from_kv()

        # 解析参数（跳过命令名本身）
        args = event.message_str.strip().split()[1:]
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

            # 解析推送时间（小时）
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

            fetch_interval = self._push_config.get("fetch_interval", 60)
            hour_strs = [f"{h:02d}:00" for h in sorted(hours)]
            result = (
                f"✅ 用量监控已开启！\n\n"
                f"  ⏱ 数据采集: 每 {fetch_interval} 分钟\n"
                f"  ⏰ 推送时间: {', '.join(hour_strs)}\n"
                f"  📊 监控账号: 全部账号\n"
                f"  🎯 推送目标: 当前会话\n\n"
                f"💡 其他操作:\n"
                f"  /cfpush interval 30|60  → 修改采集间隔\n"
                f"  /cfpush hours 9 18      → 修改推送时间\n"
                f"  /cfpush accounts 名称   → 指定监控账号\n"
                f"  /cfpush off             → 关闭监控\n"
                f"  /cfpush now             → 立即推送一次"
            )
            yield event.plain_result(result)

        elif sub_cmd == "off":
            # 关闭定时推送
            self._push_config["enabled"] = False
            await self._save_push_config()
            yield event.plain_result("✅ 用量监控已关闭")

        elif sub_cmd == "interval":
            # 修改数据采集间隔
            if len(args) < 2:
                yield event.plain_result(
                    "⚠️ 请指定采集间隔（分钟）\n\n"
                    "用法: /cfpush interval 30|60\n\n"
                    "支持: 30 分钟、60 分钟\n\n"
                    "  30 → 数据更实时，API 调用更频繁\n"
                    "  60 → 数据较新，API 调用较少（推荐）"
                )
                return

            try:
                interval = int(args[1])
            except ValueError:
                yield event.plain_result("❌ 间隔必须是数字（30 或 60）")
                return

            if interval not in VALID_FETCH_INTERVALS:
                yield event.plain_result(f"❌ 不支持的间隔: {interval} 分钟\n仅支持: {', '.join(str(x) for x in VALID_FETCH_INTERVALS)} 分钟")
                return

            self._push_config["fetch_interval"] = interval
            # 如果之前未开启，自动开启
            if not self._push_config.get("enabled"):
                self._push_config["enabled"] = True
                if not self._push_config.get("umo"):
                    self._push_config["umo"] = event.unified_msg_origin
                if not self._push_config.get("hours"):
                    self._push_config["hours"] = [8, 20]

            await self._save_push_config()
            yield event.plain_result(f"✅ 数据采集间隔已更新: 每 {interval} 分钟")

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
            # 指定监控哪些账号
            account_names = args[1:]
            if not account_names:
                # 清空 = 监控全部
                self._push_config["accounts"] = []
                await self._save_push_config()
                yield event.plain_result("✅ 已重置为监控全部账号")
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

            result = f"✅ 监控账号已更新: {', '.join(valid_names) if valid_names else '全部'}"
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

            # 触发一次数据采集
            await self._fetch_all_usage()

            # 直接格式化结果返回（不走 _do_push 的主动消息，避免 yield 两次的问题）
            push_account_names = self._push_config.get("accounts", [])
            accounts_to_push = []
            if push_account_names:
                for acc in self._accounts:
                    if acc.get("name") in push_account_names:
                        accounts_to_push.append(acc)
            else:
                accounts_to_push = self._accounts

            all_results = []
            for acc in accounts_to_push:
                alias = acc.get("name", "未命名")
                account_name = acc.get("account_name", "未知")
                cached = self._usage_cache.get(alias)
                if cached and cached.get("usage"):
                    result = format_quota_text(
                        cached.get("account_name", account_name),
                        alias,
                        cached["usage"],
                        cache_time=cached.get("fetched_at", ""),
                    )
                    if cached.get("fetch_error"):
                        result += f"\n\n⚠️ 上次采集出错: {cached['fetch_error']}"
                    all_results.append(result)
                elif cached and cached.get("fetch_error"):
                    all_results.append(f"❌ {alias}: 采集失败 - {cached['fetch_error']}")
                else:
                    all_results.append(f"⚠️ {alias}: 暂无数据")

            if all_results:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                header = f"⏰ CF Workers 额度推送\n🕐 {now_str}\n{'─' * 30}"
                body = f"\n\n{'─' * 30}\n\n".join(all_results)
                yield event.plain_result(f"{header}\n\n{body}")
            else:
                yield event.plain_result("⚠️ 无可推送的数据")

        elif sub_cmd == "fetch":
            # 手动触发一次数据采集
            await self._fetch_all_usage()
            cache_count = len(self._usage_cache)
            if cache_count > 0:
                lines = [f"✅ 数据采集完成，已缓存 {cache_count} 个账号的数据\n"]
                for alias, cached in self._usage_cache.items():
                    fetched_at = cached.get("fetched_at", "未知")
                    error = cached.get("fetch_error")
                    if error:
                        lines.append(f"  • {alias}: ❌ 采集失败 - {error}")
                    else:
                        lines.append(f"  • {alias}: ✅ 采集于 {fetched_at}")
                yield event.plain_result("\n".join(lines))
            else:
                yield event.plain_result("⚠️ 采集完成但无数据，请检查账号配置")

        else:
            # 未知子命令，显示帮助
            yield event.plain_result(
                "📡 用量监控管理\n\n"
                "架构: 每 30/60 分钟采集 CF 数据 → 缓存 → 到整点推送缓存数据\n\n"
                "用法: /cfpush [子命令] [参数]\n\n"
                "子命令:\n"
                "  status                 - 查看监控状态\n"
                "  on [小时...]           - 开启监控（默认 8 20）\n"
                "  off                    - 关闭监控\n"
                "  interval 30|60         - 数据采集间隔（分钟）\n"
                "  hours 小时...          - 修改推送时间\n"
                "  accounts 名称...       - 指定监控账号\n"
                "  now                    - 采集并立即推送一次\n"
                "  fetch                  - 手动触发一次数据采集\n\n"
                "示例:\n"
                "  /cfpush on 8 20          → 每天 8:00、20:00 推送\n"
                "  /cfpush interval 30      → 每 30 分钟采集一次数据\n"
                "  /cfpush hours 9 18 21    → 改为 9:00、18:00、21:00 推送\n"
                "  /cfpush accounts 主账号   → 只监控主账号\n"
                "  /cfpush now              → 立即推送一次\n"
                "  /cfpush fetch            → 手动采集数据"
            )

    @filter.command("cfhelp")
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
            "  /cfpush on [小时...] - 开启用量监控\n"
            "  /cfpush interval 30|60 - 采集间隔\n"
            "  /cfpush off      - 关闭监控\n"
            "  /cfpush now      - 立即推送一次\n"
            "  /cfpush fetch    - 手动采集数据\n"
            "  /cfhelp          - 显示此帮助\n\n"
            "📡 监控架构:\n"
            "  数据采集: 每 30/60 分钟从 CF API 拉取 → 缓存\n"
            "  定时推送: 在自定义整点推送缓存数据\n\n"
            "📝 示例:\n"
            "  /cf额度           → 查询默认账号额度\n"
            "  /cf额度 工作账号   → 查询指定账号额度\n"
            "  /cfadd 主账号 abc123 token456\n"
            "  /cfpush on 8 20   → 每天 8:00 和 20:00 推送\n"
            "  /cfpush interval 30 → 每 30 分钟采集一次数据\n\n"
            "🔑 获取凭证:\n"
            "  • Account ID: Cloudflare Dashboard → API 区域\n"
            "  • API Token: https://dash.cloudflare.com/profile/api-tokens\n"
            "    权限: Account > Workers Scripts > Read\n\n"
            "💡 也可以在 AstrBot 管理面板中配置账号"
        )
        yield event.plain_result(help_text)
