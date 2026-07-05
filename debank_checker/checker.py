"""  
Проверка балансов кошельков: tokens + portfolio (+ NFT заглушка)  
"""  
  
import time  
from typing import Any  
  
from debank_checker.api.client import DeBankClient  
from debank_checker.config import (
    CORROBORATION_ABS_TOL,
    CORROBORATION_ENABLED,
    CORROBORATION_MAX_FETCHES,
    CORROBORATION_MIN_AGREE,
    CORROBORATION_REL_TOL,
    DEBUG,
    MIN_VALUE_DISPLAY,
    RETRY_ATTEMPTS,
)  
from debank_checker.proxy.manager import ProxyManager  
  
  
def _mask_proxy(proxy: str | None) -> str:  
    """Маскирует прокси для лога (оставляет ip:port)."""  
    if not proxy:  
        return "—"  
    try:  
        if "@" in proxy:  
            return proxy.split("@")[-1]  
        return proxy  
    except Exception:  
        return "?"  
  
  
def _safe_position_value(item: dict) -> float:  
    """  
    Вычисляет стоимость позиции протокола, защищаясь от фантомных данных API.  
  
    Логика:  
    - Пересчитывает стоимость из asset_token_list (фильтруя скам-токены).  
    - Если asset_token_list пустой или все токены скам → возвращает 0.  
    - Использует min(api_value, recalc_value):  
        * Для lending: api_value (залог−долг) < recalc_value (только залог) → берём api_value.  
        * Для farming/common: recalc_value ≈ api_value → результат корректный.  
        * Для фантомной позиции: recalc_value = 0 → возвращаем 0.  
    """  
    api_value = max(0.0, item.get("stats", {}).get("net_usd_value", 0))  
  
    asset_tokens = item.get("asset_token_list", [])  
    if not asset_tokens:  
        return 0.0  # нет токенов — фантомная позиция  
  
    recalc = 0.0  
    for t in asset_tokens:  
        value = t.get("price", 0) * t.get("amount", 0)  
        if t.get("is_verified", True) and not t.get("is_scam", False):  
            recalc += value  
  
    recalc = max(0.0, recalc)  
    if recalc == 0.0:  
        return 0.0  # все токены в позиции — скам  
  
    return min(api_value, recalc)  
  
  
def _write_log(address: str, total_usd: float, tokens_usd: float,  
               protocols_usd: float, tokens: list, portfolio: list) -> None:  
    """Пишет лог в logs.txt в корневой директории проекта."""  
    import json  
    from pathlib import Path  
    from datetime import datetime  
  
    log_path = Path(__file__).parent.parent / "logs.txt"  
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  
  
    with open(log_path, "a", encoding="utf-8") as f:  
        f.write(f"\n{'=' * 60}\n")  
        f.write(f"[{timestamp}] {address}\n")  
        f.write(f"  total_usd={total_usd:.2f}  tokens_usd={tokens_usd:.2f}"  
                f"  protocols_usd={protocols_usd:.2f}\n")  
        if total_usd > 1_000:  
            f.write("  [!] АНОМАЛЬНЫЙ БАЛАНС — сохраняю сырые данные\n")  
            f.write("  --- TOKENS ---\n")  
            f.write(json.dumps(tokens, ensure_ascii=False, indent=2))  
            f.write("\n  --- PORTFOLIO ---\n")  
            f.write(json.dumps(portfolio, ensure_ascii=False, indent=2))  
            f.write("\n")  
  
  
def _agree(a: float, b: float) -> bool:
    """Две суммы считаются согласованными в пределах относ./абс. допуска."""
    return abs(a - b) <= max(CORROBORATION_ABS_TOL,
                             CORROBORATION_REL_TOL * max(abs(a), abs(b)))


def _largest_agreeing_cluster(snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Наибольшая группа выборок, согласованных по total_usd.

    При равенстве размеров выбирается группа с НАИМЕНЬШИМ значением — фантом
    всегда завышает баланс, поэтому консервативный выбор защищает от инфляции.
    """
    best: list[dict[str, Any]] = []
    for anchor in snaps:
        cluster = [s for s in snaps if _agree(s["total_usd"], anchor["total_usd"])]
        if (len(cluster) > len(best)
                or (len(cluster) == len(best) and best
                    and _rep(cluster)["total_usd"] < _rep(best)["total_usd"])):
            best = cluster
    return best


def _rep(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """Представитель группы: выборка с минимальным total_usd (все согласованы)."""
    return min(cluster, key=lambda s: s["total_usd"])


def _fetch_snapshot(address: str, proxy: str) -> dict[str, Any]:
    """Одна независимая выборка баланса кошелька (tokens + portfolio + NFT).

    Бросает исключение при сетевой ошибке. Возвращает result-dict со status=OK.

    ВНИМАНИЕ: под высокой конкуренцией DeBank иногда отдаёт ответ от ДРУГОГО
    адреса (response contamination) — такая выборка внутренне консистентна и
    в одиночку неотличима от настоящей. Отсев делает корроборация в
    check_wallet(): фантом случаен и не повторяется, истинный баланс стабилен.
    """
    client = DeBankClient(proxy=proxy)

    # Токены в кошельке — фильтруем скам и неверифицированные
    tokens = client.get_total_balance_cache(address)
    tokens = [
        t for t in tokens
        if t.get("is_verified", True) and not t.get("is_scam", False)
    ]

    portfolio = client.get_portfolio(address)

    tokens_usd = sum(t.get("price", 0) * t.get("amount", 0) for t in tokens)

    protocols_data = []
    protocols_usd = 0.0
    for proto in portfolio:
        proto_value = sum(
            _safe_position_value(item)
            for item in proto.get("portfolio_item_list", [])
        )
        if round(proto_value, 2) < MIN_VALUE_DISPLAY:
            continue
        protocols_usd += proto_value

        positions = []
        for item in proto.get("portfolio_item_list", []):
            net = _safe_position_value(item)
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

    # Лог сырых данных при аномалии (помогает увидеть, что именно отсеяла корроборация)
    _write_log(address, total_usd, tokens_usd, protocols_usd, tokens, portfolio)

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
        "proxy": proxy or "—",
        "status": "OK",
        "error": "",
        "corroborated": True,
    }


def check_wallet(address: str, proxy_manager: ProxyManager, wallet_idx: int = -1) -> dict[str, Any]:
    """Проверяет баланс одного кошелька с защитой от «фантомных» балансов.

    Сетевые ошибки → повтор с новым прокси. При CORROBORATION_ENABLED баланс
    принимается только если подтверждён CORROBORATION_MIN_AGREE независимыми
    выборками, сошедшимися по total_usd; иначе берётся консервативное значение
    (наибольшая согласованная группа, при равенстве — меньшая сумма).
    """
    last_error = None
    used_proxy = None
    total_start = time.perf_counter()

    snapshots: list[dict[str, Any]] = []
    fetches = 0
    attempts = 0
    # Запас попыток на сетевые сбои поверх бюджета успешных выборок.
    max_attempts = (max(RETRY_ATTEMPTS, CORROBORATION_MAX_FETCHES * 3)
                    if CORROBORATION_ENABLED else RETRY_ATTEMPTS)

    while attempts < max_attempts:
        attempts += 1
        if CORROBORATION_ENABLED and fetches >= CORROBORATION_MAX_FETCHES:
            break

        attempt_start = time.perf_counter()
        proxy = proxy_manager.get_proxy()
        if not proxy:
            if not snapshots:
                if DEBUG:
                    _debug_log(wallet_idx, address, "NO_PROXY", attempts, 0, "—")
                return _error_result(address, "Нет доступных прокси", used_proxy)
            break
        used_proxy = proxy

        try:
            snap = _fetch_snapshot(address, proxy)
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            if "timeout" in err_str or "timed out" in err_str:
                proxy_manager.report_timeout(used_proxy)
            if DEBUG:
                _debug_log(wallet_idx, address, "FAIL", attempts,
                           time.perf_counter() - attempt_start,
                           _mask_proxy(used_proxy), str(e)[:80])
            continue

        fetches += 1

        if not CORROBORATION_ENABLED:
            if DEBUG:
                _debug_log(wallet_idx, address, "OK", attempts,
                           time.perf_counter() - total_start, _mask_proxy(used_proxy))
            return snap

        # Корроборация: ищем группу согласованных по total_usd выборок.
        snapshots.append(snap)
        cluster = [s for s in snapshots if _agree(s["total_usd"], snap["total_usd"])]
        if len(cluster) >= CORROBORATION_MIN_AGREE:
            chosen = _rep(cluster)
            chosen["corroborated"] = True
            if DEBUG:
                _debug_log(wallet_idx, address, "OK", attempts,
                           time.perf_counter() - total_start, _mask_proxy(used_proxy))
            return chosen

    # Бюджет исчерпан без подтверждения — берём консервативный результат.
    if snapshots:
        chosen = _rep(_largest_agreeing_cluster(snapshots))
        chosen["corroborated"] = False
        chosen["error"] = (chosen.get("error")
                           or "баланс не подтверждён (возможен фантом) — взято консервативное значение")
        if DEBUG:
            others = sorted(round(s["total_usd"], 2) for s in snapshots)
            _debug_log(wallet_idx, address, "WARN", attempts,
                       time.perf_counter() - total_start, _mask_proxy(used_proxy),
                       f"не подтверждён, выборки={others}")
        return chosen

    if DEBUG:
        elapsed = time.perf_counter() - total_start
        _debug_log(wallet_idx, address, "ERROR", attempts, elapsed,
                   _mask_proxy(used_proxy), str(last_error)[:80])

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