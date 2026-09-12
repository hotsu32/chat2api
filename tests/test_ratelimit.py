"""ratelimit: 滑动窗口 + 内存有界性。

限流的 key 现在含用户提交的 email（登录账号桶），也就是**攻击者可控**，
所以除了窗口语义，还要锁住「不会被灌爆内存」这条。
"""
import time

import utils.ratelimit as ratelimit


def setup_function():
    ratelimit.reset()


def test_allow_counts_and_blocks():
    for _ in range(3):
        assert ratelimit.allow("k", 3) is True
    assert ratelimit.allow("k", 3) is False


def test_allow_zero_limit_means_unlimited():
    for _ in range(50):
        assert ratelimit.allow("k", 0) is True
    assert ratelimit.remaining("k", 0) == -1


def test_window_expiry_releases_quota():
    ratelimit.hit("k", window=1)
    assert ratelimit.over_limit("k", 1, window=1) is True
    time.sleep(1.1)
    assert ratelimit.over_limit("k", 1, window=1) is False


def test_over_limit_does_not_count():
    """只读探测不能消耗配额，否则「查一下还剩多少」本身就会把人锁死。"""
    for _ in range(10):
        assert ratelimit.over_limit("k", 1) is False
    assert ratelimit.allow("k", 1) is True


def test_read_paths_do_not_allocate_keys():
    """读未知 key 不得建条目。

    这是内存耗尽的入口：登录账号桶的 key 来自用户提交的 email，
    若读也建条目，灌一百万个不存在的邮箱就是一百万条常驻记录。
    """
    ratelimit.over_limit("ghost@example.com", 5)
    ratelimit.remaining("ghost2@example.com", 5)
    assert len(ratelimit._hits) == 0


def test_expired_key_is_dropped_not_kept_empty():
    ratelimit.hit("k", window=1)
    time.sleep(1.1)
    ratelimit.over_limit("k", 1, window=1)
    assert "k" not in ratelimit._hits


def test_key_count_stays_bounded_under_flood(monkeypatch):
    """灌入远超上限的不同 key，条目数仍然有界。"""
    monkeypatch.setattr(ratelimit, "_MAX_KEYS", 100)
    for i in range(1000):
        ratelimit.hit(f"signin:email:{i}@example.com")
    assert len(ratelimit._hits) <= 100


def test_flood_does_not_erase_an_active_bucket(monkeypatch):
    """灌 key 不能被用来把某个账号自己的失败计数挤掉从而绕过限流。

    必须真的灌过 ``_MAX_KEYS`` 才算数：不过上限则淘汰路径根本没跑，断言恒真。
    这个测试的上一版就是那样（45 个 key < 上限 50），空转了还声称性质成立；
    实测下改成真的灌过上限，受害者的桶**会**被逐出 —— 因为当时按「最久未命中」淘汰，
    而受害者的记录必然早于攻击者的洪水，等于攻击者点名就能清掉谁的计数。
    现在按「命中数最少」淘汰：噪声 key 各 1 次，受害者 5 次，洪水先自相残杀。
    """
    monkeypatch.setattr(ratelimit, "_MAX_KEYS", 50)
    victim = "signin:email:victim@example.com"
    for _ in range(5):
        ratelimit.hit(victim)
    for i in range(200):          # 远超上限，确保淘汰反复触发
        ratelimit.hit(f"signin:email:noise{i}@example.com")
    assert len(ratelimit._hits) <= 50
    assert victim in ratelimit._hits, "受害者的失败计数被洪水挤掉了 = 限流可被绕过"
    assert ratelimit.over_limit(victim, 5) is True


def test_prune_respects_the_longest_window_seen():
    """兜底清理窗口必须跟着实际配置走，不能写死 1 小时。

    ``SIGNIN_EMAIL_RATE_WINDOW`` 若配成大于 1 小时，写死 3600 会把窗口内**仍然有效**的
    失败记录当成过期清掉 —— 限流被静默稀释，而且没有任何迹象。
    """
    now = time.time()
    ratelimit.hit("short", window=3600)
    ratelimit.hit("long", window=7200)          # 让模块见过 2 小时窗口
    # 两条都挪到「1 小时前、但仍在 2 小时窗口内」
    ratelimit._hits["short"][0] = now - 5000
    ratelimit._hits["long"][0] = now - 5000
    ratelimit._prune_locked(now)
    assert "long" in ratelimit._hits, "2 小时窗口内的记录被按 1 小时清掉了"
    # 5000 秒前的记录对 1 小时窗口早就无效，留着只是晚一点回收，不影响正确性
    assert ratelimit.over_limit("short", 1, window=3600) is False
