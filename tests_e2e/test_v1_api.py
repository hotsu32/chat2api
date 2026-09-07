"""Stage 3 — /v1 OpenAI-compatible API E2E.

Runs the real ChatService chain against the loopback mock upstream with
CONVERSATION_ONLY=true (skips sentinel/PoW) and a fake JWT access token
(verify_token direct-pass, no refresh network).
"""


def test_models_endpoint(client, make_access_token):
    token = make_access_token(plan_type="plus")
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["object"] == "list"
    ids = {m["id"] for m in payload["data"]}
    assert "gpt-5-5" in ids
    assert "gpt-4o" in ids
    for m in payload["data"]:
        assert m["object"] == "model"


def test_chat_completions_non_stream(client, make_access_token):
    token = make_access_token(plan_type="plus")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello, world"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] >= 1


def test_chat_completions_stream(client, make_access_token):
    token = make_access_token(plan_type="plus")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "Hello, world" in body
    assert "[DONE]" in body


def test_responses_non_stream(client, make_access_token):
    token = make_access_token(plan_type="plus")
    resp = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "gpt-4o", "input": "hello"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "response"
    assert data["output_text"] == "Hello, world"


def test_responses_stream_rejected(client, make_access_token):
    token = make_access_token(plan_type="plus")
    resp = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "gpt-4o", "input": "hello", "stream": True},
    )
    assert resp.status_code == 400
