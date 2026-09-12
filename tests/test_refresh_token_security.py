import pytest
from fastapi import HTTPException

from chatgpt import refreshToken
import utils.globals as globals


class _Response:
    status_code = 403
    text = "PRIVATE-UPSTREAM-BODY"
    headers = {"content-type": "text/html"}
    cookies = {}


class _Client:
    def __init__(self, **_kwargs):
        pass

    async def get(self, *_args, **_kwargs):
        return _Response()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_session_refresh_failure_does_not_log_or_return_credentials(monkeypatch, caplog):
    secret = "session-secret-material"
    monkeypatch.setattr(refreshToken, "Client", _Client)
    monkeypatch.setattr(refreshToken, "get_bound_proxy", lambda _token: None)
    monkeypatch.setattr(refreshToken, "proxy_url_list", [])
    monkeypatch.setattr(refreshToken, "persist_error_tokens", lambda: None)
    monkeypatch.setattr(refreshToken, "persist_refresh_map", lambda: None)
    monkeypatch.setattr(globals, "refresh_map", {})
    monkeypatch.setattr(globals, "error_token_list", [])

    with pytest.raises(HTTPException) as error:
        await refreshToken.fetch_session_access_token(secret)

    assert error.value.status_code == 503
    assert error.value.detail == "Account website session unavailable"
    assert secret not in caplog.text
    assert _Response.text not in caplog.text
    assert _Response.text not in str(globals.refresh_map)
