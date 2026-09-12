"""Account-domain SQLite store (fleet / 车队管理).

This module is the durable source of truth for the account domain. It replaces the
scattered JSON files (token.txt / seed_map.json / conversation_map.json /
refresh_map.json / fp_map.json / routing_config.json) with a single SQLite database,
while keeping the in-memory ``utils.globals`` structures as a write-through cache.

Design notes (mirroring the persistence-boundary swap decision):
  - ``utils.globals`` keeps its existing in-memory names (``token_list``,
    ``seed_map``, ``conversation_map``, ``refresh_map``, ``fp_map``,
    ``routing_config``, ``error_token_list``). This module only swaps *where* those
    structures are persisted: JSON files -> SQLite.
  - Antiban / wss / harvester states stay in their own JSON files (orthogonal
    subsystems, not part of the account domain).
  - Connection pattern copied from ``chatgpt/session_sticky.py`` (WAL +
    synchronous=NORMAL + busy_timeout + a write lock).

This module must NOT import ``utils.globals`` (globals imports us); DAO primitives
are kept pure and the persist helpers in globals.py call these.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from utils import configs
from utils.Logger import logger
from utils.token_type import detect_token_type

DATA_FOLDER = "data"

_DB_PATH = getattr(configs, "fleet_db_path", None) or os.path.join(DATA_FOLDER, "chat2api.db")


class StoreError(Exception):
    """A read failed at the storage layer (locked / corrupt / disk full).

    Read helpers normally swallow errors and return an empty result, which is fine
    for rendering a page but dangerous for authorization: "query failed" would be
    indistinguishable from "no such row", and the two imply opposite decisions.
    Callers that authorize pass ``strict=True`` and handle this explicitly.
    """

_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()

# Allowed columns for dynamic partial upserts (whitelist guards against typos).
ACCOUNT_COLUMNS = {
    "token", "token_type", "plan_type", "real_email", "nickname", "status",
    "proxy_name", "proxy_url", "group_name", "impersonate", "user_agent", "note",
    "refresh_info", "fingerprint", "last_health_check", "created_at", "updated_at",
}


def _db_path() -> str:
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    path = _db_path()
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def init_db() -> None:
    """Create schema if missing. Idempotent."""
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        try:
            with _connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS accounts (
                        token             TEXT PRIMARY KEY,
                        token_type        TEXT,
                        plan_type         TEXT,
                        real_email        TEXT,
                        nickname          TEXT,
                        status            TEXT NOT NULL DEFAULT 'healthy',
                        proxy_name        TEXT,
                        proxy_url         TEXT,
                        group_name        TEXT,
                        impersonate       TEXT,
                        user_agent        TEXT,
                        note              TEXT,
                        refresh_info      TEXT,
                        fingerprint       TEXT,
                        last_health_check INTEGER,
                        created_at        INTEGER,
                        updated_at        INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS users (
                        seed            TEXT PRIMARY KEY,
                        plan_type       TEXT,
                        current_account TEXT,
                        status          TEXT NOT NULL DEFAULT 'active',
                        created_at      INTEGER,
                        updated_at      INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS conversations (
                        conv_id     TEXT PRIMARY KEY,
                        seed        TEXT NOT NULL,
                        account     TEXT,
                        title       TEXT,
                        create_time TEXT,
                        update_time TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_conv_seed ON conversations(seed);

                    CREATE TABLE IF NOT EXISTS usage_events (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        seed       TEXT,
                        account    TEXT,
                        kind       TEXT,
                        created_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_usage_seed ON usage_events(seed, created_at);
                    CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_events(account, created_at);

                    CREATE TABLE IF NOT EXISTS proxies (
                        name       TEXT PRIMARY KEY,
                        proxy_url  TEXT NOT NULL,
                        group_size INTEGER DEFAULT 25,
                        created_at INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS user_auth (
                        email         TEXT PRIMARY KEY,
                        password_hash TEXT,
                        seed          TEXT,
                        tier_id       TEXT,
                        status        TEXT NOT NULL DEFAULT 'active',
                        pw_version    INTEGER NOT NULL DEFAULT 1,
                        created_at    INTEGER,
                        updated_at    INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_user_auth_seed ON user_auth(seed);

                    CREATE TABLE IF NOT EXISTS orders (
                        order_id   TEXT PRIMARY KEY,
                        email      TEXT,
                        tier_id    TEXT,
                        amount     TEXT,
                        status     TEXT NOT NULL DEFAULT 'pending',
                        expires_at INTEGER,
                        created_at INTEGER,
                        updated_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(email);

                    CREATE TABLE IF NOT EXISTS email_tokens (
                        token      TEXT PRIMARY KEY,
                        email      TEXT NOT NULL,
                        kind       TEXT NOT NULL,
                        expires_at INTEGER,
                        used       INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_email_tokens_email ON email_tokens(email);

                    CREATE TABLE IF NOT EXISTS meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT
                    );
                    """
                )
                _add_missing_columns(conn)
            _INITIALIZED = True
            logger.info(f"[store] db ready at {_db_path()}")
        except Exception as e:
            logger.error(f"[store] init_db failed: {e}")


# 既有库的增量列：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 老库需要 ALTER 一次。只加可空列（或带常量默认值的 NOT NULL 列，SQLite 允许），
# 不改类型不删列，因此对回滚安全 —— 旧代码看不见新列，照常工作。
_ADDED_COLUMNS = (
    ("orders", "expires_at", "INTEGER"),
    # 密码版本：改密 / 重置密码时 +1，旧会话 token 里的 pwv 对不上即失效。
    # 默认 1 让老行天然等于「从未改过密」，无需数据回填。
    ("user_auth", "pw_version", "INTEGER NOT NULL DEFAULT 1"),
)


def _add_missing_columns(conn) -> None:
    """幂等补列：列已存在则跳过（老库升级路径）。"""
    for table, column, coltype in _ADDED_COLUMNS:
        try:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                logger.info(f"[store] migrated: {table}.{column} added")
        except Exception as e:
            logger.error(f"[store] add column {table}.{column} failed: {e}")


# --------------------------------------------------------------------------- meta

def get_meta(key: str) -> Optional[str]:
    try:
        with _connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"[store] get_meta error: {e}")
        return None


def set_meta(key: str, value: str) -> None:
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    except Exception as e:
        logger.error(f"[store] set_meta error: {e}")


def is_migrated() -> bool:
    return get_meta("migrated") == "1"


# ----------------------------------------------------------------------- accounts

def _account_row_to_dict(row: tuple, cols: List[str]) -> Dict[str, Any]:
    return dict(zip(cols, row))


_ACCOUNT_SELECT_COLS = [
    "token", "token_type", "plan_type", "real_email", "nickname", "status",
    "proxy_name", "proxy_url", "group_name", "impersonate", "user_agent", "note",
    "refresh_info", "fingerprint", "last_health_check", "created_at", "updated_at",
]


def get_account(token: str) -> Optional[Dict[str, Any]]:
    try:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_ACCOUNT_SELECT_COLS)} FROM accounts WHERE token=?",
                (token,),
            ).fetchone()
            return _account_row_to_dict(row, _ACCOUNT_SELECT_COLS) if row else None
    except Exception as e:
        logger.error(f"[store] get_account error: {e}")
        return None


def list_accounts() -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_ACCOUNT_SELECT_COLS)} FROM accounts ORDER BY rowid"
            ).fetchall()
            return [_account_row_to_dict(r, _ACCOUNT_SELECT_COLS) for r in rows]
    except Exception as e:
        logger.error(f"[store] list_accounts error: {e}")
        return []


def get_account_by_plan(plan_type: str, status: Optional[str] = "healthy") -> List[Dict[str, Any]]:
    """Return full account rows for a tier, filtered by status (None = any)."""
    try:
        with _connect() as conn:
            if status:
                rows = conn.execute(
                    f"SELECT {', '.join(_ACCOUNT_SELECT_COLS)} FROM accounts "
                    "WHERE plan_type=? AND status=? ORDER BY rowid",
                    (plan_type, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {', '.join(_ACCOUNT_SELECT_COLS)} FROM accounts "
                    "WHERE plan_type=? ORDER BY rowid",
                    (plan_type,),
                ).fetchall()
            return [_account_row_to_dict(r, _ACCOUNT_SELECT_COLS) for r in rows]
    except Exception as e:
        logger.error(f"[store] get_account_by_plan error: {e}")
        return []


def get_healthy_accounts() -> List[Dict[str, Any]]:
    """Return all healthy accounts (any tier), for graceful fallback when a tier pool is empty."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_ACCOUNT_SELECT_COLS)} FROM accounts WHERE status='healthy' ORDER BY rowid"
            ).fetchall()
            return [_account_row_to_dict(r, _ACCOUNT_SELECT_COLS) for r in rows]
    except Exception as e:
        logger.error(f"[store] get_healthy_accounts error: {e}")
        return []


def upsert_account(token: str, **fields: Any) -> None:
    """Partial upsert. Only non-None whitelisted fields are written; updated_at bumped."""
    if not token:
        return
    cols = [c for c in fields if c in ACCOUNT_COLUMNS and c != "token" and fields[c] is not None]
    if not cols:
        return
    now = int(time.time())
    if "updated_at" not in cols:
        cols.append("updated_at")
    values = [fields[c] if c != "updated_at" else now for c in cols]
    col_sql = ", ".join(cols)
    val_sql = ", ".join("?" for _ in cols)
    upd_sql = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO accounts (token, {col_sql}) VALUES (?, {val_sql}) "
        f"ON CONFLICT(token) DO UPDATE SET {upd_sql}"
    )
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(sql, [token] + values)
    except Exception as e:
        logger.error(f"[store] upsert_account error: {e}")


def delete_account(token: str) -> None:
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("DELETE FROM accounts WHERE token=?", (token,))
    except Exception as e:
        logger.error(f"[store] delete_account error: {e}")


# -------------------------------------------------------------------------- users

def get_user(seed: str) -> Optional[Dict[str, Any]]:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT seed, plan_type, current_account, status, created_at, updated_at "
                "FROM users WHERE seed=?",
                (seed,),
            ).fetchone()
            if not row:
                return None
            return {
                "seed": row[0], "plan_type": row[1], "current_account": row[2],
                "status": row[3], "created_at": row[4], "updated_at": row[5],
            }
    except Exception as e:
        logger.error(f"[store] get_user error: {e}")
        return None


def upsert_user(seed: str, **fields: Any) -> None:
    if not seed:
        return
    now = int(time.time())
    allowed = {"plan_type", "current_account", "status"}
    cols = [c for c in fields if c in allowed and fields[c] is not None]
    cols.append("updated_at")
    values = [fields[c] if c != "updated_at" else now for c in cols]
    col_sql = ", ".join(cols)
    val_sql = ", ".join("?" for _ in cols)
    upd_sql = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO users (seed, {col_sql}) VALUES (?, {val_sql}) "
        f"ON CONFLICT(seed) DO UPDATE SET {upd_sql}"
    )
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(sql, [seed] + values)
    except Exception as e:
        logger.error(f"[store] upsert_user error: {e}")


def delete_user(seed: str) -> None:
    """Delete a user row and cascade-delete its conversations."""
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("DELETE FROM conversations WHERE seed=?", (seed,))
            conn.execute("DELETE FROM users WHERE seed=?", (seed,))
    except Exception as e:
        logger.error(f"[store] delete_user error: {e}")


def list_users() -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT seed, plan_type, current_account, status, created_at, updated_at "
                "FROM users ORDER BY rowid"
            ).fetchall()
            return [{
                "seed": r[0], "plan_type": r[1], "current_account": r[2],
                "status": r[3], "created_at": r[4], "updated_at": r[5],
            } for r in rows]
    except Exception as e:
        logger.error(f"[store] list_users error: {e}")
        return []


# ---------------------------------------------------------------------- user_auth

_USER_AUTH_COLUMNS = {
    "password_hash", "seed", "tier_id", "status",
}

# 会话失效用的密码版本。不放进 _USER_AUTH_COLUMNS：它只能经 bump_pw_version
# 原子自增，不接受调用方传值 —— 允许直接赋值就迟早会有人写回一个旧版本号，
# 把刚吊销的会话又放回来。
_PW_VERSION_DEFAULT = 1


def _row_pw_version(value: Any) -> int:
    """老库补列前的行读出来可能是 None，统一按初始版本 1 处理。"""
    try:
        return int(value) if value is not None else _PW_VERSION_DEFAULT
    except (TypeError, ValueError):
        return _PW_VERSION_DEFAULT


def get_user_auth(email: str) -> Optional[Dict[str, Any]]:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT email, password_hash, seed, tier_id, status, pw_version, created_at, updated_at "
                "FROM user_auth WHERE email=?",
                (email,),
            ).fetchone()
            if not row:
                return None
            return {
                "email": row[0], "password_hash": row[1], "seed": row[2],
                "tier_id": row[3], "status": row[4], "pw_version": _row_pw_version(row[5]),
                "created_at": row[6], "updated_at": row[7],
            }
    except Exception as e:
        logger.error(f"[store] get_user_auth error: {e}")
        return None


def get_user_auth_by_seed(seed: str, strict: bool = False) -> Optional[Dict[str, Any]]:
    """Look up the SaaS account owning ``seed``.

    ``strict=True`` raises :class:`StoreError` instead of returning None when the
    query itself fails, so the entitlement layer can tell "operator seed, no row"
    (fail-open) apart from "database unreachable" (must not fail open).
    """
    if not seed:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT email, password_hash, seed, tier_id, status, pw_version, created_at, updated_at "
                "FROM user_auth WHERE seed=?",
                (seed,),
            ).fetchone()
            if not row:
                return None
            return {
                "email": row[0], "password_hash": row[1], "seed": row[2],
                "tier_id": row[3], "status": row[4], "pw_version": _row_pw_version(row[5]),
                "created_at": row[6], "updated_at": row[7],
            }
    except Exception as e:
        logger.error(f"[store] get_user_auth_by_seed error: {e}")
        if strict:
            raise StoreError(str(e)) from e
        return None


def bump_pw_version(email: str) -> int:
    """密码版本 +1（吊销该账号已签发的全部会话），返回新版本号；失败抛 StoreError。

    自增在 SQL 里做而不是「读出来 +1 再写回」：两台设备同时改密时，读-改-写会让
    后写的那次覆盖前一次，结果版本只涨了 1，其中一条旧会话就活了下来。

    吊销失败必须让调用方知道 —— 静默吞掉异常等于「提示密码已更新，但旧会话还在」，
    这正是用户改密码想要解决的那个问题。
    """
    if not email:
        raise StoreError("bump_pw_version: empty email")
    try:
        with _WRITE_LOCK, _connect() as conn:
            cur = conn.execute(
                "UPDATE user_auth SET pw_version = COALESCE(pw_version, ?) + 1, updated_at = ? "
                "WHERE email = ?",
                (_PW_VERSION_DEFAULT, int(time.time()), email),
            )
            if cur.rowcount == 0:
                raise StoreError(f"bump_pw_version: no such user {email}")
            row = conn.execute(
                "SELECT pw_version FROM user_auth WHERE email=?", (email,)
            ).fetchone()
            return _row_pw_version(row[0] if row else None)
    except StoreError:
        raise
    except Exception as e:
        logger.error(f"[store] bump_pw_version error: {e}")
        raise StoreError(str(e)) from e


def upsert_user_auth(email: str, **fields: Any) -> None:
    """Partial upsert of a user_auth row. Whitelisted columns; updated_at bumped.

    Pass ``strict=True`` when the caller cannot proceed if the write did not land --
    registration is the case that matters: swallowing the error there hands the user
    a session cookie for a row that does not exist, so every page afterwards 401s
    with no explanation.
    """
    strict = bool(fields.pop("strict", False))
    if not email:
        if strict:
            raise StoreError("upsert_user_auth: empty email")
        return
    cols = [c for c in fields if c in _USER_AUTH_COLUMNS and fields[c] is not None]
    if not cols:
        if strict:
            raise StoreError("upsert_user_auth: no writable columns")
        return
    now = int(time.time())
    cols.append("updated_at")
    values = [fields[c] if c != "updated_at" else now for c in cols]
    col_sql = ", ".join(cols)
    val_sql = ", ".join("?" for _ in cols)
    upd_sql = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO user_auth (email, {col_sql}) VALUES (?, {val_sql}) "
        f"ON CONFLICT(email) DO UPDATE SET {upd_sql}"
    )
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(sql, [email] + values)
    except Exception as e:
        logger.error(f"[store] upsert_user_auth error: {e}")
        if strict:
            raise StoreError(f"upsert_user_auth failed for {email}: {e}") from e


def list_user_auth() -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT email, seed, tier_id, status, created_at, updated_at "
                "FROM user_auth ORDER BY rowid"
            ).fetchall()
            return [{
                "email": r[0], "seed": r[1], "tier_id": r[2], "status": r[3],
                "created_at": r[4], "updated_at": r[5],
            } for r in rows]
    except Exception as e:
        logger.error(f"[store] list_user_auth error: {e}")
        return []


# ------------------------------------------------------------------------ orders

_ORDER_COLS = "order_id, email, tier_id, amount, status, expires_at, created_at, updated_at"


def _order_row_to_dict(r: tuple) -> Dict[str, Any]:
    return {
        "order_id": r[0], "email": r[1], "tier_id": r[2], "amount": r[3],
        "status": r[4], "expires_at": r[5], "created_at": r[6], "updated_at": r[7],
    }


def create_order(order_id: str, email: str, tier_id: str, amount: str,
                 status: str = "pending") -> None:
    if not order_id:
        return
    now = int(time.time())
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(
                "INSERT INTO orders (order_id, email, tier_id, amount, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, email, tier_id, amount, status, now, now),
            )
    except Exception as e:
        logger.error(f"[store] create_order error: {e}")


def activate_order(order_id: str, expires_at: int) -> bool:
    """把 pending 单置为 paid 并物化到期时间。幂等：非 pending 单不再改动。

    返回是否发生了状态流转 —— 支付回调可能重复投递，重复激活不能延长有效期。
    用单条带 ``WHERE status='pending'`` 的 UPDATE 保证原子性，避免并发回调各读各写。
    """
    if not order_id:
        return False
    try:
        with _WRITE_LOCK, _connect() as conn:
            cur = conn.execute(
                "UPDATE orders SET status='paid', expires_at=?, updated_at=? "
                "WHERE order_id=? AND status='pending'",
                (int(expires_at), int(time.time()), order_id),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"[store] activate_order error: {e}")
        return False


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    try:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT {_ORDER_COLS} FROM orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
            return _order_row_to_dict(row) if row else None
    except Exception as e:
        logger.error(f"[store] get_order error: {e}")
        return None


def update_order_status(order_id: str, status: str) -> None:
    if not order_id:
        return
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
                (status, int(time.time()), order_id),
            )
    except Exception as e:
        logger.error(f"[store] update_order_status error: {e}")


def list_orders(email: Optional[str] = None, strict: bool = False) -> List[Dict[str, Any]]:
    """Orders for ``email``; ``None`` lists every order (admin view).

    An empty string means "a user whose email is empty", never "all users" --
    treating it as the latter would hand one caller the whole table's worth of
    entitlements. Only ``None`` widens the scope.
    """
    if email is not None and not email:
        return []
    try:
        with _connect() as conn:
            if email is not None:
                rows = conn.execute(
                    f"SELECT {_ORDER_COLS} FROM orders WHERE email=? ORDER BY rowid DESC",
                    (email,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_ORDER_COLS} FROM orders ORDER BY rowid DESC"
                ).fetchall()
            return [_order_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[store] list_orders error: {e}")
        if strict:
            raise StoreError(str(e)) from e
        return []


# ------------------------------------------------------------------ email_tokens
# 邮箱验证 / 找回密码的一次性 token（kind: verify / reset）

def create_email_token(token: str, email: str, kind: str, expires_at: int) -> None:
    try:
        now = int(time.time())
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(
                "INSERT INTO email_tokens (token, email, kind, expires_at, used, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (token, email, kind, expires_at, now),
            )
    except Exception as e:
        logger.error(f"[store] create_email_token error: {e}")


def get_email_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT token, email, kind, expires_at, used, created_at "
                "FROM email_tokens WHERE token=?",
                (token,),
            ).fetchone()
            if not row:
                return None
            return {
                "token": row[0], "email": row[1], "kind": row[2],
                "expires_at": row[3], "used": row[4], "created_at": row[5],
            }
    except Exception as e:
        logger.error(f"[store] get_email_token error: {e}")
        return None


def mark_email_token_used(token: str) -> None:
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("UPDATE email_tokens SET used=1 WHERE token=?", (token,))
    except Exception as e:
        logger.error(f"[store] mark_email_token_used error: {e}")


# ------------------------------------------------------------------ conversations

def upsert_conversation(conv_id: str, seed: str, account: Optional[str],
                        title: Optional[str], create_time: Optional[str],
                        update_time: Optional[str]) -> None:
    if not conv_id or not seed:
        return
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (conv_id, seed, account, title, create_time, update_time)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conv_id) DO UPDATE SET
                    seed=excluded.seed,
                    account=excluded.account,
                    title=excluded.title,
                    update_time=excluded.update_time
                """,
                (conv_id, seed, account, title, create_time, update_time),
            )
    except Exception as e:
        logger.error(f"[store] upsert_conversation error: {e}")


def list_seed_conversations(seed: str) -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT conv_id, seed, account, title, create_time, update_time "
                "FROM conversations WHERE seed=? ORDER BY update_time DESC",
                (seed,),
            ).fetchall()
            return [{
                "conv_id": r[0], "seed": r[1], "account": r[2], "title": r[3],
                "create_time": r[4], "update_time": r[5],
            } for r in rows]
    except Exception as e:
        logger.error(f"[store] list_seed_conversations error: {e}")
        return []


def delete_conversation(conv_id: str) -> None:
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("DELETE FROM conversations WHERE conv_id=?", (conv_id,))
    except Exception as e:
        logger.error(f"[store] delete_conversation error: {e}")


def replace_conversations(rows: List[tuple]) -> None:
    """Atomically rebuild the conversations table from (conv_id, seed, account, title,
    create_time, update_time) tuples."""
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("DELETE FROM conversations")
            conn.executemany(
                "INSERT INTO conversations (conv_id, seed, account, title, create_time, update_time) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
    except Exception as e:
        logger.error(f"[store] replace_conversations error: {e}")


def all_conversations() -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT conv_id, seed, account, title, create_time, update_time "
                "FROM conversations"
            ).fetchall()
            return [{
                "conv_id": r[0], "seed": r[1], "account": r[2], "title": r[3],
                "create_time": r[4], "update_time": r[5],
            } for r in rows]
    except Exception as e:
        logger.error(f"[store] all_conversations error: {e}")
        return []


# ------------------------------------------------------------------------ proxies

def list_proxies() -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT name, proxy_url, group_size, created_at FROM proxies ORDER BY rowid").fetchall()
            return [{"name": r[0], "proxy_url": r[1], "group_size": r[2], "created_at": r[3]} for r in rows]
    except Exception as e:
        logger.error(f"[store] list_proxies error: {e}")
        return []


def save_proxies(proxies: List[Dict[str, Any]]) -> None:
    """Full replace of the proxies table (cold path)."""
    now = int(time.time())
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("DELETE FROM proxies")
            for p in proxies or []:
                if not p or not p.get("proxy_url"):
                    continue
                conn.execute(
                    "INSERT INTO proxies (name, proxy_url, group_size, created_at) VALUES (?, ?, ?, ?)",
                    (p.get("name") or p.get("proxy_url"), p.get("proxy_url"),
                     int(p.get("group_size") or 25), now),
                )
    except Exception as e:
        logger.error(f"[store] save_proxies error: {e}")


# ------------------------------------------------------------------- usage events

def add_usage_event(seed: str, account: str, kind: str) -> None:
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(
                "INSERT INTO usage_events (seed, account, kind, created_at) VALUES (?, ?, ?, ?)",
                (seed, account, kind, int(time.time())),
            )
    except Exception as e:
        logger.error(f"[store] add_usage_event error: {e}")


def query_usage(since: int = 0) -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT seed, account, kind, created_at FROM usage_events WHERE created_at >= ? ORDER BY created_at",
                (since,),
            ).fetchall()
            return [{"seed": r[0], "account": r[1], "kind": r[2], "created_at": r[3]} for r in rows]
    except Exception as e:
        logger.error(f"[store] query_usage error: {e}")
        return []


def add_usage_events(events: List[tuple]) -> None:
    """Batch-insert usage events (seed, account, kind, created_at)."""
    if not events:
        return
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.executemany(
                "INSERT INTO usage_events (seed, account, kind, created_at) VALUES (?, ?, ?, ?)",
                events,
            )
    except Exception as e:
        logger.error(f"[store] add_usage_events error: {e}")


def query_usage_count(seed: Optional[str] = None, account: Optional[str] = None,
                      since: int = 0) -> int:
    """Aggregated usage count (per seed or per account, or total when both empty)."""
    try:
        with _connect() as conn:
            if seed:
                row = conn.execute(
                    "SELECT COUNT(*) FROM usage_events WHERE seed=? AND created_at>=?",
                    (seed, since),
                ).fetchone()
            elif account:
                row = conn.execute(
                    "SELECT COUNT(*) FROM usage_events WHERE account=? AND created_at>=?",
                    (account, since),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM usage_events WHERE created_at>=?", (since,),
                ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"[store] query_usage_count error: {e}")
        return 0


def query_seed_usage(seed: str, since: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
    """Return bounded, seed-scoped usage events without internal identifiers."""
    if not seed or limit <= 0:
        return []
    limit = min(int(limit), 500)
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT kind, created_at FROM usage_events "
                "WHERE seed=? AND created_at>=? ORDER BY created_at DESC LIMIT ?",
                (seed, int(since), limit),
            ).fetchall()
            return [{"kind": r[0], "created_at": r[1]} for r in rows]
    except Exception as e:
        logger.error(f"[store] query_seed_usage error: {e}")
        return []


def query_seed_usage_daily(seed: str, since: int = 0) -> List[Dict[str, Any]]:
    """Return seed-scoped usage counts grouped by local date and raw kind."""
    if not seed:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') AS day, "
                "kind, COUNT(*) FROM usage_events "
                "WHERE seed=? AND created_at>=? "
                "GROUP BY day, kind ORDER BY day DESC, kind",
                (seed, int(since)),
            ).fetchall()
            return [{"date": r[0], "kind": r[1], "count": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"[store] query_seed_usage_daily error: {e}")
        return []


# --------------------------------------------------------- migration / full load

def migrate(
    token_list: List[str],
    error_token_list: List[str],
    seed_map: Dict[str, Any],
    conversation_map: Dict[str, Any],
    refresh_map: Dict[str, Any],
    fp_map: Dict[str, Any],
    routing_config: Dict[str, Any],
) -> None:
    """One-shot JSON -> SQLite migration. Idempotent via the ``migrated`` meta flag."""
    init_db()
    if is_migrated():
        return
    now = int(time.time())
    try:
        with _WRITE_LOCK, _connect() as conn:
            error_set = set(error_token_list or [])
            bindings = (routing_config or {}).get("bindings", {})
            account_meta = (routing_config or {}).get("account_meta", {})

            for token in token_list or []:
                fp = fp_map.get(token, {}) or {}
                refresh_info = refresh_map.get(token, {}) or {}
                binding = bindings.get(token, {}) or {}
                meta = account_meta.get(token, {}) or {}
                conn.execute(
                    """
                    INSERT OR REPLACE INTO accounts
                        (token, token_type, plan_type, real_email, nickname, status,
                         proxy_name, proxy_url, group_name, impersonate, user_agent, note,
                         refresh_info, fingerprint, last_health_check, created_at, updated_at)
                    VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        token,
                        detect_token_type(token),
                        "unhealthy" if token in error_set else "healthy",
                        binding.get("proxy_name"),
                        binding.get("proxy_url") or fp.get("proxy_url"),
                        binding.get("group"),
                        fp.get("impersonate"),
                        fp.get("user-agent"),
                        meta.get("note", binding.get("note", "")),
                        json.dumps(refresh_info, ensure_ascii=False) if refresh_info else None,
                        json.dumps(fp, ensure_ascii=False) if fp else None,
                        now, now,
                    ),
                )

            # Error tokens absent from token_list (defensive: keep their status).
            for token in error_set:
                if token in (token_list or []):
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO accounts
                        (token, token_type, status, created_at, updated_at)
                    VALUES (?, ?, 'unhealthy', ?, ?)
                    """,
                    (token, detect_token_type(token), now, now),
                )

            # users + conversations from seed_map / conversation_map.
            for seed, entry in (seed_map or {}).items():
                if not isinstance(entry, dict):
                    continue
                account = entry.get("token", "") or ""
                conn.execute(
                    "INSERT OR REPLACE INTO users (seed, plan_type, current_account, status, created_at, updated_at) "
                    "VALUES (?, NULL, ?, 'active', ?, ?)",
                    (seed, account, now, now),
                )
                for conv_id in entry.get("conversations", []) or []:
                    c = (conversation_map or {}).get(conv_id, {}) or {}
                    conn.execute(
                        "INSERT OR REPLACE INTO conversations (conv_id, seed, account, title, create_time, update_time) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (conv_id, seed, account, c.get("title"), c.get("create_time"), c.get("update_time")),
                    )

            # Proxies from routing_config.
            for p in (routing_config or {}).get("proxies", []) or []:
                if not p or not p.get("proxy_url"):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO proxies (name, proxy_url, group_size, created_at) VALUES (?, ?, ?, ?)",
                    (p.get("name") or p.get("proxy_url"), p.get("proxy_url"),
                     int(p.get("group_size") or 25), now),
                )

            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated', '1')"
            )
        logger.info(
            f"[store] migrated JSON -> SQLite: {len(token_list or [])} accounts, "
            f"{len(seed_map or {})} users"
        )
    except Exception as e:
        logger.error(f"[store] migrate failed: {e}")


def load_all() -> Dict[str, Any]:
    """Reconstruct the in-memory account-domain structures from SQLite.

    Returns a dict with the same keys/names used by ``utils.globals`` so the caller
    can assign them back verbatim:
      token_list, error_token_list, refresh_map, fp_map, routing_config, seed_map,
      conversation_map.
    """
    result = {
        "token_list": [],
        "error_token_list": [],
        "refresh_map": {},
        "fp_map": {},
        "routing_config": {"proxies": [], "groups": [], "bindings": {}, "account_meta": {}, "updated_at": None},
        "seed_map": {},
        "conversation_map": {},
    }
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT token, token_type, plan_type, status, proxy_name, proxy_url, group_name, note, refresh_info, fingerprint FROM accounts ORDER BY rowid").fetchall()
            bindings = {}
            account_meta = {}
            for r in rows:
                token, token_type, plan_type, status, proxy_name, proxy_url, group_name, note, refresh_info, fingerprint = r
                result["token_list"].append(token)
                if status == "unhealthy":
                    result["error_token_list"].append(token)
                if refresh_info:
                    try:
                        result["refresh_map"][token] = json.loads(refresh_info)
                    except Exception:
                        pass
                if fingerprint:
                    try:
                        result["fp_map"][token] = json.loads(fingerprint)
                    except Exception:
                        pass
                binding = {}
                if proxy_name or proxy_url or group_name:
                    binding = {
                        "proxy_name": proxy_name, "proxy_url": proxy_url, "group": group_name,
                    }
                bindings[token] = binding
                if note:
                    account_meta[token] = {"note": note}
            result["routing_config"]["bindings"] = bindings
            result["routing_config"]["account_meta"] = account_meta

            proxies = conn.execute("SELECT name, proxy_url, group_size FROM proxies ORDER BY rowid").fetchall()
            result["routing_config"]["proxies"] = [
                {"id": f"proxy-{i + 1}", "name": p[0], "proxy_url": p[1]} for i, p in enumerate(proxies)
            ]

            users = conn.execute("SELECT seed, plan_type, current_account FROM users ORDER BY rowid").fetchall()
            for seed, plan_type, account in users:
                result["seed_map"][seed] = {"token": account, "plan_type": plan_type, "conversations": []}

            convs = conn.execute("SELECT conv_id, seed, account, title, create_time, update_time FROM conversations").fetchall()
            for conv_id, seed, account, title, create_time, update_time in convs:
                result["conversation_map"][conv_id] = {
                    "id": conv_id, "title": title, "create_time": create_time, "update_time": update_time,
                    "account": account,
                }
                entry = result["seed_map"].get(seed)
                if entry and conv_id not in entry["conversations"]:
                    entry["conversations"].append(conv_id)
    except Exception as e:
        logger.error(f"[store] load_all error: {e}")
    return result
