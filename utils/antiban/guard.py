"""Antiban 对外统一入口（PR-1 骨架：不拦截不改动，仅日志）。

后续 PR 按顺序充实：
  PR-2 bucket 粘性
  PR-3 cooldown/geo
  PR-4 circuit 报错上报
  PR-5 fingerprint 扩展
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import hashlib
import os
import time

from fastapi import HTTPException

from utils import configs
from utils.antiban import account_risk, bucket, circuit, concurrency, cooldown, fingerprint, geo
from utils.Logger import logger

# 拒绝原因 → 对外 HTTP 状态。
#   503：账号暂时不可用，换号重试有意义（冷却中、桶降级、并发打满、出口已失效、
#        严格绑定下分不到健康桶）。
#   403：这个号别再试了，重试只会加重风险（已熔断为死号）。
DENIAL_STATUS = {
    "account_dead": 403,
    "bucket_dead": 503,
    "bucket_degraded": 503,
    "cooldown": 503,
    "concurrency": 503,
    # 严格绑定要求每个号有一个固定出口；分不到桶时这个请求没有任何绑定保证。
    # 503 而不是 403：问题在容量与出口，换号/等桶恢复都可能成功。
    "no_healthy_bucket": 503,
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
#
# 当前契约（最小且可证伪）：
#   * 单 Worker / 未声明 worker 数：正常启用，coordination 字段如实标注
#     capacity_is_global=False（未声明时是「假定单进程」，不是「保证」）。
#   * 声明 worker 数 > 1 且没有共享协调层：**启动期拒绝**（fail closed）。
#     没有 Redis 之类的共享后端时，多 Worker 下本层无法兑现它宣称的账号保护，
#     静默按每 Worker 上限放行等于把「worker 数 × 上限」的真实并发当成上限。
#     与其降级运行并让人误以为已受保护，不如拒绝启动，把问题交给部署方：
#     要么回到单 Worker，要么先落地共享协调层。
#   * 声明了 worker 数但读不出「一个正整数」（例如 WEB_CONCURRENCY=0 或 auto）：
#     同样算无法证明单进程，同样启动期拒绝。0 不是「一个 worker」——gunicorn
#     把 0 展开成 (2 × CPU 核数) + 1，把它读成未声明就是在多 Worker 下静默放行。
#   * 声明了共享协调层（ANTIBAN_COORDINATOR_URL）：本层**没有**协调层客户端，
#     所以这个声明既不能解锁多 Worker，也不能被静默忽略——启动期拒绝，并在
#     coordination 里如实报告「声明了但不可用」。假装协调层生效才是 fail open。
# 判定与拒绝都放在 antiban 层，因为它才是唯一知道自身协调能力的地方。
# ---------------------------------------------------------------------------
COORDINATION_DISABLED = "disabled"
COORDINATION_SINGLE_PROCESS = "single_process"
COORDINATION_SINGLE_PROCESS_ASSUMED = "single_process_assumed"
COORDINATION_MULTI_PROCESS_UNCOORDINATED = "multi_process_uncoordinated"
COORDINATION_COORDINATOR_UNUSABLE = "coordinator_declared_unusable"

# worker 声明状态。区分「没声明」与「声明了但读不出来」：
#   前者是「假定单进程」（既有形态），后者必须按多 Worker 处理（fail closed）。
WORKERS_ABSENT = "absent"
WORKERS_DECLARED = "declared"
WORKERS_UNUSABLE = "unusable"

# 进程模型由部署方声明。这不是凭据，只是 worker 数量。
_WORKER_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "WORKERS")

# 共享协调层的声明入口。只认本层命名空间下的变量：通用的 REDIS_URL 可能属于
# 别的子系统，拿它当「antiban 已协调」的证据会是凭空的跨模块推断。
_COORDINATOR_ENV_VARS = ("ANTIBAN_COORDINATOR_URL", "ANTIBAN_REDIS_URL")


class CoordinationConfigError(RuntimeError):
    """协调能力与部署声明不匹配。启动期抛出即 fail closed。"""


class UncoordinatedMultiWorkerError(CoordinationConfigError):
    """antiban 已启用，但部署形态是多 Worker 且没有共享协调层。

    进程内状态无法在多 Worker 间共享，容量与冷却会被按 worker 数稀释。
    启动期抛此异常即 fail closed：拒绝在无法兑现保护的情况下运行。
    """


class UnusableCoordinatorError(CoordinationConfigError):
    """部署声明了共享协调层，但本层没有可用的协调层客户端。

    声称「已协调」而实际仍按进程内状态运行，正是本层要避免的 fail open。
    启动期抛此异常：要么去掉这个声明，要么先落地能真正使用的协调层。
    """


def _worker_declaration() -> Tuple[str, Optional[int]]:
    """读取部署声明的 worker 数。

    返回 (state, count)：
      (absent, None)   没有任何声明——既有「假定单进程」形态；
      (declared, n)    所有声明都是正整数，取最大值作为上界；
      (unusable, None) 有声明但不是正整数（0 / auto / 4,4 ...）。**不得**当成
                       未声明：0 与 auto 在主流服务器里都是「由框架决定」，
                       而框架的决定通常是多进程。
    """
    counts: List[int] = []
    for var in _WORKER_ENV_VARS:
        raw = os.getenv(var)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            value = int(text)
        except (TypeError, ValueError):
            return WORKERS_UNUSABLE, None
        if value < 1:
            return WORKERS_UNUSABLE, None
        counts.append(value)
    if not counts:
        return WORKERS_ABSENT, None
    return WORKERS_DECLARED, max(counts)


def _declared_worker_source() -> Optional[str]:
    """哪个环境变量给出了 worker 声明。只返回变量名，绝不回显其值。"""
    for var in _WORKER_ENV_VARS:
        raw = os.getenv(var)
        if raw is not None and str(raw).strip():
            return var
    return None


def _declared_coordinator() -> Optional[str]:
    """共享协调层的声明（已脱敏）。未声明返回 None。

    返回值是「scheme://…:摘要」形式：协调层 URL 通常内嵌密码，原值一律不入日志、
    不入指标。这里只回答「有没有声明、声明的是哪一类」，不回答「能不能用」——
    能不能用由本层是否真的实现了协调客户端决定。
    """
    for var in _COORDINATOR_ENV_VARS:
        raw = os.getenv(var)
        if raw is None or not str(raw).strip():
            continue
        return redact_proxy(str(raw).strip())
    return None


def _declared_coordinator_source() -> Optional[str]:
    """声明协调层的环境变量名（便于运维定位），不回显其值。"""
    for var in _COORDINATOR_ENV_VARS:
        raw = os.getenv(var)
        if raw is not None and str(raw).strip():
            return var
    return None


def coordination_status() -> Dict[str, Any]:
    """本层协调能力的事实陈述。绝不声称多 Worker 安全。"""
    workers_state, workers = _worker_declaration()
    coordinator = _declared_coordinator()
    if not configs.enable_antiban:
        mode = COORDINATION_DISABLED
    elif coordinator is not None:
        # 有协调层声明但没有可用的协调客户端：既不能算已协调，也不能算未声明
        mode = COORDINATION_COORDINATOR_UNUSABLE
    elif workers_state == WORKERS_UNUSABLE:
        mode = COORDINATION_MULTI_PROCESS_UNCOORDINATED
    elif workers_state == WORKERS_ABSENT:
        mode = COORDINATION_SINGLE_PROCESS_ASSUMED
    elif workers > 1:
        mode = COORDINATION_MULTI_PROCESS_UNCOORDINATED
    else:
        mode = COORDINATION_SINGLE_PROCESS
    return {
        "mode": mode,
        "declared_workers": workers,
        "worker_declaration": workers_state,
        "worker_declaration_source": _declared_worker_source() if configs.enable_antiban else None,
        # 声明 ≠ 可用。没有可用的共享协调客户端时这里永远是 None。
        "declared_shared_coordinator": coordinator,
        "declared_shared_coordinator_source": (
            _declared_coordinator_source() if configs.enable_antiban else None
        ),
        # 没有共享协调层，容量永远是本进程口径
        "capacity_is_global": False,
        "shared_coordinator": None,
        "state_scope": "process",
    }


def _log_coordination_status(status: Dict[str, Any]) -> None:
    if status["mode"] == COORDINATION_DISABLED:
        return
    if status["mode"] == COORDINATION_COORDINATOR_UNUSABLE:
        logger.error(
            f"[antiban] coordination={COORDINATION_COORDINATOR_UNUSABLE} "
            f"declared_by={status['declared_shared_coordinator_source']}: this build has no "
            f"coordinator client, so shared capacity is not in effect."
        )
        return
    if status["mode"] == COORDINATION_MULTI_PROCESS_UNCOORDINATED:
        logger.error(
            f"[antiban] coordination={COORDINATION_MULTI_PROCESS_UNCOORDINATED} "
            f"workers={status['declared_workers']} "
            f"declaration={status['worker_declaration']} "
            f"declared_by={status['worker_declaration_source']}: per-account concurrency, "
            f"cooldown and circuit state are process-local; effective per-account "
            f"concurrency is workers x cap and cooldowns are diluted. No shared "
            f"coordinator configured."
        )
        return
    logger.info(f"[antiban] coordination={status['mode']} capacity_scope=process")


def _refuse_uncoordinated_multi_worker(status: Dict[str, Any]) -> None:
    """声明多 Worker（或声明不可用）且无共享协调层 → 启动期拒绝（fail closed）。

    先记 ERROR 再抛：日志留下「为什么拒绝了」，异常让进程起不来，
    两条证据都需要，缺一条就会变成「静默降级」。
    """
    if status["mode"] != COORDINATION_MULTI_PROCESS_UNCOORDINATED:
        return
    _log_coordination_status(status)
    source = status["worker_declaration_source"] or "/".join(_WORKER_ENV_VARS)
    if status["worker_declaration"] == WORKERS_UNUSABLE:
        declared = (
            f"{source} is set but does not read as a positive worker count (0 and "
            f"framework keywords such as 'auto' mean 'let the server decide', which is "
            f"usually several processes)"
        )
    else:
        declared = f"{status['declared_workers']} workers are declared"
    raise UncoordinatedMultiWorkerError(
        f"antiban is enabled but {declared} and no "
        f"shared coordinator is configured. Per-account concurrency, cooldown and circuit "
        f"state are process-local, so effective per-account concurrency would be "
        f"workers x cap and cooldowns would be diluted. Refusing to start: run a single "
        f"worker, or deploy a shared coordinator before enabling ENABLE_ANTIBAN. "
        f"(Worker count is read from {'/'.join(_WORKER_ENV_VARS)}; a platform-injected "
        f"value you do not control can be unset or pinned to 1.)"
    )


def _refuse_unusable_coordinator(status: Dict[str, Any]) -> None:
    """声明了共享协调层但本层用不上 → 启动期拒绝（fail closed）。

    静默忽略这个声明的后果是：运维以为每号上限是全局的，实际仍是每进程一份。
    这里选择起不来，并把「该去掉哪个变量 / 该先落地什么」写进异常。
    """
    if status["mode"] != COORDINATION_COORDINATOR_UNUSABLE:
        return
    _log_coordination_status(status)
    raise UnusableCoordinatorError(
        f"antiban is enabled and "
        f"{status['declared_shared_coordinator_source']} declares a shared coordinator, but "
        f"this build has no coordinator client: per-account concurrency, cooldown and circuit "
        f"state would still be process-local, so shared capacity would NOT be in effect. "
        f"Refusing to start rather than run with a false claim of coordination. Unset "
        f"{status['declared_shared_coordinator_source']} to run with process-local state, or "
        f"deploy a build that implements the coordinator client."
    )


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

    # 先把部署形态说清楚：多 Worker 无协调层必须启动即拒绝，不能静默按本进程上限放行；
    # 声明了协调层但本层用不上，同样是「假装已协调」，也必须拒绝启动。
    status = coordination_status()
    _refuse_unusable_coordinator(status)
    _refuse_uncoordinated_multi_worker(status)
    _log_coordination_status(status)

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
      1. 死号 → 403，不占槽位，也不产生任何桶绑定；
      2. 桶降级/失效 → 503，不占槽位；
      3. 严格绑定下分不到健康桶 → 503，不占槽位；
      4. 冷却未到期且等不起 → 503，不占槽位；
      5. 并发打满 → 503，不占槽位；
      6. 拿到槽位后复查 1、2：等待窗口里新变坏的号必须在此被挡住，槽位归还；
      7. 放行，其后任何失败/取消都把槽位还回去。

    被拒时返回的 ctx 带 admission_denied / denial_status，且 concurrency_acquired=False。
    """
    ctx = AntibanContext(token=req_token or "", enabled=configs.enable_antiban)
    if not configs.enable_antiban:
        return ctx

    # 1. 死号：最便宜、也最确定的判断，且必须在分配之前——给一个已封的号建绑定
    #    （写 routing_config / 桶索引）是无谓的副作用，还会让它挤占健康桶的位置。
    if ctx.token and circuit.is_token_dead(ctx.token):
        return _deny(ctx, "account_dead")

    # 已绑定即复用；未绑定在此分配
    ctx.bucket_id = bucket.assign_account(ctx.token)
    ctx.proxy_url = bucket.get_bucket_proxy(ctx.token)

    # 2. 桶降级/失效：等待之前先挡掉，省下排队成本。
    #    必须排在「分不到桶」之前：桶降级是确定的结论，而分不到桶最多只是 503 语义。
    reason = _ineligible_reason(ctx)
    if reason:
        return _deny(ctx, reason)

    # 3. 严格绑定：分不到桶的号没有任何出口保证。bucket.assign_account 返回 None 的
    #    语义正是「这个号此刻绑不到任何健康出口」，而严格模式承诺每个号有固定出口。
    #    把 None 当成「无需绑定」放过去，等于让请求走一个没有绑定保证的出口。
    #    边界：配方里一个桶都没有时（未配代理）不存在可违约的绑定，继续放行——
    #    那是「没启用出口固定」，不是「固定失败」。宽松模式同理是显式选择。
    if ctx.bucket_id is None and configs.strict_ip_binding and bucket.has_buckets():
        return _deny(ctx, "no_healthy_bucket")

    # 4. 冷却放行检查。必须在占槽位之前——占了再拒绝等于白吃一个在飞名额
    if not await cooldown.wait_or_skip(ctx.token):
        return _deny(ctx, "cooldown")

    # 5. 每号并发上限：拿到租约才算准入
    lease = await concurrency.acquire_lease(ctx.token)
    if lease is None:
        return _deny(ctx, "concurrency")
    ctx._lease = lease
    ctx.concurrency_acquired = True

    # 槽位已在手：此后任何异常或取消都必须归还，否则容量单调泄漏
    try:
        # 6. 复查资格。cooldown sleep 与并发排队都是可观测的等待窗口，
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
