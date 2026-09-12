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
from contextlib import closing
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

                    CREATE TABLE IF NOT EXISTS trial_grants (
                        email      TEXT PRIMARY KEY,
                        seed       TEXT,
                        tier       TEXT NOT NULL,
                        total      INTEGER NOT NULL,
                        used       INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER,
                        updated_at INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS trial_reservations (
                        res_id      TEXT PRIMARY KEY,
                        email       TEXT NOT NULL,
                        seed        TEXT,
                        status      TEXT NOT NULL DEFAULT 'reserved',
                        instance_id TEXT,
                        created_at  INTEGER,
                        updated_at  INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_trial_res_email
                        ON trial_reservations(email, status);

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
    # 预留归属进程实例 id（格式 "<pid>:<uuid>"），用于孤儿预留回收。
    # 旧库升级时补空列；旧行 NULL 表示来源未知，回收时不触碰（保守处理）。
    ("trial_reservations", "instance_id", "TEXT"),
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


# Outcome codes for bind_payment_transaction. Callers must switch on these instead of
# collapsing them into a bool: "already bound to this order" (idempotent, let it
# through) and "bound to another order" (replay, refuse) are opposite decisions.
PAYMENT_TXN_BOUND = "bound"
PAYMENT_TXN_IDEMPOTENT = "idempotent"
PAYMENT_TXN_CONFLICT = "conflict"


def bind_payment_transaction(key: str, order_id: str) -> str:
    """Atomically claim ``key`` (a provider transaction id) for ``order_id``.

    Returns :data:`PAYMENT_TXN_BOUND` (new claim), :data:`PAYMENT_TXN_IDEMPOTENT`
    (this order already owns it) or :data:`PAYMENT_TXN_CONFLICT` (another order owns
    it). Raises :class:`StoreError` when the read, the write or the transaction fails.

    The read and the write must be one transaction. ``get_meta`` -> ``set_meta`` is two,
    so two concurrent callbacks carrying the same provider transaction id can both read
    "unbound" and both claim it, settling one payment against two orders. BEGIN IMMEDIATE
    (plus ``_WRITE_LOCK`` for in-process callers) makes the loser observe the winner's
    row instead of a stale empty one.

    Unlike ``get_meta``/``set_meta`` this never swallows a failure: "the binding store is
    unreadable" is a different fact from "no binding", and collapsing the two is what
    lets a database hiccup fail open into settlement.
    """
    if not key or not order_id:
        raise StoreError("payment transaction binding requires a key and an order id")
    try:
        with _WRITE_LOCK, closing(_connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, order_id))
                    conn.execute("COMMIT")
                    return PAYMENT_TXN_BOUND
                # A row that exists but does not hold this exact order id owns the
                # transaction — including a blank legacy value: refuse rather than guess.
                matched = row[0] == order_id
                conn.execute("ROLLBACK")
                return PAYMENT_TXN_IDEMPOTENT if matched else PAYMENT_TXN_CONFLICT
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except Exception as exc:
        logger.error("[store] payment transaction binding unavailable")
        raise StoreError("payment transaction binding unavailable") from exc


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


def sync_account_presence(token: str, *, errored: bool) -> None:
    """Import credentials without treating inventory presence as health evidence.

    Error signals can restrict a healthy row; clearing an error list requires a
    separate successful health probe before an unhealthy row becomes routable.
    """
    status = "unhealthy" if errored else "healthy"
    now = int(time.time())
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute(
                "INSERT INTO accounts (token, status, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at "
                "WHERE accounts.status='healthy' AND excluded.status='unhealthy'",
                (token, status, now),
            )
    except Exception as exc:
        raise StoreError("account inventory persistence unavailable") from exc


def apply_health_probe(token: str, expected_status: str, status: str, checked_at: int) -> bool:
    """Apply probe evidence only to an unchanged, probe-managed account.

    A probe never inserts accounts or clears manual/circuit restrictions. False
    means its snapshot is stale; persistence failures remain explicit.
    """
    if expected_status not in {"healthy", "degraded", "unhealthy", "dead"} \
            or status not in {"healthy", "unhealthy"}:
        return False
    try:
        with _WRITE_LOCK, _connect() as conn:
            result = conn.execute(
                "UPDATE accounts SET status=?, last_health_check=?, updated_at=? "
                "WHERE token=? AND status=? AND COALESCE(last_health_check, 0)<=?",
                (status, checked_at, int(time.time()), token, expected_status, checked_at),
            )
            return result.rowcount == 1
    except Exception as exc:
        raise StoreError("health state persistence unavailable") from exc


def set_account_status(token: str, status: str) -> bool:
    """Set an existing account's canonical routing status.

    This never creates an account: circuit signals must not turn arbitrary
    caller input into durable pool inventory.
    """
    if status not in {"healthy", "degraded", "unhealthy", "dead", "disabled"}:
        raise ValueError("invalid account status")
    try:
        with _WRITE_LOCK, _connect() as conn:
            result = conn.execute(
                "UPDATE accounts SET status=?, updated_at=? WHERE token=?",
                (status, int(time.time()), token),
            )
            return result.rowcount == 1
    except Exception as exc:
        raise StoreError("account status persistence unavailable") from exc


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


def _revoke_grants(conn, status: str, seed: Optional[str] = None) -> int:
    """Delete grant rows (and their conversations) inside one open transaction."""
    if seed is None:
        rows = conn.execute(
            "SELECT seed FROM users WHERE status=?", (status,)
        ).fetchall()
        seeds = [r[0] for r in rows]
    else:
        # The status guard stays: this revokes a *grant*, it does not delete an
        # arbitrary identity row that happens to share the name.
        seeds = [r[0] for r in conn.execute(
            "SELECT seed FROM users WHERE seed=? AND status=?", (seed, status)
        ).fetchall()]
    if seeds:
        marks = ",".join("?" for _ in seeds)
        conn.execute(f"DELETE FROM conversations WHERE seed IN ({marks})", seeds)
        conn.execute(f"DELETE FROM users WHERE seed IN ({marks})", seeds)
    return len(seeds)


def _revoke_grants_transactionally(status: str, seed: Optional[str]) -> int:
    """Run :func:`_revoke_grants` under one transaction; fail closed on any error."""
    try:
        with _WRITE_LOCK, closing(_connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                removed = _revoke_grants(conn, status, seed)
                conn.execute("COMMIT")
                return removed
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except sqlite3.Error as e:
        logger.error(f"[store] revoke grants error: {e}")
        raise StoreError("operator grant revocation failed") from None


def delete_operator_grants(status: str) -> int:
    """Revoke **every** grant carrying ``status``; returns the number removed.

    This is the revoke-all primitive behind ``DELETE /seedtoken`` with
    ``seed="clear"``. It is deliberately self-contained: it selects the rows and
    deletes them in one ``BEGIN IMMEDIATE`` transaction instead of delegating to a
    ``list_users`` sweep. That sweep could not tell "no grant left" from "the read
    failed" (``list_users`` folds a storage error into an empty list), so a single
    failed read silently turned "revoke everything" into "revoke nothing" while the
    caller answered success — and the grants, not the bindings, are what authorize
    the paid pool.

    Failures raise :class:`StoreError` instead: the caller must be able to report a
    failed revocation rather than a completed one.

    ``status`` is passed in by the caller (``chatgpt.authorization`` owns the
    constant) because this module must not import the authorization layer.
    """
    return _revoke_grants_transactionally(status, None)


def delete_operator_grant(seed: str, status: str) -> bool:
    """Revoke one named grant; ``True`` when a grant row was actually removed.

    A durable grant can outlive its in-memory ``seed_map`` binding (a failed
    persist, a restart, a direct DB edit), so revocation is keyed by name against
    SQLite and does not require the binding to still exist. Returns ``False`` when
    there was no such grant — that is the caller's "not found".

    Raises :class:`StoreError` on a storage failure, like
    :func:`delete_operator_grants`.
    """
    if not seed:
        return False
    return _revoke_grants_transactionally(status, seed) > 0


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


def get_user_auth(email: str, strict: bool = False) -> Optional[Dict[str, Any]]:
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
    except Exception:
        logger.error('[store] user_auth_lookup_unavailable')
        if strict:
            raise StoreError('User auth lookup unavailable') from None
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


def settle_order(order_id: str, now: Optional[int] = None) -> bool:
    """Atomically stack same-owner/tier paid time and settle a pending order.

    BEGIN IMMEDIATE protects independent connections/processes as well as local
    callers. This does not verify payment: only a verified provider or explicit
    local mock settlement may call it. Failures raise; a retry never adds time
    to an already paid order.
    """
    from utils import plans
    from utils.entitlements import _order_window
    now = int(time.time()) if now is None else int(now)
    try:
        with _WRITE_LOCK, closing(_connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute(f'SELECT {_ORDER_COLS} FROM orders WHERE order_id=?', (order_id,)).fetchone()
                order = _order_row_to_dict(row) if row else None
                detail = plans.plan_detail(order['tier_id']) if order else None
                if not order or order['status'] != 'pending' or not detail or not order['email']:
                    conn.execute('ROLLBACK')
                    return False
                base = now
                for paid in conn.execute(
                    f"SELECT {_ORDER_COLS} FROM orders WHERE email=? AND status='paid'",
                    (order['email'],),
                ).fetchall():
                    window = _order_window(_order_row_to_dict(paid))
                    if window and window[0] == detail['tier']:
                        base = max(base, window[2])
                conn.execute(
                    "UPDATE orders SET status='paid', expires_at=?, updated_at=? WHERE order_id=? AND status='pending'",
                    (base + detail['days'] * 86400, now, order_id),
                )
                conn.execute('COMMIT')
                return True
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
    except Exception as exc:
        raise StoreError('Order settlement unavailable') from exc


def get_order(order_id: str, strict: bool = False) -> Optional[Dict[str, Any]]:
    """Read an order; payment paths use strict=True to distinguish DB failure."""
    try:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT {_ORDER_COLS} FROM orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
            return _order_row_to_dict(row) if row else None
    except Exception:
        logger.error('[store] order_lookup_unavailable')
        if strict:
            raise StoreError('Order lookup unavailable') from None
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


# ------------------------------------------------------------------- trials
# Plus 免费试用的两张表。余额是钱，不是统计量，所以状态流转全部落库：
#
#   trial_grants        每个 email 一行，``total`` 发了多少次、``used`` 结算掉多少次。
#   trial_reservations  每次生成一行，``reserved`` -> ``settled`` / ``released``。
#
# 余额 = total - used - 未终结的 reserved 数。预留先占额度，成功才计 used，失败退回。
# 不做内存计数：进程重启后余额必须原样，多线程下也不能靠 GIL 侥幸。

_TRIAL_GRANT_COLS = "email, seed, tier, total, used, created_at, updated_at"


def _trial_grant_row_to_dict(r: tuple) -> Dict[str, Any]:
    return {
        "email": r[0], "seed": r[1], "tier": r[2], "total": int(r[3] or 0),
        "used": int(r[4] or 0), "created_at": r[5], "updated_at": r[6],
    }


def create_trial_grant(email: str, seed: str, tier: str, total: int) -> bool:
    """发一份试用额度。已经发过则不覆盖，返回 False（授予幂等）。

    用 ``INSERT ... ON CONFLICT DO NOTHING`` 而不是「先查再插」：注册重试和并发
    重复提交都会走到这里，读-改-写会让同一个人拿到两份额度。
    """
    if not email or total <= 0:
        return False
    now = int(time.time())
    try:
        with _WRITE_LOCK, _connect() as conn:
            cur = conn.execute(
                "INSERT INTO trial_grants (email, seed, tier, total, used, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?) ON CONFLICT(email) DO NOTHING",
                (email, seed, tier, int(total), now, now),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error("[store] create_trial_grant error")
        raise StoreError("create_trial_grant failed") from e


def create_user_with_trial(
    email: str,
    *,
    password_hash: str,
    seed: str,
    status: str,
    trial_tier: str,
    trial_total: int,
) -> None:
    """注册原子 DAO：建 user_auth 行 + 发试用额度，同一事务。

    两步任一失败则整笔回滚 —— 不会出现「注册成功但没有额度」的僵尸账号，
    也不会出现「有额度但没有账号」的游离授权。

    ``user_auth`` 行以 ``INSERT OR FAIL`` 写入（主键冲突即失败）；
    ``trial_grants`` 行用与 :func:`create_trial_grant` 相同的 ``ON CONFLICT DO NOTHING``
    保留授予幂等语义，但此处赠额失败（即已存在同 email 的 grant）也视为整笔失败：
    同一 email 不能拿两份试用额度，重复注册应被拒绝在外层。

    失败时抛 :class:`StoreError`。
    """
    if not email or not seed:
        raise StoreError("create_user_with_trial: email and seed are required")
    now = int(time.time())
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO user_auth (email, password_hash, seed, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (email, password_hash, seed, status, now, now),
                )
                cur = conn.execute(
                    "INSERT INTO trial_grants (email, seed, tier, total, used, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?) ON CONFLICT(email) DO NOTHING",
                    (email, seed, trial_tier, int(trial_total), now, now),
                )
                if cur.rowcount == 0:
                    # grant conflict = duplicate email grant; roll back the user row too
                    conn.execute("ROLLBACK")
                    raise StoreError("create_user_with_trial: trial grant conflict for email")
                conn.execute(
                    "INSERT INTO users (seed, plan_type, current_account, status, created_at, updated_at) "
                    "VALUES (?, ?, '', 'trial', ?, ?)",
                    (seed, trial_tier, now, now),
                )
                conn.execute("COMMIT")
            except StoreError:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except StoreError:
        raise
    except Exception as e:
        logger.error("[store] create_user_with_trial error")
        raise StoreError("create_user_with_trial failed") from e


def get_trial_grant(email: str, strict: bool = False) -> Optional[Dict[str, Any]]:
    """该 email 的试用额度行；从未发过返回 None。

    ``strict=True`` 时查询失败抛 :class:`StoreError` —— 权益判定分不清「没发过」和
    「查不到」，把后者当成前者等于一次锁库就让所有试用用户被拒。
    """
    if not email:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT {_TRIAL_GRANT_COLS} FROM trial_grants WHERE email=?", (email,)
            ).fetchone()
            return _trial_grant_row_to_dict(row) if row else None
    except Exception as e:
        logger.error("[store] get_trial_grant error")
        if strict:
            raise StoreError("get_trial_grant failed") from e
        return None


def count_open_trial_reservations(email: str, strict: bool = False) -> int:
    """尚未终结（``reserved``）的预留数 —— 已占住但还没计入 used 的那部分。"""
    if not email:
        return 0
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trial_reservations WHERE email=? AND status='reserved'",
                (email,),
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.error("[store] count_open_trial_reservations error")
        if strict:
            raise StoreError("count_open_trial_reservations failed") from e
        return 0


def reserve_trial(res_id: str, email: str, seed: str, limit: int,
                  instance_id: str = "") -> bool:
    """原子占一次试用额度：额度够且账号仍有效才插预留行。

    整个「校验账号 + 算余额 + 插行」在一条 SQL 的单事务里完成（INSERT ... SELECT ... WHERE），
    并且持 _WRITE_LOCK。两件事必须在同一条语句里做：
    1. 检查 user_auth 行的 seed / status 仍与调用方读到的一致（防止预读后账号被吊销或 seed 被轮换）；
    2. 检查余额 >= limit（防止并发四次都通过）。
    分成两步写就会有竞态；此处用 subquery 把两个条件折进 WHERE，保证原子性。

    instance_id 标记本次预留的归属进程（格式 "<pid>:<uuid>"），用于进程崩溃后的孤儿回收。
    空字符串表示调用方未传（兼容旧调用），存为 NULL，回收时不触碰（保守处理）。
    """
    if not res_id or not email:
        return False
    now = int(time.time())
    iid = instance_id if instance_id else None
    try:
        with _WRITE_LOCK, _connect() as conn:
            cur = conn.execute(
                "INSERT INTO trial_reservations "
                "  (res_id, email, seed, status, instance_id, created_at, updated_at) "
                "SELECT ?, ?, ?, 'reserved', ?, ?, ? WHERE ("
                # 余额条件：total - used - open_reserved >= limit
                "  SELECT g.total - g.used - ("
                "    SELECT COUNT(*) FROM trial_reservations r"
                "     WHERE r.email = g.email AND r.status = 'reserved'"
                "  ) FROM trial_grants g WHERE g.email = ? AND g.seed = ? AND g.tier = 'plus'"
                ") >= ? "
                # 账号有效性二次校验：seed 和 status 必须与调用方读到的一致，
                # 防止 reserve() 读完 user_auth 到这里之间账号被吊销或 seed 被轮换。
                "AND EXISTS ("
                "  SELECT 1 FROM user_auth u"
                "   WHERE u.email = ? AND u.seed = ? AND u.status = 'active'"
                ")",
                (res_id, email, seed, iid, now, now, email, seed, int(limit), email, seed),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error("[store] reserve_trial error")
        raise StoreError("reserve_trial failed") from e


def get_trial_reservation(res_id: str) -> Optional[Dict[str, Any]]:
    if not res_id:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT res_id, email, seed, status, created_at, updated_at "
                "FROM trial_reservations WHERE res_id=?",
                (res_id,),
            ).fetchone()
            if not row:
                return None
            return {"res_id": row[0], "email": row[1], "seed": row[2], "status": row[3],
                    "created_at": row[4], "updated_at": row[5]}
    except Exception as e:
        logger.error("[store] get_trial_reservation error")
        return None


def settle_trial_reservation(res_id: str, seed: str) -> bool:
    """预留 → 已消费：置 ``settled`` 并把 ``used`` +1，两步在同一事务里。

    返回是否真的发生了流转。重复终态（回调重投 / 终止事件投递两次）第二次返回
    False 且不再扣 —— ``WHERE status='reserved'`` 保证只有一次 UPDATE 命中。

    ``seed`` 必填且在 SQL 里校验归属：预留 id 是服务端生成的，但它会随请求上下文流转，
    拿到一个 id 就能结算别人的额度不是可接受的边界。
    """
    if not res_id or not seed:
        return False
    now = int(time.time())
    try:
        with _WRITE_LOCK, _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE trial_reservations SET status='settled', updated_at=? "
                    "WHERE res_id=? AND status='reserved' AND seed=? "
                    "AND email IN (SELECT email FROM user_auth WHERE seed=?)",
                    [now, res_id, seed, seed],
                )
                if cur.rowcount == 0:
                    conn.execute("ROLLBACK")
                    return False
                row = conn.execute(
                    "SELECT email FROM trial_reservations WHERE res_id=?", (res_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE trial_grants SET used = used + 1, updated_at = ? WHERE email = ?",
                    (now, row[0] if row else ""),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception as e:
        logger.error("[store] settle_trial_reservation error")
        raise StoreError("settle_trial_reservation failed") from e


def release_trial_reservation(res_id: str, seed: str) -> bool:
    """预留 → 已释放（生成失败 / 断连 / 非 2xx），额度退回余额。

    只对仍是 ``reserved`` 的行生效：已结算的不退款，避免一次成功被迟到的错误路径
    抹掉；已释放的重复调用返回 False，不产生第二次退款。

    ``seed`` 必填且在 SQL 里校验归属，防止跨账号释放。
    """
    if not res_id or not seed:
        return False
    try:
        with _WRITE_LOCK, _connect() as conn:
            cur = conn.execute(
                "UPDATE trial_reservations SET status='released', updated_at=? "
                "WHERE res_id=? AND status='reserved' AND seed=? "
                "AND email IN (SELECT email FROM user_auth WHERE seed=?)",
                [int(time.time()), res_id, seed, seed],
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error("[store] release_trial_reservation error")
        raise StoreError("release_trial_reservation failed") from e


def get_orphan_instance_ids(current_instance_id: str) -> List[str]:
    """返回所有「外来」reserved 预留行的 instance_id 去重列表。

    用于孤儿回收：调用方拿到这个列表后逐一检查进程存活性，再把确认已死的 id
    传给 :func:`release_reservations_by_instance_ids` 批量释放。

    只返回 ``instance_id IS NOT NULL AND instance_id != current`` 的行；
    ``NULL`` 行（旧代码写入、未迁移）不返回，调用方不触碰它们。
    """
    if not current_instance_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT instance_id FROM trial_reservations "
                "WHERE status='reserved' AND instance_id IS NOT NULL "
                "AND instance_id != ?",
                (current_instance_id,),
            ).fetchall()
            return [r[0] for r in rows]
    except Exception as e:
        logger.error("[store] get_orphan_instance_ids error")
        raise StoreError("get_orphan_instance_ids failed") from e


def release_reservations_by_instance_ids(dead_instance_ids: List[str]) -> int:
    """将归属已死进程实例的 ``reserved`` 预留行批量释放，返回释放数量。

    调用方（``trials.recover_orphan_reservations``）负责通过 PID 存活检查确认这些
    instance_id 对应的进程已死；本函数只做 SQL 更新，不做任何存活判断。
    空列表直接返回 0，不执行 SQL。
    """
    if not dead_instance_ids:
        return 0
    now = int(time.time())
    placeholders = ",".join("?" * len(dead_instance_ids))
    try:
        with _WRITE_LOCK, _connect() as conn:
            cur = conn.execute(
                f"UPDATE trial_reservations SET status='released', updated_at=? "
                f"WHERE status='reserved' AND instance_id IN ({placeholders})",
                [now, *dead_instance_ids],
            )
            return cur.rowcount
    except Exception as e:
        logger.error("[store] release_reservations_by_instance_ids error")
        raise StoreError("release_reservations_by_instance_ids failed") from e


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

            users = conn.execute("SELECT seed, plan_type, current_account, status FROM users ORDER BY rowid").fetchall()
            for seed, plan_type, account, status in users:
                result["seed_map"][seed] = {
                    "token": account, "plan_type": plan_type, "status": status, "conversations": []
                }

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
