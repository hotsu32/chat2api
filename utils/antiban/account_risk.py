"""账号风险嗅探（Step A 记录 + Step B 联动）。

Step A（enable_antiban 开启即生效，仅记录）：在流式响应中检测 ChatGPT 服务端下发的
"账号使用异常"软警告（降智/封控预警），记录到 data/account_warnings.json 供校准。
命中后**不**中断响应。

Step B（account_degraded_link_enabled 打开后生效）：命中后联动熔断/冷却——
  累计命中 < account_degraded_mark_dead_threshold → extend_cooldown（软退避）；
  累计命中 >= 阈值 → mark_dead（硬熔断，等待 circuit 的 recheck）。
因 WARNING_PATTERNS 偏宽松（可能误判正常 system 提示），Step B 默认关闭，校准后再开。

接入点：chatgpt/chatFormat.py 的 system 角色分支 + moderation 类型 chunk。
"""

import hashlib
import json
import re
import threading
import time
from typing import Any, Dict, Optional

import utils.globals as globals
from utils import configs
from utils.antiban.concurrency import anon_id
from utils.Logger import logger


# 关键词：英文官方文案 + 中文常见文案 + 通用近义词
# 多语言覆盖，宽松匹配（precision 低没关系，先用样本校准）
WARNING_PATTERNS = [
    # 英文标准文案
    r"unusual\s+activity",
    r"detected\s+automated",
    r"automated\s+behavior",
    r"flagged\s+for",
    r"flagged\s+as",
    r"violat\w+\s+(our|the)\s+(usage\s+)?polic",
    r"suspicious\s+activity",
    r"abnormal\s+(use|activity)",
    r"account\s+has\s+been\s+(temporarily\s+)?(suspended|restricted|flagged)",
    r"please\s+slow\s+down",
    r"sharing\s+(your\s+)?account",
    # 中文常见文案
    r"账[号户].*?(异常|被限制|被封)",
    r"检测到.*?(异常|可疑|自动化)",
    r"涉嫌.*?违反",
    r"使用频率过高",
    r"账[号户]共享",
]
_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in WARNING_PATTERNS]


# metadata 中可能携带的风险标记字段（OpenAI 经常调整字段名，按发现增减）
METADATA_FLAG_KEYS = (
    "is_user_system_message",
    "user_system_message",
    "warning",
    "warning_type",
    "moderation_response",
    "message_safety_events",
    "safety_event",
    "risk_event",
    "abuse_event",
)


_write_lock = threading.Lock()
_MAX_RECORDS_PER_TOKEN = 50  # 单 token 最多保留 50 条最新警告，避免文件无限增长


def _persist() -> None:
    """写盘（持锁调用）。失败仅打日志，不向上抛。"""
    try:
        with open(globals.ACCOUNT_WARNINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(globals.account_warnings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[account_risk] persist failed: {type(e).__name__}")


def _extract_text(message: Dict[str, Any]) -> str:
    """从 ChatGPT 消息体里提取所有可能含警告文本的字段，拼成单条字符串供 regex 扫描。"""
    fragments = []

    content = message.get("content") or {}
    if isinstance(content, dict):
        # 主要文本：content.parts[]
        parts = content.get("parts") or []
        for p in parts:
            if isinstance(p, str):
                fragments.append(p)
            elif isinstance(p, dict):
                # multimodal_text 等场景，部分 part 是 dict
                for v in p.values():
                    if isinstance(v, str):
                        fragments.append(v)
        # 备用字段
        for k in ("text", "result"):
            v = content.get(k)
            if isinstance(v, str):
                fragments.append(v)

    # metadata 里的 banner/warning 文案
    meta = message.get("metadata") or {}
    for k in ("warning_text", "banner_text", "rate_limit_reached_text", "reason"):
        v = meta.get(k)
        if isinstance(v, str):
            fragments.append(v)

    return "\n".join(fragments).strip()


def _check_metadata_flags(meta: Dict[str, Any]) -> Optional[str]:
    """返回命中的 metadata flag 名（用作 pattern 字段）；未命中返回 None。"""
    if not isinstance(meta, dict):
        return None
    for k in METADATA_FLAG_KEYS:
        v = meta.get(k)
        if v in (None, False, "", 0, [], {}):
            continue
        return f"metadata.{k}"
    return None


def _match_text(text: str) -> Optional[str]:
    """返回命中的 pattern 源串；未命中返回 None。"""
    if not text:
        return None
    for pat, src in zip(_COMPILED_PATTERNS, WARNING_PATTERNS):
        if pat.search(text):
            return src
    return None


def _escalate(token: str, hit_count: int) -> None:
    """Step B：降智命中 → 联动冷却/熔断。

    hit_count < threshold → 延长冷却（软退避，暂避风头）；
    hit_count >= threshold → mark_dead（硬熔断，等待 circuit 的 recheck）。
    惰性导入避免与 guard 的循环依赖；异常吞掉，不打断响应流。
    """
    try:
        from utils.antiban import circuit, cooldown
        threshold = configs.account_degraded_mark_dead_threshold
        if hit_count >= threshold:
            circuit.mark_dead(token, "degraded_quality")
            logger.error(
                f"[account_risk] {anon_id(token)} degraded x{hit_count} >= {threshold} → mark_dead"
            )
        else:
            cooldown.extend_cooldown(
                token, configs.account_degraded_cooldown, reason="degraded_quality"
            )
            logger.warning(
                f"[account_risk] {anon_id(token)} degraded x{hit_count} < {threshold} "
                f"→ extend_cooldown {configs.account_degraded_cooldown}s"
            )
    except Exception as e:
        # 只记录异常类型：异常原文可能带上游响应内容
        logger.error(f"[account_risk] escalate error (suppressed): {type(e).__name__}")


def sniff(token: Optional[str], message: Dict[str, Any], raw_chunk: Optional[Dict[str, Any]] = None) -> None:
    """主入口：检查一条 SSE chunk 中的 message 是否含账号风险信号。

    设计为非阻塞、强容错：任何异常都吞掉只打日志，确保不影响主响应流。

    Args:
        token: 当前请求 token（refresh_token / sess- / access_token 均可，仅作 key）
        message: chunk_old_data.get("message", {}) 对应的 dict
        raw_chunk: chunk_old_data 整体，用于读取顶层 type / safe_urls 等
    """
    if not configs.enable_antiban:
        return
    if not token:
        return

    try:
        # 1) 文本嗅探
        text = _extract_text(message)
        pattern_hit = _match_text(text)

        # 2) metadata 标记嗅探
        if not pattern_hit:
            meta_hit = _check_metadata_flags(message.get("metadata") or {})
            if meta_hit:
                pattern_hit = meta_hit

        # 3) 顶层 type 嗅探（如 type=="warning" / "safety_event"）
        if not pattern_hit and isinstance(raw_chunk, dict):
            top_type = (raw_chunk.get("type") or "").lower()
            if top_type in ("warning", "safety_event", "abuse_event", "account_warning"):
                pattern_hit = f"chunk.type={top_type}"

        if not pattern_hit:
            return

        # 命中：构造记录。**不保存上游原文**——命中文本可能含会话内容或账号信息。
        # 保留 pattern（规则命中项）、长度和不可逆摘要：足以做「同一条文案重复出现」
        # 的聚类校准，但无法还原正文。
        text_for_digest = text or json.dumps(message.get("metadata") or {}, ensure_ascii=False)
        record = {
            "hit_at": int(time.time()),
            "pattern": pattern_hit,
            "text_digest": hashlib.sha256(text_for_digest.encode("utf-8")).hexdigest()[:16],
            "text_len": len(text_for_digest),
            "role": (message.get("author") or {}).get("role"),
        }

        with _write_lock:
            bucket = globals.account_warnings.setdefault(token, [])
            bucket.append(record)
            # 截断到最大保留条数
            if len(bucket) > _MAX_RECORDS_PER_TOKEN:
                del bucket[: len(bucket) - _MAX_RECORDS_PER_TOKEN]
            _persist()

        # 只记录匿名账号标识与命中规则；命中文案本身不入日志
        logger.warning(f"[account_risk] HIT {anon_id(token)} pattern={pattern_hit}")

        # Step B: 降智命中 → 联动冷却/熔断（校准后启用，避免误杀）
        if configs.account_degraded_link_enabled:
            _escalate(token, len(bucket))
    except Exception as e:
        # 任何异常都不能影响主流程；只记异常类型，原文可能带上游内容
        logger.error(f"[account_risk] sniff error (suppressed): {type(e).__name__}")


def get_warnings(token: str) -> list:
    """供管理后台读取某 token 的历史警告。"""
    if not token:
        return []
    return list(globals.account_warnings.get(token, []))


def get_warning_summary() -> Dict[str, Dict[str, Any]]:
    """供后台批量展示：anon_id -> {count, last_hit_at, last_pattern}。

    键用匿名标识而不是 token：这个返回值会进后台接口/日志，不能携带凭据。
    """
    summary = {}
    for token, records in globals.account_warnings.items():
        if not records:
            continue
        last = records[-1]
        summary[anon_id(token)] = {
            "count": len(records),
            "last_hit_at": last.get("hit_at"),
            "last_pattern": last.get("pattern"),
        }
    return summary
