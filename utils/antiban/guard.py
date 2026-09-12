"""Antiban 对外统一入口（PR-1 骨架：不拦截不改动，仅日志）。

后续 PR 按顺序充实：
  PR-2 bucket 粘性
  PR-3 cooldown/geo
  PR-4 circuit 报错上报
  PR-5 fingerprint 扩展
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import hashlib
import os
import time

from fastapi import HTTPException

from utils import configs
from utils.antiban import account_risk, bucket, circuit, concurrency, cooldown, fingerprint, geo
from utils.Logger import logger

# 拒绝原因 → 对外 HTTP 状态。
#   503：账号暂时不可用，换号重试有意义（冷却中、桶降级、并发打满、出口已失效）。
#   403：这个号别再试了，重试只会加重风险（已熔断为死号）。
DENIAL_STATUS = {
    "account_dead": 403,
    "bucket_dead": 503,
    "bucket_degraded": 503,
    "cooldown": 503,
    "concurrency": 503,
}

# 匿名诊断：只按原因计数，不记录 token、代理或任何凭据。
_admission_denials: Dict[str, int] = {}

# 账号的匿名标识（不可逆摘要）。token 前缀不算脱敏，禁止入日志。
anon_id = concurrency.anon_id

# ---------------------------------------------------------------------------
# 部署形态与共享协调
#
# 本层的状态（并发租约、冷却、退避等级、网络错误计数）全部是**进程内**字典。
# 单进程时它们就是全局真相；多 Worker 时每个 Worker 各持一份，于是
# 「每号并发上限 N」的实际效果是 worker 数 × N，冷却窗口也会被稀释。
# 在没有共享协调层（Redis 等）之前，这不能靠文档一句话糊过去：
# 必须变成可查询、可告警的事实，禁止静默 fail-open。
#
# 本模块只做**检测与报告**。拒绝放行属于部署策略，需要 configs/app 层开关，
# 已作为接口建议提交主控（见交付报告），不在本层擅自 fail closed——
# 那会让所有多 Worker 部署直接 503。
# ---------------------------------------------------------------------------
COORDINATION_DISABLED = "disabled"
COORDINATION_SINGLE_PROCESS = "single_process"
COORDINATION_SINGLE_PROCESS_ASSUMED = "single_process_assumed"
COORDINATION_MULTI_PROCESS_UNCOORDINATED = "multi_process_uncoordinated"

# 进程模型由部署方声明。这不是凭据，只是 worker 数量。
_WORKER_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "WORKERS")


def _declared_workers() -> Optional[int]:
    """从环境读取声明的 worker 数；无法确定返回 None。"""
    for var in _WORKER_ENV_VARS:
        raw = os.getenv(var)
        if raw is None or raw == "":
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def coordination_status() -> Dict[str, Any]:
    """本层协调能力的事实陈述。绝不声称多 Worker 安全。"""
    workers = _declared_workers()
    if not configs.enable_antiban:
        mode = COORDINATION_DISABLED
    elif workers is None:
        mode = COORDINATION_SINGLE_PROCESS_ASSUMED
    elif workers > 1:
        mode = COORDINATION_MULTI_PROCESS_UNCOORDINATED
    else:
        mode = COORDINATION_SINGLE_PROCESS
    return {
        "mode": mode,
        "declared_workers": workers,
        # 没有共享协调层，容量永远是本进程口径
        "capacity_is_global": False,
        "shared_coordinator": None,
        "state_scope": "process",
    }


def _log_coordination_status(status: Dict[str, Any]) -> None:
    if status["mode"] == COORDINATION_DISABLED:
        return
    if status["mode"] == COORDINATION_MULTI_PROCESS_UNCOORDINATED:
        logger.error(
            f"[antiban] coordination={COORDINATION_MULTI_PROCESS_UNCOORDINATED} "
            f"workers={status['declared_workers']}: per-account concurrency, cooldown and "
            f"circuit state are process-local; effective per-account concurrency is "
            f"workers x cap and cooldowns are diluted. No shared coordinator configured."
        )
        return
    logger.info(f"[antiban] coordination={status['mode']} capacity_scope=process")


def redact_proxy(proxy_url: Optional[str]) -> str:
    """代理 URL 的可记录形式：保留 scheme，其余（含 user:pass@host:port）不可逆摘要。

    代理串常内嵌账号密码，整条都是凭据。日志只需要「是哪一个出口」这一区分度，
    摘要即可满足；原值一律不落日志。
    """
    if not proxy_url:
        return "proxy:none"
    scheme, sep, rest = str(proxy_url).partition("://")
    if not sep:
        scheme, rest = "", proxy_url
    digest = hashlib.sha256(str(proxy_url).encode("utf-8")).hexdigest()[:12]
    return f"{scheme}://proxy:{digest}" if scheme else f"proxy:{digest}"


@dataclass
class AntibanContext:
    token: str = ""
    bucket_id: Optional[str] = None
    proxy_url: Optional[str] = None
    header_overrides: Dict[str, str] = field(default_factory=dict)
    tz_offset_min: Optional[int] = None
    fp_overrides: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = False
    concurrency_acquired: bool = False
    # 准入结论。被拒时 concurrency_acquired 必为 False，
    # 既有 ChatService 的 503 分支因此继续生效（向后兼容）。
    admission_denied: bool = False
    denial_reason: str = ""
    denial_status: int = 0
    _lease: Optional[concurrency.Lease] = None


def _deny(ctx: AntibanContext, reason: str) -> AntibanContext:
    """标记准入拒绝并计入匿名统计。调用点必须保证此刻未持有槽位。"""
    ctx.admission_denied = True
    ctx.denial_reason = reason
    ctx.denial_status = DENIAL_STATUS.get(reason, 503)
    ctx.concurrency_acquired = False
    _admission_denials[reason] = _admission_denials.get(reason, 0) + 1
    # 只记录匿名标识与原因；token 前缀不算脱敏，绝不入日志
    logger.warning(
        f"[antiban] admission denied: {reason} -> {ctx.denial_status} "
        f"({concurrency.anon_id(ctx.token)})"
    )
    return ctx


def _ineligible_reason(ctx: AntibanContext) -> Optional[str]:
    """账号/桶此刻是否仍有资格。等待前后都要跑一遍。

    冷却 sleep 与并发排队期间号可能被判死、桶可能被降级；只在等待前检查一次
    等于让「等待窗口里新变坏的号」畅通无阻。
    """
    if ctx.token and circuit.is_token_dead(ctx.token):
        return "account_dead"
    if not circuit.is_bucket_allowed(ctx.bucket_id):
        # 判定 fail closed，标签区分 dead（出口已不存在）与 degraded（可重试）
        return circuit.bucket_denial_reason(ctx.bucket_id)
    return None


def admission_error(ctx: Optional[AntibanContext]) -> Optional[HTTPException]:
    """把准入拒绝翻译成调用方可直接抛出的 HTTPException；放行则返回 None。"""
    if not ctx or not ctx.admission_denied:
        return None
    return HTTPException(status_code=ctx.denial_status, detail=f"Account unavailable: {ctx.denial_reason}")


def get_admission_stats() -> Dict[str, int]:
    """按拒绝原因的累计计数（匿名，不含任何账号标识）。"""
    return dict(_admission_denials)


def reset_admission_stats() -> None:
    _admission_denials.clear()


async def init() -> None:
    """应用启动时调用。"""
    if not configs.enable_antiban:
        logger.info("[antiban] disabled; original behavior preserved")
        return

    # 先把部署形态说清楚：多 Worker 无协调层必须显式告警，不能静默按本进程上限放行
    _log_coordination_status(coordination_status())

    # 冷启动：把已加载 tokens 批量分配到桶（已绑定则跳过）
    try:
        import utils.globals as _globals  # 避免循环
        bucket.bulk_assign(list(_globals.token_list))
    except Exception as e:
        logger.error(f"[antiban] bulk_assign on startup failed: {e}")

    stats = bucket.get_bucket_stats()
    logger.info(
        f"[antiban] enabled | buckets={stats['bucket_count']} "
        f"accounts={stats['account_total']} healthy={stats['healthy']} degraded={stats.get('degraded', 0)}"
    )

    # D4: 后台异步探测 oai-client-version 是否过旧（不阻塞启动）
    try:
        import asyncio
        from utils.antiban import version_check
        asyncio.create_task(version_check.probe_and_compare())
    except Exception as e:
        logger.info(f"[antiban] version_check schedule failed: {e}")


async def acquire_context(req_token: Optional[str]) -> AntibanContext:
    """在 ChatService.initialize_request_context() 中调用。

    准入顺序（先便宜的判断，再占资源）：
      1. 死号 → 403，不占槽位；
      2. 桶降级 → 503，不占槽位；
      3. 冷却未到期且等不起 → 503，不占槽位；
      4. 并发打满 → 503，不占槽位；
      5. 拿到槽位后复查 1、2：等待窗口里新变坏的号必须在此被挡住，槽位归还；
      6. 放行，其后任何失败/取消都把槽位还回去。

    被拒时返回的 ctx 带 admission_denied / denial_status，且 concurrency_acquired=False。
    """
    ctx = AntibanContext(token=req_token or "", enabled=configs.enable_antiban)
    if not configs.enable_antiban:
        return ctx

    # 已绑定即复用；未绑定在此分配
    ctx.bucket_id = bucket.assign_account(ctx.token)
    ctx.proxy_url = bucket.get_bucket_proxy(ctx.token)

    # 1-2. 死号 / 桶降级：等待之前先挡掉，省下排队成本
    reason = _ineligible_reason(ctx)
    if reason:
        return _deny(ctx, reason)

    # 3. 冷却放行检查。必须在占槽位之前——占了再拒绝等于白吃一个在飞名额
    if not await cooldown.wait_or_skip(ctx.token):
        return _deny(ctx, "cooldown")

    # 4. 每号并发上限：拿到租约才算准入
    lease = await concurrency.acquire_lease(ctx.token)
    if lease is None:
        return _deny(ctx, "concurrency")
    ctx._lease = lease
    ctx.concurrency_acquired = True

    # 槽位已在手：此后任何异常或取消都必须归还，否则容量单调泄漏
    try:
        # 5. 复查资格。cooldown sleep 与并发排队都是可观测的等待窗口，
        #    号可能在窗口内被判死、桶可能被降级；只查等待前那一次等于放行。
        reason = _ineligible_reason(ctx)
        if reason:
            release_context(ctx)
            return _deny(ctx, reason)

        # An already-running request may have received a rate limit while we
        # queued for capacity. Do not start fresh work under its new cooldown,
        # and do not occupy a slot while waiting for another cooldown window.
        if cooldown.get_next_available(ctx.token) > time.time():
            release_context(ctx)
            return _deny(ctx, "cooldown")

        # 地域头（若未查到则保持默认）
        geo_info = geo.get_geo(ctx.proxy_url)
        if geo_info:
            ctx.header_overrides = {
                "accept-language": geo_info.get("accept_language", ""),
                "oai-language": geo_info.get("oai_language", ""),
                "_timezone_name": geo_info.get("timezone", ""),  # 非 header，仅透传给 chat_request
            }
            ctx.tz_offset_min = geo_info.get("tz_offset_min")

        # 指纹扩展（PR-5：按 token 维度补齐 screen/cores/device_memory，并返回拷贝）
        fp = fingerprint.ensure_extended(ctx.token)
        if fp:
            ctx.fp_overrides = fp
    except BaseException:
        release_context(ctx)
        raise

    return ctx


async def report_error(ctx: AntibanContext, status_code: int, detail: Any = None) -> None:
    if not ctx.enabled:
        return
    circuit.handle_response_error(ctx.token, ctx.bucket_id, status_code, detail)


async def report_network_error(ctx: AntibanContext, error_kind: str = "") -> None:
    """M3: transport 层失败（ConnectionError/超时/DNS）→ 累计后降级桶。"""
    if not ctx.enabled:
        return
    circuit.handle_network_error(ctx.token, ctx.bucket_id, error_kind)


async def report_success(ctx: AntibanContext) -> None:
    if not ctx.enabled:
        return
    cooldown.record_request(ctx.token)
    circuit.handle_response_success(ctx.token)
    circuit.reset_network_errors(ctx.bucket_id)
    bucket.mark_used(ctx.token)


def sniff_account_warning(ctx: AntibanContext, message: dict, raw_chunk: dict = None) -> None:
    """流式响应中的账号风险嗅探（Step A：仅记录，不联动 cooldown/dead）。

    调用方：chatgpt/chatFormat.py 的 stream_response，在 system 角色 continue 之前。
    设计为同步非阻塞，任何异常都吞掉，确保不影响主流程。
    """
    if not ctx or not ctx.enabled:
        return
    account_risk.sniff(ctx.token, message or {}, raw_chunk or {})


def release_context(ctx: AntibanContext) -> None:
    """释放本请求持有的并发槽位（由 ChatService.close_client 调用）。

    幂等且按租约归属：重复调用是 no-op，不会放掉别的在飞请求的槽位。
    """
    if not ctx or not ctx.enabled:
        return
    concurrency.release_lease(ctx._lease)
    ctx._lease = None
    ctx.concurrency_acquired = False
