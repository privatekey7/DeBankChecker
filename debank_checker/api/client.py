"""
DeBank API Client
Подпись HMAC-SHA256, curl_cffi (impersonate chrome).
Прокси обязателен.
"""

import hashlib
import hmac as hmac_lib
import json
import random
import time
import uuid
from typing import Optional

import curl_cffi.requests as cffi_requests

from debank_checker.config import (
    API_KEY_INIT,
    NFT_POLL_INTERVAL,
    NFT_POLL_MAX_WAIT,
    NFT_REQUEST_TIMEOUT,
    REQUEST_TIMEOUT,
)

API_BASE = "https://api.debank.com"
NONCE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXTZabcdefghiklmnopqrstuvwxyz"
NONCE_LENGTH = 40


def sort_params(params: dict) -> str:
    if not params:
        return ""
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hmac_sha256(key_str: str, msg_str: str) -> str:
    return hmac_lib.new(
        key_str.encode("utf-8"),
        msg_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_nonce() -> str:
    return "n_" + "".join(random.choices(NONCE_ALPHABET, k=NONCE_LENGTH))


def sign_request(
    params: dict,
    method: str,
    path: str,
    nonce: str | None = None,
    ts: int | None = None,
    version: str = "v2",
) -> dict:
    ts = ts or int(time.time())
    nonce = nonce or generate_nonce()
    prefix = "debank-web\n" if version == "v2.1" else "debank-api\n"
    sorted_p = sort_params(params)
    K = sha256_hex(f"{prefix}{nonce}\n{ts}")
    M = sha256_hex(f"{method.upper()}\n{path}\n{sorted_p}")
    signature = hmac_sha256(K, M)
    return {"signature": signature, "nonce": nonce, "ts": ts, "version": version}


class DeBankClient:
    """Клиент DeBank API. Прокси обязателен."""

    def __init__(self, proxy: str, impersonate: str = "chrome124"):
        if not proxy:
            raise ValueError("Прокси обязателен для DeBank API")
        self._api_key = API_KEY_INIT
        self._init_ts = int(time.time())
        self._random_at = self._init_ts
        self._random_id = uuid.uuid4().hex
        self._impersonate = impersonate
        proxies = {"https": proxy, "http": proxy}
        self._session = cffi_requests.Session(
            impersonate=impersonate,
            proxies=proxies,
        )

    def _build_headers(self, params: dict, method: str, path: str) -> dict:
        sign = sign_request(params, method, path)
        account = json.dumps(
            {
                "random_at": self._random_at,
                "random_id": self._random_id,
                "user_addr": None,
                "connected_addr": None,
            },
            separators=(",", ":"),
        )
        return {
            "Referer": "https://debank.com/",
            "Origin": "https://debank.com",
            "X-API-Key": self._api_key,
            "X-API-Time": str(self._init_ts),
            "x-api-ts": str(sign["ts"]),
            "x-api-nonce": sign["nonce"],
            "x-api-ver": sign["version"],
            "x-api-sign": sign["signature"],
            "source": "web",
            "account": account,
        }

    def _get(self, path: str, params: dict | None = None, timeout: float | None = None) -> dict:
        params = params or {}
        headers = self._build_headers(params, "GET", path)
        resp = self._session.get(
            API_BASE + path,
            params=params,
            headers=headers,
            timeout=timeout or REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        new_key = resp.headers.get("x-set-api-key")
        if new_key:
            self._api_key = new_key

        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def get_total_balance_cache(self, address: str) -> list:
        """Кэшированный список токенов по всем сетям."""
        result = self._get("/token/cache_balance_list", {"user_addr": address})
        return result if isinstance(result, list) else []

    def get_portfolio(self, address: str) -> list:
        """DeFi протоколы с позициями."""
        result = self._get("/portfolio/project_list", {"user_addr": address})
        return result if isinstance(result, list) else []

    def get_nft_used_chains(self, address: str) -> list:
        """Сети, в которых у пользователя есть NFT."""
        result = self._get("/nft/used_chains", {"user_addr": address})
        if isinstance(result, list):
            return [
                c.get("id") if isinstance(c, dict) else str(c)
                for c in result
                if isinstance(c, (dict, str))
            ]
        return []

    def get_nft_collection_list(self, address: str, chain: str) -> list:
        """
        NFT-коллекции по адресу и сети.
        API может вернуть async job (pending) — тогда polling с retry.
        Результат: list из result["data"] или result (если list).
        """
        params = {"user_addr": address, "chain": chain}
        deadline = time.time() + NFT_POLL_MAX_WAIT

        def _extract_list(val) -> list:
            if isinstance(val, list):
                return val
            if isinstance(val, dict) and "data" in val:
                d = val["data"]
                return d if isinstance(d, list) else []
            return []

        while True:
            result = self._get("/nft/collection_list", params, timeout=NFT_REQUEST_TIMEOUT)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                job = result.get("job") or {}
                status = job.get("status") if job else None
                res = result.get("result")
                if res is not None:
                    return _extract_list(res)
                if status == "pending" and time.time() < deadline:
                    time.sleep(NFT_POLL_INTERVAL)
                    continue
            return []
