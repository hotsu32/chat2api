"""Capacity protection over the real product entry: /v1/chat/completions.

Unit tests can show the classifier and the guard each do the right thing; they
cannot show that the *product path* reaches them. This file runs the real
ChatService chain against the loopback mock upstream with antiban enabled, and
pins three capacities the layer claims:

  1. **严格绑定下分不到桶的号不得出网。** 池子里有桶但一个都接纳不了时，
     请求必须在任何上游动作之前以 503 结束——不是一个没有绑定保证的出口。
  2. **上游确证的挑战要变成账号退避。** chat-requirements 返回 403 + turnstile
     时，这个号必须先退避；下一个请求在准入层就被拒，打不到上游。
  3. **账号暂时不可用不等于封号。** "account unavailable" 是退避，不是永久黑名单；
     两个都必须能区分。

隔离：上游是 loopback mock（见 conftest），凭据是测试内伪造的 JWT，零真实网络。
"""
import json
import time

import pytest

import utils.configs as configs
import utils.globals as globals
import utils.store as store
from utils.antiban import bucket, circuit, concurrency, cooldown, guard

import chatgpt.ChatService as chat_service_mod

SEED = "seed-af-gaps"
EMAIL = "antiban-gaps-e2e@example.test"
BUCKET_ID = "bkt::af-gaps"
CHAT_REQUIREMENTS = "/backend-api/sentinel/chat-requirements"
# 这条入口上，请求提交的凭据就是路由身份：guard 的冷却与并发上限都记在它上面
# （``ChatService.req_token`` 取自 Authorization 头）。断言必须针对这把钥匙，
# 否则测的是「另一个 token 没被冷却」这个永真命题。
IDENTITY = SEED


@pytest.fixture(autouse=True)
def antiban_enabled(monkeypatch):
    """Run the real guard with fresh in-process state, as production holds it."""
    monkeypatch.setattr(configs, "enable_antiban", True)
    monkeypatch.setattr(configs, "strict_ip_binding", True)
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    cooldown.reset_cooldown_stats()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    circuit.reset_circuit_stats()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()
    guard.reset_admission_stats()
    globals.antiban_bucket = {"buckets": {}, "account_index": {}}
    # 真实链路里 sentinel 是 get_chat_token 的一部分；跳过它就等于跳过被测路径。
    monkeypatch.setattr(chat_service_mod, "conversation_only", False)
    yield
    cooldown._account_next_available.clear()
    cooldown._account_locks.clear()
    circuit._account_backoff_level.clear()
    circuit._bucket_network_errors.clear()
    concurrency._account_semaphores.clear()
    concurrency._account_limits.clear()


@pytest.fixture
def account(make_access_token, seed_account, seed_user):
    """One routable Plus account behind one seed, as production holds it."""
    token = make_access_token(plan_type="plus", account_id="acc-af-gaps")
    seed_account(token, plan_type="plus")
    seed_user(SEED, token, plan_type="plus")
    return token


def _reject_chat_requirements(mock_upstream, monkeypatch, status, body):
    """Make the requirements endpoint answer with an upstream refusal."""
    handler = mock_upstream.RequestHandlerClass
    original = handler._send

    def send(self, code, payload, ctype="application/json", extra_headers=None):
        if self.path.split("?")[0] == CHAT_REQUIREMENTS:
            code, payload, ctype = status, body, "application/json"
        return original(self, code, payload, ctype, extra_headers)

    monkeypatch.setattr(handler, "_send", send)


def _post(client):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {SEED}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )


def _upstream_calls(mock_upstream):
    return len(mock_upstream.records)


# ---------------------------------------------------------------------------
# 1. 严格绑定：分不到桶的号不得出网
# ---------------------------------------------------------------------------

def test_unplaceable_account_is_refused_before_any_upstream_work(client, mock_upstream, account, monkeypatch):
    """池子满员 = 这个号绑不到任何出口。严格模式下不能在无绑定保证的出口上出网。"""
    from utils.antiban.bucket import _bucket_id_for_proxy

    globals.antiban_bucket["buckets"][BUCKET_ID] = {
        "proxy_url": "socks5h://testonly:testonly@203.0.113.9:1080",
        "proxy_name": "testonly",
        "group": "",
        # 桶已满（BUCKET_MAX_ACCOUNTS_PER_IP 默认 5）
        "accounts": ["synthetic-a", "synthetic-b", "synthetic-c", "synthetic-d", "synthetic-e"],
        "last_request_at": {},
        "status": "healthy",
        "degraded_until": 0,
        "created_at": 0,
        "plan_type": "plus",
    }
    assert bucket.has_buckets() is True
    assert account not in globals.antiban_bucket["account_index"]

    response = _post(client)

    assert response.status_code == 503
    assert "no_healthy_bucket" in response.text
    assert guard.get_admission_stats().get("no_healthy_bucket", 0) >= 1
    assert mock_upstream.records == [], "an unbound account reached the upstream"
    assert _bucket_id_for_proxy  # 桶 id 由代理串派生：这条断言只是把依赖显式化


def test_account_is_admitted_when_the_pool_is_empty(client, mock_upstream, account):
    """没有配置任何出口不是绑定违约：不能在未配代理时把整个入口 503 掉。"""
    response = _post(client)

    assert response.status_code == 200
    assert mock_upstream.records, "the upstream was never reached with an empty pool"


# ---------------------------------------------------------------------------
# 2. 上游确证的挑战 → 账号退避 → 下一个请求不再出网
# ---------------------------------------------------------------------------

def test_challenge_refusal_backs_the_account_off_and_stops_the_next_turn(
        client, mock_upstream, account, monkeypatch):
    _reject_chat_requirements(mock_upstream, monkeypatch, 403, b'{"detail":"turnstile required"}')

    refused = _post(client)
    calls_after_refusal = _upstream_calls(mock_upstream)

    assert refused.status_code == 403
    assert circuit.get_circuit_stats().get("turnstile_challenge:403") == 1
    assert cooldown.get_next_available(IDENTITY) > time.time(), "the challenge did not back the account off"

    # 退避生效后，下一个请求在准入层被拒：同样的号不再打上游
    second = _post(client)

    assert second.status_code == 503
    assert "cooldown" in second.text
    assert _upstream_calls(mock_upstream) == calls_after_refusal, "a cooled-down account was sent upstream again"


@pytest.mark.parametrize("detail,expected_status", [
    ("Failed to solve proof of work", 403),
    ("arkose required", 403),
])
def test_every_challenge_family_cools_the_account_down(
        client, mock_upstream, account, monkeypatch, detail, expected_status):
    _reject_chat_requirements(mock_upstream, monkeypatch, expected_status, json.dumps({"detail": detail}).encode())

    assert _post(client).status_code == expected_status
    assert cooldown.get_next_available(IDENTITY) > time.time()
    assert sum(circuit.get_circuit_stats().values()) >= 1


# ---------------------------------------------------------------------------
# 3. 账号暂时不可用 ≠ 封号
# ---------------------------------------------------------------------------

def test_account_unavailable_is_a_backoff_not_a_death(client, mock_upstream, account, monkeypatch):
    _reject_chat_requirements(mock_upstream, monkeypatch, 403, b'{"detail":"account unavailable"}')

    response = _post(client)

    assert response.status_code == 403
    assert circuit.get_circuit_stats().get("account_unavailable:403") == 1
    assert circuit.is_token_dead(IDENTITY) is False, "a temporary refusal was treated as a permanent ban"
    assert cooldown.get_next_available(IDENTITY) > time.time()
