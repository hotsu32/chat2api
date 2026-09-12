"""identity: JWT decode + session synthesis (anonymization). Pure, no network."""
from gateway.identity import build_session, decode_account_identity, decode_jwt_payload


def test_decode_jwt_payload_valid(make_jwt):
    claims = {"sub": "auth0|abc", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
    assert decode_jwt_payload(make_jwt(claims))["sub"] == "auth0|abc"


def test_decode_jwt_payload_empty_or_malformed():
    assert decode_jwt_payload("") == {}
    assert decode_jwt_payload("no-dot-here") == {}
    assert decode_jwt_payload("a.!notb64!") == {}


def test_decode_account_identity(make_access_token):
    token = make_access_token(plan_type="plus", email="owner@example.com", name="Owner")
    ident = decode_account_identity(token)
    assert ident["plan_type"] == "plus"
    assert ident["real_email"] == "owner@example.com"
    assert ident["nickname"] == "Owner"


def test_decode_account_identity_unknown_plan(make_jwt):
    # A JWT with no openai auth/profile claims -> plan_type fallback "unknown".
    ident = decode_account_identity(make_jwt({"sub": "x"}))
    assert ident["plan_type"] == "unknown"
    assert ident["real_email"] == ""
    assert ident["nickname"] == ""


def test_build_session_anonymized(make_access_token):
    token = make_access_token(plan_type="pro", account_id="acc-9",
                              email="owner@example.com", name="Real Owner")
    s = build_session(token, anonymize=True)
    assert s["user"]["name"] == "ChatGPT"
    assert s["user"]["email"] == ""
    assert s["account"]["planType"] == "pro"
    assert s["account"]["id"] == "acc-9"
    assert s["accessToken"] == ""


def test_build_session_not_anonymized(make_access_token):
    token = make_access_token(email="owner@example.com", name="Real Owner")
    s = build_session(token, anonymize=False)
    assert s["user"]["name"] == "Real Owner"
    assert s["user"]["email"] == "owner@example.com"


def test_build_session_empty():
    assert build_session("") == {}
    assert build_session("not-a-jwt") == {}
