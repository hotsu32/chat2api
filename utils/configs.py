import ast
import os

from dotenv import load_dotenv

from utils.Logger import logger

load_dotenv(encoding="ascii")


def is_true(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.lower() in ['true', '1', 't', 'y', 'yes']
    elif isinstance(x, int):
        return x == 1
    else:
        return False


api_prefix = os.getenv('API_PREFIX', None)
authorization = os.getenv('AUTHORIZATION', '').replace(' ', '')
admin_password = os.getenv('ADMIN_PASSWORD', None)
# 管理后台 IP 白名单（逗号分隔，支持单 IP / CIDR / 'trust_proxy'）
# 空串 = 不启用（允许所有 IP 访问登录页；真正 API 仍受 ADMIN_PASSWORD 保护）
# 示例: ADMIN_IP_WHITELIST="1.2.3.4,10.0.0.0/8,192.168.1.0/24"
admin_ip_whitelist_raw = os.getenv('ADMIN_IP_WHITELIST', '').replace(' ', '')
admin_ip_whitelist = [x for x in admin_ip_whitelist_raw.split(',') if x]
# 是否信任 X-Forwarded-For 头（仅在 CF / Nginx 反代场景开启，否则可被伪造绕过）
admin_trust_proxy = os.getenv('ADMIN_TRUST_PROXY', '').lower() in ('true', '1', 'yes')
chatgpt_base_url = os.getenv('CHATGPT_BASE_URL', 'https://chatgpt.com').replace(' ', '')
auth_key = os.getenv('AUTH_KEY', None)
x_sign = os.getenv('X_SIGN', None)

ark0se_token_url = os.getenv('ARK' + 'OSE_TOKEN_URL', '').replace(' ', '')
if not ark0se_token_url:
    ark0se_token_url = os.getenv('ARK0SE_TOKEN_URL', None)
proxy_url = os.getenv('PROXY_URL', '').replace(' ', '')
sentinel_proxy_url = os.getenv('SENTINEL_PROXY_URL', None)
export_proxy_url = os.getenv('EXPORT_PROXY_URL', None)
file_host = os.getenv('FILE_HOST', None)
voice_host = os.getenv('VOICE_HOST', None)
impersonate_list_str = os.getenv('IMPERSONATE', '[]')
user_agents_list_str = os.getenv('USER_AGENTS', '[]')
device_tuple_str = os.getenv('DEVICE_TUPLE', '()')
browser_tuple_str = os.getenv('BROWSER_TUPLE', '()')
platform_tuple_str = os.getenv('PLATFORM_TUPLE', '()')

cf_file_url = os.getenv('CF_FILE_URL', None)
turnstile_solver_url = os.getenv('TURNSTILE_SOLVER_URL', None)

history_disabled = is_true(os.getenv('HISTORY_DISABLED', True))
pow_difficulty = os.getenv('POW_DIFFICULTY', '000032')
# PoW 解算总预算（迭代次数上限）。默认 200 万：难度 000032 时失败率约 0.25%（原 50 万为 ~22%）
pow_budget = int(os.getenv('POW_BUDGET', 2_000_000))
# PoW 并行进程数；0 = 自动 min(cpu, 8)。多进程绕开 GIL，8 核下吞吐约 8x
pow_workers = int(os.getenv('POW_WORKERS', 0))
retry_times = int(os.getenv('RETRY_TIMES', 3))
conversation_only = is_true(os.getenv('CONVERSATION_ONLY', False))
enable_limit = is_true(os.getenv('ENABLE_LIMIT', True))
upload_by_url = is_true(os.getenv('UPLOAD_BY_URL', False))
check_model = is_true(os.getenv('CHECK_MODEL', False))
scheduled_refresh = is_true(os.getenv('SCHEDULED_REFRESH', False))
random_token = is_true(os.getenv('RANDOM_TOKEN', True))
oai_language = os.getenv('OAI_LANGUAGE', 'en-US')
chat_requirements_timeout = int(os.getenv('CHAT_REQUIREMENTS_TIMEOUT', 15))
chat_request_timeout = int(os.getenv('CHAT_REQUEST_TIMEOUT', 30))
accept_language = os.getenv('ACCEPT_LANGUAGE', 'en-US,en;q=0.9')
client_timezone = os.getenv('CLIENT_TIMEZONE', 'America/Los_Angeles')
client_timezone_offset_min = int(os.getenv('CLIENT_TIMEZONE_OFFSET_MIN', -480))

authorization_list = authorization.split(',') if authorization else []
chatgpt_base_url_list = chatgpt_base_url.split(',') if chatgpt_base_url else []
ark0se_token_url_list = ark0se_token_url.split(',') if ark0se_token_url else []
proxy_url_list = proxy_url.split(',') if proxy_url else []
sentinel_proxy_url_list = sentinel_proxy_url.split(',') if sentinel_proxy_url else []
impersonate_list = ast.literal_eval(impersonate_list_str)
user_agents_list = ast.literal_eval(user_agents_list_str)
device_tuple = ast.literal_eval(device_tuple_str)
browser_tuple = ast.literal_eval(browser_tuple_str)
platform_tuple = ast.literal_eval(platform_tuple_str)

enable_gateway = is_true(os.getenv('ENABLE_GATEWAY', False))
auto_seed = is_true(os.getenv('AUTO_SEED', True))
force_no_history = is_true(os.getenv('FORCE_NO_HISTORY', False))
no_sentinel = is_true(os.getenv('NO_SENTINEL', False))
# f/conversation 的 sentinel chat-requirements token 有时效，缓存超时视为未命中重新解算
# 0 = 不缓存（每次都重新解，最保守）；默认 300s 对齐 chat_token 的典型有效期
sentinel_cache_ttl = int(os.getenv('SENTINEL_CACHE_TTL', 300))
# 开启后 prepare 阶段 PoW 失败恢复 403 硬失败；默认降级继续（返回假 prepare_token + 不写缓存），
# 避免纯 Python 解 PoW 约 22% 概率跑满预算失败时把 403 透传给前端
sentinel_strict = is_true(os.getenv('SENTINEL_STRICT', False))
init_tokens = os.getenv('INIT_TOKENS', '')
init_proxies = os.getenv('INIT_PROXIES', '')
init_group_size = int(os.getenv('INIT_GROUP_SIZE', 25))
init_apply_on_empty = is_true(os.getenv('INIT_APPLY_ON_EMPTY', True))
init_force = is_true(os.getenv('INIT_FORCE', False))

# ========================= OpenAI 前端版本指纹（反降智） =========================
# 这两个值需要定期（1-2 周）从 chatgpt.com 的真实请求中刷新，否则会被风控识别
# 抓取方法：浏览器登录 chatgpt.com → F12 Network → 任一 /backend-api/* 请求 → Headers
oai_client_version = os.getenv(
    'OAI_CLIENT_VERSION',
    'prod-767c16cfce2fbcbdd1ae079fcf0b43838ff1b3ed',
)
oai_client_build_number = os.getenv(
    'OAI_CLIENT_BUILD_NUMBER',
    '6549031',
)

# ========================= OpenAI Auth0 凭据刷新 =========================
# 默认 Codex CLI client_id（新版 OpenAI 登录流程，适用于 auth.openai.com 端点）
# 老版 iOS app client_id `pdlLIX2Y72MIl2rhLhTE9VV9bN905kBh` + auth0.openai.com 已失效（返回 404）
openai_auth_client_id = os.getenv(
    'OPENAI_AUTH_CLIENT_ID',
    'app_EMoamEEZ73f0CkXaXp7hrann',
)
# 默认 localhost HTTP 回调（Codex CLI 风格，浏览器能正常识别）
openai_auth_redirect_uri = os.getenv(
    'OPENAI_AUTH_REDIRECT_URI',
    'http://localhost:1455/auth/callback',
)
# 新版 Authorize / Token 端点（去掉了 0）
openai_auth_authorize_url = os.getenv(
    'OPENAI_AUTH_AUTHORIZE_URL',
    'https://auth.openai.com/oauth/authorize',
)
openai_auth_token_url = os.getenv(
    'OPENAI_AUTH_TOKEN_URL',
    'https://auth.openai.com/oauth/token',
)
openai_auth_scope = os.getenv(
    'OPENAI_AUTH_SCOPE',
    'openid profile email offline_access',
)

# ========================= Antiban (风控规避层) =========================
# 总开关；默认关闭，保持向后兼容
enable_antiban = is_true(os.getenv('ENABLE_ANTIBAN', False))
# IP 粘性桶：每个代理最多容纳的账号数
bucket_max_accounts_per_ip = int(os.getenv('BUCKET_MAX_ACCOUNTS_PER_IP', 5))
# 严格 IP 绑定：开启后账号一旦绑定 IP 即永不漂移
strict_ip_binding = is_true(os.getenv('STRICT_IP_BINDING', True))
# 账号级最小请求间隔秒数（Team/Plus 默认 60s）
account_min_interval_seconds = int(os.getenv('ACCOUNT_MIN_INTERVAL_SECONDS', 60))
# 免费账号最小请求间隔秒数（通常需更长）
free_account_min_interval_seconds = int(os.getenv('FREE_ACCOUNT_MIN_INTERVAL_SECONDS', 180))
# 冷却抖动比例（±jitter）
account_cooldown_jitter = float(os.getenv('ACCOUNT_COOLDOWN_JITTER', 0.3))
# 账号排队最长等待秒数；超过则返回 503 让上游切换
account_max_wait_seconds = int(os.getenv('ACCOUNT_MAX_WAIT_SECONDS', 30))
# 每号并发上限（出租率）：free 号多共享（廉价可弃），plus/paid 号少共享（保护）
account_max_concurrency = int(os.getenv('ACCOUNT_MAX_CONCURRENCY', 5))
free_account_max_concurrency = int(os.getenv('FREE_ACCOUNT_MAX_CONCURRENCY', 10))
# 并发槽位排队最长等待秒数；超过则 503 让上游 failover
account_concurrency_wait_seconds = float(os.getenv('ACCOUNT_CONCURRENCY_WAIT_SECONDS', 5))
# 降智检测 Step B：命中软警告后是否联动冷却/熔断（校准后再开，避免误杀）
account_degraded_link_enabled = is_true(os.getenv('ACCOUNT_DEGRADED_LINK_ENABLED', False))
# 单次降智命中延长的冷却秒数
account_degraded_cooldown = int(os.getenv('ACCOUNT_DEGRADED_COOLDOWN', 1800))
# 累计命中达到该次数 → mark_dead（硬熔断，等待 recheck）
account_degraded_mark_dead_threshold = int(os.getenv('ACCOUNT_DEGRADED_MARK_DEAD_THRESHOLD', 3))
# Geo 查询服务提供商：ip-api | ipinfo
ip_geo_provider = os.getenv('IP_GEO_PROVIDER', 'ip-api')
# Geo 缓存 TTL（天）
ip_geo_cache_ttl_days = int(os.getenv('IP_GEO_CACHE_TTL_DAYS', 30))
# IP 信誉（IPQS）：欺诈分 + ASN，识别数据中心/垃圾 IP 前置过滤。缺 key 时 fail-open 不过滤。
ipqs_api_key = os.getenv('IPQS_API_KEY', '').replace(' ', '')
# 欺诈分 >= 该阈值 → 判黑，跳过该 IP 的桶
ipqs_fraud_threshold = int(os.getenv('IPQS_FRAUD_THRESHOLD', 80))
# 是否把数据中心 / 代理 IP 也判黑（默认关：数据中心代理是镜像的廉价默认，全拦会误伤存量部署）
ipqs_block_datacenter = is_true(os.getenv('IPQS_BLOCK_DATACENTER', False))
ipqs_block_proxy = is_true(os.getenv('IPQS_BLOCK_PROXY', False))
# IPQS 查询超时（秒）
ipqs_timeout_seconds = float(os.getenv('IPQS_TIMEOUT_SECONDS', 3))
# IP 信誉缓存 TTL（天）
ip_rep_cache_ttl_days = int(os.getenv('IP_REP_CACHE_TTL_DAYS', 7))
# 熔断参数
circuit_429_cooldown = int(os.getenv('CIRCUIT_429_COOLDOWN', 1800))
circuit_403_cooldown = int(os.getenv('CIRCUIT_403_COOLDOWN', 3600))
circuit_dead_account_recheck_hours = int(os.getenv('CIRCUIT_DEAD_ACCOUNT_RECHECK_HOURS', 24))
circuit_bucket_heal_minutes = int(os.getenv('CIRCUIT_BUCKET_HEAL_MINUTES', 30))

# ========================= Session Sticky (LibreChat 会话粘性) =========================
# 用于 LibreChat → New-API → chat2api 链路；将 LibreChat 端 conversationId
# 翻译为 ChatGPT 服务端 conversation_id，实现窗口级会话连续。默认关闭。
enable_session_sticky = is_true(os.getenv('ENABLE_SESSION_STICKY', False))
# SQLite 文件路径（默认在 data 卷内，跟随实例数据；与 utils/globals.DATA_FOLDER 保持一致）
session_db_path = os.getenv('SESSION_DB_PATH', os.path.join('data', 'sessions.db'))
# 多少天未更新的映射会被清理（cleanup_expired 调用时生效）
session_ttl_days = int(os.getenv('SESSION_TTL_DAYS', 30))
# request body 中携带 LibreChat conversationId 的字段名（默认与 librechat.yaml addParams 对齐）
session_lc_field = os.getenv('SESSION_LC_FIELD', 'librechat_conversation_id')
# 命中映射时是否把 messages[] 截短到最后一条 user message（依赖 ChatGPT 服务端续接历史，省 token）
session_trim_to_last_user = is_true(os.getenv('SESSION_TRIM_TO_LAST_USER', True))

# ========================= Fleet (车队管理) =========================
# 账号域统一 SQLite 存储（token/seed_map/conversation_map/refresh_map/fp_map/routing_config）
fleet_db_path = os.getenv('FLEET_DB_PATH', os.path.join('data', 'chat2api.db'))
# 用量统计内存计数 → usage_events 落库间隔（秒）
usage_flush_interval_seconds = int(os.getenv('USAGE_FLUSH_INTERVAL_SECONDS', 60))
# Explicit bound for shared/trial SaaS bindings, separate from request concurrency.
# Zero means unconfigured (deny shared allocation); solo always has one binding.
max_shared_seeds_per_account = int(os.getenv('FLEET_MAX_SHARED_SEEDS_PER_ACCOUNT', 0))

# ---- 用户侧 SaaS（Stage 1 注册/登录；Stage 0 档位）----
user_session_secret = os.getenv('USER_SESSION_SECRET', '').strip()
user_session_max_age = int(os.getenv('USER_SESSION_MAX_AGE', 8 * 3600))
user_session_cookie = 'user_session'
user_csrf_cookie = 'user_csrf'
require_email_verification = is_true(os.getenv('REQUIRE_EMAIL_VERIFICATION', False))

# 信任 X-Forwarded-For 的反代 IP 白名单（逗号分隔，支持单 IP / CIDR）。
# 空 = 谁的 XFF 都不信，一律用 TCP 对端 IP。
#
# 默认不信任是**故意**的：走 Nginx/CF 时忘了配，后果是所有用户共用反代出口 IP 一个
# 限流桶 → 全员 429，上线第一天就会被发现；反过来默认信任，则任何人加一行
# X-Forwarded-For 头就能把限流清零，而且无声无息。宁可选会响的那个失败方向。
#
# 与 ADMIN_TRUST_PROXY（布尔）的区别：那个只保护后台白名单，且后台本来就另有口令；
# 这里保护的是面向公网的注册/登录限流，必须能指明「只信任哪台反代」。
user_trusted_proxies_raw = os.getenv('USER_TRUSTED_PROXIES', '').replace(' ', '')
user_trusted_proxies = [x for x in user_trusted_proxies_raw.split(',') if x]

# ---- 注册后台服务：SMTP 邮件 / Turnstile 人机验证 / 注册限流 ----
# 站点对外地址（邮件里的验证 / 重置链接用它拼绝对 URL）
site_base_url = os.getenv('SITE_BASE_URL', 'http://127.0.0.1:5005').rstrip('/')

# SMTP（邮箱验证 + 找回密码）。未配置时 mailer 降级为「只记日志不发信」，不崩。
smtp_host = os.getenv('SMTP_HOST', '').strip()
smtp_port = int(os.getenv('SMTP_PORT', 465))
smtp_user = os.getenv('SMTP_USER', '').strip()
smtp_password = os.getenv('SMTP_PASSWORD', '').strip()
smtp_from = os.getenv('SMTP_FROM', '').strip()
smtp_from_name = os.getenv('SMTP_FROM_NAME', 'Chat-Share').strip()
smtp_ssl = is_true(os.getenv('SMTP_SSL', True))  # True=465 SSL；False=587 STARTTLS
# False=不加密（仅本地测试 SMTP 捕获用）
smtp_starttls = is_true(os.getenv('SMTP_STARTTLS', True))

# Cloudflare Turnstile。未配置时注册跳过人机校验（dev / 未接入时）。
turnstile_site_key = os.getenv('TURNSTILE_SITE_KEY', '').strip()
turnstile_secret_key = os.getenv('TURNSTILE_SECRET_KEY', '').strip()

# 同 IP 每小时注册上限（0 = 关闭限流）
register_rate_limit = int(os.getenv('REGISTER_RATE_LIMIT', 5))
# 登录失败限流：两个桶都要过。
#  - IP 桶宽（同一出口 NAT 后面可能有很多正常用户，卡太死会误伤整栋楼）
#  - 账号桶窄（针对单个账号的撞库，换 IP 也绕不过去）
# 0 = 关闭对应的桶。
signin_ip_rate_limit = int(os.getenv('SIGNIN_IP_RATE_LIMIT', 20))
signin_email_rate_limit = int(os.getenv('SIGNIN_EMAIL_RATE_LIMIT', 5))
signin_email_rate_window = int(os.getenv('SIGNIN_EMAIL_RATE_WINDOW', 15 * 60))
# 验证 / 重置链接有效期（秒），默认 30 分钟
email_token_ttl = int(os.getenv('EMAIL_TOKEN_TTL', 30 * 60))

# 未配置 SMTP 时，是否把验证/重置链接直接渲染到页面上（本地开发用）。
# 默认关：开着等于「知道任一已注册邮箱就能直接拿到它的重置链接」= 无条件账号接管。
# 生产上 SMTP 配错/挂掉的那一刻，这个开关就是全站可接管的总闸。
user_debug_links = is_true(os.getenv('USER_DEBUG_LINKS', False))

# ---- 支付 ----
# 支付渠道：留空 = 关闭下单（fail-closed，防止没接真支付时被白拿套餐）。
# 目前可选：mock（演示，立即成功且不扣款，仅供本地联调）。
app_env = os.getenv('APP_ENV', 'production').strip().lower()
payment_provider = os.getenv('PAYMENT_PROVIDER', '').strip().lower()
# 结算币种（服务端口径）：回调必须声明同一币种，否则拒绝激活。
# 服务端不接受「回调说了算」——订单金额与币种都必须与库里/配置里的一致。
payment_currency = (os.getenv('PAYMENT_CURRENCY', 'CNY').strip().upper() or 'CNY')
# 未支付订单的保留时长（秒），超时视为废单，默认 30 分钟
order_pending_ttl = int(os.getenv('ORDER_PENDING_TTL', 30 * 60))

# ---- 开发 / 运营专用入口（默认关闭）----
# /try、/demo 这类绕过注册与订阅的演示入口，以及公开档位目录里的非售卖档（free），
# 只有显式打开本开关才可达。默认关：生产环境必须显式配置才能暴露这些入口，
# 免得「部署时忘了关」变成任何人都能进核心聊天页的后门。
# 打开它同时保留本地开发时对 /try、/demo、free 档目录的访问。
dev_access_enabled = is_true(os.getenv('DEV_ACCESS_ENABLED', False))

# ---- 运营审计 ----
# 敏感池 / 支付 / 用户操作的审计库（独立 SQLite 文件，与车队库分开）。
# 只记动作、对象匿名 id 与白名单内的非敏感字段，绝不写 token / cookie / 邮箱原文。
audit_db_path = os.getenv('AUDIT_DB_PATH', os.path.join('data', 'audit.db'))


def smtp_configured() -> bool:
    # 只要有 host 即视为已配置（本地 / 内网 SMTP 可能不需要认证）
    return bool(smtp_host)


def turnstile_enabled() -> bool:
    return bool(turnstile_site_key and turnstile_secret_key)


def sender_address() -> str:
    return smtp_from or smtp_user


with open('version.txt') as f:
    version = f.read().strip()

logger.info("-" * 60)
logger.info(f"Chat2Api {version} | https://github.com/lanqian528/chat2api")
logger.info("-" * 60)
logger.info("Environment variables:")
logger.info("------------------------- Security -------------------------")
logger.info("API_PREFIX:        " + str(api_prefix))
logger.info("AUTHORIZATION:     " + str(len(authorization_list)) + " configured")
logger.info("ADMIN_PASSWORD:    " + str(bool(admin_password)))
logger.info("ADMIN_IP_WHITELIST:" + (f" {len(admin_ip_whitelist)} rule(s) [{'trust_proxy' if admin_trust_proxy else 'no_proxy'}]" if admin_ip_whitelist else " (disabled)"))
logger.info("AUTH_KEY:          " + str(bool(auth_key)))
logger.info("------------------------- Request --------------------------")
logger.info("CHATGPT_BASE_URL:  " + str(chatgpt_base_url_list))
logger.info("PROXY_URL:         " + str(len(proxy_url_list)) + " configured")
logger.info("EXPORT_PROXY_URL:  " + str(bool(export_proxy_url)))
logger.info("FILE_HOST:     " + str(file_host))
logger.info("VOICE_HOST:    " + str(voice_host))
logger.info("IMPERSONATE:       " + str(impersonate_list))
logger.info("USER_AGENTS:       " + str(user_agents_list))
logger.info("---------------------- Functionality -----------------------")
logger.info("HISTORY_DISABLED:  " + str(history_disabled))
logger.info("POW_DIFFICULTY:    " + str(pow_difficulty))
logger.info("POW_BUDGET:        " + str(pow_budget))
logger.info("POW_WORKERS:       " + str(pow_workers))
logger.info("RETRY_TIMES:       " + str(retry_times))
logger.info("CONVERSATION_ONLY: " + str(conversation_only))
logger.info("ENABLE_LIMIT:      " + str(enable_limit))
logger.info("UPLOAD_BY_URL:     " + str(upload_by_url))
logger.info("CHECK_MODEL:       " + str(check_model))
logger.info("SCHEDULED_REFRESH: " + str(scheduled_refresh))
logger.info("RANDOM_TOKEN:      " + str(random_token))
logger.info("OAI_LANGUAGE:      " + str(oai_language))
logger.info("ACCEPT_LANGUAGE:   " + str(accept_language))
logger.info("CLIENT_TIMEZONE:   " + str(client_timezone))
logger.info("CLIENT_TZ_OFFSET:  " + str(client_timezone_offset_min))
logger.info("CHAT_REQUIREMENTS_TIMEOUT: " + str(chat_requirements_timeout))
logger.info("CHAT_REQUEST_TIMEOUT:      " + str(chat_request_timeout))
logger.info("OAI_CLIENT_VERSION:        " + str(oai_client_version))
logger.info("OAI_CLIENT_BUILD_NUMBER:   " + str(oai_client_build_number))
logger.info("------------------------- Gateway --------------------------")
logger.info("ENABLE_GATEWAY:    " + str(enable_gateway))
logger.info("AUTO_SEED:         " + str(auto_seed))
logger.info("FORCE_NO_HISTORY: " + str(force_no_history))
logger.info("SENTINEL_CACHE_TTL: " + str(sentinel_cache_ttl))
logger.info("SENTINEL_STRICT:   " + str(sentinel_strict))
logger.info("INIT_TOKENS:       " + str(bool(init_tokens)))
logger.info("INIT_PROXIES:      " + str(bool(init_proxies)))
logger.info("INIT_GROUP_SIZE:   " + str(init_group_size))
logger.info("INIT_FORCE:        " + str(init_force))
logger.info("------------------------- Antiban --------------------------")
logger.info("ENABLE_ANTIBAN:    " + str(enable_antiban))
logger.info("STRICT_IP_BINDING: " + str(strict_ip_binding))
logger.info("BUCKET_MAX_ACCOUNTS_PER_IP: " + str(bucket_max_accounts_per_ip))
logger.info("ACCOUNT_MIN_INTERVAL_SECONDS: " + str(account_min_interval_seconds))
logger.info("ACCOUNT_MAX_WAIT_SECONDS:     " + str(account_max_wait_seconds))
logger.info("ACCOUNT_MAX_CONCURRENCY:      " + str(account_max_concurrency))
logger.info("FREE_ACCOUNT_MAX_CONCURRENCY: " + str(free_account_max_concurrency))
logger.info("ACCOUNT_DEGRADED_LINK:       " + str(account_degraded_link_enabled))
logger.info("IP_GEO_PROVIDER:   " + str(ip_geo_provider))
logger.info("IPQS_ENABLED:      " + str(bool(ipqs_api_key)))
logger.info("CIRCUIT_429_COOLDOWN: " + str(circuit_429_cooldown))
logger.info("CIRCUIT_403_COOLDOWN: " + str(circuit_403_cooldown))
logger.info("--------------------- Session Sticky -----------------------")
logger.info("ENABLE_SESSION_STICKY: " + str(enable_session_sticky))
if enable_session_sticky:
    logger.info("SESSION_DB_PATH:       " + str(session_db_path))
    logger.info("SESSION_TTL_DAYS:      " + str(session_ttl_days))
    logger.info("SESSION_LC_FIELD:      " + str(session_lc_field))
    logger.info("SESSION_TRIM_TO_LAST_USER: " + str(session_trim_to_last_user))
logger.info("--------------------- User SaaS --------------------------")
logger.info("USER_SESSION_SECRET: " + str(bool(user_session_secret)))
logger.info("REQUIRE_EMAIL_VERIFICATION: " + str(require_email_verification))
logger.info("USER_TRUSTED_PROXIES: " + (f"{len(user_trusted_proxies)} rule(s)" if user_trusted_proxies else " (none, XFF ignored)"))
logger.info("USER_DEBUG_LINKS:    " + str(user_debug_links))
logger.info("------------------------- Payment ---------------------------")
logger.info("APP_ENV:            " + str(app_env))
logger.info("PAYMENT_PROVIDER:   " + (payment_provider or " (none, checkout disabled)"))
logger.info("PAYMENT_CURRENCY:   " + str(payment_currency))
logger.info("DEV_ACCESS_ENABLED: " + str(dev_access_enabled))
logger.info("-" * 60)
