"""运营者删号必须是「持久下线」，而不是只从内存缓存里消失。

回归的 P1：``/admin/routing/accounts/delete`` 过去只把 token 从
``globals.token_list`` 里摘掉。``accounts`` 行仍是 ``healthy``，而路由
（``chatgpt.authorization._pick_healthy_account``）读的正是 ``accounts.status``，
于是账号继续被派发；进程重启后 ``store.load_all`` 又按行重建 ``token_list``，
把它原样装回池子 —— 运营者以为号已经删了，实际什么都没变。

本文件钉死修好后的四条行为：
  1. 成功返回前，账号在 SQLite 里已经是 ``disabled``，并且不再被路由选中；
  2. 持久化失败 = fail-closed：不返回成功，内存与库都保持原样；
  3. 重启 / ``load_all`` 重建内存后，账号既不是 healthy 也不能被路由；
  4. 审计仍不落凭据，且如实描述这次操作。

删除保留 ``accounts`` 行（改为 disabled）而不是物理删除，是为了不丢历史与可审计性；
代价与残留风险见 REVIEW_PACKET。
"""

import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import utils.globals as globals
import utils.store as store
from chatgpt import authorization as auth
from gateway import admin
from utils import audit

TOKEN = "synthetic-plus-account"
OTHER_TOKEN = "synthetic-other-account"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_state(db):
    """每个用例一套干净的内存缓存；SQLite 由 ``db`` 夹具隔离。"""
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.refresh_map.clear()
    globals.fp_map.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()
    yield
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.refresh_map.clear()
    globals.fp_map.clear()
    globals.seed_map.clear()
    globals.antiban_dead_tokens.clear()
    globals._plan_synced.clear()


@pytest.fixture
def admin_call(monkeypatch):
    """放行鉴权，其余依赖（路由解绑、持久化写穿）都走真实实现。"""
    monkeypatch.setattr(admin, "require_admin_auth", lambda request: None)

    def _call(token):
        return asyncio.run(admin.routing_admin_delete_account(_request(token)))

    return _call


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    """审计落盘指向 per-test 文件，避免用例之间互相看到记录。"""
    from utils import configs
    monkeypatch.setattr(configs, "audit_db_path", str(tmp_path / "audit.db"))
    monkeypatch.setattr(audit, "_initialized_paths", set())
    return tmp_path / "audit.db"


def _request(token):
    """一个只带 ``{"token": ...}`` 的最小 POST 请求。"""
    body = json.dumps({"token": token}).encode()
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()

    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": "/admin/routing/accounts/delete",
        "raw_path": b"/admin/routing/accounts/delete",
        "query_string": b"", "headers": [], "scheme": "http",
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "asgi": {"version": "3.0", "spec_version": "2.3"},
    }
    return Request(scope, receive)


def _seed_account(token, plan_type="plus", status="healthy"):
    """镜像生产导入：库里一行 + 内存一条。"""
    store.upsert_account(token, plan_type=plan_type, status=status)
    globals.token_list.append(token)
    return token


def _simulate_restart():
    """按 ``utils/globals.py`` 的启动路径从 SQLite 重建内存域。"""
    loaded = store.load_all()
    globals.token_list[:] = loaded["token_list"]
    globals.error_token_list[:] = loaded["error_token_list"]
    globals.refresh_map = loaded["refresh_map"]
    globals.fp_map = loaded["fp_map"]
    globals.routing_config = loaded["routing_config"]
    globals.seed_map = loaded["seed_map"]
    globals.conversation_map = loaded["conversation_map"]
    # 启动时的写穿：导入清单不得被当作健康证据（这正是能把它救活的那条路径）。
    globals.persist_token_list()


# ---------------------------------------------------------------- 端点成功

def test_delete_returns_success_only_after_the_account_is_durably_disabled(admin_call, audit_db):
    _seed_account(TOKEN)

    response = admin_call(TOKEN)

    assert json.loads(response.body)["status"] == "success"
    # 返回成功的那一刻，库里的状态就已经是终态。
    assert store.get_account(TOKEN)["status"] == "disabled"
    assert TOKEN not in globals.token_list

    # 审计如实记录，且不落凭据。
    events = [e for e in audit.recent() if e["action"] == "pool.account_deleted"]
    assert len(events) == 1
    assert events[0]["subject"] == audit.subject_id(TOKEN)
    assert events[0]["detail"]["status"] == "disabled"
    assert TOKEN not in json.dumps(events[0], ensure_ascii=False)


def test_deleted_account_is_excluded_from_routing_while_the_rest_stay(admin_call):
    _seed_account(TOKEN)
    _seed_account(OTHER_TOKEN)

    admin_call(TOKEN)

    assert auth._account_is_usable(TOKEN) is False
    assert [a["token"] for a in store.get_healthy_accounts()] == [OTHER_TOKEN]
    assert auth._pick_healthy_account("plus") == OTHER_TOKEN
    # 账号没有被物理删除：历史行留着，可审计。
    assert store.get_account(TOKEN) is not None


# ---------------------------------------------------------- 持久化失败必须封闭

def test_delete_fails_closed_when_the_status_write_fails(admin_call, audit_db, monkeypatch):
    _seed_account(TOKEN)

    def _boom(*_args, **_kwargs):
        raise store.StoreError("disk full")

    monkeypatch.setattr(store, "set_account_status", _boom)

    with pytest.raises(HTTPException) as exc:
        admin_call(TOKEN)

    assert exc.value.status_code == 503
    # 失败 = 什么都没发生：内存与库都保持删除前的样子。
    assert TOKEN in globals.token_list
    assert store.get_account(TOKEN)["status"] == "healthy"
    assert auth._account_is_usable(TOKEN) is True
    assert [e for e in audit.recent() if e["action"] == "pool.account_deleted"] == []


def test_delete_reports_an_error_rather_than_a_silent_success(admin_call, monkeypatch):
    """写库抛任何异常都不得退化成「成功」，否则运营者会以为号已经下线。"""
    _seed_account(TOKEN)

    def _boom(*_args, **_kwargs):
        raise store.StoreError("locked")

    monkeypatch.setattr(store, "set_account_status", _boom)

    with pytest.raises(HTTPException):
        admin_call(TOKEN)
    assert store.get_account(TOKEN)["status"] == "healthy"


# ---------------------------------------------------------- 重启 / load_all

def test_restart_reload_cannot_restore_the_deleted_account(admin_call):
    _seed_account(TOKEN)

    admin_call(TOKEN)
    _simulate_restart()

    # 重启后可能重新出现在导入清单里（load_all 按行重建），但状态必须是 disabled。
    assert store.get_account(TOKEN)["status"] == "disabled"
    assert auth._account_is_usable(TOKEN) is False
    assert auth._pick_healthy_account("plus") == ""
    assert store.get_healthy_accounts() == []


def test_restart_reload_keeps_the_deletion_visible_in_the_inventory(admin_call):
    """重启不复活账号，但保留可审计的停用记录（不静默物理删除）。"""
    _seed_account(TOKEN)

    admin_call(TOKEN)
    _simulate_restart()

    row = next(a for a in store.list_accounts() if a["token"] == TOKEN)
    assert row["status"] == "disabled"
    # 停用号不得因为出现在清单里而被当成可路由号。
    assert row["token"] not in [a["token"] for a in store.get_healthy_accounts()]
