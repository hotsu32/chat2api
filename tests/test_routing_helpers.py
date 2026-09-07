"""routing helpers: token detection, masking, time formatting, group assignment."""
import pytest

import utils.globals as globals
import utils.routing as routing


@pytest.fixture(autouse=True)
def _reset_globals(db):
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.conversation_map.clear()
    globals.refresh_map.clear()
    globals.fp_map.clear()
    globals.routing_config.clear()
    globals.antiban_dead_tokens.clear()
    yield
    globals.token_list.clear()
    globals.error_token_list.clear()
    globals.seed_map.clear()
    globals.conversation_map.clear()
    globals.refresh_map.clear()
    globals.fp_map.clear()
    globals.routing_config.clear()
    globals.antiban_dead_tokens.clear()


@pytest.mark.parametrize("token,expected", [
    ("eyJhbGciOiJIUzI1NiJ9.x.y", "AccessToken"),
    ("fk-abcdef", "AccessToken"),
    ("r" * 45, "RefreshToken"),
    ("rt_" + "a" * 60, "RefreshToken"),
    ("sess-abc123", "SessionToken"),
    ("whatever", "CustomToken"),
    ("", "Unknown"),
])
def test_detect_token_type(token, expected):
    assert routing.detect_token_type(token) == expected


def test_mask_token():
    assert routing.mask_token("") == ""
    assert routing.mask_token("short") == "short"  # <= 12 chars, unchanged
    assert routing.mask_token("a" * 30) == f"{'a' * 6}...{'a' * 4}"


def test_format_refresh_time():
    assert routing.format_refresh_time(None) == "-"
    assert routing.format_refresh_time(0) == "-"  # falsy -> "-"
    assert routing.format_refresh_time(1700000000) == "2023-11-14T22:13:20Z"


def test_get_routing_config_defaults():
    cfg = routing.get_routing_config()
    assert cfg["proxies"] == []
    assert cfg["bindings"] == {}


def test_build_group_assignments():
    tokens = ["t1", "t2", "t3", "t4"]
    proxies = [{"name": "ip1", "proxy_url": "http://1"}, {"name": "ip2", "proxy_url": "http://2"}]
    result = routing.build_group_assignments(tokens, proxies, group_size=2)
    assert len(result["proxies"]) == 2
    assert len(result["groups"]) == 2
    # group A -> t1,t2 ; group B -> t3,t4
    assert result["bindings"]["t1"]["proxy_url"] == "http://1"
    assert result["bindings"]["t3"]["proxy_url"] == "http://2"


def test_get_dashboard_payload_empty():
    payload = routing.get_dashboard_payload()
    assert payload["summary"]["accounts_total"] == 0
    assert payload["summary"]["users_total"] == 0
    assert payload["accounts"] == []
    assert payload["users"] == []
    assert isinstance(payload["alerts"], list)
