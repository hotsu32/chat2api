"""启动期版本自检的隔离契约：没配置上游就什么都不发。

判据：``CHATGPT_BASE_URL=""`` 是「不要碰真实主机」的显式声明。旧实现
``configs.chatgpt_base_url_list or ["https://chatgpt.com"]`` 把它变成一次对真实
chatgpt.com 的 HTTPS 探测——只要启用 antiban 就自动发生，没有任何人要求。
本探测只做告警、不强制更新，跳过比偷偷出网更符合它的定位。

第二个用例是反向保险：配置了上游时必须照常探测，别把「不发」修成「永远不发」。
"""

import logging

import pytest

import utils.configs as configs
from utils.antiban import version_check


@pytest.fixture(autouse=True)
def _pinned_local_version(monkeypatch):
    monkeypatch.setattr(configs, "oai_client_version", "prod-test")
    monkeypatch.setattr(configs, "oai_client_build_number", 1)


def _explode_on_client(monkeypatch):
    """任何生产 Client 构造都算「发出了请求」。"""
    def _explode(*args, **kwargs):
        raise AssertionError("no HTTP client may be constructed when no base URL is configured")

    monkeypatch.setattr(version_check, "Client", _explode)


async def test_empty_base_url_list_skips_without_constructing_a_client(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(configs, "chatgpt_base_url_list", [])
    _explode_on_client(monkeypatch)

    is_drift, message = await version_check.probe_and_compare()

    assert is_drift is False
    assert message == "no-base-url"
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "no CHATGPT_BASE_URL configured" in blob
    assert "chatgpt.com" not in blob


async def test_configured_base_url_is_still_probed(monkeypatch):
    monkeypatch.setattr(configs, "chatgpt_base_url_list", ["http://upstream.example"])
    seen = {}

    class _Response:
        status_code = 200
        text = '<html data-build="prod-test">'

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, headers=None, **kwargs):
            seen["url"] = url
            return _Response()

        async def close(self):
            pass

        async def discard(self):
            pass

    monkeypatch.setattr(version_check, "Client", _Client)

    is_drift, message = await version_check.probe_and_compare()

    assert seen["url"] == "http://upstream.example/"
    assert (is_drift, message) == (False, "in-sync")
