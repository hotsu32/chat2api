import asyncio
import json
import random

from fastapi import HTTPException

import utils.configs as configs
import utils.globals as globals
import utils.store as store
from chatgpt.refreshToken import rt2ac, sess2ac
from utils.Logger import logger
from utils.antiban import circuit as antiban_circuit


def _account_is_usable(token: str) -> bool:
    """只有持久化 healthy 且未被熔断/错误列表拒绝的账号可接新请求。"""
    if not token:
        return False
    if antiban_circuit.is_token_dead(token):
        return False
    if token in globals.error_token_list:
        return False
    acct = store.get_account(token)
    if not acct:
        return False
    return acct.get("status") == "healthy"


def _account_tier(token: str):
    """读取账号的 plan_type 等级（SQLite 真相源）。"""
    if not token:
        return None
    acct = store.get_account(token)
    return (acct or {}).get("plan_type")


# 公开的开发 / 运营种子句柄：名字对外公开、可枚举，因此**名字本身不构成授权**。
# 它们唯一的授权来源是显式的 ``DEV_ACCESS_ENABLED``：
#   - ``frontend-proof-*``：``gateway/landing.py`` 的 /try 验收别名；
#   - 下面两个 demo 句柄：``gateway/demo.py`` 的 /demo 分组 seed，与 /try 同一把闸
#     （见 docs/FLEET_ECOSYSTEM_SPEC.md 的「开发 / 运营入口」）。
# 闸门关闭时这些句柄一律 fail-closed —— 包括早前开发运行遗留在库里的绑定，
# 否则一个公开可猜的名字就是一把万能钥匙。
PUBLIC_SEED_ALIAS_PREFIX = "frontend-proof-"
PUBLIC_SEED_HANDLES = frozenset({"demo-free-pool", "demo-plus-pool"})

# 显式运营者授权的持久标记（写进 ``users.status``）。**只有** AUTHORIZATION 鉴权的
# ``POST /seedtoken`` 导入路径会写它；分配写回、``seed_map.json`` 迁移、开发别名
# 修复都不写。判据是「标记等于此值」，不是「users 行存在」—— 升级前匿名分配
# 落库的行不带标记，必须按未授权处理（见 :func:`_has_durable_seed_grant`）。
OPERATOR_SEED_STATUS = "operator"


def _is_public_seed_handle(seed) -> bool:
    return isinstance(seed, str) and (
        seed.startswith(PUBLIC_SEED_ALIAS_PREFIX) or seed in PUBLIC_SEED_HANDLES
    )


def reject_development_alias(seed: str) -> None:
    """闸门关闭时把公开开发句柄按不存在处理。"""
    if _is_public_seed_handle((seed or "").strip()) and not getattr(
        configs, "dev_access_enabled", False
    ):
        raise HTTPException(status_code=404, detail="Not Found")


def _has_durable_seed_grant(seed: str) -> bool:
    """该 seed 是否持有**显式**的运营者/导入授权（``users.status`` 标记）。

    判据是标记值，不是「行存在」：``users`` 行本身说明不了任何事 ——
    修复前任意匿名 cookie 经 ``_bind_seed_account`` 就写过同样的行，
    ``seed_map.json`` 的一次性迁移也给每个历史 seed 写了行。若按「有行 = 授权」，
    攻击者升级前就已经落库的 seed 会永远保留付费号池的访问权，P0 只关了一半。

    因此只有 ``POST /seedtoken``（``AUTHORIZATION`` 鉴权）这条显式导入路径会写
    该标记，见 :data:`OPERATOR_SEED_STATUS`。绑定写回（``_bind_seed_account`` /
    ``route_seed``）与 ``persist_seed_map`` 都不带 status 字段，
    ``store.upsert_user`` 会跳过 None 字段，标记因此不会被日常分配抹掉。

    升级后的既有部署：运营者用 ``POST /seedtoken`` 重新导入（幂等）即重新授权；
    在此之前这些 seed 一律按未授权处理 —— 不静默续期来路不明的历史行。

    读的是 SQLite 而不是内存 ``seed_map``：内存是缓存，缓存里的一条绑定
    不构成权限。``store.get_user`` 异常时返回 ``None``，即本函数 fail-closed。
    """
    if not isinstance(seed, str) or not seed:
        return False
    row = store.get_user(seed)
    return bool(row) and (row.get("status") or "") == OPERATOR_SEED_STATUS


def _seed_allocation_authorized(seed: str) -> bool:
    """无 ``user_auth`` 行时的第二授权来源：显式运营者 / 导入 / 开发句柄。

    没有 SaaS 身份（注册用户）的时候，**分配车队账号需要服务端已有的授权事实**，
    不能靠「这个字符串没见过」放行。两类显式事实：

      - 持久化的运营者/导入授权（``_has_durable_seed_grant``）；
      - 公开开发句柄（``_is_public_seed_handle``）：只在 ``DEV_ACCESS_ENABLED``
        显式打开时成立。闸门关闭时一律 fail-closed —— 包括早前开发运行遗留在
        库里的「过期别名」，否则一个公开可猜的名字就是一把万能钥匙。

    真实用户与本函数无关：付费权益 / Plus 试用走 ``_seed_plan_types`` 的档位路径。
    """
    if _is_public_seed_handle(seed):
        return bool(getattr(configs, "dev_access_enabled", False))
    return _has_durable_seed_grant(seed)


def _seed_plan_types(seed: str):
    """半专属分池：user.tier → account.plan_type 号组范围。

    返回三态，调用方必须用 ``is None`` 区分，不能用真值判断：
      - ``None``  —— 无 user_auth 行（注册用户之外的身份），**本函数不授权任何号组**；
        是否可分配由 ``_seed_allocation_authorized`` 决定，两者都不通过就不分配；
      - ``[]``    —— SaaS 用户但当前无有效权益：一个号组都没授权，不是「随便挑」；
      - 非空 list —— 该档授权的 account.plan_type 号组。

    ``store.StoreError`` 原样上抛：数据层故障时分不清运营者和过期用户，
    静默按「不设限」处理等于把闸门焊死在开的位置（见 tiers.resolve_user_tier 注释）。
    """
    from utils.tiers import resolve_user_tier, tier_account_plan_types
    tier_id = resolve_user_tier(seed)
    if tier_id is None:
        return None
    return tier_account_plan_types(tier_id)


def _pick_healthy_account(tier=None, plan_types=None):
    """在**授权范围内**挑一个健康账号；范围内没有就空手而归。

    plan_types（account.plan_type 集合）优先于 tier：供 user.tier → 号组 的半专属分池。
    授权范围是硬边界不是优先级 —— 显式请求的档位/号组用尽时返回 ""，绝不跨档借号：
    否则 Pro/Plus 用户会被静默降级到 Free 号，Free 号也会反过来吃掉付费号池的容量。
    ``plan_types=[]`` 与 ``None`` 语义相反：前者是「没授权任何号组」，后者才是「不设限」。

    仅当既没 plan_types 也没 tier（运营者 seed / 直传 token 的历史链路）才回退：
    先 free 池，再任意健康号。

    候选额外经 _account_is_usable 过滤（剔除熔断 dead / error / disabled），
    否则 mark_dead 只写 antiban_dead_tokens 不改 accounts.status，
    failover 会把死号重选回来（见真号冒烟：60 采样命中死号 31 次）。
    """
    def _usable(candidates):
        return [c for c in candidates if _account_is_usable(c.get("token", ""))]

    def _choose(candidates):
        usable = _usable(candidates)
        return random.choice(usable)["token"] if usable else ""

    if plan_types is not None:
        candidates: list = []
        for pt in plan_types:
            candidates.extend(store.get_account_by_plan(pt, status="healthy"))
        return _choose(candidates)

    if tier:
        return _choose(store.get_account_by_plan(tier, status="healthy"))

    token = _choose(store.get_account_by_plan("free", status="healthy"))
    return token or _choose(store.get_healthy_accounts())


def _binding_in_scope(token: str, plan_types) -> bool:
    """旧绑定是否仍落在当前授权号组内。``plan_types is None``（运营者）恒为真。"""
    if plan_types is None:
        return True
    return _account_tier(token) in plan_types


def _bind_seed_account(seed: str, entry, token: str, tier) -> str:
    """把选中的号写回 seed_map（保留 conversations 等历史归属）。"""
    assigned_tier = _account_tier(token) or tier or "free"
    if isinstance(entry, dict):
        entry["token"] = token
        entry["plan_type"] = assigned_tier
    else:
        globals.seed_map[seed] = {"token": token, "plan_type": assigned_tier, "conversations": []}
    globals.persist_seed(seed)
    return token


def _route_saas_seed(seed: str, *, force_switch=False) -> str:
    from utils.seed_lifecycle import LifecycleDenied, freeze_if_expired, route_seed
    if not force_switch:
        # Hot path: a persisted active binding to a healthy same-tier account is
        # already an allocation decision. Avoid reopening a SQLite write
        # transaction for every message; the atomic router remains the repair
        # path when any part of this read-only check is stale or missing.
        user_row = store.get_user(seed) or {}
        current = user_row.get("current_account")
        if user_row.get("status") == "active" and current and _account_is_usable(current):
            allowed = _seed_plan_types(seed)
            if allowed and _account_tier(current) in allowed:
                return current
    try:
        return route_seed(seed, configs.max_shared_seeds_per_account, force_switch=force_switch)
    except LifecycleDenied as exc:
        if exc.reason == 'capacity_unconfigured':
            raise HTTPException(503, 'Account capacity is not configured') from None
        if exc.reason == 'no_entitlement':
            try:
                freeze_if_expired(seed)
            except (LifecycleDenied, store.StoreError):
                raise HTTPException(503, 'Account allocation unavailable') from None
        return ''
    except store.StoreError:
        logger.error('[routing] seed_allocation_unavailable')
        raise HTTPException(503, 'Account allocation unavailable') from None


def _resolve_seed_account(seed: str) -> str:
    """粘性路由：seed 已绑定且仍在授权号组内的健康号则复用，否则按号组重选并写回。

    分不到号时返回 ""，且**不动** seed_map —— 没权益是不再分配名额，
    不是删除历史归属（到期冻结仍要能看到旧会话）；未经授权同理：
    连一条绑定都不留，否则「拒绝」本身就在制造持久化痕迹。

    授权由两个来源之一给出，缺一不可分配：
      - ``_seed_plan_types`` 非 ``None``：注册用户的付费权益 / Plus 试用；
      - ``_seed_allocation_authorized``：显式运营者、导入或开发句柄。

    走运营者链路时号组范围取该 seed 已声明的 ``plan_type``（持久化在内存 ``seed_map``，
    由运营者导入时写入）；没有声明档位的运营者 seed 保持历史兜底（先 free 池，再任意健康号）。
    """
    reject_development_alias(seed)
    entry = globals.seed_map.get(seed)
    current = entry.get("token", "") if isinstance(entry, dict) else ""
    try:
        plan_types = _seed_plan_types(seed)
    except store.StoreError:
        logger.error("[routing] entitlement_store_unavailable")
        raise HTTPException(status_code=503, detail="Entitlement service unavailable") from None

    if plan_types is not None:
        return _route_saas_seed(seed)

    if not _seed_allocation_authorized(seed):
        # 固定文案，不记录 seed 原文（seed 是凭据）。
        logger.warning("[routing] seed_allocation_denied reason=unauthorized_seed")
        return ""

    tier = entry.get("plan_type") if isinstance(entry, dict) else None
    scope = [tier] if tier else None
    # 运营者有明确档次时，同样校验旧绑定，不能把错误 Free 绑定无限复用。
    if current and _binding_in_scope(current, scope) and _account_is_usable(current):
        return current

    token = _pick_healthy_account(tier, plan_types)
    if not token:
        return ""  # 号池耗尽，或该 seed 无授权号组
    return _bind_seed_account(seed, entry, token, tier)


def switch_seed_account(seed: str) -> str:
    """强制切换 seed 到同号组的健康账号（供 /api/switch-account 调用）。返回新账号 token 或 ""。

    同样受授权号组约束，否则「切号」就成了绕过分池的后门；未经授权的 seed
    在这里也必须空手而归，否则它就成了绕过分配闸的第二条路。
    """
    reject_development_alias(seed)
    entry = globals.seed_map.get(seed)
    try:
        plan_types = _seed_plan_types(seed)
    except store.StoreError:
        logger.error("[routing] entitlement_store_unavailable")
        raise HTTPException(status_code=503, detail="Entitlement service unavailable") from None
    if plan_types is not None:
        return _route_saas_seed(seed, force_switch=True)
    if not _seed_allocation_authorized(seed):
        logger.warning("[routing] seed_switch_denied reason=unauthorized_seed")
        return ""
    tier = entry.get("plan_type") if isinstance(entry, dict) else None
    token = _pick_healthy_account(tier, plan_types)
    if not token:
        return ""
    return _bind_seed_account(seed, entry, token, tier)


def get_req_token(req_token, seed=None):
    # API consumers may pass a registered Seed without the separate seed arg.
    # Neither AUTO_SEED mode may turn that identity into an upstream credential.
    reject_development_alias(req_token)
    reject_development_alias(seed)
    try:
        if req_token and _seed_plan_types(req_token) is not None:
            return _route_saas_seed(req_token)
    except store.StoreError:
        raise HTTPException(503, 'Entitlement service unavailable') from None
    if configs.auto_seed:
        available_token_list = list(set(globals.token_list) - set(globals.error_token_list))
        length = len(available_token_list)
        if seed:
            return _resolve_seed_account(seed)

        if req_token in configs.authorization_list:
            if len(available_token_list) > 0:
                if configs.random_token:
                    req_token = random.choice(available_token_list)
                    return req_token
                else:
                    globals.count += 1
                    globals.count %= length
                    return available_token_list[globals.count]
            else:
                return ""
        else:
            return req_token
    else:
        seed = req_token
        if seed not in globals.seed_map.keys():
            raise HTTPException(status_code=401, detail={"error": "Invalid Seed"})
        # Map mode is the operator's explicit seed -> account table. A binding that
        # no durable grant backs is cache, not permission: without this check an
        # in-memory-only entry would hand out a fleet account.
        if not _seed_allocation_authorized(seed):
            logger.warning("[routing] seed_allocation_denied reason=unauthorized_seed")
            raise HTTPException(status_code=401, detail={"error": "Invalid Seed"})
        return globals.seed_map[seed]["token"]


async def verify_token(req_token):
    if not req_token:
        if configs.authorization_list:
            logger.error("Unauthorized with empty token.")
            raise HTTPException(status_code=401)
        else:
            return None
    else:
        from gateway.frontend_sync import verified_access_token, FrontendSessionError
        account = store.get_account(req_token) or {}
        if (account.get('status') in ('disabled', 'unhealthy')
                or req_token in globals.error_token_list
                or antiban_circuit.is_token_dead(req_token)):
            raise HTTPException(status_code=401, detail='Account unavailable')
        try:
            website_access = await verified_access_token(req_token)
        except FrontendSessionError:
            raise HTTPException(status_code=503, detail='Account website session unavailable') from None
        if website_access:
            return website_access
        if req_token.startswith("eyJhbGciOi") or req_token.startswith("fk-"):
            access_token = req_token
            globals.sync_account_plan(req_token)
            return access_token
        # SessionToken：带 sess- 前缀的 chatgpt.com __Secure-next-auth.session-token
        elif req_token.startswith("sess-"):
            try:
                if req_token in globals.error_token_list:
                    raise HTTPException(status_code=401, detail="Error SessionToken")
                access_token = await sess2ac(req_token, force_refresh=False)
                globals.sync_account_plan(req_token, access_token)
                return access_token
            except HTTPException as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail)
        # 识别 RefreshToken：老版 45 字符 或 新版 Auth0 'rt_' 前缀（长度 ≥ 60）
        elif (req_token.startswith("rt_") and len(req_token) >= 60) or len(req_token) == 45:
            try:
                if req_token in globals.error_token_list:
                    raise HTTPException(status_code=401, detail="Error RefreshToken")

                access_token = await rt2ac(req_token, force_refresh=False)
                globals.sync_account_plan(req_token, access_token)
                return access_token
            except HTTPException as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail)
        else:
            return req_token


async def refresh_all_tokens(force_refresh=False):
    for token in list(set(globals.token_list) - set(globals.error_token_list)):
        try:
            if token.startswith("sess-"):
                await asyncio.sleep(0.5)
                await sess2ac(token, force_refresh=force_refresh)
            elif (token.startswith("rt_") and len(token) >= 60) or len(token) == 45:
                await asyncio.sleep(0.5)
                await rt2ac(token, force_refresh=force_refresh)
        except HTTPException:
            pass
    logger.info("All tokens refreshed.")
