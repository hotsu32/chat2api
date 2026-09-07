"""mock upstream contract: prove the E2E stand-in serves canned chatgpt.com payloads."""
import json
import urllib.request


def test_mock_upstream_me(mock_upstream):
    with urllib.request.urlopen(f"{mock_upstream}/backend-api/me") as r:
        data = json.loads(r.read())
    assert data["email"] == "owner@example.com"


def test_mock_upstream_sentinel(mock_upstream):
    with urllib.request.urlopen(f"{mock_upstream}/backend-api/sentinel/chat-requirements") as r:
        data = json.loads(r.read())
    assert data["token"] == "req-token"
