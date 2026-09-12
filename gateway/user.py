"""用户侧注册/登录/账号（Stage 1：把「运营者发 seed」改为「用户自注册 → 拿 seed → 绑档位」）。

复用 orchestrator 的 session + CSRF 模式，但作用域是「真实用户」而非「运营者」：

  - 密码用 PBKDF2 加盐哈希落库（不存明文）。
  - 注册即生成 seed，落到 ``globals.seed_map`` + ``store.user_auth``；
    并发放 Plus 免费试用 3 次（``utils.trials``）。正式产品不提供 Free 档。
  - 登录后跳 ``/?token=<seed>`` 直接进聊天，全程无运营者介入。
  - 邮箱验证若缺 SMTP 资源 → 占位（``require_email_verification`` 默认 False，跳过验证）。

路由命名与既有 ``/login``（seed 表单）错开，避免冲突：``/register``、``/signin``、
``/signout``、``/account``。
"""
import asyncio
import hmac
import hashlib
import base64
import ipaddress
import json
import secrets as pysecrets
import time
from typing import Optional

from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, templates
import utils.configs as configs
import utils.globals as globals
import utils.mailer as mailer
import utils.ratelimit as ratelimit
import utils.store as store
import utils.trials as trials
from utils.Logger import logger
from utils.tiers import get_tier, normalize_tier_id, user_usage_total

_PBKDF2_ITERATIONS = 100_000

# 未配置 USER_SESSION_SECRET 时用进程内随机密钥（重启后会话失效，dev/test 可接受）。
_session_secret = configs.user_session_secret or pysecrets.token_hex(32)
if not configs.user_session_secret:
    logger.warning("[user] USER_SESSION_SECRET 未配置，使用进程内随机密钥（重启会话失效）")


# --------------------------------------------------------------------------- crypto

def hash_password(password: str) -> str:
    salt = pysecrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _sign(data: str) -> str:
    return hmac.new(_session_secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()


def _pw_version(email: str) -> int:
    """该账号当前密码版本；查不到按初始版本 1（与 store 的默认值一致）。"""
    row = store.get_user_auth(email) or {}
    try:
        return int(row.get("pw_version") or 1)
    except (TypeError, ValueError):
        return 1


def _issue_session(email: str, pw_version: Optional[int] = None) -> str:
    """自签名会话 token（stdlib HMAC，无 itsdangerous 依赖）。

    payload 带 ``pwv``（密码版本）：改密 / 重置密码会把库里的版本 +1，于是此前签发的
    所有 token 全部对不上号 —— 这就是「改了密码，别的设备被踢下线」的实现。
    没有它的话，token 一旦泄漏就在有效期内无法挽回，改密码也救不了。
    """
    pwv = _pw_version(email) if pw_version is None else int(pw_version)
    payload = {
        "email": email,
        "pwv": pwv,
        "exp": int(time.time()) + configs.user_session_max_age,
    }
    data = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{data}.{_sign(data)}"


def _verify_session(token: str):
    """校验会话 token，返回 ``(email, pwv)``；无效返回 ``(None, 0)``。

    这里只做「签名对不对、过没过期」这类无需查库的校验；``pwv`` 的比对留给
    :func:`_current_email`，因为那里本来就要查一次 user_auth（零额外开销）。
    """
    try:
        data, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(data), sig):
            return None, 0
        pad = "=" * (-len(data) % 4)
        payload = json.loads(base64.urlsafe_b64decode(data + pad).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None, 0
        # 缺 pwv 的是本次改动之前签发的旧 token：按初始版本 1 认，避免升级瞬间把
        # 所有在线用户踢下线；它们最长 8 小时（user_session_max_age）内自然消亡。
        return payload.get("email"), int(payload.get("pwv", 1) or 1)
    except Exception:
        return None, 0


def _valid_email(email: str) -> bool:
    return bool(email) and "@" in email and "." in email.split("@", 1)[-1]


def _dev_link(path: str) -> str:
    """本地开发用的「链接直接显示在页面上」，仅在 ``USER_DEBUG_LINKS`` 显式打开时返回。

    以前的条件是「SMTP 没配就显示」—— 那等于把重置链接的发放条件交给运维状态：
    SMTP 一旦配错或挂掉，任何人在 /forgot-password 填一个已注册邮箱，就能在返回页面上
    直接读到那个账号的重置链接，无需碰邮箱即可接管账号。开关必须是显式的、与 SMTP 无关的。
    """
    return path if configs.user_debug_links else ""


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _peer_ip(request: Request) -> str:
    """TCP 对端 IP —— 唯一不可伪造的那个。"""
    return request.client.host if request.client else "unknown"


def _is_trusted_proxy(peer: str) -> bool:
    """``peer`` 是否是配置里声明过的反代（支持单 IP 与 CIDR）。"""
    if not configs.user_trusted_proxies or peer in ("", "unknown"):
        return False
    try:
        peer_obj = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for rule in configs.user_trusted_proxies:
        try:
            if "/" in rule:
                if peer_obj in ipaddress.ip_network(rule, strict=False):
                    return True
            elif peer_obj == ipaddress.ip_address(rule):
                return True
        except ValueError:
            logger.warning(f"[user] USER_TRUSTED_PROXIES 规则无效: {rule}")
    return False


def _client_ip(request: Request) -> str:
    """限流用的客户端 IP。

    **只有**当 TCP 对端本身就在 ``USER_TRUSTED_PROXIES`` 里时才看 X-Forwarded-For；
    否则一律用对端 IP。XFF 是纯客户端可写的头，无条件采信等于把限流的 key 交给
    攻击者决定 —— 每次请求换一个假 IP，注册/登录限流就等于不存在。

    采信时**从右往左**找第一个不可信条目，而不是取最左一段。Cloudflare 和 Nginx 的
    ``$proxy_add_x_forwarded_for`` 都是**追加**而非覆盖：客户端自带 ``XFF: 1.2.3.4``
    时，app 看到的是 ``1.2.3.4, <真实IP>``，取最左拿到的正是攻击者写的那个值。
    右侧是各级反代自己追加的、伪造不了的部分，逐跳剥掉可信的，剩下的第一个就是客户端。
    多级反代（CF -> Nginx -> app）需要把每一级的出口 IP 都写进白名单。

    未配置时的失败方向是「所有人挤在反代出口 IP 的同一个桶里」（全员 429，很吵），
    而不是「限流静默失效」。前者五分钟内就会被发现并修掉配置。
    """
    peer = _peer_ip(request)
    if not _is_trusted_proxy(peer):
        return peer
    hops = [h.strip() for h in (request.headers.get("x-forwarded-for") or "").split(",")]
    for hop in reversed(hops):
        if hop and not _is_trusted_proxy(hop):
            return hop
    # 整条链都是可信反代（或 XFF 为空）：没有比对端更靠谱的信息了
    return peer


def _turnstile_sync(secret: str, response: str, remoteip: str):
    """同步调用 Cloudflare siteverify（丢线程池执行，不阻塞事件循环）。"""
    from curl_cffi import requests as cffi_requests
    return cffi_requests.post(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data={"secret": secret, "response": response, "remoteip": remoteip},
        timeout=8,
    ).json()


async def _turnstile_check(request: Request, token: str) -> tuple:
    """返回 ``(ok, err_msg)``。未启用视为通过；网络不可达 fail-open（可用性优先）。"""
    if not configs.turnstile_enabled():
        return True, ""
    if not token:
        return False, "请完成人机验证"
    try:
        res = await asyncio.to_thread(
            _turnstile_sync, configs.turnstile_secret_key, token, _client_ip(request)
        )
    except Exception as e:
        logger.warning(f"[turnstile] 校验不可达，放行（fail-open）: {e}")
        return True, ""
    if res.get("success") is True:
        return True, ""
    logger.info(f"[turnstile] reject: {res.get('error-codes')}")
    return False, "人机验证未通过，请重试"


def _turnstile_ctx() -> dict:
    """注册页模板上下文里的 Turnstile site key（未启用则为空串）。"""
    return {"turnstile_site_key": configs.turnstile_site_key if configs.turnstile_enabled() else ""}


# ------------------------------------------------------------------------ session

# 会话「因改密被吊销」与「本来就没登录」要能区分开：前者必须给用户一句解释，
# 否则另一台设备只是莫名其妙跳回登录页，看起来像 bug 而不像安全措施。
REVOKED_DETAIL = "登录状态已失效，请重新登录"
# 跳登录页时带上它，模板据此显示原因（值进 URL，保持 ASCII 短标记）
REVOKED_QUERY = "reason=pw_changed"

# 被封禁 / 冻结的账号是**另一回事**：不是「你的会话过期了」，而是「这个账号不能用了」。
# 两者混用会让被封的人去改密码重试，然后反复撞在同一堵墙上。
DISABLED_DETAIL = "账号已被停用"
DISABLED_QUERY = "reason=disabled"

# 这些 user_auth.status 值视为「登录态一律无效」。封禁走的是会话吊销（bump_pw_version）
# 这条既有合同，但库里 status 与 cookie 版本是两份状态：万一 bump 失败或有人手工改库，
# 这里再挡一道，方向是 fail-closed（宁可把一个状态异常的账号挡在门外，也不放进来）。
#
# **只有 banned**：`frozen` 属于 Seed 绑定的到期状态（见 utils/seed_lifecycle），
# 它冻结的是绑定而不是账号 —— 订阅过期的用户必须还能登录并续费，把他一并锁在门外
# 等于让「续费」这个唯一的恢复路径不可达。两者不是一回事，所以不合并。
_REVOKED_STATUSES = frozenset({"banned"})


def revoke_user_sessions(email: str) -> int:
    """吊销该账号此前签发的**全部** web 会话，返回新的密码版本号。

    实现就是既有的 ``store.bump_pw_version``：会话 token 里带 ``pwv``，版本一变，
    所有旧 token 在 :func:`_current_email` 里都对不上号。不引入第二套会话表 ——
    「改密即踢下线」这条路径已经被验证过，封禁复用同一条，行为一致且没有新状态。

    ``StoreError`` 向上抛：吊销失败必须让调用方知道，绝不能报「已封禁」而实际上
    对方手里的 cookie 还能用。
    """
    return store.bump_pw_version(email)


def _set_session_cookies(request: Request, response: Response, email: str,
                         pw_version: Optional[int] = None) -> None:
    """下发会话 + CSRF cookie。

    ``pw_version`` 由刚做完改密的调用方显式传入：此时库里已经是新版本，但如果让
    ``_issue_session`` 自己再查一次，就多一次可以省掉的 DB 往返。
    """
    token = _issue_session(email, pw_version)
    csrf = pysecrets.token_hex(16)
    secure = _is_https(request)
    response.set_cookie(
        configs.user_session_cookie, token,
        max_age=configs.user_session_max_age, httponly=True, samesite="strict",
        secure=secure, path="/",
    )
    response.set_cookie(
        configs.user_csrf_cookie, csrf,
        max_age=configs.user_session_max_age, httponly=False, samesite="strict",
        secure=secure, path="/",
    )


def _current_email(request: Request) -> str:
    """校验会话，返回已登录用户 email；无效抛 401。

    密码版本在这里比对：本函数本来就要查一次 ``user_auth``（确认账号还在），
    顺带核对 ``pwv`` 是零成本的，而放到 ``_verify_session`` 里则要额外查库。
    账号状态也一并核对：封禁/冻结的账号即使持有未过期的旧 cookie 也不放行。
    """
    token = request.cookies.get(configs.user_session_cookie) or ""
    email, pwv = _verify_session(token) if token else (None, 0)
    if not email:
        raise HTTPException(status_code=401, detail="未登录")
    row = store.get_user_auth(email)
    if not row:
        raise HTTPException(status_code=401, detail="未登录")
    if (row.get("status") or "active").strip().lower() in _REVOKED_STATUSES:
        raise HTTPException(status_code=401, detail=DISABLED_DETAIL)
    if pwv != int(row.get("pw_version") or 1):
        # 该账号改过密码（或被封禁触发了同样的吊销）—— 这张 token 是之前签发的，已失效
        raise HTTPException(status_code=401, detail=REVOKED_DETAIL)
    return email


def _ensure_csrf(request: Request, response: Response) -> str:
    """读/下发 CSRF cookie，返回当前 token（表单 hidden 字段用）。"""
    token = request.cookies.get(configs.user_csrf_cookie) or pysecrets.token_hex(16)
    response.set_cookie(
        configs.user_csrf_cookie, token,
        max_age=configs.user_session_max_age, httponly=False, samesite="strict",
        secure=_is_https(request), path="/",
    )
    return token


def _render_with_csrf(request: Request, template: str, ctx: dict, status_code: int = 200):
    """渲染模板并在响应上确保 CSRF cookie，表单内嵌同一 token。"""
    token = _ensure_csrf(request, Response())
    resp = templates.TemplateResponse(template, {**ctx, "request": request, "csrf_token": token})
    resp.set_cookie(
        configs.user_csrf_cookie, token,
        max_age=configs.user_session_max_age, httponly=False, samesite="strict",
        secure=_is_https(request), path="/",
    )
    resp.status_code = status_code
    return resp


def _verify_csrf(request: Request, form_token: str) -> None:
    cookie_val = request.cookies.get(configs.user_csrf_cookie) or ""
    if not cookie_val or not form_token or not hmac.compare_digest(cookie_val, form_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


def _login_redirect(request: Request, email: str) -> RedirectResponse:
    """登录/注册后统一进入 Dashboard，由页面承载订阅空态或入口。"""
    resp = RedirectResponse(url="/dashboard", status_code=303)
    _set_session_cookies(request, resp, email)
    return resp


# ------------------------------------------------------------------ 登录失败限流

_SIGNIN_IP_WINDOW = 3600

# 被限流时的统一文案。必须指路「忘记密码」：账号桶连正确密码也挡，若不给这句，
# 被别人恶意灌满桶的用户会以为自己被永久锁死，而其实邮箱那条路一直开着。
_THROTTLE_MSG = "登录尝试过于频繁，请稍后再试；或通过「忘记密码」用邮箱重置后立即登录"

# 账号不存在时也跑一次 PBKDF2 的靶子。不这么做的话「查无此人」立刻返回、
# 「有此人」要算 10 万轮哈希，两者的耗时差是一个免费的账号枚举接口。
_DUMMY_HASH = hash_password(pysecrets.token_hex(16))


def _signin_email_key(email: str) -> str:
    return f"signin:email:{email}"


def _signin_keys(request: Request, email: str) -> tuple:
    """``((ip_key, limit, window), (email_key, limit, window))``；email 为空则只有 IP 桶。"""
    buckets = [(f"signin:ip:{_client_ip(request)}", configs.signin_ip_rate_limit, _SIGNIN_IP_WINDOW)]
    if email:
        buckets.append((
            _signin_email_key(email),
            configs.signin_email_rate_limit,
            configs.signin_email_rate_window,
        ))
    return tuple(buckets)


def _signin_over_limit(request: Request, email: str) -> bool:
    """任一桶超限。只读，不计数 —— 计数发生在密码错误时。"""
    return any(
        ratelimit.over_limit(key, limit, window)
        for key, limit, window in _signin_keys(request, email)
    )


def _signin_record_failure(request: Request, email: str) -> None:
    """密码错误才记账。登录成功不计数：正常用户换设备连登几次不该把自己锁在门外。"""
    for key, limit, window in _signin_keys(request, email):
        if limit > 0:
            ratelimit.hit(key, window)


# --------------------------------------------------------------------------- routes

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _render_with_csrf(request, "register.html", {"error": None, **_turnstile_ctx()})


@app.post("/register")
async def register(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    csrf = form.get("csrf_token") or ""
    _verify_csrf(request, csrf)
    ctx = _turnstile_ctx()

    def fail(msg: str, code: int = 400):
        return _render_with_csrf(
            request, "register.html", {**ctx, "error": msg, "email": email}, status_code=code
        )

    # 1) 同 IP 注册限流（防脚本批量注册）
    if not ratelimit.allow(f"register:{_client_ip(request)}", configs.register_rate_limit):
        return fail("注册过于频繁，请稍后再试", 429)

    # 2) 人机验证（未配置 Turnstile 时自动跳过）
    ok, ts_err = await _turnstile_check(request, form.get("cf-turnstile-response") or "")
    if not ok:
        return fail(ts_err)

    # 3) 字段校验
    if not _valid_email(email):
        return fail("请输入有效邮箱")
    if len(password) < 8:
        return fail("密码至少 8 位")
    if store.get_user_auth(email):
        return fail("该邮箱已注册")

    seed = pysecrets.token_hex(16)
    need_verify = configs.require_email_verification
    # 注册与试用赠额同一事务：若赠额写入失败，user_auth 行一起回滚，不留无额度僵尸账号。
    try:
        store.create_user_with_trial(
            email,
            password_hash=hash_password(password),
            seed=seed,
            status="unverified" if need_verify else "active",
            trial_tier=trials.TRIAL_TIER,
            trial_total=trials.SIGNUP_TRIAL_COUNT,
        )
    except store.StoreError:
        logger.error("[user] 注册写库失败")
        return fail("注册暂时不可用，请稍后重试", 503)
    # users 已与账号和试用额度一起提交；只发布这个 Seed，避免重写其他绑定/历史。
    globals.seed_map[seed] = {
        "token": "", "plan_type": trials.TRIAL_TIER, "status": "trial", "conversations": []
    }

    # 4) 需验证邮箱：发验证信并停在提示页（不发登录 session）
    if need_verify:
        token = pysecrets.token_urlsafe(32)
        store.create_email_token(token, email, "verify", int(time.time()) + configs.email_token_ttl)
        sent = await mailer.send_verify_email(email, token)
        dev_link = _dev_link(f"/verify-email?token={token}")
        return _render_with_csrf(
            request, "verify_email.html",
            {**ctx, "mode": "sent", "email": email, "sent": sent, "dev_link": dev_link},
        )

    return _login_redirect(request, email)


@app.get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request, reason: str = ""):
    """登录页。``reason=pw_changed`` 来自被吊销的会话（见 ``REVOKED_QUERY``）。

    区分「被踢下线」和「本来就没登录」：前者要给一句解释，否则用户在另一台设备上
    只看到莫名跳回登录页，会当成 bug 报上来，而不是「安全措施生效了」。

    封禁是第三种情况，必须说成「账号被停用」而不是「密码变了」—— 后者会让被封的人
    去走一遍找回密码，然后在成功改密后继续被挡，白跑一趟还以为是系统故障。
    """
    if reason == "pw_changed":
        notice = "密码已变更，此设备的登录状态已失效，请用新密码重新登录"
    elif reason == "disabled":
        notice = "该账号已被停用，如有疑问请联系客服"
    else:
        notice = None
    return _render_with_csrf(request, "signin.html", {"error": None, "notice": notice})


@app.post("/signin")
async def signin(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    csrf = form.get("csrf_token") or ""
    _verify_csrf(request, csrf)

    def fail(msg: str, code: int):
        return _render_with_csrf(
            request, "signin.html", {"error": msg, "email": email}, status_code=code
        )

    # 两个桶都在验密码之前查（验密码要跑 10 万轮 PBKDF2，不先卡住的话这个口
    # 本身就是放大 CPU 消耗的入口）：
    #  - IP 桶挡「一个来源狂试很多账号」；
    #  - 账号桶挡「一个账号被很多 IP（代理池 / 肉鸡）轮流试」，撞库换 IP 绕不过。
    #
    # 账号桶**连正确密码一起挡**。反过来做（「密码对就放行」）等于取消账号桶：
    # 攻击者可以无限次提交，撞对的那一次照样放行，桶只是把错误码从 401 换成 429 ——
    # 而撞库防护的全部价值就在于让他到不了那一次。代价是「知道邮箱的人能把你锁在门外」，
    # 用两个出口化解：
    #  - 登录成功立刻清桶（本人笔误不会累积到下一次）；
    #  - 「忘记密码」走邮箱令牌、不受本桶约束，重置成功即清桶 ——
    #    被锁的人永远有一条攻击者关不掉的回家路（他控制不了受害者的邮箱）。
    if _signin_over_limit(request, email):
        return fail(_THROTTLE_MSG, 429)

    row = store.get_user_auth(email)
    # 查无此人也跑一次哈希：否则「立刻返回」与「算 10 万轮」的耗时差本身就是账号枚举接口
    if row:
        ok = verify_password(password, row.get("password_hash") or "")
    else:
        verify_password(password, _DUMMY_HASH)
        ok = False
    if not ok:
        _signin_record_failure(request, email)
        return fail("邮箱或密码错误", 401)

    # 成功即清账号桶：否则本人 4 次笔误 + 攻击者补 1 次就被锁，太脆
    ratelimit.reset(_signin_email_key(email))

    # 封禁 / 冻结账号：口令正确也不发会话。放在验密之后 —— 否则这个分支就成了
    # 「输入任意密码即可查询该邮箱是否被封」的状态预言机。
    if (row.get("status") or "active").strip().lower() in _REVOKED_STATUSES:
        return fail("该账号已被停用，如有疑问请联系客服", 403)

    # 需邮箱验证但尚未验证 → 拦截（防止未验证账号直接使用）
    if configs.require_email_verification and (row.get("status") or "") != "active":
        return fail("邮箱尚未验证，请先查收验证邮件", 403)
    return _login_redirect(request, email)


@app.post("/signout")
async def signout(request: Request):
    form = await request.form()
    _verify_csrf(request, form.get("csrf_token") or "")
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(configs.user_session_cookie, path="/")
    resp.delete_cookie(configs.user_csrf_cookie, path="/")
    return resp


# ------------------------------------------------------------ 邮箱验证 / 找回密码

# 同一邮箱的重置信冷却。不做成配置项：它跟运营口径无关，3 封/小时对真人足够
# （收不到信的人会先去翻垃圾箱而不是连点 10 次），对邮件轰炸则已经拦掉了量。
_FORGOT_EMAIL_LIMIT = 3


def _forgot_email_window() -> int:
    """账号级冷却的窗口，**跟着 token TTL 走**，不写死 1 小时。

    这两者一旦脱钩就会开出一个「静默空窗」：攻击者 t=0 灌满 3 格，token 在 TTL
    （默认 30 分钟）后全部过期，但槽位还占着 —— 此后到窗口滑走之前，受害者自己点
    「忘记密码」会拿到「已发送」的页面，收件箱里却没有任何可用链接，而这条路正是
    他被登录限流挡住时唯一的出口。让窗口不长于 TTL，槽位释放不会晚于最后一封信失效。

    下限 5 分钟：TTL 配得极短时也别退化成「几乎不限流」，那就成了邮件轰炸口。
    """
    return max(300, min(3600, configs.email_token_ttl))


def _token_state(token: str, kind: str) -> str:
    """返回 token 状态：ok / invalid / expired / used。"""
    row = store.get_email_token(token) if token else None
    if not row or row.get("kind") != kind:
        return "invalid"
    if row.get("used"):
        return "used"
    if int(row.get("expires_at") or 0) < int(time.time()):
        return "expired"
    return "ok"


def account_is_disabled(email: str) -> bool:
    """账号是否处于「停用」状态（封禁）。

    封禁必须是一条**单向**的状态：任何「证明你是本人」的流程（验证邮箱、重置密码）
    都只该恢复登录能力，而不该顺手把人解封。否则封禁形同虚设 —— 被封的人控制着
    自己的邮箱，走一遍找回密码就把自己放回来了。

    查不到行按「未停用」处理：调用方各自有「账号不存在」的处理，语义不在这里混淆。
    """
    row = store.get_user_auth(email) or {}
    return (row.get("status") or "").strip().lower() in _REVOKED_STATUSES


@app.get("/verify-email", response_class=HTMLResponse)
async def verify_email_page(request: Request, token: str = ""):
    """邮箱验证链接落地页：校验 token → 置 status=active（被封禁账号除外）。"""
    state = _token_state(token, "verify")
    email = ""
    if state == "ok":
        row = store.get_email_token(token)
        email = row.get("email") or ""
        store.mark_email_token_used(token)
        if account_is_disabled(email):
            # 验证邮箱只该把 unverified 变 active，不该覆盖 banned。
            logger.warning("[user] verify-email ignored for a disabled account")
        else:
            store.upsert_user_auth(email, status="active")
    return _render_with_csrf(request, "verify_email.html", {"state": state, "email": email, "dev_link": ""})


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return _render_with_csrf(request, "forgot_password.html", {"error": None, "sent": False, "dev_link": ""})


@app.post("/forgot-password")
async def forgot_password(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    _verify_csrf(request, form.get("csrf_token") or "")
    if not ratelimit.allow(f"forgot:{_client_ip(request)}", configs.register_rate_limit):
        return _render_with_csrf(
            request, "forgot_password.html",
            {"error": "请求过于频繁，请稍后再试", "sent": False, "dev_link": ""}, status_code=429,
        )
    # 账号级冷却：只有 IP 桶的话，攻击者换一批 IP 就能对着受害者的邮箱狂发重置信。
    # 静默丢弃而不是回 429 —— 回 429 等于确认「这个邮箱存在且刚被请求过」，
    # 把下面那条「不暴露邮箱是否存在」的口径拆穿了。
    if email and not ratelimit.allow(f"forgot:email:{email}", _FORGOT_EMAIL_LIMIT,
                                     _forgot_email_window()):
        return _render_with_csrf(
            request, "forgot_password.html", {"error": None, "sent": True, "dev_link": ""})
    dev_link = ""
    # 不暴露邮箱是否存在：无论有无账号都提示「已发送」
    if store.get_user_auth(email):
        token = pysecrets.token_urlsafe(32)
        store.create_email_token(token, email, "reset", int(time.time()) + configs.email_token_ttl)
        sent = await mailer.send_reset_email(email, token)
        dev_link = _dev_link(f"/reset-password?token={token}")
    return _render_with_csrf(request, "forgot_password.html", {"error": None, "sent": True, "dev_link": dev_link})


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    state = _token_state(token, "reset")
    if state != "ok":
        return _render_with_csrf(
            request, "reset_password.html",
            {"error": "链接无效或已过期，请重新申请", "token": "", "done": False},
        )
    return _render_with_csrf(request, "reset_password.html", {"error": None, "token": token, "done": False})


@app.post("/reset-password")
async def reset_password(request: Request):
    form = await request.form()
    token = form.get("token") or ""
    password = form.get("password") or ""
    _verify_csrf(request, form.get("csrf_token") or "")
    state = _token_state(token, "reset")
    if state != "ok":
        return _render_with_csrf(
            request, "reset_password.html",
            {"error": "链接无效或已过期，请重新申请", "token": "", "done": False}, status_code=400,
        )
    if len(password) < 8:
        return _render_with_csrf(
            request, "reset_password.html",
            {"error": "密码至少 8 位", "token": token, "done": False}, status_code=400,
        )
    row = store.get_email_token(token)
    target = row.get("email")
    # 被封禁 / 冻结的账号不能靠「重置密码」复活。写在最前面：不 bump、不作废 token、
    # 不写新密码 —— 被封的人控制着自己的邮箱，这条路径若能把人放回来，封禁就是装饰。
    if account_is_disabled(target):
        logger.warning("[user] reset-password refused for a disabled account")
        return _render_with_csrf(
            request, "reset_password.html",
            {"error": "该账号已被停用，如有疑问请联系客服", "token": "", "done": False},
            status_code=403,
        )
    # 先吊销会话再作废 token：bump 失败时 token 还活着，用户原地重试即可，
    # 不用重走一遍 forgot 流程 —— 别把罕见的 DB 故障摊给刚证明了邮箱所有权的人。
    try:
        store.bump_pw_version(target)
    except store.StoreError as e:
        logger.error(f"[user] reset-password 吊销旧会话失败 {target}: {e}")
        return _render_with_csrf(
            request, "reset_password.html",
            {"error": "旧登录状态未能全部失效，密码未修改，请重试", "token": token, "done": False},
            status_code=500,
        )
    store.mark_email_token_used(token)
    # 重置密码即证明邮箱控制权 → 一并置为 active（上一段已挡住停用账号）。
    # strict：写不进去就不能报「已完成」。静默失败的话页面说改好了，实际密码没变 ——
    # 用户拿新密码登录 401、旧密码却还能用，比直接报错难排查得多。
    try:
        store.upsert_user_auth(
            target, password_hash=hash_password(password), status="active", strict=True)
    except store.StoreError as e:
        logger.error(f"[user] reset-password 写新密码失败 {target}: {e}")
        return _render_with_csrf(
            request, "reset_password.html",
            {"error": "密码未能保存，请重新申请重置链接", "token": "", "done": False},
            status_code=500,
        )
    # 清掉登录失败计数：这是被别人灌满账号桶的用户唯一的回家路（攻击者控制不了受害者的邮箱），
    # 不清的话重置完了照样登不进去，等于这条逃生口是假的。
    ratelimit.reset(_signin_email_key(target))
    return _render_with_csrf(request, "reset_password.html", {"error": None, "token": "", "done": True})


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    email = _current_email(request)
    row = store.get_user_auth(email) or {}
    seed = row.get("seed") or ""
    tier_id = normalize_tier_id(row.get("tier_id"))
    tier = get_tier(tier_id) or {}
    used = user_usage_total(seed, tier_id)
    limit = tier.get("quota_limit")
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "email": email,
            "seed": seed,
            "tier_id": tier_id,
            "tier_label": tier.get("label") or tier_id,
            "models": tier.get("models") or [],
            "used": used,
            "quota_limit": limit,
            "status": row.get("status") or "active",
        },
    )
