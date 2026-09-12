"""运营审计（最小实现）。

敏感操作（号池增删改、支付结算、用户封禁/解封）必须留下一条**可检索、且不含凭据**
的记录。这不是合规意义上的完整审计系统，只是让「谁在什么时候把某个账号删了」这一类
问题有一个可以回答的地方。

三条硬规矩：

1. **不落敏感值。** 记录里永远不出现 token、cookie、代理凭据、订单外的个人信息。
   主体（用户）只以一个不可逆的派生 id（:func:`subject_id`）出现，运营者需要定位时
   用同一函数对 email 求值即可比对。
2. **白名单字段。** ``detail`` 只接受 :data:`_ALLOWED_DETAIL_KEYS` 里的键，其余键
   直接丢弃；值一律转成有上限的字符串。调用方无法「顺手」把整个请求体塞进来。
3. **审计失败不阻断业务。** 写不进去只记 error 日志并放行 —— 审计是旁路，让
   删账号因为审计库写失败而失败是本末倒置。代价是它**可能缺记录**，这一点明确
   写在这里，而不是假装它永远完整。

存储是独立的 SQLite 文件（``AUDIT_DB_PATH``），与车队库分开：审计库损坏或需要
单独归档时不影响业务库，反之亦然。表在首次使用时惰性创建，因此不需要改动
``utils.store`` 的建表脚本。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

import utils.configs as configs
from utils.Logger import logger

# ``detail`` 白名单：只允许承载匿名代码、订单标识与金额这类非敏感字段。
# 放进来的键必须能回答「发生了什么」，而不能标识「谁的凭据是什么」。
_ALLOWED_DETAIL_KEYS = frozenset({
    "amount", "count", "currency", "group", "order_id", "outcome",
    "plan_id", "proxy_name", "reason", "result", "source", "status", "tier_id",
})

_MAX_VALUE_LEN = 120
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS audit_events ("
    "  id      INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  at      INTEGER NOT NULL,"
    "  action  TEXT    NOT NULL,"
    "  actor   TEXT    NOT NULL DEFAULT '',"
    "  subject TEXT    NOT NULL DEFAULT '',"
    "  ok      INTEGER NOT NULL DEFAULT 1,"
    "  detail  TEXT    NOT NULL DEFAULT '{}'"
    ")"
)

_initialized_paths: set = set()


def subject_id(value: str) -> str:
    """把 email / seed 之类的标识压成不可逆的固定长度 id。

    审计记录里不留原文：邮件地址是个人信息，seed 是凭据。运营者要按某个邮箱翻记录，
    用同一个函数算一遍再查即可 —— 单向性是刻意的。
    """
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    return hashlib.sha256(f"audit-subject:{raw}".encode("utf-8")).hexdigest()[:32]


def _db_path() -> str:
    return getattr(configs, "audit_db_path", os.path.join("data", "audit.db"))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """惰性建表。按路径缓存，换库（测试隔离 / 重新部署）时各自建一次。"""
    path = _db_path()
    if path in _initialized_paths:
        return
    conn.execute(_SCHEMA)
    _initialized_paths.add(path)


def _clean_detail(detail: Optional[Dict[str, Any]]) -> str:
    """白名单过滤 + 定长截断，产出可安全落库的 JSON 串。"""
    cleaned: Dict[str, str] = {}
    for key, value in (detail or {}).items():
        if key not in _ALLOWED_DETAIL_KEYS or value is None:
            continue
        text = str(value)
        if len(text) > _MAX_VALUE_LEN:
            text = text[:_MAX_VALUE_LEN]
        cleaned[str(key)] = text
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)


def record(action: str, *, actor: str = "operator", subject: str = "",
           ok: bool = True, detail: Optional[Dict[str, Any]] = None) -> bool:
    """写入一条审计记录，返回是否落库成功。

    ``subject`` 必须是 :func:`subject_id` 的结果（或空），调用方不要直接传 email。
    """
    action = (action or "").strip()
    if not action:
        return False
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO audit_events (at, action, actor, subject, ok, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), action, (actor or "")[:64], (subject or "")[:64],
                 1 if ok else 0, _clean_detail(detail)),
            )
        return True
    except Exception as e:  # 旁路失败不阻断业务
        logger.error(f"[audit] write failed: {e}")
        return False


def recent(limit: int = 100) -> List[Dict[str, Any]]:
    """最近的审计记录（新在前）。读失败返回空列表，不抛。"""
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT at, action, actor, subject, ok, detail FROM audit_events "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
    except Exception as e:
        logger.error(f"[audit] read failed: {e}")
        return []
    events = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except Exception:
            detail = {}
        events.append({
            "at": row["at"],
            "action": row["action"],
            "actor": row["actor"],
            "subject": row["subject"],
            "ok": bool(row["ok"]),
            "detail": detail,
        })
    return events
