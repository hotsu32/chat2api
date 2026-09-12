"""产品入口 ``/v1/chat/completions`` 的试用账：真实 app + 真实 ChatService + mock 上游。

``tests/test_v1_trial_lifetime.py`` 用替身证明生命周期接线；这里补的是替身证明不了的
那一半：**成功完成信号取自真实格式化器的输出**。``chatFormat.stream_response`` 生成的
OpenAI 兼容分片长什么样（``finish_reason`` 落在哪、上游报错时有没有终止分片）决定了
扣费判据对不对，只有跑真实链路才能验。

覆盖：
  - 流式成功 → 扣 1 次；
  - 非流式成功 → 扣 1 次；
  - 上游只给 in_progress、没有终止分片（真实形状的空/截断流）→ 不扣；
  - 三次用完 → 第 4 次 402 且不打上游。

判据全部读 ``utils.trials`` 的真实 SQLite 账面，不 mock 余额。
"""
import json

import utils.globals as globals
import utils.store as store
import utils.trials as trials

EMAIL = "v1-trial-e2e@example.test"
SEED = "seed-v1-trial-e2e"


def _register_trial_user(make_access_token):
    """建真实试用账号 + Plus 账号，并让 seed 走真实路由（不预置绑定）。"""
    store.create_user_with_trial(
        EMAIL, password_hash="pbkdf2_sha256$1$00$00", seed=SEED, status="active",
        trial_tier=trials.TRIAL_TIER, trial_total=trials.SIGNUP_TRIAL_COUNT,
    )
    store.upsert_user(SEED, status="active")
    token = make_access_token(plan_type="plus", account_id="acc-v1-trial")
    store.upsert_account(token, plan_type="plus", status="healthy")
    globals.token_list.append(token)
    globals.seed_map[SEED] = {"token": "", "plan_type": "", "conversations": []}
    return token


def _truncated_stream_body():
    """只有 in_progress 帧、没有 finished_successfully / [DONE] 的上游流。"""
    frame = json.dumps({
        "message": {
            "id": "msg-1",
            "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": [""]},
            "status": "in_progress",
            "metadata": {},
        },
        "conversation_id": "conv-1",
    })
    return ("data: " + frame + "\n\n").encode("utf-8")


def _post(client, seed, stream):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {seed}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
              "stream": stream},
    )


def test_stream_generation_charges_exactly_one_trial(client, make_access_token):
    _register_trial_user(make_access_token)

    resp = _post(client, SEED, stream=True)

    assert resp.status_code == 200
    assert "Hello, world" in resp.text
    assert "[DONE]" in resp.text
    state = trials.trial_state(EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (1, 2, 0)
    # 试用身份按 Plus 档分号（不借 Free 号），绑定落在 users 表里
    user_row = store.get_user(SEED)
    assert user_row["plan_type"] == "plus"
    assert user_row["current_account"]


def test_non_stream_generation_charges_exactly_one_trial(client, make_access_token):
    _register_trial_user(make_access_token)

    resp = _post(client, SEED, stream=False)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello, world"
    state = trials.trial_state(EMAIL)
    assert (state["used"], state["remaining"]) == (1, 2)


def test_truncated_upstream_stream_is_not_charged(client, mock_upstream, make_access_token):
    """上游没给出终止分片：不是一次成功的生成，额度必须原样退回。"""
    _register_trial_user(make_access_token)
    mock_upstream.conversation_sse = _truncated_stream_body()

    resp = _post(client, SEED, stream=True)

    assert resp.status_code == 200
    state = trials.trial_state(EMAIL)
    assert (state["used"], state["remaining"], state["reserved"]) == (0, 3, 0)


def test_exhausted_trial_is_denied_without_touching_upstream(client, mock_upstream,
                                                             make_access_token):
    _register_trial_user(make_access_token)
    for _ in range(trials.SIGNUP_TRIAL_COUNT):
        assert trials.settle(trials.reserve(SEED), SEED) is True
    before = len(mock_upstream.records)

    resp = _post(client, SEED, stream=False)

    assert resp.status_code == 402
    assert len(mock_upstream.records) == before
    assert trials.trial_state(EMAIL)["used"] == trials.SIGNUP_TRIAL_COUNT


def test_paid_subscription_is_not_charged_to_trial(client, mock_upstream, make_access_token):
    """有效付费订单：按订单档次走，不占试用账。"""
    _register_trial_user(make_access_token)
    store.create_order("ord-v1-trial", EMAIL, "plus-shared-1m", "39", status="pending")
    store.activate_order("ord-v1-trial", 2_000_000_000)

    resp = _post(client, SEED, stream=False)

    assert resp.status_code == 200
    state = trials.trial_state(EMAIL)
    assert (state["used"], state["remaining"]) == (0, 3)
