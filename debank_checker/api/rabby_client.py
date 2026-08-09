"""
Rabby API Client (api.rabby.io).

Схема подписи запросов ИДЕНТИЧНА DeBank (см. api/client.py), меняются только
префикс (`rabby-api\\n`) и набор заголовков идентификации клиента
(`x-client`/`x-version` вместо `source`/`account`). Проверено воспроизведением
подписи из HAR веб-версии Rabby байт-в-байт (65/68 запросов; расхождения — в
неиспользуемых эндпоинтах has_new_tx/history_list).

Зачем миграция: DeBank-чекер считал total ВРУЧНУЮ (tokens_usd + protocols_usd
из двух запросов), а под высокой конкуренцией DeBank изредка отдаёт ответ от
чужого адреса — так на кошельке с $15 «появлялись» миллионы. Rabby отдаёт
готовый агрегированный `total_usd_value` одним запросом
(`/v1/user/total_balance?is_core=true`), поэтому итоговая сумма больше не
зависит от ручного суммирования потенциально загрязнённых ответов.

Прокси обязателен (как и в DeBank-клиенте).
"""

import hashlib
import hmac as hmac_lib
import random
import time
from typing import Any

import curl_cffi.requests as cffi_requests

from debank_checker.config import (
    RABBY_API_KEY_INIT,
    RABBY_CLIENT_VERSION,
    REQUEST_TIMEOUT,
)

API_BASE = "https://api.rabby.io"
SIGN_PREFIX = "rabby-api\n"
# Алфавит nonce — как в клиенте Rabby/DeBank (sic: без Y и j).
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
) -> dict:
    """Подпись запроса к Rabby API.

    K    = sha256("rabby-api\\n{nonce}\\n{ts}")
    M    = sha256("{METHOD}\\n{path}\\n{отсортированные query-параметры}")
    sign = HMAC-SHA256(key=K, msg=M)

    nonce/ts передаются явно только в тестах (воспроизведение подписи из HAR).
    """
    ts = ts or int(time.time())
    nonce = nonce or generate_nonce()
    sorted_p = sort_params(params)
    K = sha256_hex(f"{SIGN_PREFIX}{nonce}\n{ts}")
    M = sha256_hex(f"{method.upper()}\n{path}\n{sorted_p}")
    signature = hmac_sha256(K, M)
    return {"signature": signature, "nonce": nonce, "ts": ts, "version": "v2"}


class RabbyClient:
    """Клиент Rabby API. Прокси обязателен."""

    def __init__(self, proxy: str, impersonate: str = "chrome124"):
        if not proxy:
            raise ValueError("Прокси обязателен для Rabby API")
        self._api_key = RABBY_API_KEY_INIT
        self._init_ts = int(time.time())
        self._impersonate = impersonate
        proxies = {"https": proxy, "http": proxy}
        self._session = cffi_requests.Session(
            impersonate=impersonate,
            proxies=proxies,
        )

    def _build_headers(self, params: dict, method: str, path: str) -> dict:
        sign = sign_request(params, method, path)
        return {
            "accept": "application/json, text/plain, */*",
            "X-API-Key": self._api_key,
            "X-API-Time": str(self._init_ts),
            "x-api-ts": str(sign["ts"]),
            "x-api-nonce": sign["nonce"],
            "x-api-ver": sign["version"],
            "x-api-sign": sign["signature"],
            "x-client": "Rabby",
            "x-version": RABBY_CLIENT_VERSION,
        }

    def _get(self, path: str, params: dict | None = None, timeout: float | None = None) -> Any:
        params = params or {}
        headers = self._build_headers(params, "GET", path)
        resp = self._session.get(
            API_BASE + path,
            params=params,
            headers=headers,
            timeout=timeout or REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        # Сервер может ротировать ключ через x-set-api-key (как в DeBank).
        new_key = resp.headers.get("x-set-api-key")
        if new_key:
            self._api_key = new_key

        data = resp.json()
        if isinstance(data, dict) and "data" in data and set(data.keys()) <= {"data", "error_code"}:
            return data["data"]
        return data

    def get_total_balance(self, address: str, is_core: bool = True) -> dict:
        """Итоговый баланс кошелька — АВТОРИТЕТНЫЙ агрегат от сервера.

        Возвращает dict: {total_usd_value: float, chain_list: [{id, usd_value, ...}]}.
        is_core=true — только проверенные (core) токены, отсекает скам.
        Это ключевой запрос миграции: total берётся отсюда напрямую, без
        ручного суммирования → устраняет фантомные балансы в итоговой сумме.
        """
        params = {"id": address.lower(), "is_core": "true" if is_core else "false"}
        result = self._get("/v1/user/total_balance", params)
        return result if isinstance(result, dict) else {}

    def get_token_list(self, address: str, chain_id: str, is_all: bool = False) -> list:
        """Список токенов кошелька в конкретной сети.

        Rabby не отдаёт токены всех сетей одним запросом (в отличие от DeBank
        /token/cache_balance_list), поэтому запрашиваем по каждой сети отдельно.
        is_all=false → только проверенные/core токены (нужный нам режим).
        """
        params = {
            "id": address.lower(),
            "chain_id": chain_id,
            "is_all": "true" if is_all else "false",
        }
        result = self._get("/v1/user/token_list", params)
        return result if isinstance(result, list) else []

    def get_complex_app_list(self, address: str) -> list:
        """DeFi-протоколы с позициями (аналог DeBank /portfolio/project_list).

        Возвращает список приложений (apps); каждое содержит portfolio_item_list
        с той же структурой (stats.net_usd_value, asset_token_list, detail), что
        и DeBank — поэтому _safe_position_value переиспользуется без изменений.
        """
        result = self._get("/v1/user/complex_app_list", {"id": address.lower()})
        if isinstance(result, dict):
            apps = result.get("apps", [])
            return apps if isinstance(apps, list) else []
        return result if isinstance(result, list) else []

    def get_collection_list(self, address: str, is_all: bool = True) -> list:
        """NFT-коллекции кошелька (все сети одним запросом).

        Заменяет пару DeBank-запросов /nft/used_chains + /nft/collection_list.
        """
        params = {"id": address.lower(), "is_all": "true" if is_all else "false"}
        result = self._get("/v1/user/collection_list", params)
        return result if isinstance(result, list) else []

    def get_used_chain_list(self, address: str) -> list:
        """Сети, в которых кошелёк проявлял активность."""
        result = self._get("/v1/user/used_chain_list", {"id": address.lower()})
        return result if isinstance(result, list) else []
