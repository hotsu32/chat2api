import json
import time
from datetime import datetime, timezone

import utils.globals as globals
import utils.store as store
from utils.Logger import logger
from utils.token_type import detect_token_type


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def mask_token(token):
    if not token:
        return ""
    if len(token) <= 12:
        return token
    return f"{token[:6]}...{token[-4:]}"


def format_refresh_time(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_routing_config():
    config = globals.routing_config or {}
    config.setdefault("proxies", [])
    config.setdefault("groups", [])
    config.setdefault("bindings", {})
    config.setdefault("account_meta", {})
    config.setdefault("updated_at", None)
    return config


def save_routing_config(config):
    config["updated_at"] = utc_now()
    globals.routing_config = config
    globals.persist_routing_config()


def resolve_group_name(config, proxy_name, proxy_url):
    for group in config.get("groups", []):
        if group.get("proxy_url") == proxy_url:
            return group.get("name")
    return f"Group {proxy_name}"


def build_group_assignments(tokens, proxies, group_size):
    group_size = max(int(group_size or 1), 1)
    existing_config = get_routing_config()
    bindings = {}
    groups = []
    normalized_proxies = []

    for index, proxy in enumerate(proxies):
        proxy_name = (proxy.get("name") or f"IP-{index + 1}").strip()
        proxy_url = (proxy.get("proxy_url") or "").strip()
        if not proxy_url:
            continue
        normalized_proxies.append({
            "id": f"proxy-{index + 1}",
            "name": proxy_name,
            "proxy_url": proxy_url,
        })

    for proxy_index, proxy in enumerate(normalized_proxies):
        start = proxy_index * group_size
        end = min(start + group_size, len(tokens))
        group_tokens = tokens[start:end]
        if not group_tokens:
            break
        group_name = f"Group {chr(65 + proxy_index)}"
        groups.append({
            "id": f"group-{proxy_index + 1}",
            "name": group_name,
            "proxy_name": proxy["name"],
            "proxy_url": proxy["proxy_url"],
            "size": len(group_tokens),
            "strategy": "fixed",
            "status": "enabled",
        })
        for token in group_tokens:
            previous_meta = existing_config.get("account_meta", {}).get(token, {})
            bindings[token] = {
                "group": group_name,
                "proxy_name": proxy["name"],
                "proxy_url": proxy["proxy_url"],
                "updated_at": utc_now(),
                "note": previous_meta.get("note", ""),
            }

    return {
        "proxies": normalized_proxies,
        "groups": groups,
        "bindings": bindings,
        "account_meta": existing_config.get("account_meta", {}),
    }


def sync_bindings_to_fp(bindings):
    changed = False
    for token, binding in bindings.items():
        if not token:
            continue
        fp = globals.fp_map.get(token, {})
        proxy_url = binding.get("proxy_url")
        if fp.get("proxy_url") != proxy_url:
            fp["proxy_url"] = proxy_url
            changed = True
        if binding.get("group"):
            fp["group"] = binding["group"]
        if binding.get("proxy_name"):
            fp["proxy_name"] = binding["proxy_name"]
        fp["updated_at"] = binding.get("updated_at", utc_now())
        globals.fp_map[token] = fp
    if changed:
        logger.info("Routing bindings synced to fp_map.json")
    globals.persist_fp_map()


def update_single_binding(token, proxy_name, proxy_url, group_name=None):
    config = get_routing_config()
    existing_proxy = next((item for item in config.get("proxies", []) if item.get("proxy_url") == proxy_url), None)
    if not existing_proxy:
        config.setdefault("proxies", []).append({
            "id": f"proxy-{len(config.get('proxies', [])) + 1}",
            "name": proxy_name,
            "proxy_url": proxy_url,
        })

    if not group_name:
        group_name = resolve_group_name(config, proxy_name, proxy_url)

    config.setdefault("bindings", {})[token] = {
        "group": group_name,
        "proxy_name": proxy_name,
        "proxy_url": proxy_url,
        "updated_at": utc_now(),
        "note": config.get("account_meta", {}).get(token, {}).get("note", ""),
    }
    save_routing_config(config)
    sync_bindings_to_fp({token: config["bindings"][token]})
    return config["bindings"][token]


def update_account_meta(token, note="", group_name=None, proxy_name=None, proxy_url=None):
    config = get_routing_config()
    meta = config.setdefault("account_meta", {}).get(token, {})
    meta["note"] = (note or "").strip()
    meta["updated_at"] = utc_now()
    config["account_meta"][token] = meta

    if proxy_url:
        binding = config.setdefault("bindings", {}).get(token, {})
        binding.update({
            "group": (group_name or binding.get("group") or resolve_group_name(config, proxy_name or "Custom Proxy", proxy_url)),
            "proxy_name": proxy_name or binding.get("proxy_name") or "Custom Proxy",
            "proxy_url": proxy_url,
            "updated_at": utc_now(),
            "note": meta["note"],
        })
        config["bindings"][token] = binding
    elif token in config.get("bindings", {}):
        config["bindings"][token]["note"] = meta["note"]
        config["bindings"][token]["updated_at"] = utc_now()

    save_routing_config(config)
    if token in config.get("bindings", {}):
        sync_bindings_to_fp({token: config["bindings"][token]})
    return {
        "meta": config["account_meta"].get(token, {}),
        "binding": config.get("bindings", {}).get(token),
    }


def remove_account_binding(token):
    config = get_routing_config()
    removed_binding = config.get("bindings", {}).pop(token, None)
    config.get("account_meta", {}).pop(token, None)

    grouped_rules = {}
    for binding_token, binding in config.get("bindings", {}).items():
        group_name = binding.get("group") or binding.get("proxy_name") or "Ungrouped"
        proxy_name = binding.get("proxy_name", "-")
        proxy_url = binding.get("proxy_url", "")
        rule = grouped_rules.setdefault(group_name, {
            "id": f"group-{len(grouped_rules) + 1}",
            "name": group_name,
            "proxy_name": proxy_name,
            "proxy_url": proxy_url,
            "size": 0,
            "strategy": "fixed",
            "status": "enabled",
        })
        rule["size"] += 1
    config["groups"] = list(grouped_rules.values())
    save_routing_config(config)

    if token in globals.fp_map:
        globals.fp_map.pop(token, None)
        globals.persist_fp_map()

    return removed_binding


def get_bound_proxy(req_token):
    binding = get_routing_config().get("bindings", {}).get(req_token)
    if binding:
        return binding.get("proxy_url")
    return None


def _fleet_health():
    """Lazy import to avoid a circular import (fleet_health -> routing.get_bound_proxy)."""
    from utils import fleet_health
    return fleet_health


# The panel's own vocabulary predates the five-state model and is what the
# template renders, so it stays three-valued. Every restricted state maps to
# 异常; only a state the canonical machine actually proved healthy maps to 正常.
_PANEL_HEALTHY = "正常"
_PANEL_IMPAIRED = "异常"
_PANEL_DISABLED = "停用"


def project_account_status(token, account):
    """``(panel label, canonical status, canonical label)`` for one account.

    The panel used to keep its own three-state mapping over ``accounts.status``
    plus the in-memory error list, which rendered a circuit-dead account (row
    ``status='dead'``, ``antiban_status='dead'``) as 正常 -- an account the Seed
    router refuses to bind. Delegating to ``fleet_health.resolve_account_status``
    makes the operator view and the routing gate read the same state machine.
    """
    health = _fleet_health()
    status = health.resolve_account_status(token, (account or {}).get("status"))
    if status == health.STATUS_DISABLED:
        label = _PANEL_DISABLED
    elif status == health.STATUS_HEALTHY:
        label = _PANEL_HEALTHY
    else:
        label = _PANEL_IMPAIRED
    return label, status, health.status_label(status)


def _antiban_modules():
    """Lazy import to avoid a circular import (routing -> antiban.bucket -> routing)."""
    from utils.antiban import circuit as antiban_circuit
    from utils.antiban import cooldown
    return antiban_circuit, cooldown


def _antiban_status(token):
    """dead/cooling/healthy — read-only projection of the antiban subsystem state."""
    antiban_circuit, cooldown = _antiban_modules()
    if antiban_circuit.is_token_dead(token):
        return "dead"
    next_available = cooldown.get_next_available(token)
    if next_available and next_available > time.time():
        return "cooling"
    return "healthy"


def _dead_reason(token):
    entry = globals.antiban_dead_tokens.get(token) or {}
    return entry.get("reason", "")


def _next_available_at(token):
    _, cooldown = _antiban_modules()
    next_available = cooldown.get_next_available(token)
    if next_available and next_available > time.time():
        return format_refresh_time(next_available)
    return "-"


def get_dashboard_payload():
    config = get_routing_config()
    bindings = config.get("bindings", {})
    proxies = config.get("proxies", [])
    tokens = list(globals.token_list)
    error_tokens = set(globals.error_token_list)
    health = _fleet_health()

    _projected = {}

    def project(token):
        """Memoised canonical projection; also covers bound tokens off token_list."""
        if token not in _projected:
            account = store.get_account(token) or {}
            _projected[token] = project_account_status(token, account)
        return _projected[token]

    for token in tokens:
        project(token)
    healthy_tokens = {t for t in _projected if _projected[t][1] == health.STATUS_HEALTHY}
    impaired_tokens = {t for t in _projected if health.is_impaired(_projected[t][1])}
    dead_tokens = {t for t in _projected if _projected[t][1] == health.STATUS_DEAD}
    disabled_count = len([
        t for t in _projected if _projected[t][1] == health.STATUS_DISABLED
    ])
    grouped_rules = {}

    proxy_stats = []
    for proxy in proxies:
        matched_tokens = [token for token, binding in bindings.items() if binding.get("proxy_url") == proxy["proxy_url"]]
        bad_count = len([token for token in matched_tokens if project(token)[1] in health.IMPAIRED_STATUSES])
        ok_count = len([token for token in matched_tokens if project(token)[1] == health.STATUS_HEALTHY])
        rule_name = None
        if matched_tokens:
            rule_name = bindings[matched_tokens[0]].get("group")
            grouped_rules[rule_name or proxy["name"]] = {
                "name": rule_name or proxy["name"],
                "proxy_name": proxy["name"],
                "proxy_url": proxy["proxy_url"],
                "size": len(matched_tokens),
                "strategy": "fixed",
                "status": "enabled",
            }
        proxy_stats.append({
            "name": proxy["name"],
            "proxy_url": proxy["proxy_url"],
            "accounts": len(matched_tokens),
            "ok": ok_count,
            "bad": bad_count,
            "group": rule_name or "-",
        })

    accounts = []
    for index, token in enumerate(tokens, start=1):
        binding = bindings.get(token, {})
        account_meta = config.get("account_meta", {}).get(token, {})
        acct = store.get_account(token) or {}
        status, account_status, status_label = project(token)
        proxy_name = binding.get("proxy_name", "-")
        proxy_url = binding.get("proxy_url", "")
        group_name = binding.get("group", "-")
        token_type = detect_token_type(token)
        refresh_info = globals.refresh_map.get(token, {}) if token_type == "RefreshToken" else {}
        refresh_status = "-"
        if token_type == "RefreshToken":
            if token in error_tokens:
                refresh_status = "刷新异常"
            elif refresh_info.get("last_success_at") or refresh_info.get("timestamp"):
                refresh_status = "已刷新"
            else:
                refresh_status = "待刷新"
        accounts.append({
            "id": f"acct-{index:03d}",
            "token": token,
            "token_masked": mask_token(token),
            "token_type": token_type,
            "status": status,
            "account_status": account_status,
            "status_label": status_label,
            "plan_type": acct.get("plan_type") or "-",
            "nickname": acct.get("nickname") or "",
            "antiban_status": _antiban_status(token),
            "dead_reason": _dead_reason(token),
            "next_available_at": _next_available_at(token),
            "usage_count": store.query_usage_count(account=token),
            "proxy_name": proxy_name,
            "proxy_url": proxy_url,
            "group": group_name,
            "note": account_meta.get("note", binding.get("note", "")),
            "updated_at": binding.get("updated_at") or globals.fp_map.get(token, {}).get("updated_at") or "-",
            "refresh_status": refresh_status,
            "refresh_updated_at": format_refresh_time(
                refresh_info.get("last_success_at") or refresh_info.get("timestamp")
            ),
            "refresh_error": refresh_info.get("last_error", ""),
            "refresh_fail_count": refresh_info.get("fail_count", 0),
            "can_refresh": token_type == "RefreshToken",
        })

    users = []
    for user in store.list_users():
        seed = user.get("seed") or ""
        current_account = user.get("current_account") or ""
        users.append({
            "seed": seed,
            "seed_masked": mask_token(seed),
            "tier": user.get("plan_type") or "unknown",
            "current_account": current_account,
            "current_account_masked": mask_token(current_account),
            "conversation_count": len(store.list_seed_conversations(seed)),
            "usage_count": store.query_usage_count(seed=seed),
            "status": user.get("status") or "active",
        })

    alerts = []
    if impaired_tokens:
        alerts.append(f"当前有 {len(impaired_tokens)} 个异常账号，建议优先检查刷新状态。")
    if dead_tokens:
        alerts.append(f"其中有 {len(dead_tokens)} 个账号已被熔断，需重新取证后才可恢复接量。")
    unbound_count = max(len(tokens) - len(bindings), 0)
    if unbound_count:
        alerts.append(f"还有 {unbound_count} 个账号未绑定固定代理。")
    if not alerts:
        alerts.append("当前未发现异常告警。")

    refresh_token_count = len([token for token in tokens if detect_token_type(token) == "RefreshToken"])
    stale_refresh_count = len([
        token for token in tokens
        if detect_token_type(token) == "RefreshToken"
        and not globals.refresh_map.get(token, {}).get("last_success_at")
        and not globals.refresh_map.get(token, {}).get("timestamp")
    ])
    if refresh_token_count:
        alerts.append(f"当前共导入 {refresh_token_count} 个 RefreshToken。")
    if stale_refresh_count:
        alerts.append(f"其中有 {stale_refresh_count} 个 RefreshToken 还没有成功刷新过。")

    return {
        "summary": {
            "accounts_total": len(tokens),
            "accounts_ok": len(healthy_tokens),
            "accounts_bad": len(impaired_tokens),
            "accounts_disabled": disabled_count,
            "proxy_total": len(proxies),
            "group_total": len(grouped_rules),
            "bound_total": len(bindings),
            "users_total": len(users),
        },
        "ip_cards": proxy_stats,
        "accounts": accounts,
        "users": users,
        "rules": list(grouped_rules.values()),
        "alerts": alerts,
        "updated_at": config.get("updated_at"),
    }
