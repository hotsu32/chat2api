"""IP 信誉（IPQS 欺诈分 + ASN）——IP 前置过滤。

目的：识别数据中心/垃圾出口 IP，避免把账号分到高风险 IP（会被 OpenAI 风控连带）。
策略：
  1. 从 proxy_url 解析 host + resolve 出 IP；
  2. 若配置了 IPQS_API_KEY，查 ipqualityscore.com 的 ip lookup，返回 fraud_score /
     is_datacenter / is_proxy / ASN；
  3. 结果缓存到 data/antiban_iprep.json，TTL=IP_REP_CACHE_TTL_DAYS；
  4. fail-open：无 key / 查询失败 / 超时 → 返回 None（未知），不阻塞分配；
  5. 前置过滤：fraud_score >= IPQS_FRAUD_THRESHOLD（或按开关 is_datacenter/is_proxy）
     → 判定 blocked，由 bucket 层跳过该桶。

输出结构（缓存）：
  {
    "host": "1.2.3.4",
    "fraud_score": 12,
    "is_datacenter": false,
    "is_proxy": false,
    "asn": 24940,
    "isp": "Hetzner Online GmbH",
    "organization": "Hetzner",
    "_ts": <unix ts>
  }
"""

import json
import socket
import threading
import time
from typing import Dict, Optional
from urllib import request as urllib_request
from urllib.error import URLError
from urllib.parse import urlencode

import utils.globals as globals
from utils import configs
from utils.Logger import logger
from utils.antiban.geo import _extract_host, _resolve_host

_write_lock = threading.Lock()


def _persist() -> None:
    with _write_lock:
        with open(globals.ANTIBAN_IPREP_FILE, "w", encoding="utf-8") as f:
            json.dump(globals.antiban_iprep_cache, f, indent=2, ensure_ascii=False)


def _query_ipqs(ip: str) -> Optional[Dict]:
    """IPQS ip lookup。返回原始字段子集；失败/无 key 返回 None（fail-open）。"""
    if not configs.ipqs_api_key:
        return None
    try:
        params = urlencode({
            "key": configs.ipqs_api_key,
            "ip": ip,
            "strictness": 1,
        })
        url = f"https://www.ipqualityscore.com/api/json/ip?{params}"
        with urllib_request.urlopen(url, timeout=configs.ipqs_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("success"):
            logger.info(f"[antiban][iprep] IPQS rejected {ip}: {data.get('message')}")
            return None
        return {
            "fraud_score": data.get("fraud_score"),
            "is_datacenter": data.get("is_datacenter"),
            "is_proxy": data.get("is_proxy"),
            "asn": data.get("ASN"),
            "isp": data.get("ISP"),
            "organization": data.get("organization"),
        }
    except (URLError, socket.timeout, json.JSONDecodeError) as e:
        logger.info(f"[antiban][iprep] IPQS query failed for {ip}: {e}")
    except Exception as e:  # pragma: no cover
        logger.warning(f"[antiban][iprep] IPQS unexpected error: {e}")
    return None


def get_reputation(proxy_url: Optional[str]) -> Optional[Dict]:
    """返回 proxy IP 的信誉（带缓存，fail-open）。未配置 key 时不发任何网络请求。"""
    if not configs.enable_antiban or not proxy_url:
        return None
    if not configs.ipqs_api_key:
        return None

    host = _extract_host(proxy_url)
    if not host:
        return None

    cache = globals.antiban_iprep_cache
    ttl = configs.ip_rep_cache_ttl_days * 86400
    cached = cache.get(host)
    if cached and time.time() - cached.get("_ts", 0) < ttl:
        return cached

    ip = _resolve_host(host)
    if not ip:
        return None

    rep = _query_ipqs(ip)
    if not rep:
        return None
    rep["_ts"] = int(time.time())
    cache[host] = rep
    try:
        _persist()
    except Exception as e:  # pragma: no cover
        logger.error(f"[antiban][iprep] persist failed: {e}")
    logger.info(f"[antiban][iprep] {host} fraud={rep.get('fraud_score')} dc={rep.get('is_datacenter')}")
    return rep


def is_blocked(proxy_url: Optional[str]) -> bool:
    """是否因信誉判黑而应跳过该 IP。fail-open：查不到 / 无 key → False。"""
    rep = get_reputation(proxy_url)
    if not rep:
        return False
    fs = rep.get("fraud_score")
    if isinstance(fs, (int, float)) and fs >= configs.ipqs_fraud_threshold:
        return True
    if configs.ipqs_block_datacenter and rep.get("is_datacenter"):
        return True
    if configs.ipqs_block_proxy and rep.get("is_proxy"):
        return True
    return False
