# EVALUATOR.md — E2E 测试补齐 + 6 项身份泄漏修复

## 目标

补齐 `docs/TEST_PLAN.md` §6「待补覆盖」，并修复探索中发现的 6 个真实身份泄漏/越权 bug。

## 完成判据（可证伪）

### Stage 0 — E2E harness 底座

- [x] `tests_e2e/conftest.py` 独立隔离（临时 cwd + `ENABLE_GATEWAY=true` + 零真实网络/零真实凭据）。
- [x] `import app` 在 harness 下无异常（`tests_e2e/test_smoke.py` 冒烟通过）。
- [x] `tests/` 现有 48 例不回归。

### Stage 1 — 6 项身份泄漏修复（每项附回归测试）

| # | 修复 | 回归测试 | 状态 |
|---|------|---------|------|
| 1 | seed 解析失败→登录页，不下发 owner live 模板 | `test_empty_session_serves_login_not_owner_template` | [x] |
| 2 | banned_paths 按 seed cookie 判断，非 Authorization | `test_banned_path_blocked_for_mirror_user_with_direct_token` / `..._allowed_for_direct_client` | [x] |
| 3 | `/backend-api/me` email 匿名化 `""` | `test_me_returns_anonymized_email` | [x] |
| 4 | `/accounts/check` 抹除 account 内 owner 身份字段 | `test_accounts_check_scrubs_owner_identity` | [x] |
| 5 | catch-all backend-api JSON 定向脱敏 | `test_catchall_backend_api_json_scrubs_identity` | [x] |
| 6 | 跨 seed 会话详情越权（归属校验在代理之前） | `test_cross_seed_conversation_detail_forbidden` | [x] |

### Stage 2 — 网关黄金路径 E2E（E1/E2/E3）

- [x] E1 首访登录 / seed 访问匿名化 / auth session 匿名化。
- [x] E2 号挂自动 fail-over / switch-account 两态 / 会话按账号过滤。
- [x] E3 用量双粒度（seed + account）。
- [x] `/ces/*` 短路 202。

### Stage 3 — /v1 OpenAI 兼容 API E2E

- [x] `/v1/models`、`/v1/chat/completions`（非流式 + 流式）、`/v1/responses`（非流式 + 流式拒绝）。

### Stage 4 — antiban B1–B5

- [x] B1 熔断（mark_dead / 429 指数退避封顶 / 403 降级桶 / 401 分流 / success 复位）。
- [x] B2 冷却（record_request 间隔 / wait_or_skip 三态 / extend_cooldown 单调）。
- [x] B3 自愈（degraded→healthy / 死号不复活）。
- [x] B4 分桶（粘性 / 最少负载 / 容量封顶 / degrade meta）。
- [x] B5 guard（acquire_context 关启两态 / report_success/error 路由）。

### Stage 5 — harvester H1–H3

- [x] H1 OAuth（start→exchange→rt 进 token_list + harvester_meta；state 不匹配 400；无鉴权 401）。
- [x] H2 Cookie 导入（好 cookie→sess- key + refresh_map；坏 cookie→error_token_list + 400）。
- [x] H3 RT 生命周期（refresh→refresh_map 落 access_token）。

### 验证（Definition of Done）

- [x] `pytest tests/` = 48 passed。
- [x] `pytest tests_e2e/` = 47 passed，0 failed。
- [x] `git status` 无 `data/*` 变更；无 `.pyc`、无 secrets。
- [x] `REVIEW_PACKET.md` 附实际命令输出（非口头声称）。

## 验证命令

```bash
.venv/bin/pytest tests/            # 48 passed
.venv/bin/pytest tests_e2e/        # 47 passed
```
