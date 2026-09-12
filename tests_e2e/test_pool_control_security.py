"""Legacy pool controls must share the admin authorization boundary."""

import pytest

import utils.globals as globals


@pytest.fixture
def admin_secret(monkeypatch):
    from gateway import admin
    monkeypatch.setattr(admin, "admin_password", "test-admin-secret")
    return {"Authorization": "Bearer test-admin-secret"}


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/tokens", {}),
        ("post", "/tokens/upload", {"data": {"text": "new-account"}}),
        ("post", "/tokens/clear", {}),
        ("post", "/tokens/error", {}),
        ("get", "/tokens/add/secret-in-url", {}),
        ("post", "/tokens/add", {"data": {"text": "new-account"}}),
        ("post", "/seed_tokens/clear", {}),
    ],
)
def test_pool_controls_reject_unauthorized_requests_without_mutation(
        client, method, path, kwargs):
    globals.token_list[:] = ["existing-account"]
    globals.error_token_list[:] = ["existing-error"]
    globals.seed_map.clear()
    globals.seed_map["existing-seed"] = {
        "token": "existing-account", "plan_type": "plus", "conversations": []
    }

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code in (401, 503)
    assert globals.token_list == ["existing-account"]
    assert globals.error_token_list == ["existing-error"]
    assert "existing-seed" in globals.seed_map


def test_authorized_add_uses_request_body_and_legacy_url_route_is_disabled(
        client, admin_secret):
    globals.token_list.clear()

    response = client.post(
        "/tokens/add", data={"text": "body-account"}, headers=admin_secret
    )
    assert response.status_code == 200
    assert globals.token_list == ["body-account"]

    legacy = client.get("/tokens/add/url-account", headers=admin_secret)
    assert legacy.status_code == 410
    assert globals.token_list == ["body-account"]
