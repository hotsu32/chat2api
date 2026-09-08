"""真号冒烟（本地，无网络依赖）。

用 3 个真实账号（1 free + 2 plus）验证 Stage 5 antiban 各子系统的真实行为。
关键：plan_type 全部来自 SQLite 真相源（真实账号），不 monkeypatch store.get_account。

安全约束：
  - 全程不回显 token 明文，只打印 masked 前缀。
  - 所有落盘副作用（bucket._persist / circuit._persist_dead / update_single_binding /
    fingerprint._persist_fp）一律 stub 为 no-op，保证冒烟可重复且不污染 SQLite / JSON。
"""

import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import utils.configs as configs
import utils.globals as globals
import utils.store as store
from utils.antiban import bucket, circuit, concurrency, fingerprint
import chatgpt.authorization as authorization

configs.enable_antiban = True

# --- 屏蔽落盘副作用（冒烟只测内存逻辑 + 真实 plan_type，不写库/文件） ---
bucket._persist = lambda: None
bucket.update_single_binding = lambda *a, **k: None
circuit._persist_dead = lambda: None
fingerprint._persist_fp = lambda: None


def mask(t):
    if not t:
        return "(empty)"
    return f"{t[:8]}...{t[-4:]}" if len(t) > 12 else t


def identify():
    """从真实 accounts 表按 plan_type 分组。"""
    free, plus = [], []
    for t in globals.token_list:
        pt = (store.get_account(t) or {}).get("plan_type")
        if pt == "free":
            free.append(t)
        elif pt == "plus":
            plus.append(t)
    return free, plus


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}  {detail}")


def main():
    print("=" * 70)
    print("真号冒烟：1 free + 2 plus，plan_type 读自 SQLite 真相源")
    print("=" * 70)

    free, plus = identify()
    check("导入落库：恰好 1 free + 2 plus",
          len(free) == 1 and len(plus) == 2,
          f"free={len(free)} plus={len(plus)}")

    # ---- 1) plan_type 正确性（分池/限流/降智的驱动源） ----
    print("\n--- 1) plan_type 落库正确性 ---")
    for t in free:
        check("free 号 plan_type == 'free'",
              authorization._account_tier(t) == "free",
              f"token={mask(t)}")
    for t in plus:
        check("plus 号 plan_type == 'plus'",
              authorization._account_tier(t) == "plus",
              f"token={mask(t)}")

    # ---- 2) 半专属分池：free/plus 不串池（真实 store plan_type） ----
    print("\n--- 2) 半专属分池：free/plus 不串池 ---")
    # 手动 seed 2 个空桶（模拟路由里的 2 个代理）；plan_type 由 assign_account 内部
    # 从真实 store 读取，非 monkeypatch。
    globals.antiban_bucket["buckets"] = {
        "bkt::p1": {"proxy_url": "http://p1", "proxy_name": "p1", "group": "",
                    "accounts": [], "last_request_at": {}, "status": "healthy",
                    "degraded_until": 0, "created_at": 0, "plan_type": None},
        "bkt::p2": {"proxy_url": "http://p2", "proxy_name": "p2", "group": "",
                    "accounts": [], "last_request_at": {}, "status": "healthy",
                    "degraded_until": 0, "created_at": 0, "plan_type": None},
    }
    globals.antiban_bucket["account_index"] = {}

    b_free = bucket.assign_account(free[0])
    b_plus = bucket.assign_account(plus[0])
    check("free 号成功分配", b_free is not None, f"bucket={b_free}")
    check("plus 号成功分配", b_plus is not None, f"bucket={b_plus}")
    check("free/plus 不落同一桶", b_free != b_plus, f"free->{b_free} plus->{b_plus}")
    if b_free:
        check("free 桶定型为 free",
              globals.antiban_bucket["buckets"][b_free].get("plan_type") == "free")
    if b_plus:
        check("plus 桶定型为 plus",
              globals.antiban_bucket["buckets"][b_plus].get("plan_type") == "plus")

    # ---- 3) 模拟封禁 failover：mark_dead(plus1) → 不再选它 ----
    print("\n--- 3) 模拟 plus 封禁 failover ---")
    plus1, plus2 = plus[0], plus[1]
    circuit.mark_dead(plus1, "smoke: simulated ban")
    check("plus1 已熔断", circuit.is_token_dead(plus1) is True)
    check("_account_is_usable(plus1) == False",
          authorization._account_is_usable(plus1) is False)

    # 反复采样 _pick_healthy_account，观察是否会把 dead 号重选出来。
    samples = 60
    picked = [authorization._pick_healthy_account(plan_types=["plus"]) for _ in range(samples)]
    dead_picks = sum(1 for t in picked if t == plus1)
    check("failover 不再选 dead 号",
          dead_picks == 0,
          f"{samples} 次采样中选到 dead 号 {dead_picks} 次 "
          f"(dead={mask(plus1)} healthy={mask(plus2)})")
    check("failover 选中唯一健康 plus 号",
          all(t == plus2 for t in picked),
          f"healthy 候选={mask(plus2)}")

    # ---- 4) 并发上限分层（真实 plan_type） ----
    print("\n--- 4) 并发上限分层 ---")
    check("free 号 limit == free_account_max_concurrency(10)",
          concurrency._resolve_limit(free[0]) == configs.free_account_max_concurrency,
          f"limit={concurrency._resolve_limit(free[0])}")
    check("plus 号 limit == account_max_concurrency(5)",
          concurrency._resolve_limit(plus1) == configs.account_max_concurrency,
          f"limit={concurrency._resolve_limit(plus1)}")

    async def _acquire_sanity():
        ok1 = await concurrency.acquire(plus2)
        ok2 = await concurrency.acquire(plus2)
        concurrency.release(plus2)
        concurrency.release(plus2)
        return ok1 and ok2
    check("plus 号 acquire/release 平衡", asyncio.run(_acquire_sanity()))

    # ---- 5) 指纹稳定（同号不漂移） ----
    print("\n--- 5) 指纹稳定（同号不漂移） ---")
    fp_a = fingerprint.ensure_extended(plus2)
    fp_b = fingerprint.ensure_extended(plus2)
    required = ("screen", "hardware_concurrency", "webgl", "canvas_hash",
                "font_list_hash", "audio_fp_hash", "timezone")
    check("扩展指纹字段齐全",
          all(k in fp_a for k in required),
          f"missing={[k for k in required if k not in fp_a]}")
    check("同号指纹稳定不漂移",
          fp_a.get("canvas_hash") == fp_b.get("canvas_hash")
          and fp_a.get("webgl", {}).get("renderer") == fp_b.get("webgl", {}).get("renderer"))

    # ---- 汇总 ----
    print("\n" + "=" * 70)
    fails = [r for r in RESULTS if not r[1]]
    print(f"SUMMARY: total={len(RESULTS)} pass={len(RESULTS) - len(fails)} fail={len(fails)}")
    if fails:
        print("FAILED:")
        for name, _ok, detail in fails:
            print(f"  - {name}  {detail}")
    print("=" * 70)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
