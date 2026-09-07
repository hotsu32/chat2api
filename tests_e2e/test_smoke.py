"""Stage 0 smoke test — de-risk the harness: import app, serve login, hit /v1/models via mock."""


def test_login_page_serves(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"login" in resp.content.lower() or b"<html" in resp.content.lower()


def test_v1_models_served_via_mock(client, make_access_token):
    token = make_access_token(plan_type="plus")
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    payload = resp.json()
    ids = {m["id"] for m in payload["data"]}
    assert "gpt-5-5" in ids
    assert "gpt-4o" in ids
