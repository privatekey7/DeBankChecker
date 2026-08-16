"""
Rabby API Client (api.rabby.io).

Схема подписи запросов: см. sign_request ниже. Проверено воспроизведением
подписи из HAR веб-версии Rabby байт-в-байт (65/68 запросов; расхождения — в
неиспользуемых эндпоинтах has_new_tx/history_list).

Заголовки идентификации должны ТОЧНО повторять клиент Rabby (сверено с HAR
браузерного расширения — tests/fixtures и свежий HAR расширения):
  - подписные заголовки — в нижнем регистре (x-api-key, x-api-time, ...);
  - x-api-time — время ВЫДАЧИ текущего API-ключа, а не время запроса
    (в HAR оно одинаково во всех запросах сессии и на ~23 млн сек старше
    x-api-ts);
  - x-api-ver: v2 отправляется (в отличие от урезанной веб-фикстуры);
  - браузерные заголовки (accept-language, dnt, priority, sec-fetch-*)
    досылаются поверх impersonate-фингерпринта curl_cffi.
Анти-бот API на любое отклонение отвечает фейковым 429 с пустым телом —
именно так душился /v1/user/token_list при верной подписи (проверено на
live-API: старые заголовки → 429, заголовки клиента → 200; расширение
делает ~10 req/s без единого 429).

Прокси обязателен.
"""

import hashlib
import hmac as hmac_lib
import random
import threading
import time
from typing import Any

import curl_cffi.requests as cffi_requests

from debank_checker.config import (
    RABBY_API_KEY_INIT,
    RABBY_API_KEY_INIT_TIME,
    RABBY_CLIENT_VERSION,
    REQUEST_TIMEOUT,
)

API_BASE = "https://api.rabby.io"
SIGN_PREFIX = "rabby-api\n"
# Алфавит nonce — как в клиенте Rabby (sic: без Y и j).
NONCE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXTZabcdefghiklmnopqrstuvwxyz"
NONCE_LENGTH = 40

# Общий на процесс магазин ключа: снапшоты создают новый RabbyClient на каждую
# попытку, а ротированный сервером ключ терять нельзя — иначе каждый клиент
# снова начнёт с init-ключа и его лимитов. Все клиенты продолжают с последнего
# выданного ключа; время выдачи нового ключа — момент ротации.
_KEY_LOCK = threading.Lock()
_KEY_STATE = {"key": RABBY_API_KEY_INIT, "time": RABBY_API_KEY_INIT_TIME}


def _current_key() -> tuple[str, int]:
    with _KEY_LOCK:
        return _KEY_STATE["key"], _KEY_STATE["time"]


def _rotate_key(new_key: str) -> int:
    """Обновляет общий ключ; возвращает время выдачи действующего ключа."""
    with _KEY_LOCK:
        if new_key and new_key != _KEY_STATE["key"]:
            _KEY_STATE["key"] = new_key
            _KEY_STATE["time"] = int(time.time())
        return _KEY_STATE["time"]


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
        self._api_key, self._key_time = _current_key()
        self._impersonate = impersonate
        proxies = {"https": proxy, "http": proxy}
        self._session = cffi_requests.Session(
            impersonate=impersonate,
            proxies=proxies,
        )

    def _build_headers(self, params: dict, method: str, path: str) -> dict:
        # Состав и кейсинг — строго по HAR расширения Rabby (см. докстринг
        # модуля): отклонение карается фейковым 429 с пустым телом.
        sign = sign_request(params, method, path)
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "ru,ru-RU;q=0.9,en-US;q=0.8,en;q=0.7",
            "dnt": "1",
            "priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "none",
            "sec-fetch-storage-access": "active",
            "x-api-key": self._api_key,
            "x-api-time": str(self._key_time),
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

        # Ротация ключа читается ДО raise_for_status: сервер может выдать
        # новый ключ и вместе с ошибкой (429/403) — раньше он терялся.
        new_key = resp.headers.get("x-set-api-key")
        if new_key and new_key != self._api_key:
            self._key_time = _rotate_key(new_key)
            self._api_key = new_key

        resp.raise_for_status()

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

    def get_cache_token_list(self, address: str) -> list:
        """Токены кошелька по ВСЕМ сетям одним запросом (серверный кэш).

        Заменяет десятки запросов token_list (по одному на сеть) — расширение
        Rabby само использует этот эндпоинт для быстрой загрузки. Ответ — тот
        же формат токенов (amount/price/is_core/is_scam), фильтрация на
        стороне чекера.
        """
        result = self._get("/v1/user/cache_token_list", {"id": address.lower()})
        return result if isinstance(result, list) else []

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
