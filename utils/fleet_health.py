"""Periodic account health check (fleet 健康检查).

Probes every account and writes a three-state status into ``accounts.status``:
  healthy   - /backend-api/me probe succeeded
  unhealthy - circuit-dead, in the error list, or the probe failed
  disabled  - manual override; never probed or overwritten here

This module *reads* the antiban verdicts (dead list, error list, cooldown) rather
than re-deriving them. It is a liveness probe, not a second health model.
"""

import asyncio
import hashlib
import random
import time

from fastapi import HTTPException

import utils.globals as globals
import utils.store as store
from chatgpt.authorization import verify_token
from utils import configs
from utils.Client import Client
from utils.Logger import logger
from utils.antiban import circuit as antiban_circuit
from utils.routing import get_bound_proxy


def _resolve_proxy(token: str):
    """Bound proxy if set, else a random pool proxy ({} filled with the session id)."""
    session_id = hashlib.md5(token.encode()).hexdigest()
    bound = get_bound_proxy(token)
    proxy = bound or (random.choice(configs.proxy_url_list) if configs.proxy_url_list else None)
    if proxy:
        proxy = proxy.replace("{}", session_id)
    return proxy


async def _probe_account(token: str, access_token: str, proxy_url) -> bool:
    """Lightweight /backend-api/me probe. Returns True when the account is alive."""
    base_url = (
        random.choice(configs.chatgpt_base_url_list)
        if configs.chatgpt_base_url_list else "https://chatgpt.com"
    )
    client = Client(proxy=proxy_url, impersonate="safari15_3")
    try:
        r = await client.get(
            f"{base_url}/backend-api/me",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        logger.info(f"[health] token={token[:12]}... /me status={r.status_code}")
        return False
    except Exception as e:
        logger.info(f"[health] token={token[:12]}... probe error: {str(e)[:120]}")
        return False
    finally:
        await client.close()


async def check_account(token: str) -> str:
    """Return the health status of one account (no status side effects)."""
    # Permanent dead (circuit breaker verdict) -> unhealthy.
    if antiban_circuit.is_token_dead(token):
        return "unhealthy"
    # Soft dead / refreshable error -> unhealthy; only the refresh flow recovers it.
    if token in globals.error_token_list:
        return "unhealthy"

    try:
        access_token = await verify_token(token)
    except HTTPException:
        return "unhealthy"
    if not access_token:
        return "unhealthy"

    if await _probe_account(token, access_token, _resolve_proxy(token)):
        return "healthy"
    return "unhealthy"


async def check_all_accounts():
    """Probe every account and sync status + last_health_check. Disabled are skipped."""
    now = int(time.time())
    summary = {"healthy": 0, "unhealthy": 0, "skipped_disabled": 0}
    for token in list(globals.token_list):
        acct = store.get_account(token)
        if (acct or {}).get("status") == "disabled":
            summary["skipped_disabled"] += 1
            continue
        status = await check_account(token)
        store.upsert_account(token, status=status, last_health_check=now)
        summary[status] += 1
        # Gentle pacing so a large fleet does not hammer the upstream at once.
        await asyncio.sleep(0.2)
    logger.info(
        f"[health] fleet probe done: healthy={summary['healthy']} "
        f"unhealthy={summary['unhealthy']} disabled={summary['skipped_disabled']}"
    )
    return summary
