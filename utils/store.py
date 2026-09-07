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

DATA_FOLDER = "data"

_DB_PATH = getattr(configs, "fleet_db_path", None) or os.path.join(DATA_FOLDER, "chat2api.db")

_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()

# Allowed columns for dynamic partial upserts (whitelist guards against typos).
ACCOUNT_COLUMNS = {
    "token", "token_type", "plan_type", "real_email", "nickname", "status",
    "proxy_name", "proxy_url", "group_name", "impersonate", "user_agent", "note",
    "refresh_info", "fingerprint", "last_health_check", "created_at", "updated_at",
}


def _detect_token_type(token: str) -> str:
    """Mirror of utils.routing.detect_token_type (kept local to avoid circular import)."""
    if not token:
        return "Unknown"
    if token.startswith("sess-"):
        return "SessionToken"
    if token.startswith("eyJhbGciOi") or token.startswith("fk-"):
        return "AccessToken"
    if token.startswith("rt_") and len(token) >= 60:
        return "RefreshToken"
    if len(token) == 45:
        return "RefreshToken"
    return "CustomToken"


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

                    CREATE TABLE IF NOT EXISTS meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT
                    );
                    """
                )
            _INITIALIZED = True
            logger.info(f"[store] db ready at {_db_path()}")
        except Exception as e:
            logger.error(f"[store] init_db failed: {e}")


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
                        _detect_token_type(token),
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
                    (token, _detect_token_type(token), now, now),
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

            users = conn.execute("SELECT seed, current_account FROM users ORDER BY rowid").fetchall()
            for seed, account in users:
                result["seed_map"][seed] = {"token": account, "conversations": []}

            convs = conn.execute("SELECT conv_id, seed, account, title, create_time, update_time FROM conversations").fetchall()
            for conv_id, seed, account, title, create_time, update_time in convs:
                result["conversation_map"][conv_id] = {
                    "id": conv_id, "title": title, "create_time": create_time, "update_time": update_time,
                }
                entry = result["seed_map"].get(seed)
                if entry and conv_id not in entry["conversations"]:
                    entry["conversations"].append(conv_id)
    except Exception as e:
        logger.error(f"[store] load_all error: {e}")
    return result
