# 车队管理 + 完整生态 SPEC

> 目标：在现有 chat2api fork（已含 admin 面板、antiban、harvester、session-sticky）之上，
> 补全「等级号池 + 健康检查 + 自主切换 + 匿名化 + 用量统计」这一层，对标 xyhelper 小强车队。
> 同时把**账号域持久化全量统一到 SQLite**，为后续扩展留干净地基。
> 本文档冻结需求与决策，作为实现与验收的唯一依据。

## 1. 冻结的需求（grill 结论）

| 维度 | 决策 |
|------|------|
| 轮换模型 | B：用户粘性 + 等级号池。权益绑「等级」，不绑具体账号；粘性绑定当前账号；号挂了自主切换，历史跟号走（找不回，但诚实告知） |
| 切换触发 | 自动检测 + 提示切换：健康检查发现异常 → 前端提示 → 用户点切换 |
| 等级划分 | 按 `plan_type` 自动分池（free / plus / pro / team / unknown） |
| 匿名化 | 通用身份 + 显示等级：`name="ChatGPT"`、隐藏 email，保留 planType 显示等级 |
| 用量统计 | 每用户 + 每账号 双粒度 |
| 持久化 | **SQLite 全量统一（账号域）**，冷启动一次性迁移 JSON → SQLite，旧 JSON 转为只读回退 |

核心数据流：

```
用户(SeedToken) ──绑定──> 等级(plan_type) ──包含──> 号池 ──粘性──> 当前健康账号
                             ▲                                    │ 挂了
                             └──────────── 自主切换 ──────────────┘ (历史留旧号)
```

## 2. 现状盘点（已存在，可复用）

- `gateway/admin.py`（46KB）：完整 admin 后端——ADMIN_PASSWORD + IP 白名单 + 限流 + 登录锁定 +
  HttpOnly cookie；账号导入/删除/刷新、代理绑定、日志、harvester OAuth PKCE 采集 + session cookie 导入。
- `templates/account_proxy_bindings.html`（124KB SPA）：已有 dashboard（账号/代理/分组/告警）。
- `utils/routing.py`：`routing_config`、`get_dashboard_payload`、`detect_token_type`、`update_account_meta`、
  `build_group_assignments`、`sync_bindings_to_fp`。
- `utils/antiban/`：bucket / circuit（429/403 熔断标 dead）/ cooldown / account_risk / geo / guard。
- `chatgpt/session_sticky.py`：SQLite（WAL/短连接/threading lock）连接模式先例。
- `utils/token_parser.py`：`mask_token`、`parse_file`。
- `gateway/identity.py`：`decode_jwt_payload` + `build_session`。
- `chatgpt/authorization.py`：`get_req_token`、`verify_token`。
- 后台任务先例：`api/chat2api.py` 的 `@app.on_event("startup")` + `scheduled_refresh`。

## 3. 全量迁移边界（关键决策）

### 3.1 迁入 SQLite（账号域 + 车队域，单一 `data/chat2api.db`）

| 现状（JSON/内存） | 迁入后 |
|-------------------|--------|
| `token.txt` → `token_list` | `accounts` 表（一行一账号） |
| `error_token.txt` → `error_token_list` | `accounts.status = unhealthy` |
| `seed_map.json`（seed→token + conversations） | `users`（seed→tier+current_account）+ `conversations`（seed 索引） |
| `conversation_map.json` | `conversations` 表 |
| `refresh_map.json` | `accounts.refresh_info`（JSON 列） |
| `fp_map.json` | `accounts.impersonate / user_agent / proxy_url` |
| `routing_config.json`（proxies/groups/bindings/account_meta） | `proxies` 表 + `accounts` 字段 |
| （新增） | `usage_events` 表 |

### 3.2 保留独立（正交子系统，不迁，不属账号域）

当前健康写入约束：凭据列表同步只维护成员关系和错误信号，不以“凭据仍在列表”证明恢复。已有 disabled、dead、degraded、unhealthy 状态不会因清空错误列表而变为 healthy；账号刷新后仍需有效健康证据。健康扫描仅更新已有且状态未发生变化的 healthy/unhealthy 行，不插入已删除账号，也不覆盖扫描期间的停用或降级决定。写入失败必须报告，不能计入成功探针数。dead 的 dwell 恢复已有回归覆盖（`tests/test_proxy_route_coupling.py`：代理故障 → 探针失败 → 不可路由；持续成功满 dwell 才重新可路由）。

唯一状态机是 `utils.fleet_health.resolve_account_status`：人工停用 > dead（熔断标记或账号行）> 持久化 degraded > 错误列表 > 已被证实的 healthy；未建模取值与缺失账号行一律落 `unhealthy`。现在**面板与路由闸门读同一个判定**：`utils.routing.project_account_status` 用它产出 `status`（面板三档词表：正常/异常/停用）以及新增的机器可读 `account_status`、`status_label`；`utils.seed_lifecycle._candidate_denial` 也用它决定候选是否可路由。此前面板自带的映射只认账号行三态与错误列表，把 `status='dead'` 的熔断账号投影成「正常」——那是路由会拒绝、面板却说可用的账号。面板的汇总/告警/代理卡计数同样改为按该判定聚合（新增 `accounts_disabled`；`accounts_bad` 只统计 degraded/unhealthy/dead，不含运营主动停用）。运营者路径的全量 `seed_map` 写入收敛仍未处理。

路由候选的排序仍未纳入 `utils/proxy_health` 的逐节点 EWMA：该模块只暴露 `record` 与随机化的 `weighted_choice`，没有单节点健康读取接口，加入会引入随机分配并改动不属于本模块的公开面。当前代理健康对路由的影响是经由探针的（绑定出口 → 探针判定 → 状态 → 闸门），不是候选排序权重。

Seed 到期处理使用 `utils.seed_lifecycle.freeze_if_expired`：在同一 SQLite 事务内重新读取权益，仅冻结，不隐式激活续费用户；保留原账号、档次和会话记录。聊天门禁即时执行，启动时与每60秒的 `seed_expiry` 任务补齐闲置用户的到期状态。安全封禁仍归 `user_auth.status` 管理，普通到期不能禁止登录和续费。

容量激活基础接口 `activate_seed(seed, candidate_account, max_active_seeds)` 优先尝试原账号，再尝试同档健康候选，并在同一事务内检查绑定数。该参数统计 active/trial Seed，不代表上游安全人数，也不代表请求并发上限。独享权益必须使用无其他活跃绑定的账号；已有当前有效独享权益的活跃绑定也会阻止共享或试用用户进入。同档重叠订单有任一有效独享订单时保留独享，不使用过期或较低档订单决定当前档的密度；历史裸档位订单与试用按共享处理。冲突时仅尝试同档候选，失败保留原状态和历史。内存发布失败会显式报错；数据库若已提交，重试会从数据库恢复该 Seed 的状态和历史。

SaaS 进入镜像和切号现经 `route_seed` 在同一事务内选取并激活，读取数据库原绑定而非内存快照。优先恢复原账号；原号不符合条件时，在同档 healthy 候选中优先填充已有绑定的账号，同时遵守容量与独享限制。强制切换排除当前账号，找不到候选时保留绑定。两种 AUTO_SEED 模式都执行 SaaS 约束；镜像无可分配账号时返回503，不以空凭据继续发请求。运营者历史分配路径仍有全量 seed_map 写入，须继续收敛，不能据 SaaS 路径通过就宣布全部写入并发安全。

运营配置 `FLEET_MAX_SHARED_SEEDS_PER_ACCOUNT` 为共享/试用的活跃绑定上限，默认0表示未配置，此时共享/试用分配返回503；独享固定1人。启用共享前必须根据本部署实测设置正整数，测试中的2只是合成测试数据，不能作为上游安全容量结论。冻结绑定不占此计数（`tests/test_seed_route_failclosed.py` 覆盖），恢复时重新检查。请求并发仍由独立准入租约控制。已覆盖的失败路径：受限账号（dead/disabled/degraded/unhealthy/错误列表/熔断标记）一律拒绝且不动已有绑定、跨档候选拒绝、候选池耗尽与强切无候选分别以 `no_healthy_candidate` 拒绝、并发强切后库与内存一致、重启后沿用持久化绑定且不复活冻结 Seed。支付后立即分配、绑定失败恢复与真实账号容量校准尚待验收；已有订单在用户再次进入时会经该路由恢复，但不等于支付回调已完成自动分配。代理健康对路由的影响经探针生效（见上），尚未成为候选排序权重。

容量是**用户可见**的：未配置时三次注册试用的入口不会出现（Dashboard 显示「试用容量暂未开放」），而不是渲染一个必然失败的「开始试用」。该状态由 `gateway/saas.py` 的 `trial_capacity_configured` 驱动，取值仍是同一个 `FLEET_MAX_SHARED_SEEDS_PER_ACCOUNT` —— 不新增第二份容量口径，也不因为有用户点不到试用而放宽默认值。

### 3.2.1 产品面闸门（P0）

- **Free 不是商品**：正式售卖档是 Plus / Pro（`utils/plans.py` 的 12 个 SKU）。公开档位目录 `GET /api/tiers` 只返回 `plus` / `pro`；`free` 是账号侧采集档，只在 `DEV_ACCESS_ENABLED=true` 时出现在目录里。
- **开发 / 运营入口**：`/try`、`/demo` 绕过注册与订阅，直接把种子别名指向真实号池账号，二者统一由 `DEV_ACCESS_ENABLED` 控制（默认 false → 404）。关闭闸门不影响任何售卖路径（`/landing`、`/store`、`/api/orders`、`/checkout` 等照常）。
- **支付契约**：`utils.payment` 的 provider `verify` 必须返回结构化 `ProviderCallback`（订单号 + 流水号 + 金额 + 币种），裸 `order_id` 一律判为无效回调。结算前由 `validate_callback` 用库里的订单逐条核对金额、币种与流水号归属/重放；mock 渠道无签名可验，`verify` 恒为 None 且回调口 403 —— 它只能通过 `api_checkout` 的 `auto_settle` 在本地/测试环境结算，且审计记录带 `source=mock_auto_settle`。
- **封禁即下线**：`POST /admin/users/status`（status=banned）在写库后调用 `gateway.user.revoke_user_sessions`（即既有的 `store.bump_pw_version` 会话合同）吊销该账号全部 web 会话；`_current_email` 另有一道 `user_auth.status` 检查，避免吊销失败时旧 cookie 继续可用。登录页对被停用账号给出「账号已被停用」而不是「密码已变更」。
- **运营审计**：`utils/audit.py`（独立 SQLite，`AUDIT_DB_PATH`）为号池增删改、支付结算、用户封禁/解封记录动作、匿名主体 id（`sha256` 派生）与白名单字段，不写 token / cookie / 代理凭据 / 邮箱原文；`GET /admin/audit` 读取。审计写入失败不阻断业务，因此它可能缺记录，不能当作不可篡改的账本。

- `utils/antiban/` 的状态（`antiban_bucket/geo/dead`、`account_warnings`）——运行时风控状态，高频读写、自成模块。
- `wss_map.json`——瞬态 websocket 状态。
- `harvester_accounts.json`——harvester 模块自身的 email/note/采集历史存储。

> 理由：这三个是独立子系统，与「账号/车队」域正交；迁它们高风险低收益，保留不会阻碍后续扩展。
> 「不埋雷」体现在：账号域从此只有 SQLite 一个真相源，未来扩展（多车队、配额、商业化）都建在这张 schema 上。

### 3.3 表结构（`data/chat2api.db`，WAL 模式）

```sql
CREATE TABLE accounts (
  token              TEXT PRIMARY KEY,   -- 原始凭据，与 token_list 语义对齐
  token_type         TEXT NOT NULL,      -- detect_token_type()
  plan_type          TEXT,               -- free/plus/pro/team/unknown（JWT 解码）
  real_email         TEXT,               -- 仅管理员可见
  nickname           TEXT,               -- 可配显示名，NULL → "ChatGPT"
  status             TEXT NOT NULL DEFAULT 'healthy',  -- healthy/unhealthy/disabled
  proxy_name         TEXT,
  proxy_url          TEXT,
  group_name         TEXT,
  impersonate        TEXT,               -- 指纹
  user_agent         TEXT,
  note               TEXT,
  refresh_info       TEXT,               -- JSON 列（原 refresh_map）
  last_health_check  INTEGER,
  created_at         INTEGER,
  updated_at         INTEGER
);

CREATE TABLE users (
  seed              TEXT PRIMARY KEY,
  plan_type         TEXT,                -- 权益等级
  current_account   TEXT,                -- 粘性账号 token
  status            TEXT NOT NULL DEFAULT 'active',   -- active/disabled
  created_at        INTEGER,
  updated_at        INTEGER
);

CREATE TABLE conversations (
  conv_id      TEXT PRIMARY KEY,
  seed         TEXT NOT NULL,
  account      TEXT,                     -- 创建该会话的账号（换号后仍可回溯）
  title        TEXT,
  create_time  TEXT,
  update_time  TEXT
);
CREATE INDEX idx_conv_seed ON conversations(seed);

CREATE TABLE usage_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  seed        TEXT,
  account     TEXT,
  kind        TEXT,                      -- chat / image / other
  created_at  INTEGER
);
CREATE INDEX idx_usage_seed ON usage_events(seed, created_at);
CREATE INDEX idx_usage_account ON usage_events(account, created_at);

CREATE TABLE proxies (
  name       TEXT PRIMARY KEY,
  proxy_url  TEXT NOT NULL,
  group_size INTEGER DEFAULT 25,
  created_at INTEGER
);
```

### 3.4 访问层与热路径策略

- 新增 `utils/store.py`（SQLite DAO）：连接模式照抄 `session_sticky.py`（WAL + `synchronous=NORMAL`
  + `busy_timeout` + threading lock）；对外提供与现有 `globals` 语义一致的 API。
- `utils/globals.py` 改为「SQLite 之上的内存缓存」：启动时从 SQLite 加载进内存（保留 `token_list` /
  `seed_map` / `conversation_map` 等名字，避免大改下游），写操作 write-through 落 SQLite。
- **热路径**（会话列表隔离、用量计数）：内存计数/缓存，周期批量落库，避免每请求一次同步 SQLite 写。
- **冷路径**（账号 CRUD、seed 绑定、切换）：同步写 SQLite。

### 3.5 等级分池 / 健康 / 粘性 / 切换 / 匿名化 / 用量

与前一版一致（见下）：

- **等级**：复用 `identity.decode_jwt_payload` 解 `plan_type`/`real_email`；AccessToken 导入即解，
  Refresh/Session 首次换出 access_token 后懒解码写回 `accounts`。
- **产品分池边界**：Plus 只选 Plus，Pro 只选 Pro；`data/tiers.json` 的
  `account_plan_types` 可以缩小可选范围，不能扩大到其他档次。配置缺少对应档次或
  该档没有健康账号时不跨档回退。权益数据库失败返回 503，与没有权益或号池耗尽区分；
  拒绝分配不会删除原 Seed 绑定和会话记录。
- **付费用量**：Plus/Pro 权益按有效期执行，旧档位配置中的每日次数不再阻断付费聊天。
  历史 usage 仅用于观测；注册试用必须使用独立的成功结算额度，不能混用付费次数统计。
- **健康**：周期后台任务（挂 `@app.on_event("startup")`）对每账号 `verify_token` → 轻量探活；
  账号行的可路由状态；与 antiban `circuit` 的 dead 判定整合为一个折叠判定（避免两套健康判定打架）。持久化列仍只由探针写 healthy/unhealthy，dead/disabled/degraded 由熔断器或人工持有，见 3.2 的唯一状态机。
- **粘性**：`get_req_token(seed)` 查 `users[seed].current_account`，空则按 `users[seed].plan_type`
  从健康号池分配并写回。会话隔离由 `conversations.seed` 负责。
- **切换**：`GET /api/account-status`（当前账号健康态 + 匿名身份）+ `POST /api/switch-account`
  （从同等级号池换一个健康号，健康时拒绝）。换号后旧会话不再可用（历史跟号走）。
- **匿名化**：`build_session(anonymize=True)`——`name`→nickname/"ChatGPT"、`email` 置空、
  保留 `planType`/`account.id`。
- **用量**：反代路径按 `(seed, account, kind)` 递增内存计数，周期落库。

## 4. 分阶段实现计划

| Stage | 目标 | 成功标准（可测） |
|-------|------|------------------|
| **0 持久化底座** | `utils/store.py` + 建表 + 冷启动一次性迁移 JSON→SQLite + globals 改为缓存 | 冷启动后 SQLite 有原 JSON 全部数据；旧 JSON 不再作为真相源；重启幂等 |
| **1 等级分池** | 导入时解 plan_type/real_email；懒解码写回 | 导入 access_token 后 `accounts.plan_type` 正确；refresh/session 首次换出后写回 |
| **2 健康检查** | 周期探活 + 唯一账号状态机（healthy/degraded/unhealthy/dead/disabled）+ 与 antiban/error_token 整合 | 坏账号周期后 `status=unhealthy` 且后台可见；面板与路由闸门读同一判定 |
| **3 粘性路由 + 切换 + 匿名化** | `get_req_token` 走 users 粘性+等级；`/api/account-status` + `/api/switch-account`；`build_session` 匿名化 | 新 seed 自动分号；健康拒切换；坏号切换后返回新号匿名身份，旧会话仍隔离 |
| **4 用量统计** | 内存计数 + 周期落库 + admin 聚合 | 发 N 次后后台可见用户与账号各 N 次 |
| **5 管理后台 SPA** | 扩展 `get_dashboard_payload` + `account_proxy_bindings.html`：等级/状态/用量列 + 用户绑定 + 图表 | 后台可见账号 tier/status/usage、用户 tier/当前账号，图表可渲染 |

## 5. Definition of Done（验收清单）

- [ ] `data/chat2api.db` 自动建表，冷启动幂等；旧 JSON 一次性迁移，迁移后不再写 JSON。
- [ ] 账号按 `plan_type` 自动归池；`real_email` 仅 admin 可见，用户侧匿名化生效。
- [ ] 健康检查周期运行；坏账号标 `unhealthy`，恢复后可回 `healthy`。
- [ ] 用户绑等级、粘性账号；异常时 `/api/account-status` 提示、`/api/switch-account` 切到同等级健康号。
- [ ] 换号后旧会话不再可用（历史跟号走）；用户侧不泄漏真实 email。
- [ ] 用量按用户+账号双粒度可查，admin 面板可渲染。
- [ ] antiban / wss / harvester 独立子系统行为不变（不回归）。
- [ ] 每个 Stage 附实际命令/请求输出（REVIEW_PACKET），无「未验证」结论。
- [ ] 不提交任何含 secrets 的数据文件；git 提交不改变作者。

## 6. 镜像修复待办：任务 6 — 号池完整凭据管理

状态：待实施。用户于 2026-09-11 加入；排在账号对应前端、流式输出、底部按钮延迟、四项工具、联合验收这五项任务之后。本节是任务 6 的权威记录，不代表前面的 Stage 0–5 已完成。

问题：本轮排查发现，两个 Plus 账号在号池中只保存了 Access Token。它们能调用业务接口，却不能仅凭该 Token 获取官网已登录首页。号池需要区分“能聊天”和“能取得本账号官网启动状态”，并保留恢复和刷新会话所需的完整信息。

本轮已将用户提供的两个 Plus 和一个 Pro 完整会话 JSON 保存到 `data/private/account-sessions/`，索引为该目录下的 `index.json`。保存了所有原始字段，不只提取 AT；这些文件是本地凭据档案，尚未作为新的运行时数据源接入，也不表示新附件已通过业务验证。目录权限为 `0700`，文件为 `0600`，由现有 `/data/` Git 忽略规则覆盖。

任务范围与验收条件：

- [ ] 支持导入完整会话 JSON，保留原始字段及未来未知字段；按实际账号和工作区关联，拒绝凭据身份不一致的导入。
- [ ] 分别保存和识别 Access Token、Session Token、Refresh Token（仅在来源实际提供时）、有效期、认证来源及必要账号元数据；不把 AT 或 Session Token 标作 RT。
- [ ] 重复导入更新同一账号，保留原有代理、seed 和会话归属；凭据轮换不新增重复账号，不破坏绑定。
- [ ] 账号状态分别记录凭据解析、会话续期、官网已登录 bootstrap、业务接口的验证结果及时间；单个接口成功不等于全部能力可用。
- [ ] 刷新时保存有效的新凭据及有效期，兼容分片 Cookie；失败时保留可恢复信息，明确提示失效或缺失项，不使用其他账号凭据兜底。
- [ ] 管理后台展示凭据类型、缺失项和各项验证状态，默认隐藏凭据正文；原始档案的读写仅限服务端及授权管理操作。
- [ ] 明确原始导入档案与运行时有效凭据的归属和更新规则，提供本地恢复路径；重启后数据完整，不被旧内存快照覆盖。
- [ ] 凭据不进入 Git、普通日志、用户页面、截图、评审或记忆文件；验证完整保存、受限权限、导入幂等、刷新持久化及跨账号隔离。

实施边界：本次仅保存原始材料并登记待办，不顺带改造管理后台或数据库。任务 1 可使用已授权的本地会话材料完成必要账号前端修复；号池管理的完整改造在任务 6 单独实施和验收。
