"""usage: in-memory counting + periodic flush, double granularity (seed + account)."""
import pytest

import utils.usage as usage


@pytest.fixture(autouse=True)
def _clear_pending(db):
    usage._pending.clear()
    yield
    usage._pending.clear()


def test_record_buffers_in_memory(db):
    usage.record_usage("seed-a", "acct-1", "conversation")
    assert usage.pending_count() == 1
    assert usage.user_usage("seed-a") == 0  # not yet flushed to SQLite


def test_flush_persists_and_resets(db):
    usage.record_usage("seed-a", "acct-1", "conversation")
    usage.record_usage("seed-a", "acct-1", "image")
    n = usage.flush_usage()
    assert n == 2
    assert usage.pending_count() == 0
    assert usage.user_usage("seed-a") == 2
    assert usage.account_usage("acct-1") == 2


def test_double_granularity(db):
    usage.record_usage("seed-a", "acct-1", "conversation")
    usage.record_usage("seed-a", "acct-2", "image")
    usage.record_usage("seed-b", "acct-1", "audio")
    usage.flush_usage()
    assert usage.user_usage("seed-a") == 2
    assert usage.user_usage("seed-b") == 1
    assert usage.account_usage("acct-1") == 2
    assert usage.account_usage("acct-2") == 1


def test_record_ignores_empty(db):
    usage.record_usage("", "", "conversation")   # no seed/account
    usage.record_usage("seed-a", "acct-1", "")   # no kind
    assert usage.pending_count() == 0


def test_flush_empty(db):
    assert usage.flush_usage() == 0
