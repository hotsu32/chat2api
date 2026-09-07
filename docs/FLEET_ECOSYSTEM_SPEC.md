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
- **健康**：周期后台任务（挂 `@app.on_event("startup")`）对每账号 `verify_token` → 轻量探活；
  `status` 三态；与 antiban `circuit` 的 dead 判定整合（避免两套健康判定打架）。
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
| **2 健康检查** | 周期探活 + status 三态 + 与 antiban/error_token 整合 | 坏账号周期后 `status=unhealthy` 且后台可见 |
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
