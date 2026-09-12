import hashlib
import json
import random
import time

import jwt
from fastapi import Request, HTTPException, Security
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials

import utils.globals as globals
import utils.store as store
from app import app, security_scheme
from chatgpt.authorization import verify_token, OPERATOR_SEED_STATUS
from chatgpt.fp import extract_header_fp, get_fp
from gateway.reverseProxy import get_real_req_token
from utils.Client import Client
from utils.Logger import logger
from utils.configs import proxy_url_list, chatgpt_base_url_list, authorization_list, accept_language, oai_language
from utils.routing import get_bound_proxy

base_headers = {
    'accept': '*/*',
    'accept-encoding': 'gzip, deflate, br, zstd',
    'accept-language': accept_language,
    'content-type': 'application/json',
    'oai-language': oai_language,
    'priority': 'u=1, i',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-origin',
}


def verify_authorization(bearer_token):
    if not bearer_token:
        raise HTTPException(status_code=401, detail="Authorization header is missing")
    if bearer_token not in authorization_list:
        raise HTTPException(status_code=401, detail="Invalid authorization")


def is_direct_upstream_credential(value) -> bool:
    """值本身是否已经是一个上游凭据（可以直接交给上游，无需号池参与）。

    判据抄自 ``get_real_req_token`` 的放行分支（该模块不在本次改动范围，因此这里
    存一份并与它一起钉在测试里）：AUTO_SEED 下，只有这个形状的值会原样放行，其余
    字符串会被当成 Seed 送进号池分配器；非 AUTO_SEED 下非 Seed 值直接 401，同样到
    不了分配器。两种模式下「非凭据输入不可能换来一个号池账号」都成立，这也正是本
    路由需要的性质。

    注意 ``fk-`` 前缀的 AccessToken 故意不在放行之列：号池的放行分支不认它，本路由
    若放行就会把它交给分配器。宁可 401（fail-closed）。
    """
    return isinstance(value, str) and (len(value) == 45 or value.startswith("eyJhbGciOi"))


@app.get("/seedtoken")
async def get_seedtoken(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    verify_authorization(credentials.credentials)
    try:
        params = request.query_params
        seed = params.get("seed")

        if seed:
            if seed not in globals.seed_map:
                raise HTTPException(status_code=404, detail=f"Seed '{seed}' not found")
            return {
                "status": "success",
                "data": {
                    "seed": seed,
                    "token": globals.seed_map[seed]["token"]
                }
            }

        token_map = {
            seed: data["token"]
            for seed, data in globals.seed_map.items()
        }
        return {"status": "success", "data": token_map}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/seedtoken")
async def set_seedtoken(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    """运营者导入：建立 seed → 账号 绑定，并写下**显式授权标记**。

    ``AUTHORIZATION`` 鉴权通过的事实就是授权本身，因此这条路径是唯一会写
    ``OPERATOR_SEED_STATUS`` 的地方（见 ``chatgpt/authorization.py``）。幂等：
    对历史遗留（升级前匿名分配/迁移落库）的 seed 再导入一次即重新授权。
    """
    verify_authorization(credentials.credentials)
    data = await request.json()

    seed = data.get("seed")
    token = data.get("token")

    if not isinstance(seed, str) or not seed:
        raise HTTPException(status_code=400, detail="Missing required field: seed")

    if seed not in globals.seed_map:
        globals.seed_map[seed] = {
            "token": token,
            "conversations": []
        }
    else:
        globals.seed_map[seed]["token"] = token

    globals.persist_seed_map()
    store.upsert_user(seed, status=OPERATOR_SEED_STATUS)

    return {"status": "success", "message": "Token updated successfully"}


@app.delete("/seedtoken")
async def delete_seedtoken(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    verify_authorization(credentials.credentials)

    try:
        data = await request.json()
        seed = data.get("seed")

        if seed == "clear":
            # 显式原子吊销全部运营者授权。**不能**指望 persist_seed_map 的 list_users
            # 顺带扫到：那个读取把故障折叠成空表，撤销一行都不会发生，而这里照样回
            # success —— 授权（users.status 标记）才是号池的钥匙，不是内存绑定。
            # 先吊销再清内存：吊销失败时两边都保持原状，调用方拿到 503 而不是一个
            # 「看起来成功」的空绑定表。
            try:
                store.delete_operator_grants(OPERATOR_SEED_STATUS)
            except store.StoreError:
                raise HTTPException(status_code=503, detail="Seed revocation unavailable") from None
            globals.seed_map.clear()
            globals.persist_seed_map()
            return {"status": "success", "message": "All seeds deleted successfully"}

        if not seed:
            raise HTTPException(status_code=400, detail="Missing required field: seed")

        # 名字吊销走显式的持久化撤销（单事务，失败即 503），不依赖 delete_user /
        # persist_seed_map 的静默失败路径：授权是钥匙，绑定只是缓存。
        try:
            revoked = store.delete_operator_grant(seed, OPERATOR_SEED_STATUS)
        except store.StoreError:
            raise HTTPException(status_code=503, detail="Seed revocation unavailable") from None

        if seed in globals.seed_map:
            del globals.seed_map[seed]
            globals.persist_seed_map()
            # 历史绑定（含非运营者的）连同会话一并清掉，保持既有语义。
            store.delete_user(seed)
        elif not revoked:
            # 内存里没有绑定、库里也没有授权：这才是真正的「没有这个 seed」。
            # 反过来，持久授权可能比内存绑定活得久（persist 失败 / 重启 / 直接改库），
            # 那种情况必须能按名字吊销，否则删不掉的授权会永远留在库里。
            raise HTTPException(status_code=404, detail=f"Seed '{seed}' not found")

        return {
            "status": "success",
            "message": f"Seed '{seed}' deleted successfully"
        }

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON data")
    except HTTPException:
        # 本路由刻意抛出的 400/404/503 必须原样送出；下面那条兜底会把它们改写成 500，
        # 让「没有这个 seed」和「吊销服务不可用」丢掉各自的语义。
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


async def chatgpt_account_check(access_token):
    # 账号对象匿名化与 /backend-api/accounts/check 共用同一份契约；在**取回处**抹除，
    # 调用方不需要记得擦，未来新增调用方也不会重新开这个洞。
    # 必须延迟导入：gateway.backend 末尾注册了 catch-all 路由，而 app.py 刻意把它放在
    # 最后导入，让 share 的路由先注册；在模块级导入 backend 会把 catch-all 提到
    # /seedtoken、/auth/refresh 之前，把这两个路由整个盖掉。
    from gateway.backend import sanitize_account_check

    # 「绝不为非凭据分配号池账号」是这条函数的性质，而不是调用方的纪律：OAuth 那一段
    # 拿回来的值也要过同一道形状检查，否则换条腿就能重新踩进号池分配分支。
    if not is_direct_upstream_credential(access_token):
        return {}

    auth_info = {}
    client = Client(proxy=random.choice(proxy_url_list) if proxy_url_list else None)
    try:
        host_url = random.choice(chatgpt_base_url_list) if chatgpt_base_url_list else "https://chatgpt.com"
        req_token = await get_real_req_token(access_token)
        access_token = await verify_token(req_token)
        fp = get_fp(req_token).copy()
        proxy_url = fp.pop("proxy_url", None)
        impersonate = fp.pop("impersonate", "safari15_3")

        headers = base_headers.copy()
        headers.update(extract_header_fp(fp))
        headers.update({"authorization": f"Bearer {access_token}"})

        session_id = hashlib.md5(access_token.encode()).hexdigest()
        proxy_url = proxy_url.replace("{}", session_id) if proxy_url else None
        client = Client(proxy=proxy_url, impersonate=impersonate)
        r = await client.get(f"{host_url}/backend-api/models?history_and_training_disabled=false", headers=headers,
                             timeout=10)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        models = r.json()
        r = await client.get(f"{host_url}/backend-api/accounts/check/v4-2023-04-27", headers=headers, timeout=10)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        accounts_info = sanitize_account_check(r.json())

        auth_info.update({"models": models["models"]})
        auth_info.update({"accounts_info": accounts_info})

        account_ordering = accounts_info.get("account_ordering", [])
        is_deactivated = True
        plan_type = None
        team_ids = []
        for account in account_ordering:
            this_is_deactivated = accounts_info['accounts'].get(account, {}).get("account", {}).get("is_deactivated", False)
            this_plan_type = accounts_info['accounts'].get(account, {}).get("account", {}).get("plan_type", "free")

            if not this_is_deactivated:
                is_deactivated = False

            if "team" in this_plan_type and not this_is_deactivated:
                plan_type = this_plan_type
                team_ids.append(account)
            elif plan_type is None:
                plan_type = this_plan_type

        auth_info.update({"accountCheckInfo": {
            "is_deactivated": is_deactivated,
            "plan_type": plan_type,
            "team_ids": team_ids
        }})

        return auth_info
    except Exception as e:
        logger.error(f"chatgpt_account_check: {e}")
        return {}
    finally:
        await client.close()


async def chatgpt_refresh(refresh_token):
    session_id = hashlib.md5(refresh_token.encode()).hexdigest()
    bound_proxy = get_bound_proxy(refresh_token)
    proxy_url = bound_proxy or (random.choice(proxy_url_list).replace("{}", session_id) if proxy_url_list else None)
    if proxy_url:
        proxy_url = proxy_url.replace("{}", session_id)
    client = Client(proxy=proxy_url)
    try:
        data = {
            "client_id": "pdlLIX2Y72MIl2rhLhTE9VV9bN905kBh",
            "grant_type": "refresh_token",
            "redirect_uri": "com.openai.chat://auth0.openai.com/ios/com.openai.chat/callback",
            "refresh_token": refresh_token
        }
        r = await client.post("https://auth0.openai.com/oauth/token", json=data, timeout=10)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        res = r.json()
        auth_info = {}
        auth_info.update(res)
        auth_info.update({"refresh_token": refresh_token})
        auth_info.update({"accessToken": res.get("access_token", "")})
        return auth_info
    except Exception as e:
        logger.error(f"chatgpt_refresh: {e}")
        return {}
    finally:
        await client.close()


@app.post("/auth/refresh")
async def refresh(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    # 与 /seedtoken 同一把钥匙：这个路由会换取上游凭据并驱动号池，属运营者接口，
    # 不是镜像用户接口。此前它无任何鉴权依赖，任何人 POST 一个字符串即可触达号池。
    verify_authorization(credentials.credentials)

    auth_info = {}
    form_data = await request.form()

    auth_info.update(form_data)

    # 只接受「本身已是上游凭据」的值。Seed（或任意字符串）不是凭据：把它交给
    # get_real_req_token 会走号池分配分支，为一个伪造的 Seed 绑定一个健康账号，
    # 并用该账号的 Bearer 去打上游。形状不符一律当作「没给凭据」处理。
    access_token = auth_info.get("access_token", auth_info.get("accessToken", ""))
    if not is_direct_upstream_credential(access_token):
        access_token = ""
    refresh_token = auth_info.get("refresh_token", "")

    if not refresh_token and not access_token:
        raise HTTPException(status_code=401, detail="refresh_token or access_token is required")

    need_refresh = True
    if access_token:
        try:
            access_token_info = jwt.decode(access_token, options={"verify_signature": False})
            exp = access_token_info.get("exp", 0)
            if exp > int(time.time()) + 60 * 60 * 24 * 5:
                need_refresh = False
        except Exception as e:
            logger.error(f"access_token: {e}")

    if refresh_token and need_refresh:
        chatgpt_refresh_info = await chatgpt_refresh(refresh_token)
        if chatgpt_refresh_info:
            auth_info.update(chatgpt_refresh_info)
            access_token = auth_info.get("accessToken", "")
            account_check_info = await chatgpt_account_check(access_token)
            if account_check_info:
                auth_info.update(account_check_info)
                auth_info.update({"accessToken": access_token})
                return Response(content=json.dumps(auth_info), media_type="application/json")
    elif access_token:
        account_check_info = await chatgpt_account_check(access_token)
        if account_check_info:
            auth_info.update(account_check_info)
            auth_info.update({"accessToken": access_token})
            return Response(content=json.dumps(auth_info), media_type="application/json")

    raise HTTPException(status_code=401, detail="Unauthorized")
