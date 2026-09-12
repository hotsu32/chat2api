# GitHub 上传计划（供审阅，未执行任何 push）

> 状态：**仅规划**。尚未创建仓库、尚未 push、尚未改任何文件。
> 目标：把当前内容以「新项目、新名字、全新历史」的方式，公开到用户 GitHub 账号（hotsu32）。

---

## 0. 现状审计结论

| 项 | 结论 |
|---|---|
| 当前 git | fork 链：`origin=hotsu32/chat2api` ← `upstream=nanashiwang/chat2api` ← `LanQian528/chat2api` |
| 跟踪文件 | 163 个（含 `.DS_Store`、多个内部流程文档） |
| 真实凭据是否进历史 | **否**。`.env`、`data/` 全在 `.gitignore`，未跟踪 |
| 跟踪文件内是否含真实凭据 | **否**。`git grep` 扫描 `eyJ`/`sess-`/`rt_`/真实 `socks5h://`，仅命中 `*.example`/`*.template` 占位符 |
| 账号上已有仓库 | `hotsu32/chat2api`（fork）已存在 → 新项目必须换名 |

---

## 1. 仓库命名（提案）

| 候选 | 含义 | 评估 |
|---|---|---|
| **`chatpool`**（首选） | chat + pool，双重语义：号池 / 拼车（carpool） | 短、好记、直击「号池拼车」本质，且与「chat to API」彻底切割 |
| `gptpool` | 同上，强调 GPT | 简洁，但「chat」比「gpt」更中性 |
| `chat-carpool` | 直译「拼车」 | 语义最贴，略长 |
| `poolchat` | 池在前 | 可读性略弱 |

推荐 **`chatpool`**。创建前需用 `gh repo create` 或网页确认该名未被占用；若占用，回退 `chat-carpool` 或 `gptpool`。

仓库简介（description）建议：
> 托管 ChatGPT 号池网关 —— 运营者出号池，用户网页直接聊（拼车/共享模式），无需自部署。

---

## 2. 上传策略：全新仓库，不继承 fork 历史

**决策：新建空仓库 + `git init` 空历史，不推现有 fork 历史。**

理由：
1. 用户明确「本质上不是同一个项目，关系没那么大」—— 继承 fork 历史会延续与上游的绑定，与新定位冲突。
2. fork 历史包含 nanashiwang/LanQian528 海量上游提交，**无法逐条审计是否含历史凭据**；空历史零风险。
3. 你的自有改动（demo 选择页、client-bootstrap 重写、tier 白名单等）已体现在**工作区最终代码**里，丢的只是「混着上游的提交记录」，代码零损失。

代价：丢掉本地 commit 历史（含你的提交）。若你在意，可保留工作区文件 + 一次性「导入提交」，但**不保留上游 fork 链**。

---

## 3. 模块审计（逐模块 include / exclude + 描述）

### 3A. 核心产品 —— 全部 INCLUDE

| 模块 | 文件数 | 描述 | 决定 |
|---|---|---|---|
| `app.py` | 1 | FastAPI 入口，挂载 gateway + /v1 + admin 路由 | 包含 |
| `gateway/` | 16 | 网页镜像主体：login/register/landing/demo/user/share/account/backend/reverseProxy/chatgpt/identity/admin/frontend_sync/f_conversation_gateway/gpts/route/v1。号池反代 + seed 会话隔离 + 用户侧 SaaS（Stage 0–4） | 包含 |
| `chatgpt/` | 13 + services(6) | 上游客户端：authorization/refreshToken/ChatService/SSE wss/fp 指纹/ProofOfWork/turnstile/session_sticky | 包含 |
| `utils/` | 28（含 antiban/ 14） | store/configs/globals/resp_cache/proxy_health/routing/tiers/usage/bootstrap/Logger + antiban（bucket/circuit/cooldown/concurrency/fingerprint/geo/account_risk/iprep/guard） | 包含 |
| `templates/` | 14 | 前端 UI：chatgpt/landing/login/register/demo/admin 等 HTML + 4 个 context json | 包含（json 上传前需扫 token） |
| `api/` | 5 | OpenAI 兼容 /v1 接口（chat2api/files/image_generations/models/tokens）。非产品本质，但属现有能力，保留 | 包含 |
| `harvester/` | 14 | Cookie/账号采集（src/ + accounts.csv.example + README + requirements） | 包含 |

### 3B. 运行配置 —— INCLUDE

| 模块 | 决定 | 备注 |
|---|---|---|
| `requirements.txt` / `requirements-dev.txt` | 包含 | — |
| `Dockerfile` / `docker-compose.yml` / `docker-compose-warp.yml` | 包含 | warp 的 `socks5://warp:1080` 是容器内名，安全 |
| `.env.example` / `deploy/.env.template` | 包含 | 占位模板，无真凭据 |
| `deploy/`（install.sh/chat2api.sh/install-command.sh/docker-compose.template.yml/multi/） | 包含 | `accounts.example.csv` 是占位，安全 |
| `.github/workflows` | 包含 | CI |
| `pytest.ini` | 包含 | — |
| `.gitignore` / `.dockerignore` | 包含 | **需补充**（见 §5） |
| `LICENSE`（MIT） | 包含 | — |
| `version.txt` | 包含 | — |

### 3C. 测试 —— INCLUDE

| 模块 | 决定 | 备注 |
|---|---|---|
| `tests/`（7 + conftest） | 包含 | 单测 |
| `tests_e2e/`（8 + conftest） | 包含 | mock 上游 E2E；**须与 tests/ 分进程跑**（conftest 已注明） |

### 3D. 文档 —— INCLUDE 但需重写/裁剪

| 模块 | 决定 | 备注 |
|---|---|---|
| `README.md` | **重写** | 当前是 chat2api「ChatGPT TO API」的 README，与新定位不符，须改为新项目名 +「号池拼车镜像」定位 + 面向运营者的部署说明 |
| `docs/`（9 文件） | 保留，逐份审 | FEATURES/SECURITY/COOKIE_HARVEST/TEST_PLAN + 4 张图；`FLEET_ECOSYSTEM_SPEC.md` 含产品规划，是否公开需你定（见 §6） |

### 3E. 排除 —— 绝不公开

| 模块 | 原因 |
|---|---|
| `.env` | 真实密钥（已 gitignore） |
| `data/` 全部（chat2api.db、token.txt、plus_account_*.txt、session_cookie.txt、error_token.txt、fp_map.json、seed_map.json、conversation_map.json、pow_config_cache） | OpenAI 真实凭据 + 指纹/代理绑定 + 会话映射（已 gitignore） |
| `.venv/` | 虚拟环境 |
| `__pycache__/`、`.pytest_cache/` | 构建产物 |
| `.DS_Store` | 误跟踪，需 untrack + gitignore |
| `.git/`（fork 历史） | 全新空历史 |

### 3F. 内部流程工件 —— 默认排除，待你确认

| 模块 | 内容 | 建议 |
|---|---|---|
| `REVIEW_PACKET*.md`（7 份，含 cache/demo/frontend/stage 等） | 修复证据包 | 排除 |
| `EVALUATOR*.md`（3 份） | 验收标准 | 排除 |
| `KIMI_REVIEW.md` | Kimi 评审记录 | 排除 |
| `handoff.md` / `memory.md` | 会话笔记 | 排除 |
| `smoke_real_accounts.py` | 真号冒烟脚本（引用 data/ 凭据） | 排除 |

---

## 4. 最小 MVP 闭环

闭环 = 一条完整可运行的「运营者 → 号池 → 用户网页聊天」链路，共 7 环，每环都有 INCLUDE 模块支撑：

```
① 导入账号   harvester / seed / admin      → utils/store 落库 data/chat2api.db（运行时自动生成，非上传内容）
② 用户落地   templates/landing.html         → 引流/注册入口
③ 注册登录   gateway/login.py + register.html → 拿 seed
④ 进入聊天   templates/chatgpt.html          → seed 会话隔离（seed_map）
⑤ 反代上游   gateway/reverseProxy.py + chatgpt/fp.py → 指纹 + 代理 + 上游 chatgpt.com
⑥ 稳定切号   utils/antiban/circuit.py       → 号挂自动 failover 无感
⑦ 验证回归   tests/ + tests_e2e/            → 全绿
```

`data/` 目录部署后为空目录，首次导入账号时自动创建 —— 闭环不依赖任何被排除文件。

---

## 5. 安全红线（push 前必须逐项执行）

1. **终版 `git grep` 扫描**：确认跟踪文件无 `eyJ`/`sess-`/`rt_`/真实 `socks5h://`（已做一次干净，push 前再跑终版）。
2. **`templates/*.json` 逐一检查**：chatgpt_context_1/2.json、gpts_context.json、initialize.json 无内嵌 token。
3. **新仓库 `.gitignore` 补齐**：`data/`、`.env`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`.DS_Store`、`*.db`、`*.db-*`、`*.pyc`。
4. **空历史**：新 repo `git init`，不推 fork 链。
5. **`gh` 认证确认**：确认 `gh auth status` 是 hotsu32，push 到正确账号。

---

## 6. 待你确认的 5 个决策点

1. **仓库名**：`chatpool`（推荐）？还是 `chat-carpool` / `gptpool` / 你另有想法？
2. **历史策略**：确认全新空历史（推荐），还是保留你的本地 commit？
3. **README**：是否先由我起草一版新 README（新定位 + 部署说明）给你审，再连同代码一起 push？
4. **流程工件**：`REVIEW_PACKET*` / `EVALUATOR*` / `KIMI_REVIEW` / `handoff` / `memory` / `smoke_real_accounts.py` 确认全部排除？
5. **`docs/FLEET_ECOSYSTEM_SPEC.md`**：含产品规划（车队生态规格），是否公开？还是排除/另存私有？
