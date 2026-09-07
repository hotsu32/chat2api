import json
import os

import utils.configs as configs
import utils.store as store
from utils.Logger import logger

DATA_FOLDER = "data"
TOKENS_FILE = os.path.join(DATA_FOLDER, "token.txt")
REFRESH_MAP_FILE = os.path.join(DATA_FOLDER, "refresh_map.json")
ERROR_TOKENS_FILE = os.path.join(DATA_FOLDER, "error_token.txt")
WSS_MAP_FILE = os.path.join(DATA_FOLDER, "wss_map.json")
FP_FILE = os.path.join(DATA_FOLDER, "fp_map.json")
ROUTING_CONFIG_FILE = os.path.join(DATA_FOLDER, "routing_config.json")
SEED_MAP_FILE = os.path.join(DATA_FOLDER, "seed_map.json")
CONVERSATION_MAP_FILE = os.path.join(DATA_FOLDER, "conversation_map.json")
# Antiban 持久化文件（PR-1 骨架）
ANTIBAN_BUCKET_FILE = os.path.join(DATA_FOLDER, "antiban_bucket.json")
ANTIBAN_GEO_FILE = os.path.join(DATA_FOLDER, "antiban_geo.json")
ANTIBAN_DEAD_FILE = os.path.join(DATA_FOLDER, "antiban_dead.json")
# 账号风险嗅探：仅记录命中的软警告，不立即标 dead（Step A：观察期，校准关键词）
ACCOUNT_WARNINGS_FILE = os.path.join(DATA_FOLDER, "account_warnings.json")
# Harvester 账号元数据（不含密码，仅 email+note+proxy_name+采集历史）
HARVESTER_ACCOUNTS_FILE = os.path.join(DATA_FOLDER, "harvester_accounts.json")

count = 0
token_list = []
error_token_list = []
refresh_map = {}
wss_map = {}
fp_map = {}
routing_config = {}
seed_map = {}
conversation_map = {}
# Antiban 内存状态（PR-1 骨架，后续 PR 填充）
antiban_bucket = {"buckets": {}, "account_index": {}}
antiban_geo_cache = {}
antiban_dead_tokens = {}
# 账号风险嗅探：token -> [{hit_at, snippet, pattern, conversation_id}, ...]
account_warnings = {}
impersonate_list = [
    "chrome119",
    "chrome120",
    "chrome123",
] if not configs.impersonate_list else configs.impersonate_list

if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _load_lines(path):
    result = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip() and not line.startswith("#"):
                        result.append(line.strip())
        except Exception:
            pass
    return result


# --- orthogonal subsystems: JSON (unchanged, NOT part of the account domain) ---
wss_map = _load_json(WSS_MAP_FILE, {})
antiban_bucket = _load_json(ANTIBAN_BUCKET_FILE, {"buckets": {}, "account_index": {}})
antiban_bucket.setdefault("buckets", {})
antiban_bucket.setdefault("account_index", {})
antiban_geo_cache = _load_json(ANTIBAN_GEO_FILE, {})
antiban_dead_tokens = _load_json(ANTIBAN_DEAD_FILE, {})
account_warnings = _load_json(ACCOUNT_WARNINGS_FILE, {})

# --- account domain: SQLite-backed (single source of truth) ---
store.init_db()
if store.is_migrated():
    _loaded = store.load_all()
    token_list = _loaded["token_list"]
    error_token_list = _loaded["error_token_list"]
    refresh_map = _loaded["refresh_map"]
    fp_map = _loaded["fp_map"]
    routing_config = _loaded["routing_config"]
    seed_map = _loaded["seed_map"]
    conversation_map = _loaded["conversation_map"]
else:
    # First boot: JSON files are the one-shot migration source.
    refresh_map = _load_json(REFRESH_MAP_FILE, {})
    fp_map = _load_json(FP_FILE, {})
    routing_config = _load_json(ROUTING_CONFIG_FILE, {})
    seed_map = _load_json(SEED_MAP_FILE, {})
    conversation_map = _load_json(CONVERSATION_MAP_FILE, {})
    token_list = _load_lines(TOKENS_FILE)
    error_token_list = _load_lines(ERROR_TOKENS_FILE)
    store.migrate(token_list, error_token_list, seed_map, conversation_map,
                  refresh_map, fp_map, routing_config)

if token_list:
    logger.info(f"Token list count: {len(token_list)}, Error token list count: {len(error_token_list)}")
    logger.info("-" * 60)


# ---------------------------------------------------------------------------
# Write-through persist helpers. The in-memory structures above remain the
# working cache; these push changes into SQLite (the durable truth).
# ---------------------------------------------------------------------------

def persist_token_list():
    """Authoritative sync of accounts.token + status from token_list/error_token_list."""
    err = set(error_token_list)
    live = set(token_list) | err
    for t in token_list:
        store.upsert_account(t, status="unhealthy" if t in err else "healthy")
    for t in err:
        if t not in token_list:
            store.upsert_account(t, status="unhealthy")
    for a in store.list_accounts():
        if a["token"] not in live:
            store.delete_account(a["token"])


def persist_error_tokens():
    """Authoritative status sync from error_token_list (mark error tokens unhealthy,
    recovered tokens healthy)."""
    err = set(error_token_list)
    for t in token_list:
        store.upsert_account(t, status="unhealthy" if t in err else "healthy")
    for t in err:
        if t not in token_list:
            store.upsert_account(t, status="unhealthy")


def persist_refresh_map():
    """Sync refresh_info JSON blobs from refresh_map."""
    for t, meta in refresh_map.items():
        store.upsert_account(t, refresh_info=json.dumps(meta, ensure_ascii=False))


def persist_fp_map():
    """Sync fingerprint JSON blobs from fp_map (full; cold path)."""
    for t, fp in fp_map.items():
        store.upsert_account(
            t,
            fingerprint=json.dumps(fp, ensure_ascii=False),
            impersonate=fp.get("impersonate"),
            user_agent=fp.get("user-agent"),
            proxy_url=fp.get("proxy_url"),
        )


def persist_fp_token(token):
    """Sync a single token's fingerprint (hot path: fp.py per-request)."""
    fp = fp_map.get(token)
    if fp is None:
        return
    store.upsert_account(
        token,
        fingerprint=json.dumps(fp, ensure_ascii=False),
        impersonate=fp.get("impersonate"),
        user_agent=fp.get("user-agent"),
        proxy_url=fp.get("proxy_url"),
    )


def persist_seed_map():
    """Sync users (seed -> current_account); delete users no longer in seed_map."""
    known = set()
    for seed, entry in seed_map.items():
        if isinstance(entry, dict):
            known.add(seed)
            store.upsert_user(seed, current_account=entry.get("token", ""))
    for u in store.list_users():
        if u["seed"] not in known:
            store.delete_user(u["seed"])


def persist_conversation(seed, conv_id):
    """Sync a single conversation (targeted; used by reverseProxy.save_conversation)."""
    c = conversation_map.get(conv_id, {}) or {}
    entry = seed_map.get(seed)
    account = entry.get("token", "") if isinstance(entry, dict) else None
    store.upsert_conversation(
        conv_id, seed, account, c.get("title"), c.get("create_time"), c.get("update_time")
    )


def persist_conversation_map():
    """Authoritative rebuild of the conversations table from seed_map + conversation_map."""
    rows = []
    for seed, entry in seed_map.items():
        if not isinstance(entry, dict):
            continue
        account = entry.get("token", "")
        for conv_id in entry.get("conversations", []):
            c = conversation_map.get(conv_id, {}) or {}
            rows.append((conv_id, seed, account, c.get("title"), c.get("create_time"), c.get("update_time")))
    store.replace_conversations(rows)


def persist_routing_config():
    """Sync proxies table + accounts proxy/group/note columns from routing_config."""
    cfg = routing_config or {}
    store.save_proxies(cfg.get("proxies", []))
    bindings = cfg.get("bindings", {}) or {}
    account_meta = cfg.get("account_meta", {}) or {}
    for token, binding in bindings.items():
        meta = account_meta.get(token, {}) or {}
        store.upsert_account(
            token,
            proxy_name=binding.get("proxy_name"),
            proxy_url=binding.get("proxy_url"),
            group_name=binding.get("group"),
            note=meta.get("note", binding.get("note", "")),
        )
