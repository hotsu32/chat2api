"""Antiban: chat2api 风控规避与账号保护层。

模块分工：
  bucket       IP-账号终身粘性桶
  cooldown     账号级冷却与请求节奏
  geo          代理 IP → 地域/时区/语言
  fingerprint  指纹扩展与持久化
  circuit      熔断与黑名单自愈
  guard        统一对外入口
  account_risk 降智/风控预警嗅探

总开关：ENABLE_ANTIBAN（默认 False）。未启用时本模块不影响任何现有流程。

诊断出口（JSON / Prometheus）：
  metrics_snapshot() 返回**纯计数 + 枚举**的可序列化快照。所有键都是有界枚举，
  不含 token、bucket id、代理串或上游原文——这些计数器可以直接暴露给指标系统，
  不需要再做一次脱敏。

边界声明：
  本层状态全部是**进程内**字典。多 Worker 部署下每号并发上限、冷却与熔断状态
  都是「每 Worker 一份」，实际并发 = worker 数 × 上限。

  因此当前契约是 fail closed，三种形态都在启动期拒绝：
    * 声明 worker 数 > 1 且没有共享协调层 → UncoordinatedMultiWorkerError；
    * 声明了 worker 数但读不出正整数（0 / auto）→ 同上。0 不是「一个 worker」，
      主流服务器把它当成「由框架决定」，而框架的决定通常是多进程；
    * 声明了共享协调层（ANTIBAN_COORDINATOR_URL）→ UnusableCoordinatorError。
      本层没有协调层客户端，声明它并不能让容量变成全局的；静默按进程内状态运行
      才是 fail open，所以宁可起不来。
  单 Worker（或未声明 worker 数）正常启用。coordination 字段把部署形态、
  worker 声明状态与「有没有声明协调层」变成可查询事实，供指标与排查使用。
  任何情况下本层都**不**声称 capacity_is_global。
"""

from utils.antiban import bucket as _bucket
from utils.antiban import circuit as _circuit
from utils.antiban import cooldown as _cooldown
from utils.antiban import guard as _guard
from utils.antiban.guard import CoordinationConfigError, UncoordinatedMultiWorkerError, UnusableCoordinatorError, acquire_context, admission_error, anon_id, coordination_status, redact_proxy, release_context, report_error, report_network_error, report_success, init, sniff_account_warning  # noqa: F401


def _bucket_status_counts():
    """桶状态计数。只数数量，不暴露 bucket id（那是出口标识）。"""
    import utils.globals as globals

    buckets = (globals.antiban_bucket or {}).get("buckets", {}) or {}
    counts = {"total": len(buckets), "healthy": 0, "degraded": 0, "dead": 0, "other": 0}
    for meta in buckets.values():
        status = (meta or {}).get("status")
        if status in ("healthy", "degraded", "dead"):
            counts[status] += 1
        else:
            counts["other"] += 1
    return counts


def metrics_snapshot():
    """JSON/Prometheus 可用的匿名可查询快照。

    返回结构（全部为计数或枚举，无任何账号/代理标识）：
      enabled              antiban 总开关
      coordination         部署形态与协调能力（见模块 docstring）
      counters             各模块的匿名计数器
      known_error_classes  已知错误类别 → 处理动作
      counts               当前存量（dead 账号数、桶状态计数）

    globals/configs 用惰性导入：本模块被 `from utils.antiban import circuit`
    这类语句触发，导入包不应该顺带初始化 SQLite 连接。
    """
    import utils.globals as globals
    from utils import configs

    enabled = bool(configs.enable_antiban)
    cooldown_stats = _cooldown.get_cooldown_stats()
    dead_accounts = len(globals.antiban_dead_tokens) if enabled else 0

    if enabled:
        bucket_counts = _bucket_status_counts()
    else:
        bucket_counts = {"total": 0, "healthy": 0, "degraded": 0, "dead": 0, "other": 0}

    return {
        "enabled": enabled,
        "coordination": _guard.coordination_status(),
        "counters": {
            "admission_denials": _guard.get_admission_stats() if enabled else {},
            "cooldown_events": cooldown_stats["events"] if enabled else {},
            "cooldown_extend_reasons": cooldown_stats["extend_reasons"] if enabled else {},
            "circuit_errors": _circuit.get_circuit_stats() if enabled else {},
        },
        "known_error_classes": _circuit.known_error_classes(),
        "counts": {
            "dead_accounts": dead_accounts,
            "error_list_accounts": len(globals.error_token_list) if enabled else 0,
            "buckets": bucket_counts,
            # 网络层审计：本层没有共享协调，容量永远是本进程口径
            "capacity_scope_is_process": True,
        },
    }
