"""antiban 匿名指标的管理面出口：可查询，但不公开。

``metrics_snapshot()`` 只返回计数与有界枚举，所以可以直接暴露给指标系统；
但「可暴露」不等于「可公开」——它仍然读得到部署形态与死号存量，因而和其它
后台接口共用同一条 ``require_admin_auth`` 边界。

本文件锁定三件事：
  1. 没有有效管理员凭据时，路由拒绝且不透露任何内容；
  2. 有凭据时返回的快照是有界的、匿名的（账号标识一律不进响应体）；
  3. antiban 关闭时返回 enabled=false 的零值快照，而不是 404 ——
     「开关关掉了」和「这个版本没有这个接口」必须能区分。
"""

import json

import pytest

import utils.configs as configs
import utils.globals as globals
from utils.antiban import circuit

FAKE_TOKEN = "eyJhbGciOiJIUzI1NitestonlyADMINMETRICS123456"
METRICS_PATH = "/admin/antiban/metrics"


@pytest.fixture
def admin_secret(monkeypatch):
    from gateway import admin
    monkeypatch.setattr(admin, "admin_password", "test-admin-secret")
    return {"Authorization": "Bearer test-admin-secret"}


@pytest.fixture
def antiban_enabled(monkeypatch):
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(circuit, "_persist_dead", lambda: None)
    yield


# ---------------------------------------------------------------------------
# 1. 认证边界
# ---------------------------------------------------------------------------

def test_metrics_route_rejects_missing_admin_auth(client):
    response = client.get(METRICS_PATH)

    assert response.status_code in (401, 503)
    assert "dead_accounts" not in response.text


def test_metrics_route_rejects_invalid_admin_auth(client):
    response = client.get(METRICS_PATH, headers={"Authorization": "Bearer not-the-admin-secret"})

    assert response.status_code == 401
    assert "dead_accounts" not in response.text


# ---------------------------------------------------------------------------
# 2. 有凭据时的快照形态
# ---------------------------------------------------------------------------

def test_metrics_route_returns_bounded_anonymous_snapshot(client, admin_secret, antiban_enabled):
    globals.antiban_bucket["buckets"]["bkt::testonly"] = {
        "proxy_url": "http://proxy.test",
        "status": "healthy",
        "degraded_until": 0,
        "accounts": [FAKE_TOKEN],
    }
    circuit.mark_dead(FAKE_TOKEN, "account_deactivated")

    response = client.get(METRICS_PATH, headers=admin_secret)

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["enabled"] is True
    assert snapshot["counts"]["dead_accounts"] == 1
    assert snapshot["counts"]["buckets"]["total"] == 1
    assert snapshot["counts"]["buckets"]["healthy"] == 1
    # 容量永远是本进程口径，没有共享协调层
    assert snapshot["counts"]["capacity_scope_is_process"] is True
    assert snapshot["coordination"]["capacity_is_global"] is False
    # 指标键是有界枚举，调用方文本不进键
    assert set(snapshot["counters"]) == {
        "admission_denials", "cooldown_events", "cooldown_extend_reasons", "circuit_errors",
    }

    # 匿名：账号标识（含前缀）不得出现在响应体里
    body = json.dumps(snapshot, ensure_ascii=False)
    assert FAKE_TOKEN not in body
    for n in (6, 8, 12, 16):
        assert FAKE_TOKEN[:n] not in body, f"token prefix of length {n} leaked into metrics"


def test_metrics_route_reports_zeroed_snapshot_when_antiban_disabled(client, admin_secret):
    """关闭时路由仍在，返回零值 + enabled=false，而不是 404。"""
    assert configs.enable_antiban is False  # 本套件默认关闭

    response = client.get(METRICS_PATH, headers=admin_secret)

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["enabled"] is False
    assert snapshot["coordination"]["mode"] == "disabled"
    assert snapshot["counts"]["dead_accounts"] == 0
    assert snapshot["counts"]["buckets"]["total"] == 0
    assert all(value == 0 for value in snapshot["counters"]["circuit_errors"].values())
