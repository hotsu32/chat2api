"""store 层的密码版本（会话吊销的实现基础）。

``pw_version`` 是「改了密码就把别的设备踢下线」的唯一机制，所以这里锁死三件事：
单调递增、老库能平滑补列、以及**不能**被 ``upsert_user_auth`` 绕过写回旧值。
"""
import sqlite3

import pytest

import utils.store as store

EMAIL = "pw@example.com"
HASH = "pbkdf2_sha256$1$00$00"


def _mkuser(email=EMAIL):
    store.upsert_user_auth(email, password_hash=HASH, seed="seed-" + email, status="active")


def test_new_user_starts_at_version_one(db):
    _mkuser()
    assert store.get_user_auth(EMAIL)["pw_version"] == 1


def test_bump_increments_monotonically(db):
    _mkuser()
    assert store.bump_pw_version(EMAIL) == 2
    assert store.bump_pw_version(EMAIL) == 3
    assert store.get_user_auth(EMAIL)["pw_version"] == 3


def test_bump_is_visible_through_seed_lookup(db):
    """按 seed 查出来的行也要带版本 —— 否则走 seed 的调用方看到的是过期视图。"""
    _mkuser()
    store.bump_pw_version(EMAIL)
    assert store.get_user_auth_by_seed("seed-" + EMAIL)["pw_version"] == 2


def test_bump_unknown_user_raises(db):
    """吊销一个不存在的账号必须报错，不能静默成功。

    静默返回等于告诉调用方「会话已吊销」，而实际上一条都没吊销。
    """
    with pytest.raises(store.StoreError):
        store.bump_pw_version("nobody@example.com")
    with pytest.raises(store.StoreError):
        store.bump_pw_version("")


def test_upsert_cannot_write_pw_version(db):
    """``upsert_user_auth`` 不接受 pw_version —— 允许传值迟早会有人写回旧版本，
    把刚吊销的会话又放回来。改版本只有 ``bump_pw_version`` 一个入口。
    """
    _mkuser()
    store.bump_pw_version(EMAIL)
    store.upsert_user_auth(EMAIL, pw_version=1, password_hash=HASH)
    assert store.get_user_auth(EMAIL)["pw_version"] == 2


def test_legacy_db_without_column_is_migrated(db, tmp_path, monkeypatch):
    """老库（user_auth 无 pw_version 列）升级后：补列成功，且老行按版本 1 处理。

    这是本次 schema 变更唯一有真实风险的路径 —— 线上库已经存在，
    ``CREATE TABLE IF NOT EXISTS`` 不会补列，只有 ALTER 能救。
    """
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        "CREATE TABLE user_auth ("
        " email TEXT PRIMARY KEY, password_hash TEXT, seed TEXT,"
        " tier_id TEXT, status TEXT, created_at INTEGER, updated_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO user_auth (email, password_hash, seed, tier_id, status)"
        " VALUES (?,?,?,?,?)", (EMAIL, HASH, "seed-legacy", "free", "active"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(store, "_DB_PATH", str(legacy))
    monkeypatch.setattr(store, "_INITIALIZED", False)
    store.init_db()

    row = store.get_user_auth(EMAIL)
    assert row["pw_version"] == 1          # 老行 == 「从未改过密」，无需回填
    assert row["seed"] == "seed-legacy"    # 补列不得动原有数据
    assert store.bump_pw_version(EMAIL) == 2
