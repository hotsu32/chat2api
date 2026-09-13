"""运营者撤销**只**撤销运营者自己的授权，绝不删除注册用户。

``users`` 表被两类所有者共用，``status`` 就是它们的所有权类型：

  - 运营者/导入授权（``OPERATOR_SEED_STATUS``）：唯一写入点是 AUTHORIZATION
    鉴权的 ``POST /seedtoken``。这一类行归 ``seed_map`` 所有 —— 它不在内存里，
    就是运营者撤掉了它；
  - 注册用户（trial / active / frozen）：所有者是 ``user_auth`` + 权益，
    ``seed_map`` 只是当前绑定的一份缓存快照。它不在内存里，只说明这次快照
    没带上它（重启窗口、部分加载、clear 之后的空表）。

旧实现把两者混为一谈：``persist_seed_map`` 把「不在 seed_map」一律当作「该删」，
于是运营者按一次 ``clear`` 就会删光所有注册用户行，并级联删掉会话历史；
按名字删一个注册用户的 seed 会走同一条 ``store.delete_user`` 级联。

本文件钉死的行为（失败优先：这些用例在修复前应当变红）：
  1. 授权 clear 之后，注册用户的 users 行 / 当前账号 / 已付订单 / 会话历史
     一样都不能少；
  2. 按名字撤销一个注册用户的 seed = 404，且一行都不删；
  3. 部分快照（重启窗口 / 空表）写回时不得清理注册用户；
  4. 池管理端的 seed 清理同样只动运营者的东西；
  5. 撤销/清理留下审计记录，且记录里没有 seed 原文。
"""
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import utils.audit as audit
import utils.configs as configs
import utils.globals as globals
import utils.plans as plans
import utils.store as store
from chatgpt.authorization import OPERATOR_SEED_STATUS

OPERATOR_KEY = "operator-key"
_PW = "pbkdf2_sha256$1$00$00"
SAAS_SEED = "seed-saas-user"
SAAS_EMAIL = "saas-owner@example.test"
SAAS_ACCOUNT = "acct-saas-shared"
SAAS_CONV = "conv-saas-history"


@pytest.fixture(autouse=True)
def _isolated(db, tmp_path, monkeypatch):
    import gateway.share as share

    monkeypatch.setattr(share, "authorization_list", [OPERATOR_KEY])
    monkeypatch.setattr(configs, "audit_db_path", str(tmp_path / "audit.db"))
    monkeypatch.setattr(audit, "_initialized_paths", set())
    globals.seed_map.clear()
    globals.conversation_map.clear()
    yield
    globals.seed_map.clear()
    globals.conversation_map.clear()


def _credentials(key=OPERATOR_KEY):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)


def _request(payload=None, method="DELETE", path="/seedtoken"):
    body = json.dumps(payload).encode() if payload is not None else b""
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        import asyncio
        await asyncio.Future()

    headers = [(b"content-type", b"application/json")] if payload is not None else []
    scope = {
        "type": "http", "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers, "scheme": "http",
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "asgi": {"version": "3.0", "spec_version": "2.2"},
    }
    return Request(scope, receive)


def _registered_paid_user(seed=SAAS_SEED, email=SAAS_EMAIL, plan_id="plus-shared-1m"):
    """一个注册且已付费的 SaaS 用户：user_auth + 已付订单 + users 行 + 会话历史。

    权益的唯一来源是**已支付且未过期**的订单（``utils/entitlements.py``），
    所以「付费用户」必须落一张 paid 订单，而不是改 ``user_auth.tier_id``。
    """
    store.upsert_user_auth(email, password_hash=_PW, seed=seed, status="active")
    detail = plans.plan_detail(plan_id)
    order_id = f"ord-{seed}"
    store.create_order(order_id, email, detail["id"], str(detail["price"]), status="pending")
    store.activate_order(order_id, int(time.time()) + 30 * 86400)
    store.upsert_user(seed, current_account=SAAS_ACCOUNT, plan_type="plus", status="active")
    store.upsert_conversation(SAAS_CONV, seed, SAAS_ACCOUNT, "history", "1", "2")
    globals.seed_map[seed] = {
        "token": SAAS_ACCOUNT, "plan_type": "plus", "status": "active",
        "conversations": [SAAS_CONV],
    }
    return order_id


def _operator_grant(seed, account="acct-operator", conversations=()):
    store.upsert_user(seed, status=OPERATOR_SEED_STATUS, current_account=account)
    for conv_id in conversations:
        store.upsert_conversation(conv_id, seed, account, "t", 1, 2)
    globals.seed_map[seed] = {
        "token": account, "plan_type": "plus", "conversations": list(conversations),
    }
    return seed


def _assert_registered_user_intact(order_id):
    user = store.get_user(SAAS_SEED)
    assert user is not None, "运营者撤销删掉了注册用户的 users 行"
    assert user["current_account"] == SAAS_ACCOUNT
    assert store.get_user_auth(SAAS_EMAIL) is not None, "注册身份被删"
    order = store.get_order(order_id)
    assert order is not None and order["status"] == "paid", "已付订单被删"
    rows = store.list_seed_conversations(SAAS_SEED)
    assert [r["conv_id"] for r in rows] == [SAAS_CONV], "会话历史被级联删除"


# --------------------------------------------------------------- 1 clear all

async def test_clear_revokes_operator_grants_and_spares_registered_users():
    """运营者的 clear 是撤销授权，不是删除账号。"""
    from gateway import share

    order_id = _registered_paid_user()
    _operator_grant("op-a", conversations=["conv-op"])

    response = await share.delete_seedtoken(_request({"seed": "clear"}), _credentials())

    assert response["status"] == "success"
    assert store.get_user("op-a") is None                     # 授权确实撤销了
    assert store.list_seed_conversations("op-a") == []
    _assert_registered_user_intact(order_id)


async def test_clear_keeps_the_registered_binding_usable():
    """内存是缓存，但撤销不该顺手把它清掉：注册用户的会话归属读的就是它。

    撤销之后内存应当与「刚重启」一致 —— 库里的幸存者（注册用户）装回来，
    被撤销的运营者绑定不装回来。
    """
    from gateway import share

    order_id = _registered_paid_user()
    _operator_grant("op-a")

    await share.delete_seedtoken(_request({"seed": "clear"}), _credentials())

    assert "op-a" not in globals.seed_map
    assert globals.seed_map[SAAS_SEED]["token"] == SAAS_ACCOUNT
    assert globals.seed_map[SAAS_SEED]["conversations"] == [SAAS_CONV]
    _assert_registered_user_intact(order_id)


# ------------------------------------------------------------ 2 named revoke

async def test_named_revoke_of_a_registered_seed_deletes_nothing():
    """库里有行、内存里也有绑定，但它不是运营者授权 —— 撤销的边界到此为止。"""
    from gateway import share

    order_id = _registered_paid_user()

    with pytest.raises(HTTPException) as exc:
        await share.delete_seedtoken(_request({"seed": SAAS_SEED}), _credentials())

    assert exc.value.status_code == 404
    _assert_registered_user_intact(order_id)


async def test_named_revoke_still_removes_an_operator_grant_and_its_history():
    """运营者自有的绑定维持既有语义：行与会话一并撤销。"""
    from gateway import share

    _operator_grant("op-a", conversations=["conv-op"])

    response = await share.delete_seedtoken(_request({"seed": "op-a"}), _credentials())

    assert response["status"] == "success"
    assert store.get_user("op-a") is None
    assert store.list_seed_conversations("op-a") == []
    assert "op-a" not in globals.seed_map


# ------------------------------------------------- 3 partial snapshot / restart

def test_partial_snapshot_persist_prunes_only_operator_owned_rows():
    """重启窗口 / 部分加载时内存可能不含注册用户：那不是「撤销了他们」。"""
    order_id = _registered_paid_user()
    _operator_grant("op-a")

    globals.seed_map.clear()          # 部分快照：注册用户不在里面
    globals.persist_seed_map()

    _assert_registered_user_intact(order_id)
    assert store.get_user("op-a") is None   # 运营者自有的：缺席即撤销（既有语义）


def test_restart_reload_does_not_prune_registered_users():
    """重启装载之后再写回，注册用户一个不少。"""
    order_id = _registered_paid_user()

    reloaded = store.load_all()
    globals.seed_map.clear()
    globals.seed_map.update(reloaded["seed_map"])
    globals.persist_seed_map()

    assert SAAS_SEED in globals.seed_map
    _assert_registered_user_intact(order_id)


# ------------------------------------------------------------ 4 pool controls

async def test_pool_admin_seed_clear_spares_registered_users(monkeypatch):
    """池管理端的 seed 清理是同一个所有权边界。"""
    from api import chat2api

    order_id = _registered_paid_user()
    _operator_grant("op-a", conversations=["conv-op"])
    monkeypatch.setattr(chat2api, "_require_pool_admin", lambda request: None)

    response = await chat2api.clear_seed_tokens(_request(payload=None, method="POST",
                                                         path="/seed_tokens/clear"))

    assert response["status"] == "success"
    assert store.get_user("op-a") is None
    _assert_registered_user_intact(order_id)


# ------------------------------------------------------------------ 5 audit

async def test_clear_records_an_audit_event_without_the_seed():
    from gateway import share

    _registered_paid_user()
    _operator_grant("op-secret-name")

    await share.delete_seedtoken(_request({"seed": "clear"}), _credentials())

    events = [e for e in audit.recent() if e["action"] == "seed.grants_revoked"]
    assert events, "撤销没有留下审计记录"
    event = events[0]
    assert event["ok"] is True
    assert event["detail"].get("count") == "1"
    assert "op-secret-name" not in json.dumps(event, ensure_ascii=False)


async def test_named_revoke_records_an_audit_event_without_the_seed():
    from gateway import share

    _operator_grant("op-secret-name")

    await share.delete_seedtoken(_request({"seed": "op-secret-name"}), _credentials())

    events = [e for e in audit.recent() if e["action"] == "seed.grant_revoked"]
    assert events, "按名字撤销没有留下审计记录"
    event = events[0]
    assert event["subject"] == audit.subject_id("op-secret-name")
    assert "op-secret-name" not in json.dumps(event, ensure_ascii=False)


async def test_a_registered_seed_revoke_is_audited_as_not_found():
    from gateway import share

    _registered_paid_user()

    with pytest.raises(HTTPException):
        await share.delete_seedtoken(_request({"seed": SAAS_SEED}), _credentials())

    events = [e for e in audit.recent() if e["action"] == "seed.grant_revoked"]
    assert events and events[0]["ok"] is False
    assert events[0]["detail"].get("result") == "not_found"


# ------------------------------------------------- 6 empty upstream base URL

def _stub_upstream(monkeypatch, share, host):
    """把上游调用整体替换成替身：本用例只关心目标地址是怎么来的。"""
    calls = []

    class _StubClient:
        def __init__(self, proxy=None, impersonate="safari15_3", timeout=None):
            self.proxy = proxy
            calls.append(("client", proxy))

        async def get(self, url, headers=None, timeout=None, **kwargs):
            calls.append(("get", url))
            raise RuntimeError("stub upstream")

        async def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(share, "Client", _StubClient)
    return calls


async def test_account_check_fails_closed_without_a_configured_base_url(monkeypatch):
    """显式空的 CHATGPT_BASE_URL = 没有上游目标：不构造客户端、不触达真实站点。"""
    from gateway import share

    monkeypatch.setattr(share, "chatgpt_base_url_list", [])
    touched = []

    def _forbidden_client(*args, **kwargs):
        touched.append("client")
        raise AssertionError("empty base URL must not construct an upstream client")

    monkeypatch.setattr(share, "Client", _forbidden_client)

    assert await share.chatgpt_account_check(_synthetic_credential()) == {}
    assert touched == []


async def test_account_check_uses_the_configured_base_url(monkeypatch):
    """配了上游就照配的用，不改行为。"""
    from gateway import share

    calls = _stub_upstream(monkeypatch, share, "http://127.0.0.1:9")
    monkeypatch.setattr(share, "chatgpt_base_url_list", ["http://127.0.0.1:9"])
    monkeypatch.setattr(share, "proxy_url_list", [])
    monkeypatch.setattr(share, "get_real_req_token", _stub_req_token)
    monkeypatch.setattr(share, "verify_token", _stub_verify_token)
    monkeypatch.setattr(share, "get_fp", lambda token: {})

    assert await share.chatgpt_account_check(_synthetic_credential()) == {}
    targets = [c[1] for c in calls if c[0] == "get"]
    assert targets and all(t.startswith("http://127.0.0.1:9/") for t in targets)


def _synthetic_credential():
    # is_direct_upstream_credential 只认两种形状：45 长度或 eyJhbGciOi 前缀。
    # 合成值只用于形状检查，不是任何真实凭据。
    return "eyJhbGciOiJIUzI1NitestonlySYNTHETIC0000000000"


async def _stub_req_token(value):
    return value


async def _stub_verify_token(value):
    return value
