"""
On-chain верификация балансов токенов через публичные JSON-RPC.

Зачем: с 11.09.2026 эндпоинты Rabby (total_balance, complex_app_list,
token_list) возвращают для части адресов чужие данные. Количество токена на
адресе — единственное, что можно проверить независимо и дёшево:
  * нативный токен → eth_getBalance(address)
  * ERC-20        → eth_call balanceOf(address)

Источники RPC (объединяются, дубли убираются, в этом порядке):
  1. rpc.txt в корне проекта — необязательный пользовательский список
     (формат: <chain_id> <url>, по одному на строку);
  2. data/rpc_registry.json — встроенный реестр, проверенный живьём для всех
     сетей Rabby (tools/build_rpc_registry.py), отсортирован по задержке;
  3. chainid.network/chains.json — живой реестр (кэш 7 дней в .cache/).

Стратегия: перебираются ВСЕ RPC сети (неудачные получают короткий cooldown),
затем ещё ONCHAIN_ROUNDS раундов с паузой. Только если ни один RPC сети не
ответил — OnchainUnavailable; вызывающий код повторяет выборку целиком.
Запросы могут идти через прокси (тот же, что у Rabby), чтобы лимиты
публичных нод делились на 100 IP, а не на один.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from debank_checker import diskcache
from debank_checker.api import http
from debank_checker.config import ONCHAIN_ROUNDS, ONCHAIN_RPC_HEDGE_SEC, ONCHAIN_RPC_TIMEOUT
from debank_checker.parallel import first_success

CHAINS_JSON_URL = "https://chainid.network/chains.json"
_ROOT = Path(__file__).resolve().parent.parent.parent
_CHAINS_CACHE = "chainid_rpc.json"
_REGISTRY_FILE = Path(__file__).resolve().parent.parent / "data" / "rpc_registry.json"
_USER_RPC_FILE = _ROOT / "rpc.txt"
_CHAINS_TTL = 7 * 24 * 3600

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[int, list[str]] | None = None
_BAD_LOCK = threading.Lock()
_BAD_RPC: dict[tuple[str, str], float] = {}  # (url, прокси) -> время, до которого не использовать
_HOST_SEM: dict[str, threading.Semaphore] = {}
_HOST_LOCK = threading.Lock()
HOST_CONCURRENCY = 6      # параллельных запросов на один RPC-хост с одного IP
BAD_COOLDOWN = 45.0       # секунд не трогать RPC после ошибки


class OnchainUnavailable(RuntimeError):
    """Ни один RPC сети не ответил — проверить нельзя."""


def _read_user_rpc() -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    try:
        if not _USER_RPC_FILE.exists():
            return out
        for line in _USER_RPC_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].startswith("http"):
                out.setdefault(int(parts[0]), []).append(parts[1])
    except OSError:
        pass
    return out


def _read_builtin() -> dict[int, list[str]]:
    try:
        data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        return {int(k): list(v) for k, v in data.items()}
    except Exception:
        return {}


def _read_chainid_network() -> dict[int, list[str]]:
    """HTTPS-RPC из chainid.network (кэш .cache/chainid_rpc.json на 7 дней)."""
    cached = diskcache.load(_CHAINS_CACHE, ttl=_CHAINS_TTL)
    if isinstance(cached, dict):
        return {int(k): list(v) for k, v in cached.items()}
    try:
        resp = http.get(CHAINS_JSON_URL, None, timeout=20)
        data = resp.json() if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001
        data = None
    out: dict[int, list[str]] = {}
    for c in data or []:
        try:
            cid = int(c.get("chainId"))
        except (TypeError, ValueError):
            continue
        urls = [u for u in c.get("rpc", []) if isinstance(u, str) and u.startswith("https://") and "${" not in u]
        if urls:
            out[cid] = urls
    if out:
        diskcache.save(_CHAINS_CACHE, out)
    return out


def _load_registry() -> dict[int, list[str]]:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is not None:
            return _REGISTRY
        reg: dict[int, list[str]] = {}
        for source in (_read_user_rpc(), _read_builtin(), _read_chainid_network()):
            for cid, urls in source.items():
                lst = reg.setdefault(int(cid), [])
                for u in urls:
                    if u not in lst:
                        lst.append(u)
        _REGISTRY = reg
        return reg


def rpc_urls(chain_id: int) -> list[str]:
    return list(_load_registry().get(int(chain_id), []))


def _sem(url: str, proxy: str | None) -> threading.Semaphore:
    """Ограничение параллельности на пару (RPC-хост, исходящий IP)."""
    key = url.split("/")[2] + "|" + (proxy or "direct")
    with _HOST_LOCK:
        s = _HOST_SEM.get(key)
        if s is None:
            s = _HOST_SEM[key] = threading.Semaphore(HOST_CONCURRENCY)
        return s


def _post(url: str, payload: dict, proxy: str | None) -> Any:
    with _sem(url, proxy):
        resp = http.post(url, proxy, json=payload, timeout=ONCHAIN_RPC_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(str(body["error"])[:120])
    if not isinstance(body, dict) or "result" not in body:
        raise RuntimeError("нет result")
    return body["result"]


def _call(chain_id: int, method: str, params: list, proxy: str | None = None) -> Any:
    """Исчерпывающий перебор всех RPC сети, ONCHAIN_ROUNDS раундов.

    Ноды опрашиваются по очереди, но с хеджем: упавшая нода сразу сменяется
    следующей, а не ответившая за ONCHAIN_RPC_HEDGE_SEC дублируется следующей
    параллельно — берётся первый ответ.
    """
    urls = rpc_urls(chain_id)
    if not urls:
        raise OnchainUnavailable(f"нет публичного RPC для chain_id={chain_id}")
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    errors: list[str] = []

    def attempt(url: str) -> Any:
        def run() -> Any:
            try:
                return _post(url, payload, proxy)
            except http.ProxyDead:
                raise
            except Exception as e:  # noqa: BLE001
                errors.append(f"{url.split('/')[2]}: {str(e)[:50]}")
                with _BAD_LOCK:
                    _BAD_RPC[(url, proxy or "")] = time.time() + BAD_COOLDOWN
                raise
        return run

    for rnd in range(max(1, ONCHAIN_ROUNDS)):
        now = time.time()
        with _BAD_LOCK:
            order = [u for u in urls if _BAD_RPC.get((u, proxy or ""), 0) <= now] or list(urls)
        try:
            return first_success([attempt(u) for u in order], ONCHAIN_RPC_HEDGE_SEC)
        except http.ProxyDead:
            raise
        except Exception:  # noqa: BLE001
            pass
        if rnd + 1 < ONCHAIN_ROUNDS:
            time.sleep(1.5 * (rnd + 1))
    raise OnchainUnavailable(f"RPC chain_id={chain_id}: все {len(urls)} нод недоступны: " + "; ".join(errors[-3:]))


def _hex_to_int(x: Any) -> int:
    if isinstance(x, str) and x.startswith("0x"):
        return int(x, 16) if len(x) > 2 else 0
    raise RuntimeError(f"неожиданный ответ RPC: {x!r}")


def native_balance(chain_id: int, address: str, proxy: str | None = None) -> int:
    return _hex_to_int(_call(chain_id, "eth_getBalance", [address, "latest"], proxy))


def erc20_balance(chain_id: int, token: str, address: str, proxy: str | None = None) -> int:
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")
    res = _call(chain_id, "eth_call", [{"to": token, "data": data}, "latest"], proxy)
    if res in ("0x", "", None):
        return 0
    return _hex_to_int(res)


def token_amount(chain_id: int, token_id: str, address: str, decimals: int, is_native: bool,
                 proxy: str | None = None) -> float:
    """Количество токена на адресе по данным сети.

    Бросает OnchainUnavailable, если ни один RPC сети не ответил. Повторные
    проверки одного токена в выборках кошелька дедуплицирует checker.
    """
    raw = (native_balance(chain_id, address, proxy) if is_native
           else erc20_balance(chain_id, token_id, address, proxy))
    return raw / (10 ** int(decimals))
