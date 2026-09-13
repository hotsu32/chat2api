import json
import os

import utils.configs as configs
import utils.store as store
from gateway.identity import decode_account_identity
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
# IP 信誉（IPQS 欺诈分 + ASN）缓存
ANTIBAN_IPREP_FILE = os.path.join(DATA_FOLDER, "antiban_iprep.json")
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
antiban_iprep_cache = {}
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
antiban_iprep_cache = _load_json(ANTIBAN_IPREP_FILE, {})
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

# One-time compatibility convergence: old releases persisted circuit-dead
# markers only in JSON.  SQLite is the canonical routing status now; preserve
# manual ``disabled`` decisions while importing those legacy restrictions.
for _dead_token in list(antiban_dead_tokens):
    _account = store.get_account(_dead_token)
    if _account and _account.get("status") != "disabled":
        store.set_account_status(_dead_token, "dead")


# ---------------------------------------------------------------------------
# Write-through persist helpers. The in-memory structures above remain the
# working cache; these push changes into SQLite (the durable truth).
# ---------------------------------------------------------------------------

# --- users 行的所有权：撤销授权不是删除账号 -----------------------------------
# ``users`` 表被两类所有者共用，``status`` 就是它们的所有权类型：
#
#   - 运营者 / 导入授权（``OPERATOR_SEED_STATUS``）：唯一写入点是 AUTHORIZATION
#     鉴权的 ``POST /seedtoken``。这一类行归 ``seed_map`` 所有 —— 它不在内存里，
#     就是运营者撤掉了它；
#   - 注册用户（trial / active / frozen）：所有者是 ``user_auth`` + 权益，
#     ``seed_map`` 只是它当前绑定的一份缓存快照。它不在内存里，只说明这次快照
#     没带上它（重启窗口、部分加载、clear 之后的空表）。
#
# 判据只认持久化的所有权标记，不认「内存里有没有」：内存是缓存，缓存里的绑定
# 既不是权限，也不是删除授权。


def _operator_seed_status() -> str:
    """运营者所有权标记的唯一定义在 ``chatgpt.authorization``。

    不能在模块级导入：那个模块在模块级 ``import utils.globals``，反向依赖会成环
    （``gateway.share`` 对 ``gateway.backend`` 用的是同一个办法）。延迟导入发生在
    真正需要判所有权时，届时授权层必然已经加载完毕。
    """
    from chatgpt.authorization import OPERATOR_SEED_STATUS
    return OPERATOR_SEED_STATUS


def _operator_owned_seeds() -> list:
    """``users`` 里带运营者所有权标记的名字。

    读失败被 ``store.list_users`` 折成空表；这里的结果只用于**撤销**，空表 = 一行
    都不撤销，方向是安全的那一侧。
    """
    status = _operator_seed_status()
    return [u["seed"] for u in store.list_users() if (u.get("status") or "") == status]


def _revoke_operator_binding(seed) -> None:
    """撤销一个运营者自有的绑定（单事务，该 seed 的会话一并撤销）。"""
    try:
        store.delete_operator_grant(seed, _operator_seed_status())
    except store.StoreError:
        # 撤销是旁路：失败只会留下一行过期授权，不会误删任何东西。
        logger.error("[globals] operator grant prune unavailable")


def revoke_all_operator_grants() -> int:
    """原子吊销全部运营者/导入授权，并把账户域缓存重装成 durable 状态。

    显式的一次事务调用（``store.delete_operator_grants``），失败即抛
    ``StoreError`` 交给调用方回 503 —— 不靠「内存里没有 = 该删」的副作用推导。
    注册用户的 users 行、会话历史与权益不在撤销范围内。
    """
    removed = store.delete_operator_grants(_operator_seed_status())
    reload_account_cache()
    return removed


def reload_account_cache() -> None:
    """从 SQLite 重装账户域缓存：重启走的就是这条路径。

    撤销之后调用。内存是缓存、库里的幸存者才是真相，所以重装到「刚重启」的样子，
    而不是手工猜哪些条目该丢 —— 猜错一次，注册用户就认不出自己的会话了
    （归属检查读的是 ``seed_map``，见 ``gateway/chatgpt.py``）。读失败时
    ``store.load_all`` 返回空结构，退化为一次缓存丢失，durable 状态不受影响。
    """
    loaded = store.load_all()
    seed_map.clear()
    seed_map.update(loaded["seed_map"])
    conversation_map.clear()
    conversation_map.update(loaded["conversation_map"])


def persist_token_list():
    """Sync credential membership without deleting durable account records.

    Deletion is an explicit administrative operation.  A partial in-memory
    snapshot, process restart or bulk-clear request must never erase health,
    tier or audit-relevant account state from SQLite.
    """
    err = set(error_token_list)
    for t in token_list:
        store.sync_account_presence(t, errored=t in err)
    for t in err:
        if t not in token_list:
            store.sync_account_presence(t, errored=True)


def persist_error_tokens():
    """Persist error signals; clearing errors alone does not prove recovery."""
    err = set(error_token_list)
    for t in token_list:
        store.sync_account_presence(t, errored=t in err)
    for t in err:
        if t not in token_list:
            store.sync_account_presence(t, errored=True)


def clear_error_token(token):
    """Drop one token's error-list membership after a probe-verified recovery.

    Clearing the list is **not** evidence of recovery: the caller must already have
    published a healthy status through the guarded probe path (successful
    authenticated probe + dwell + the conditional status UPDATE). This only stops the
    in-memory list from contradicting that decision — routing reads the list, so a
    stale membership would make the recovery cosmetic.

    The in-memory list is derived state: a restart rebuilds it from ``accounts.status``
    via ``store.load_all``, so this cannot leave a durable contradiction behind.
    """
    try:
        error_token_list.remove(token)
    except ValueError:
        pass


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
    """Sync operator-owned bindings; prune only the rows this map owns.

    ``seed_map`` 拥有的是**运营者/导入授权**，不是注册用户的账号行。注册用户的
    users 行归 ``user_auth`` + 订单所有，缺一条内存绑定说明不了任何事 —— 重启窗口、
    部分加载、clear 之后的空表都会让它缺席。旧实现把「不在 seed_map」一律当作
    「该删」，于是运营者按一次 clear 就删光全部注册用户，并级联删掉他们的会话历史。

    判据是持久化的所有权标记（见本模块顶部「users 行的所有权」），不是内存快照。
    """
    owned = set()
    for seed, entry in seed_map.items():
        if isinstance(entry, dict):
            owned.add(seed)
            store.upsert_user(
                seed,
                current_account=entry.get("token", ""),
                plan_type=entry.get("plan_type"),
            )
    for seed in _operator_owned_seeds():
        if seed not in owned:
            _revoke_operator_binding(seed)


def persist_seed(seed):
    """Persist one Seed binding without rewriting unrelated users."""
    entry = seed_map.get(seed)
    if not isinstance(entry, dict):
        return
    store.upsert_user(
        seed,
        current_account=entry.get("token", ""),
        plan_type=entry.get("plan_type"),
        status=entry.get("status"),
    )


def persist_conversation(seed, conv_id):
    """Sync a single conversation (targeted; used by reverseProxy.save_conversation)."""
    c = conversation_map.get(conv_id, {}) or {}
    entry = seed_map.get(seed)
    # 会话历史跟号走：以会话自身记录的 account 为准，回退到 seed 当前账号
    account = c.get("account") or (entry.get("token", "") if isinstance(entry, dict) else None)
    store.upsert_conversation(
        conv_id, seed, account, c.get("title"), c.get("create_time"), c.get("update_time")
    )


def persist_conversation_map():
    """Rebuild the conversations of the seeds this map owns.

    旧实现是一次整表重建（``replace_conversations`` 先 ``DELETE FROM
    conversations`` 再整表重插）：``seed_map`` 为空或部分加载时，那等于删光
    **所有**会话，注册用户的对话历史一起没了。会话归属与 users 行同源，因此这里
    只补写自己名下的行，也只清理自己名下已经不在列表里的行。
    """
    written = {}
    for seed, entry in seed_map.items():
        if not isinstance(entry, dict):
            continue
        account = entry.get("token", "")
        conv_ids = set()
        for conv_id in entry.get("conversations", []):
            c = conversation_map.get(conv_id, {}) or {}
            # 每个会话保留自己创建时的账号，切号后不被当前账号覆盖
            conv_account = c.get("account") or account
            store.upsert_conversation(conv_id, seed, conv_account, c.get("title"),
                                      c.get("create_time"), c.get("update_time"))
            conv_ids.add(conv_id)
        written[seed] = conv_ids
    # 只对自己名下的会话做收敛；别人的（注册用户、历史遗留）一行都不碰。
    for row in store.all_conversations():
        seed = row.get("seed")
        if seed in written and row.get("conv_id") not in written[seed]:
            store.delete_conversation(row.get("conv_id"))


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


# --- tier identity (等级号池) --------------------------------------------------
# Tokens whose plan_type/real_email/nickname have already been synced to accounts.
# Guard avoids a decode + SQLite write on every request for Refresh/Session tokens.
_plan_synced = set()


def sync_account_plan(token, access_token=None):
    """Decode a token's tier identity and write it to ``accounts`` (idempotent).

    - AccessToken (``eyJ`` / ``fk-``): decoded directly from the token itself.
    - Refresh/Session: decoded from a cached access_token in ``refresh_map`` or from
      the freshly exchanged ``access_token`` passed in by ``verify_token``.

    If no access token is available yet, does nothing; the next exchange retries.
    """
    if not token or token in _plan_synced:
        return
    ac = access_token
    if not ac:
        if token.startswith("eyJhbGciOi") or token.startswith("fk-"):
            ac = token
        else:
            ac = (refresh_map.get(token, {}) or {}).get("token", "")
    if not ac:
        return
    identity = decode_account_identity(ac)
    if not identity:
        return
    _plan_synced.add(token)
    store.upsert_account(
        token,
        plan_type=identity["plan_type"],
        real_email=identity.get("real_email") or None,
        nickname=identity.get("nickname") or None,
    )


# Eagerly tier AccessTokens (and Refresh/Session tokens with a cached access_token)
# so the pool is classified before first use; the rest are lazily synced on exchange.
for _t in token_list:
    sync_account_plan(_t)
