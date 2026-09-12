"""上游确证的模型级限流（chatLimit）。

证据来源只有一个：上游 429 响应体里的 `detail.clears_in`（秒）。这是官方明确
告知的「该模型何时恢复」，属于可信的容量信号，据此延长账号冷却是容量保护。

明确不做的事：
  - 不从任意 assistant 正文推断封禁或限流（正文可以是用户自己让模型说的话）；
  - 不记录上游原文/detail 片段，只记录模型名与秒数这类业务状态。
"""

import time
from datetime import datetime
from typing import Optional

from utils import configs
from utils.antiban.concurrency import anon_id
from utils.Logger import logger

limit_details = {}

# 单次 chatLimit 联动冷却的上限：上游偶发给出超长 clears_in 时，
# 不把账号一次性冻到不可用；剩余时间仍由 limit_details 记录并在准入时拒绝该模型。
MAX_LINKED_COOLDOWN_SECONDS = 7200


def _link_cooldown(token: str, model: str, clears_in: int) -> None:
    """把上游确证的限流延长到账号冷却。惰性导入避免与 antiban 包的循环依赖。"""
    if not configs.enable_antiban or not token or clears_in <= 0:
        return
    try:
        from utils.antiban import cooldown
        cooldown.extend_cooldown(
            token, min(int(clears_in), MAX_LINKED_COOLDOWN_SECONDS), reason="chat_limit"
        )
    except Exception as e:
        # 联动失败不能影响主流程；只记录异常类型，不记录异常原文
        logger.error(f"[antiban] chat_limit cooldown link failed: {type(e).__name__}")


def check_is_limit(detail, token, model):
    """上游 429 的 detail 带 clears_in → 记录模型限流窗口并延长账号冷却。"""
    if not (token and isinstance(detail, dict) and detail.get('clears_in')):
        return
    clears_in = detail.get('clears_in')
    if not isinstance(clears_in, (int, float)) or clears_in <= 0:
        return
    clear_time = int(time.time()) + int(clears_in)
    limit_details.setdefault(token, {})[model] = clear_time
    logger.info(
        f"[antiban] {anon_id(token)} reached {model} limit, clears at "
        f"{datetime.fromtimestamp(clear_time).replace(microsecond=0)}"
    )
    _link_cooldown(token, model, int(clears_in))


def get_limit_clear_time(token: str, model: str) -> Optional[int]:
    """该账号该模型的限流解除时间戳；无记录或已过期返回 None。"""
    if not token:
        return None
    clear_time = (limit_details.get(token) or {}).get(model)
    if not clear_time or clear_time <= int(time.time()):
        return None
    return clear_time


async def handle_request_limit(token, model):
    try:
        clear_time = (limit_details.get(token) or {}).get(model)
        if clear_time is None:
            return None
        if clear_time > int(time.time()):
            clear_date = datetime.fromtimestamp(clear_time).replace(microsecond=0)
            result = (
                f"Request limit exceeded. You can continue with the default model now, "
                f"or try again after {clear_date}"
            )
            logger.info(f"[antiban] {anon_id(token)} {model} still limited until {clear_date}")
            return result
        del limit_details[token][model]
        return None
    except Exception as e:
        # 只记录异常类型：异常原文可能带上游响应片段
        logger.error(f"[antiban] chat_limit check error: {type(e).__name__}")
        return None
