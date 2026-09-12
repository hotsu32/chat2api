"""Per-proxy-node health tracking: EWMA latency + failure window + weighted pick."""
import time

import pytest

from utils import proxy_health as ph


@pytest.fixture(autouse=True)
def _reset_health():
    ph._health.clear()
    yield
    ph._health.clear()


def _now():
    return time.time()


def test_record_updates_ema_and_failures():
    ph.record("http://a", True, 100.0)
    ph.record("http://a", True, 300.0)
    # EMA: first sample seeds, second blends 0.2*300 + 0.8*100 = 140
    assert ph._health["http://a"]["ema"] == pytest.approx(140.0, abs=1e-6)
    ph.record("http://a", False, 500.0)
    assert len(ph._health["http://a"]["fails"]) == 1
    # a failed (timeout) request still feeds the latency signal
    assert ph._health["http://a"]["ema"] > 140.0


def test_record_ignores_empty_proxy():
    ph.record(None, True, 100.0)
    ph.record("", False, 100.0)
    assert ph._health == {}


def test_weighted_choice_empty_and_single():
    assert ph.weighted_choice([]) is None
    assert ph.weighted_choice(["only"]) == "only"


def test_weighted_choice_returns_member():
    candidates = ["http://a", "http://b", "http://c"]
    for _ in range(100):
        assert ph.weighted_choice(candidates) in candidates


def test_failures_downweight_node():
    ph.record("http://slow", False, 0.0)
    ph.record("http://slow", False, 0.0)
    ph.record("http://slow", False, 0.0)
    # 3 recent failures -> fail_factor = 1.0 - 3*0.3 = 0.1 (above floor 0.05)
    w_slow = ph._weight("http://slow", _now(), None)
    w_unknown = ph._weight("http://fresh", _now(), None)
    assert w_slow < w_unknown
    assert w_slow == pytest.approx(0.1, abs=1e-6)


def test_latency_downweights_slow_node():
    ph.record("http://fast", True, 200.0)
    ph.record("http://slow", True, 2000.0)
    # relative latency: fast keeps full weight, slow scales by 200/2000 = 0.1
    w_fast = ph._weight("http://fast", _now(), 200.0)
    w_slow = ph._weight("http://slow", _now(), 200.0)
    assert w_fast == pytest.approx(1.0, abs=1e-6)
    assert w_slow == pytest.approx(0.1, abs=1e-6)


def test_weight_never_hits_zero():
    for _ in range(10):
        ph.record("http://dead", False, 60000.0)
    w = ph._weight("http://dead", _now(), None)
    assert w >= ph._FLOOR
    assert w > 0.0
