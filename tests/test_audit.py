"""Operator audit records: they must never carry credentials or personal data.

The audit log answers "who deleted which account", so it has to survive being read by
someone who is not allowed to see tokens or customer email addresses. These tests pin
the two properties that make that true: a value whitelist for ``detail`` and an
irreversible subject id in place of the email.
"""

import json

import pytest

from utils import audit, configs


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    """Point the audit sink at a per-test file so records cannot leak between tests."""
    monkeypatch.setattr(configs, "audit_db_path", str(tmp_path / "audit.db"))
    monkeypatch.setattr(audit, "_initialized_paths", set())
    return tmp_path / "audit.db"


def test_record_persists_action_and_subject_without_email(audit_db):
    email = "audit-user@example.com"
    assert audit.record("user.status_changed", subject=audit.subject_id(email),
                        detail={"status": "banned"}) is True

    events = audit.recent()
    assert len(events) == 1
    event = events[0]
    assert event["action"] == "user.status_changed"
    assert event["detail"] == {"status": "banned"}
    assert event["ok"] is True
    # 邮箱原文与 seed 都不进记录 —— 只有不可逆的派生 id
    blob = json.dumps(event, ensure_ascii=False)
    assert email not in blob
    assert "audit-user" not in blob
    assert event["subject"] == audit.subject_id(email)


def test_subject_id_is_stable_irreversible_and_case_insensitive():
    first = audit.subject_id("Person@Example.com")
    assert first == audit.subject_id("person@example.com")
    assert first and "person" not in first and "@" not in first
    assert first != audit.subject_id("other@example.com")


def test_detail_whitelist_drops_unlisted_keys(audit_db):
    """调用方塞进来的任意字段一律丢弃：白名单之外不存在「顺手记一下凭据」。"""
    audit.record("payment.settled", detail={
        "order_id": "ord_synthetic",
        "amount": "99",
        "token": "secret-account-token",
        "cookie": "session=abc",
        "email": "audit-secret@example.com",
    })

    detail = audit.recent()[0]["detail"]
    assert detail == {"order_id": "ord_synthetic", "amount": "99"}
    assert "token" not in detail and "cookie" not in detail and "email" not in detail


def test_detail_values_are_truncated(audit_db):
    audit.record("pool.accounts_imported", detail={"source": "x" * 500})
    assert len(audit.recent()[0]["detail"]["source"]) == audit._MAX_VALUE_LEN


def test_record_requires_an_action_and_survives_write_failure(audit_db, monkeypatch):
    assert audit.record("") is False

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "_connect", _boom)
    # 审计是旁路：写不进去也不能把业务操作带崩
    assert audit.record("user.status_changed") is False
    assert audit.recent() == []


def test_recent_is_newest_first_and_bounded(audit_db):
    for index in range(5):
        audit.record("pool.account_deleted", detail={"count": index})
    events = audit.recent(limit=3)
    assert [e["detail"]["count"] for e in events] == ["4", "3", "2"]


# ---------------------------------------------------------------------------
# Account deletion is irreversible: the record must say *which* account went,
# without the token itself ever reaching the log.
# ---------------------------------------------------------------------------

def _delete_request(token):
    """A minimal POST request carrying the admin delete body."""
    import asyncio
    import json

    from starlette.requests import Request

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


def test_account_deletion_records_an_anonymized_subject(audit_db, monkeypatch):
    """删号必须留痕到「删的是哪一个」，但审计里只能有 subject_id 派生值。"""
    import asyncio

    from gateway import admin
    import utils.globals as state

    token = "synthetic-account-token"
    state.token_list.append(token)
    monkeypatch.setattr(admin, "require_admin_auth", lambda request: None)
    monkeypatch.setattr(admin, "remove_account_binding", lambda value: None)
    monkeypatch.setattr(state, "persist_token_list", lambda: None)

    try:
        response = asyncio.run(admin.routing_admin_delete_account(_delete_request(token)))
    finally:
        state.token_list[:] = [item for item in state.token_list if item != token]

    assert json.loads(response.body)["status"] == "success"
    events = [e for e in audit.recent() if e["action"] == "pool.account_deleted"]
    assert len(events) == 1
    event = events[0]
    assert event["subject"] == audit.subject_id(token), "删号记录必须带匿名主体"
    assert event["subject"], "匿名主体不能为空 —— 否则审计答不出删了哪一个"
    # 主体是单向派生值：原文、token 片段都不进记录
    assert token not in event["subject"]
    assert token not in str(event)
