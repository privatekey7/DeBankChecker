"""
Нативный клиент Lighter (zkLighter) — проверка app-chain позиций lighter /
lighter_robinhood, которые Rabby отдаёт с чужими суммами.

GET /api/v1/account?by=l1_address&value=<addr>
  * 400 {"code":21100,"message":"account not found"} → аккаунта нет → 0.
  * 200 {"accounts":[{... "total_asset_value": "...", "collateral": "..."}]}
Если схема не распознана — LighterError (fail-closed: выборка повторится).
"""
from __future__ import annotations

import time
from typing import Any

from debank_checker.api import http
from debank_checker.api.http import PerKeyLimiter
from debank_checker.config import APPCHAIN_RATE_PER_SEC, APPCHAIN_TIMEOUT, LIGHTER_API_URL

_LIMITER = PerKeyLimiter(APPCHAIN_RATE_PER_SEC)


class LighterError(RuntimeError):
    pass


def get_positions(address: str, proxy: str | None = None) -> dict[str, Any]:
    """{"total_usd": float, "details": [...]} для адреса на Lighter."""
    addr = address.lower()
    attempts, pause = http.retry_policy(proxy)
    last: Exception | None = None
    for attempt in range(attempts):
        _LIMITER.wait(proxy)
        try:
            resp = http.get(LIGHTER_API_URL, proxy, params={"by": "l1_address", "value": addr},
                            timeout=APPCHAIN_TIMEOUT)
            if resp.status_code in (429, 502, 503, 504):
                last = LighterError(f"Lighter HTTP {resp.status_code}")
                time.sleep(pause if proxy else min(3.0 * (attempt + 1), 30.0))
                continue
            break
        except http.ProxyDead:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(pause * (attempt + 1))
    else:
        raise LighterError(f"Lighter недоступен: {last}")

    if resp.status_code == 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if isinstance(body, dict) and body.get("code") == 21100:  # account not found
            return {"total_usd": 0.0, "details": []}
        raise LighterError(f"Lighter HTTP 400: {str(body)[:120]}")
    if resp.status_code != 200:
        raise LighterError(f"Lighter HTTP {resp.status_code}")

    body = resp.json()
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(accounts, list):
        raise LighterError("Lighter: неожиданная схема ответа")
    total = 0.0
    details = []
    for acc in accounts:
        val = acc.get("total_asset_value", acc.get("collateral"))
        if val is None:
            raise LighterError("Lighter: в аккаунте нет total_asset_value/collateral")
        v = float(val)
        total += v
        details.append({"type": "Account", "symbol": "USDC", "amount": v, "value": v})
    return {"total_usd": total, "details": details}
