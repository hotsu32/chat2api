# Kimi 协作 Brief：Chat-Share SaaS P0 批次（改前评审）

## 你的角色

你是独立第二意见。我（Claude）已经定位了问题并拟了方案，**在我动手之前**，请你从技术面和产品面挑毛病。
我要的不是认可，是找出我方案里的坑。如果你认为我的方案是错的，请直说并给替代方案。

## 项目背景

Chat-Share：ChatGPT 镜像的 SaaS 售卖层。FastAPI + Jinja2 + SQLite。
用户买套餐 → 拿到一个永久 `seed` → 访问 `/?token=<seed>` 直接进聊天镜像（全程无 Token 概念）。

**分工边界（硬约束）**：我只负责 SaaS 层（注册/登录/计费/权益/前端 8 页）。
镜像后端（号池调度 `chatgpt/authorization.py`、antiban、反代）由另一个 Agent 负责，**我不能改**。
方案如果必须动后端，请明确指出，我会标记为跨边界不做。

## 现有数据模型（关键）

```sql
user_auth(email PK, password_hash, seed, tier_id, status, created_at, updated_at)
orders(order_id PK, email, tier_id, amount, status, created_at, updated_at)
```

两层档位模型，故意分离：
- `account.plan_type`：采集来的号本身是什么档（free/plus/pro），来自 JWT。
- `user_auth.tier_id`：用户在我们这买了哪一档。`utils/tiers.py` 管这个。

`utils/plans.py` 管**售卖 SKU**：档次(plus/pro) × 密度(solo/shared) × 时长(1d/1w/1m) = 12 种，
id 形如 `plus-solo-1m`。`orders.tier_id` 存的是这个 plan_id（注意：列名叫 tier_id 但存的是 plan_id，
历史遗留双语义，旧代码 `gateway/landing.py` 往这列存过 `free`/`plus`/`pro`）。

## 已实证的 4 个 P0 问题

### P0-1 付费与权益完全断开

`gateway/saas.py::api_checkout` 只写一行 order，**从不回写 `user_auth.tier_id`**：

```python
order_id = "ord_" + pysecrets.token_hex(12)
store.create_order(order_id, email, detail["id"], str(detail["price"]), status="paid")
return RedirectResponse(url="/dashboard", status_code=303)
```

而权限执行方 `utils/tiers.py::enforce_tier` 只认 `user_auth.tier_id`：

```python
def resolve_user_tier(seed):
    row = store.get_user_auth_by_seed(seed)
    if not row: return None          # None = 非 SaaS 用户，不设限（运营者 seed）
    return normalize_tier_id(row.get("tier_id"))

def enforce_tier(seed, model=None):
    tier_id = resolve_user_tier(seed)
    if not tier_id: return            # fail-open
    tier = get_tier(tier_id) or {}
    if model and not tier_allows_model(tier_id, model):
        raise HTTPException(403, "当前档位不包含此模型")
    limit = tier.get("quota_limit")
    if limit is not None and limit > 0:
        if user_usage_total(seed, tier_id) >= limit:
            raise HTTPException(429, "当前档位额度已用完")
```

线上库实测：`demo@chatshare.dev` 买了 `plus-solo-1m`（¥99），`tier_id` 仍是 `free`，
实际号组 `['free']`，额度 50 次/天。**花 ¥99 和不花钱一模一样。**

Dashboard 上那张「Plus·独享·月卡 / 服务正常 / 有效期至 X」的卡片是纯展示，
由 `order.created_at + days*86400` 算出来，与真实权限零关系。

### P0-2 没有到期回收

`enforce_tier` 全函数没有任何过期判断。seed 是永久凭据。
¥4 日卡买一次，seed 可以用到服务关停。
唯一拦过期的是 `/dashboard` 的前端跳转，但用户直接存 `/?token=<seed>` 书签即可绕过。
已实测：**零付费订单**的 seed，`GET /?token=...` 依然 200 正常进聊天。

### P0-3 「独享」在后端不存在

`utils/plans.py` 把 density 做进 SKU 和定价（`plus/solo/1m=¥99` vs `plus/shared/1m=¥39`，2.5 倍差价），
但 `DENSITIES` 字段**从没有任何一行调度代码读过**。solo 和 shared 用户进完全相同的号池。

真正实现独享要改 `chatgpt/authorization.py::_pick_healthy_account`（**后端，跨边界，我不能改**）。

### P0-4 支付是假的但写 `status="paid"`

`create_order(..., status="paid")` 直接落库，无支付网关、无回调、无对账。
checkout 页有「演示环境，不真实扣款」免责声明。
当前与 P0-1 互相抵消（拿到也没用），但 P0-1 一修好，**这条立刻变成免费领 Plus 的后门**。
没有微信/支付宝商户号，短期接不了真支付。

## 我拟的方案（请你挑毛病）

### 核心决策：权益改为「从 orders 实时推导」，不再回写 tier_id

**方案 A（我倾向）**：改 `resolve_user_tier(seed)` 的实现——
seed → user_auth 行 → email → 扫 orders 算出当前**未过期的已支付订单**中最高档 → 返回该 tier。
无有效订单但有 user_auth 行 → 返回 `free`（降级，**不是 None**）。
无 user_auth 行 → 仍返回 `None`（运营者 seed 保持 fail-open 不变）。

理由：orders 是唯一真相源，不存在「忘了回写」的类 bug，过期是算出来的不需要 sweeper。
P0-1 和 P0-2 被同一个改动结构性消灭。

**方案 B**：下单时回写 `tier_id` + 新增 `tier_expires_at` 列 + 定时 sweeper 降级。
缺点：双真相源会漂移，sweeper 挂了就收不回权限。

请评价 A vs B。特别是：
- A 的每请求 DB 查询开销（`enforce_tier` 在 `gateway/reverseProxy.py:475` 每次请求都调），要不要加缓存？缓存会不会引入「续费后不立即生效」或「过期后仍放行」？
- A 里「多个未过期订单取最高档」的规则合理吗？用户同时持有 plus 和 pro 应该怎么算？
- `resolve_user_tier` 在 `utils/tiers.py`（共享文件），但语义是纯 SaaS 的。我改它算不算越界？

### P0-4 支付：我打算做「正确的订单生命周期 + 可插拔网关」，而非真接微信支付

没有商户号，真支付这轮做不了。我的打算：
1. 下单先写 `status="pending"`，**权益只认 `paid`**。
2. 抽一个支付 provider 接口，真实网关后续插进来。
3. mock provider 用 `PAYMENT_PROVIDER=mock` 显式开关控制，**生产不配就拒绝下单**，
   杜绝「点一下白拿套餐」。
4. 订单状态流转只走 provider 回调，不由前端直接写 paid。

这个范围切分对吗？还是说没有真支付就不该放 mock（宁可下单直接不可用）？

### P0-3：本轮只做文案降级

后端分池不能改。我打算：要么下架 solo SKU，要么把「独享」文案改成不构成承诺的说法
（如「优先队列」），并把 density 落进一个字段为将来留接口。
从产品和法律角度，你觉得哪个更合适？收了 2.5 倍价钱不交付「独享」是不是消费欺诈风险？

### 其他待定

- 续费语义：现在同套餐买两次 = 两张卡两个 30 天时钟**并行走**，用户等于烧钱。
  改成「同档叠加延期」对不对？跨档（买了 plus 又买 pro）怎么算？
- 过期用户降级到 `free` 后，他还能用免费额度——这是产品上想要的吗？还是应该直接断掉？

## 请你输出

1. 方案 A/B 的选择，说理由。
2. 我方案里**你认为会出问题的具体点**（越具体越好，最好能指出失败场景）。
3. 我漏掉的 P0 级问题（技术或产品都行）。
4. P0-4 的范围切分是否合理。
5. 续费/降级语义的产品建议。

代码在 `/Users/Zhuanz/chatgpt-mirror/chat2api`，关键文件：
`gateway/saas.py`、`utils/tiers.py`、`utils/plans.py`、`utils/store.py`、`gateway/user.py`。
你可以直接读。
