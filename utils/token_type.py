"""Token-type classification — single source of truth.

``utils.routing`` and ``utils.store`` both need to classify a token string, but
``store`` must stay free of the routing/globals import chain. This pure module
(no imports) is the shared canonical implementation; both callers re-export it.

Rules:
  - SessionToken: ``sess-`` prefix (chatgpt.com web session cookie, stored with prefix)
  - AccessToken: ``eyJhbGciOi`` or ``fk-`` prefix (JWT / fakeopen token)
  - RefreshToken: legacy 45-char, or new Auth0 ``rt_`` prefix (len >= 60)
  - CustomToken: anything else non-empty
  - Unknown: empty input
"""


def detect_token_type(token: str) -> str:
    if not token:
        return "Unknown"
    if token.startswith("sess-"):
        return "SessionToken"
    if token.startswith("eyJhbGciOi") or token.startswith("fk-"):
        return "AccessToken"
    # New Auth0 RefreshToken: rt_<nonce>.<payload>, len >= 60 to count as valid.
    if token.startswith("rt_") and len(token) >= 60:
        return "RefreshToken"
    if len(token) == 45:
        return "RefreshToken"
    return "CustomToken"
