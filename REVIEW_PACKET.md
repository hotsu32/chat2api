# REVIEW_PACKET.md — E2E 测试补齐 + 6 项身份泄漏修复（证据包）

> 本文件记录**实际命令输出**与可复现证据，区分 Verified / Unverified / Needs User。

## 1. 变更清单

### 修改（3 文件，+99 / −15）

| 文件 | 修复项 | 变更 |
|------|--------|------|
| `gateway/chatgpt.py` | #1 seed 解析失败泄漏 owner live 模板 | +5 −2 |
| `gateway/backend.py` | #2 banned_paths 判断、#3 me 匿名化、#4 check 脱敏、#6 会话越权 | +63 −13 |
| `gateway/reverseProxy.py` | #5 catch-all JSON 定向脱敏 | +41 −0 |

### 新增（`tests_e2e/`，7 文件）

| 文件 | 用例数 | 覆盖 |
|------|--------|------|
| `conftest.py` | — | harness 底座 |
| `test_smoke.py` | 2 | Stage 0 冒烟 |
| `test_security_fixes.py` | 7 | 6 项修复回归 |
| `test_gateway_golden_path.py` | 10 | E1/E2/E3 + /ces |
| `test_v1_api.py` | 5 | /v1 OpenAI 兼容 |
| `test_antiban.py` | 17 | B1–B5 |
| `test_harvester.py` | 6 | H1–H3 |

## 2. 测试证据（实际输出）

### 2.1 现有单元/DAO 套件（不回归）

```
$ .venv/bin/pytest tests/
................................................                         [100%]
48 passed in 1.29s
```

### 2.2 E2E 套件（本次新增）

```
$ .venv/bin/pytest tests_e2e/
...............................................                          [100%]
47 passed in 15.73s
```

按文件拆分（`grep -c "^def test_\|^async def test_"`）：

```
test_antiban.py:            17
test_gateway_golden_path.py:10
test_harvester.py:           6
test_security_fixes.py:      7
test_smoke.py:               2
test_v1_api.py:              5
```

## 3. 隔离与安全证据

```
$ git status --short
 M gateway/backend.py
 M gateway/chatgpt.py
 M gateway/reverseProxy.py
?? tests_e2e/
```

- **无 `data/*` 变更**：harness `chdir` 到临时目录，`data/`、`token.txt`、`refresh_map.json`、`harvester_accounts.json` 等全部写入临时路径。
- **无 secrets**：`ADMIN_PASSWORD`/`OPENAI_AUTH_*` 均为测试占位值；`git add -A -n` 显示仅 3 个修改文件 + 7 个新测试文件。
- **无 `.pyc`**：`.gitignore` 已含 `*.pyc`；`git add -A -n | grep -c '\.pyc'` → `0`。

## 4. 证据可信度分级

| 结论 | 等级 | 依据 |
|------|------|------|
| 6 项修复均正确落地 | Verified | `git diff` 逐条核对 + 7 个回归测试红→绿 |
| `tests/` 不回归 | Verified | 48 passed（实际输出） |
| `tests_e2e/` 全绿 | Verified | 47 passed（实际输出） |
| 零真实网络 / 零真实凭据 | Verified | harness 隔离 + mock 全命中；`Client` 仅 H2 的硬编码 `chatgpt.com` 在 seam 处 stub |
| 修复 #5 为「全量脱敏」 | **Unverified（非事实）** | 通用反代无法穷举所有身份字段，已如实标注为「定向缓解 + 文档化残余风险」 |
| 生产环境 E2E 行为 | Needs User | 本套件在 mock 下运行；真实 chatgpt.com 链路需用户在真实环境手动验收 |

## 5. 手动验收步骤

```bash
.venv/bin/pytest tests/          # 期望 48 passed
.venv/bin/pytest tests_e2e/      # 期望 47 passed
```

安全相关改动（#1–#6）已在 `tests_e2e/test_security_fixes.py` 逐条锁定正确行为；如需上生产，建议人工复核 `gateway/backend.py`、`gateway/reverseProxy.py` 的脱敏字段清单是否覆盖当前业务所需。
