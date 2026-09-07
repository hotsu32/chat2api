# 端到端测试计划（TEST PLAN）

> 范围：chat2api 车队镜像（官网镜像 + OpenAI 兼容 API + 多账号池）。
> 目标：用可 falsifiable 的测试锁定「持久化、身份匿名化、等级分池、粘性路由、用量统计」五条主线，并为后续网关/风控/真号联调留好 harness。

## 1. 测试分层总览

| 层 | 覆盖内容 | 自动化程度 | 落点 |
|---|---|---|---|
| 单元（纯函数） | JWT 解码 / 会话合成 / token 识别 / 掩码 / 时间格式化 | 全自动 | `tests/test_identity.py`、`test_routing_helpers.py` |
| 单元（DAO） | SQLite store CRUD / 迁移幂等 / load_all 往返 | 全自动 | `tests/test_store.py` |
| 单元（用量） | 内存计数 → flush → 双粒度聚合 | 全自动 | `tests/test_usage.py` |
| 单元（车队核心） | 分池 / 粘性 / 切换 / verify_token | 全自动 | `tests/test_authorization.py` |
| 集成（E2E harness） | mock chatgpt.com 上游 | 半自动 | `tests/conftest.py` + `test_mock_upstream.py` |
| 端到端（黄金路径） | 首访→登录→分配→对话→隔离→切换 | 手动/脚本 | 本文 §5 |
| 真号冒烟 | 1–2 个真号 + 独立代理 | 手动 | 本文 §7 |

## 2. 测试环境与安全红线

### 2.1 隔离机制（已实现）

`tests/conftest.py` 在**任何项目模块 import 之前**完成三件事，保证测试永不触碰生产 `data/`：

1. `os.chdir()` 到一次性临时目录（重定向 `data/`、`version.txt`、`.env` 的 cwd 相对路径）；
2. 写入 `FLEET_DB_PATH` / `SESSION_DB_PATH` / `ADMIN_PASSWORD` 等环境变量到临时路径；
3. `sys.path` 注入项目根，保证 `utils` / `gateway` / `chatgpt` 命名空间包可导入。

`db` fixture 每次测试 monkeypatch `store._DB_PATH` + `_INITIALIZED`，得到**全新空库**；`make_access_token` 生成带 `chatgpt_plan_type` / `profile.email` / `profile.name` 声明的合成 JWT。

### 2.2 红线

- 全程 `git status` 只允许 `pytest.ini` / `requirements-dev.txt` / `tests/` 三个新增；任何 `data/*.txt|json|db` 变更即失败。
- 合成 token 只含假身份（`owner@example.com` 等），不使用真实 `data/token.txt`。
- 测试不使用真实账号、不直连 chatgpt.com。

## 3. 已实现测试矩阵（48 用例）

### 3.1 `tests/test_identity.py` — 身份合成（R1/R3/R4）

- `decode_jwt_payload`：合法 JWT → dict；空串 / 无点 / 非法 base64 → `{}`。
- `decode_account_identity`：取 `plan_type`/`real_email`/`nickname`；无 openai 声明 → `plan_type="unknown"`。
- `build_session(anonymize=True)`：`name="ChatGPT"`、`email=""`，保留 `planType`/`account.id`/`accessToken`。
- `build_session(anonymize=False)`：返回真实 `name`/`email`。
- `build_session` 空/非法 → `{}`。

### 3.2 `tests/test_store.py` — 持久化 DAO（P1–P5）

- `init_db` 幂等；`migrate` 幂等（`meta.migrated=1` 后二次调用不重复）。
- 账号 upsert/get/delete；partial upsert 不覆盖未传列；`status` 默认 `healthy`。
- `get_account_by_plan` 等级过滤 + `get_healthy_accounts`。
- users 增删 + **级联删除会话**。
- 会话 **account 跟号走**（`conversations.account` 不被切号覆盖）。
- proxies / usage_events / meta 读写。
- `load_all` 往返：migrate 后重建 `token_list`/`error_token_list`/`seed_map`/`conversation_map`/`routing_config` 与库一致。

### 3.3 `tests/test_usage.py` — 用量双粒度（F6）

- `record_usage` 仅内存缓冲（`pending_count`），flush 前 `user_usage==0`。
- `flush_usage` 落库并清空；返回落库条数。
- **双粒度**：同一批事件 `user_usage(seed)` 与 `account_usage(account)` 分别正确。
- 空 seed/account 或空 kind 不计数。

### 3.4 `tests/test_routing_helpers.py` — 路由辅助（R1/掩码）

- `detect_token_type` 全矩阵（Access `eyJ`/`fk-`、Refresh 45/`rt_≥60`、Session `sess-`、Custom、空）。
- `detect_token_type` 单一来源：`routing.detect_token_type is store.detect_token_type`（锁定无镜像副本）。
- `mask_token`（≤12 原样，否则 `前6…后4`）。
- `format_refresh_time`（None/0 → `-`，时间戳 → ISO Z）。
- `build_group_assignments` 分组建表。
- `get_dashboard_payload` 空态 summary 形状（accounts_total/users_total=0）。

### 3.5 `tests/test_authorization.py` — 车队核心（F1/F2）

- `_account_is_usable`：healthy/disabled/无账号行三态（无账号行 = 不可用，悬空绑定 fail-over）。
- `_account_tier` / `_pick_healthy_account`（含等级池空 → 任意健康号回退）。
- `_resolve_seed_account`：**粘性复用**；绑死号 → **同等级切换**；新用户分配（`assigned_tier` 回写）。
- `switch_seed_account` 强制切换（含新用户 `assigned_tier` 回写）。
- `verify_token`（AccessToken 路径）：原样返回 + `sync_account_plan` 落库 `plan_type`；空 token（无 `AUTHORIZATION`）→ `None`。

### 3.6 `tests/test_mock_upstream.py` — E2E harness 契约

- 启动 `mock_upstream`（线程 HTTP server），断言 `/backend-api/me`、`/backend-api/sentinel/chat-requirements` 返回 canned 载荷。
- 该 fixture 是黄金路径 E2E 的上游替身（见 §5）。

## 4. 运行方式

```bash
# 安装测试依赖（仅测试环境，不进 production requirements.txt）
uv pip install --python .venv/bin/python -r requirements-dev.txt

# 运行（在项目根目录）
.venv/bin/pytest            # 全量
.venv/bin/pytest -v tests/test_store.py   # 单文件
```

预期：`48 passed`，无 warning。失败即回归。

## 5. 黄金路径端到端（手动验收）

前置：`mock_upstream`（`tests/conftest.py`）或指向独立 Clash 代理的真实环境；`FLEET_DB_PATH` 指向临时库。

**E1 首访 → 登录 → 分配 → 对话 → 隔离**

```bash
curl -s http://localhost:5005/ | grep -i login          # 无 seed → 登录页
curl -s -H "Cookie: token=seed-alice" http://localhost:5005/ \
  | grep -o '"name":"ChatGPT"'                          # 匿名身份已注入 client-bootstrap
curl -s -b "token=seed-alice" http://localhost:5005/api/auth/session \
  | python3 -m json.tool | grep -i email                # 断言 email 为空串
```

**E2 号挂 → 自动切换 → 历史跟号走**

```bash
# 绑定的账号被 mark_dead / status=disabled 后，新请求自动分到同等级健康号
curl -s -b "token=seed-alice" http://localhost:5005/api/account-status
# 断言 masked account 变化；会话列表只显示当前账号会话（旧会话数据仍在 sqlite）
```

**E3 用量统计（双粒度）**

```bash
sqlite3 data/chat2api.db "select seed, count(*) from usage_events group by seed;"
sqlite3 data/chat2api.db "select account, count(*) from usage_events group by account;"
```

## 6. 待补覆盖（需网关层 / 风控链 / 真号）

以下用例**无法**在纯单元层安全覆盖（需要完整 app 启动、antiban 链、或真实上游），已留 harness 待接入：

| 用例 | 为何待补 | 补法 |
|---|---|---|
| `/api/auth/session`、`/backend-api/me` 匿名化缺口（Stage 3 整改点） | 需 FastAPI TestClient + gateway 路由 | `httpx` 已装，指向 `mock_upstream`，`CHATGPT_BASE_URL` 指向 mock |
| `banned_paths` 403 / `/ces/*` 202 | 需 catch-all 路由 | 同上 |
| antiban 熔断/冷却/自愈（B1–B5） | `get_dashboard_payload` 增补字段触发 antiban 懒导入 | `ENABLE_ANTIBAN=true` + 独立 fixture |
| harvester OAuth / cookie 导入（H1–H3） | 需 OAuth 回调 mock | `mock_upstream` 扩展 |
| `/v1/chat/completions` 流式 / `/v1/responses` 转换（V1–V5） | 需 ChatService 全链 + PoW/sentinel | `NO_SENTINEL=true` + mock |
| 编排器外部读者 `deploy/multi/orchestrator` 读 `refresh_map.json` | 迁 SQLite 后未改编排器 | 待确认编排器是否在用 |

## 7. 真号冒烟（最后一步，隔离环境）

- 仅 1–2 个真实 RefreshToken，独立 `FLEET_DB_PATH`，独立代理，短时（<5 分钟）。
- 验证：`verify_token` 换出 access_token、`/backend-api/me` 200、一次真实对话。
- 结束后立即丢弃临时 `data/`，不合并回生产。

## 8. 验收门（Definition of Done）

- [ ] `pytest` 全绿（当前 48 passed，0 warning）。
- [ ] `git status` 无 secrets 变更，无 `data/` 改动。
- [ ] 黄金路径 E1/E2/E3 三场景有实际命令输出。
- [ ] 每层关键用例均有断言代码（非口头描述）。

## 9. 测试过程中观察到的行为（已修复，2026-09-07）

1. **`_account_is_usable` 对「无账号行」返回可用** → **已改为默认不可用**。`store.get_account(token)` 为 `None` 时判定不可用，悬空绑定会触发 fail-over 重新选号。锁定于 `test_account_is_usable`（`missing` → `False`）。
2. **新用户 `_resolve_seed_account` 不写回真实等级** → **已修复**。`assigned_tier` 提前计算并回写进新用户分支；`switch_seed_account` 同款遗漏一并修复。锁定于 `test_resolve_seed_account_new_user` / `test_switch_seed_account_new_user`（`plan_type == "plus"`）。
3. **`detect_token_type` 双份实现** → **已重构为单一来源**。新增 `utils/token_type.py`，`utils/routing` 与 `utils/store` 统一 import 同一函数，删除 `store._detect_token_type` 镜像。锁定于 `test_detect_token_type_single_source`（`routing.detect_token_type is store.detect_token_type`）。

> 注：`utils/token_parser.py::_classify` 是另一套返回词汇（`session/access/refresh/unknown`）的导入期分类器，职责不同，未并入；其注释声明「与 detect_token_type 规则保持一致」，如需同样收敛为单一来源可另议。
