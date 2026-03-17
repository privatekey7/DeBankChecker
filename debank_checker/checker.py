"""
Проверка балансов кошельков: tokens + portfolio (+ NFT заглушка)
"""

import time
from typing import Any

from debank_checker.api.client import DeBankClient
from debank_checker.config import DEBUG, MIN_VALUE_DISPLAY, RETRY_ATTEMPTS
from debank_checker.proxy.manager import ProxyManager


def _mask_proxy(proxy: str | None) -> str:
    """Маскирует прокси для лога (оставляет ip:port)."""
    if not proxy:
        return "—"
    try:
        # http://login:pass@1.2.3.4:5678 -> 1.2.3.4:5678
        if "@" in proxy:
            return proxy.split("@")[-1]
        return proxy
    except Exception:
        return "?"


def check_wallet(address: str, proxy_manager: ProxyManager, wallet_idx: int = -1) -> dict[str, Any]:
    """
    Проверяет баланс одного кошелька.
    Retry с новым прокси при ошибке.
    """
    last_error = None
    used_proxy = None
    total_start = time.perf_counter()

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        attempt_start = time.perf_counter()
        proxy = proxy_manager.get_proxy()
        if not proxy:
            if DEBUG:
                _debug_log(wallet_idx, address, "NO_PROXY", attempt, 0, "—")
            return _error_result(address, "Нет доступных прокси", used_proxy)

        used_proxy = proxy
        try:
            client = DeBankClient(proxy=proxy)
            tokens = client.get_total_balance_cache(address)
            portfolio = client.get_portfolio(address)

            tokens_usd = sum(t.get("price", 0) * t.get("amount", 0) for t in tokens)

            protocols_data = []
            protocols_usd = 0.0
            for proto in portfolio:
                proto_value = sum(
                    item.get("stats", {}).get("net_usd_value", 0)
                    for item in proto.get("portfolio_item_list", [])
                )
                if round(proto_value, 2) < MIN_VALUE_DISPLAY:
                    continue
                protocols_usd += proto_value

                positions = []
                for item in proto.get("portfolio_item_list", []):
                    net = item.get("stats", {}).get("net_usd_value", 0)
                    if round(net, 2) < MIN_VALUE_DISPLAY:
                        continue
                    detail = item.get("detail", {})
                    supply = [
                        f"{t.get('symbol', '?')} {t.get('amount', 0):.4f}"
                        for t in detail.get("supply_token_list", [])
                    ]
                    reward = [
                        f"{t.get('symbol', '?')} {t.get('amount', 0):.4f}"
                        for t in detail.get("reward_token_list", [])
                    ]
                    positions.append({
                        "type": item.get("name", ""),
                        "value": round(net, 2),
                        "supply": ", ".join(supply),
                        "rewards": ", ".join(reward),
                    })

                protocols_data.append({
                    "logo": proto.get("logo_url") or "",
                    "name": proto.get("name", "?"),
                    "chain": proto.get("chain", "?"),
                    "value": round(proto_value, 2),
                    "positions": positions,
                })

            protocols_data.sort(key=lambda p: p["value"], reverse=True)

            nft_usd = 0.0
            nft_data: list[dict] = []
            nft_chains: set[str] = set()
            try:
                nft_chains_list = client.get_nft_used_chains(address)
                for chain in (nft_chains_list or [])[:10]:
                    collections = client.get_nft_collection_list(address, chain)
                    for col in collections:
                        amount = col.get("amount") or col.get("nft_count", 0)
                        if not amount:
                            continue
                        nft_chains.add(col.get("chain_id") or chain)
                        nft_data.append({
                            "name": col.get("name", "?"),
                            "chain": col.get("chain_id") or chain,
                            "amount": amount,
                        })
            except Exception:
                pass
            nft_data.sort(key=lambda x: (x["chain"], x["name"]))

            total_usd = tokens_usd + protocols_usd

            chains = sorted({t.get("chain", "") for t in tokens if t.get("chain")} | nft_chains)

            tokens_data = [
                {
                    "logo": t.get("logo_url") or "",
                    "symbol": t.get("symbol", "?"),
                    "chain": t.get("chain", "?"),
                    "amount": t.get("amount", 0),
                    "price": t.get("price", 0),
                    "value": round(t.get("price", 0) * t.get("amount", 0), 2),
                }
                for t in tokens
                if round(t.get("price", 0) * t.get("amount", 0), 2) >= MIN_VALUE_DISPLAY
            ]

            top_tokens = sorted(tokens_data, key=lambda t: t["value"], reverse=True)[:3]
            top_str = ", ".join(f"{t['symbol']}(${t['value']:.2f})" for t in top_tokens)

            if DEBUG:
                elapsed = time.perf_counter() - total_start
                _debug_log(wallet_idx, address, "OK", attempt, elapsed, _mask_proxy(used_proxy))

            return {
                "address": address,
                "total_usd": total_usd,
                "tokens_usd": tokens_usd,
                "protocols_usd": protocols_usd,
                "nft_usd": nft_usd,
                "tokens": len(tokens_data),
                "chains": ", ".join(chains),
                "top_tokens": top_str,
                "tokens_data": tokens_data,
                "protocols_data": protocols_data,
                "nft_data": nft_data,
                "proxy": used_proxy or "—",
                "status": "OK",
                "error": "",
            }

        except Exception as e:
            last_error = e
            elapsed = time.perf_counter() - attempt_start
            err_str = str(e).lower()
            if "timeout" in err_str or "timed out" in err_str:
                proxy_manager.report_timeout(used_proxy)
            if DEBUG:
                _debug_log(wallet_idx, address, "FAIL", attempt, elapsed, _mask_proxy(used_proxy), str(e)[:80])
            proxy = proxy_manager.get_proxy()
            if proxy:
                used_proxy = proxy

    if DEBUG:
        elapsed = time.perf_counter() - total_start
        _debug_log(wallet_idx, address, "ERROR", RETRY_ATTEMPTS, elapsed, _mask_proxy(used_proxy), str(last_error)[:80])

    return _error_result(
        address,
        str(last_error) if last_error else "Unknown error",
        used_proxy,
    )


def _debug_log(idx: int, addr: str, status: str, attempt: int, sec: float, proxy: str, err: str = "") -> None:
    """Отладочный лог в stderr."""
    if not DEBUG:
        return
    import sys
    short = f"{addr[:10]}...{addr[-6:]}" if len(addr) > 20 else addr
    err_part = f" | {err}" if err else ""
    line = f"[DEBUG] #{idx} {short} | {status} | попытка {attempt} | {sec:.2f}s | {proxy}{err_part}\n"
    sys.stderr.write(line)
    sys.stderr.flush()


def _error_result(address: str, err_msg: str, proxy: str | None) -> dict[str, Any]:
    return {
        "address": address,
        "total_usd": 0.0,
        "tokens_usd": 0.0,
        "protocols_usd": 0.0,
        "nft_usd": 0.0,
        "tokens": 0,
        "chains": "",
        "top_tokens": "",
        "tokens_data": [],
        "protocols_data": [],
        "nft_data": [],
        "proxy": proxy or "—",
        "status": "ERROR",
        "error": err_msg,
    }
