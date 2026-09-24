"""
Проверка балансов кошельков через Rabby API: tokens + protocols + NFT.

Почему итог НЕ берётся из агрегата Rabby — см. блок «Защита от фантомных
балансов» в config.py. Кратко: с 11.09.2026 /v1/user/total_balance и
/v1/user/complex_app_list для части адресов возвращают чужие суммы; список
токенов при этом стабилен и совпадает с независимыми источниками.

Итог одной выборки = токены кошелька (core, не скам, подтверждены on-chain)
                   + EVM-позиции протоколов (пересчёт по asset_token_list)
                   + app-chain позиции, подтверждённые нативными API.
Агрегат Rabby — только контроль (aggregate_agrees / примечание).
Баланс принимается, когда CORROBORATION_MIN_AGREE выборок сошлись по итогу.

Скорость: внутри выборки все независимые запросы идут параллельно; выборки
одного кошелька идут одновременно через разные прокси (плюс «хедж» при
зависании). Данные, не зависящие от ответа Rabby (нативные app-chain API,
on-chain количества, NFT), запрашиваются один раз на кошелёк.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

from debank_checker import diskcache
from debank_checker.api.http import ProxyDead
from debank_checker.api.rabby_client import RabbyClient, direct_allowed
from debank_checker.config import (
    AGGREGATE_OPTIONAL,
    APPCHAIN_VERIFY,
    APPCHAIN_VIA_PROXY,
    CHAIN_REFETCH_MAX,
    COMPONENT_TOL_ABS,
    COMPONENT_TOL_REL,
    CORROBORATION_ABS_TOL,
    CORROBORATION_ENABLED,
    CORROBORATION_MAX_FETCHES,
    CORROBORATION_MIN_AGREE,
    CORROBORATION_REL_TOL,
    DEBUG,
    MIN_VALUE_DISPLAY,
    ONCHAIN_AMOUNT_TOL,
    ONCHAIN_MIN_USD,
    ONCHAIN_VERIFY,
    RABBY_IS_CORE,
    RAW_LOG_ENABLED,
    RAW_LOG_MAX_BYTES,
    RETRY_429_BACKOFF_SEC,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF_SEC,
    RETRY_PROXY_BACKOFF_SEC,
    NFT_ATTEMPTS,
    NFT_BACKGROUND_WORKERS,
    NFT_HEDGE_SEC,
    SNAPSHOT_HEDGE_SEC,
    TAINT_ABS_USD,
    TAINT_REL,
)
from debank_checker.parallel import Memo, first_success, run_parallel, unwrap
from debank_checker.proxy.manager import ProxyManager

# App-chain приложения Rabby, для которых есть нативная проверка.
APPCHAIN_NATIVE = {
    "hyperliquid": "hyperliquid",
    "lighter": "lighter",
    "lighter_robinhood": "lighter",
    "polymarket": "polymarket",
}
# Ключ нативного источника → (подпись в protocols_data, chain для экспорта).
APPCHAIN_LABELS = {
    "hyperliquid": ("Hyperliquid", "hyperliquid"),
    "lighter": ("Lighter", "lighter"),
    "polymarket": ("Polymarket", "matic"),
}

_CHAINS_CACHE = "rabby_chains.json"
_CHAINS_TTL = 24 * 3600
_CHAIN_MEMO = Memo()
_LOG_LOCK = threading.Lock()
_NFT_POOL = ThreadPoolExecutor(max_workers=NFT_BACKGROUND_WORKERS, thread_name_prefix="nft")
_NFT_LOCK = threading.Lock()
_NFT_PENDING: set[Future] = set()


# ---------------------------------------------------------------- сети Rabby

def _chain_map(client: Any) -> dict[str, dict[str, Any]]:
    """rabby chain id → {"evm": community_id, "native": native_token_id}.

    Кэш на процесс (singleflight) и на диске на сутки. При сбое — {} (токены
    не пройдут on-chain проверку, выборка повторится).
    """
    try:
        return _CHAIN_MEMO.get("chains", lambda: _load_chain_map(client))
    except Exception:  # noqa: BLE001
        return {}


def _load_chain_map(client: Any) -> dict[str, dict[str, Any]]:
    chains = diskcache.load(_CHAINS_CACHE, ttl=_CHAINS_TTL)
    fresh = not isinstance(chains, list) or not chains
    if fresh:
        chains = client.get_chain_list()
    m: dict[str, dict[str, Any]] = {}
    for c in chains or []:
        try:
            m[str(c["id"])] = {"evm": int(c["community_id"]), "native": str(c.get("native_token_id") or c["id"])}
        except (KeyError, TypeError, ValueError):
            continue
    if not m:
        raise RuntimeError("пустой список сетей Rabby")
    if fresh:
        diskcache.save(_CHAINS_CACHE, [{"id": k, "community_id": v["evm"], "native_token_id": v["native"]}
                                       for k, v in m.items()])
    return m


# ---------------------------------------------------------------- on-chain

class OnchainCheckFailed(RuntimeError):
    """Токен нельзя подтвердить on-chain (нет RPC сети) — выборка не засчитывается."""


def _onchain_verify_token(token: dict, address: str, chain_map: dict[str, dict[str, Any]],
                          proxy: str | None = None) -> float:
    """Количество токена по данным сети. Бросает OnchainCheckFailed. Тесты подменяют."""
    from debank_checker.api import onchain

    chain = str(token.get("chain") or "")
    info = chain_map.get(chain)
    if not info:
        raise OnchainCheckFailed(f"сеть {chain!r} не сопоставлена с EVM chain id")
    token_id = str(token.get("id") or "")
    is_native = (token_id == chain or token_id == info["native"] or not token_id.startswith("0x"))
    try:
        decimals = int(token.get("decimals") or 18)
    except (TypeError, ValueError):
        decimals = 18
    try:
        return onchain.token_amount(info["evm"], token_id, address, decimals, is_native,
                                    proxy if APPCHAIN_VIA_PROXY else None)
    except onchain.OnchainUnavailable as e:
        raise OnchainCheckFailed(str(e))


def _token_key(t: dict) -> tuple[str, str]:
    """Идентификатор токена: (сеть, адрес контракта или символ)."""
    return str(t.get("chain") or ""), str(t.get("id") or t.get("symbol") or "").lower()


def _verify_tokens(tokens: list[dict], address: str, chain_map: dict[str, dict[str, Any]],
                   notes: list[str], require: bool, proxy: str | None = None,
                   memo: Memo | None = None, stats: dict[str, Any] | None = None) -> list[dict]:
    """On-chain проверка токенов с оценкой ≥ ONCHAIN_MIN_USD (все токены — параллельно).

    Фантом (on-chain 0) отбрасывается, расхождение количества исправляется по
    сети. Если сеть недоступна (все RPC молчат) — OnchainCheckFailed: выборка
    целиком не засчитывается и повторяется; ничего непроверенного в итог не
    попадает. require=True — кандидаты из по-сетевого token_list: мелкие
    (< ONCHAIN_MIN_USD) не принимаются вовсе. memo — общий кэш кошелька:
    одинаковый токен в разных выборках проверяется один раз.

    stats (если передан) пополняется: "rejected_usd" — стоимость, которую
    on-chain проверка сняла (фантомы + завышенные количества), "confirmed" —
    ключи токенов, подтверждённых на цепочке на ≥ ONCHAIN_MIN_USD.
    """
    if not ONCHAIN_VERIFY:
        return list(tokens)
    memo = memo if memo is not None else Memo()
    todo = [(i, t) for i, t in enumerate(tokens) if _token_value(t) >= ONCHAIN_MIN_USD]

    def check(t: dict) -> Any:
        return lambda: memo.get(("onchain",) + _token_key(t),
                                lambda: _onchain_verify_token(t, address, chain_map, proxy))

    amounts = run_parallel({i: check(t) for i, t in todo})
    out: list[dict] = []
    for i, t in enumerate(tokens):
        value = _token_value(t)
        if i not in amounts:
            if not require:
                out.append(t)
            continue
        label = f"{t.get('symbol', '?')}@{t.get('chain', '?')}"
        onchain_amount = amounts[i]
        if isinstance(onchain_amount, OnchainCheckFailed):
            raise OnchainCheckFailed(f"{label} ${value:.2f}: {onchain_amount}")
        onchain_amount = unwrap(onchain_amount)
        amount = float(t.get("amount") or 0)
        price = float(t.get("price") or 0)
        if stats is not None:
            stats["rejected_usd"] = stats.get("rejected_usd", 0.0) + max(0.0, amount - max(onchain_amount, 0.0)) * price
            if onchain_amount * price >= ONCHAIN_MIN_USD:
                stats.setdefault("confirmed", set()).add(_token_key(t))
        if onchain_amount <= 0:
            notes.append(f"{label} ${value:.2f}: on-chain баланс 0 — фантомный токен отброшен")
            continue
        if abs(onchain_amount - amount) > ONCHAIN_AMOUNT_TOL * max(abs(amount), abs(onchain_amount)):
            fixed = dict(t)
            fixed["amount"] = onchain_amount
            notes.append(f"{label}: количество {amount:.6g} → {onchain_amount:.6g} по данным сети")
            out.append(fixed)
        else:
            out.append(t)
    return out


# ---------------------------------------------------------------- helpers

def _mask_proxy(proxy: str | None) -> str:
    """Маскирует прокси для лога (оставляет ip:port)."""
    if not proxy:
        return "direct"
    return proxy.split("@")[-1]


def _token_ok(t: dict) -> bool:
    return (t.get("is_verified", True) and not t.get("is_scam", False)
            and not t.get("is_suspicious", False)
            and (not RABBY_IS_CORE or t.get("is_core", True)))


def _token_value(t: dict) -> float:
    try:
        return float(t.get("price") or 0) * float(t.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _close(a: float, b: float, abs_tol: float = COMPONENT_TOL_ABS, rel_tol: float = COMPONENT_TOL_REL) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def _by_chain(tokens: list[dict]) -> dict[str, float]:
    """Сумма токенов по сетям (USD)."""
    out: dict[str, float] = {}
    for t in tokens:
        out[t.get("chain", "")] = out.get(t.get("chain", ""), 0.0) + _token_value(t)
    return out


def _safe_position_value(item: dict) -> float:
    """
    Стоимость EVM-позиции протокола с защитой от фантомных данных.

    - Пересчитывает стоимость из asset_token_list (без скам-токенов).
    - Пустой asset_token_list или все токены скам → 0.
    - min(api_value, recalc): для lending api_value (залог−долг) < recalc;
      для farming/common ≈ равны; фантом с recalc=0 → 0.
    """
    api_value = max(0.0, float((item.get("stats") or {}).get("net_usd_value") or 0))

    asset_tokens = item.get("asset_token_list") or []
    if not asset_tokens:
        return 0.0

    recalc = 0.0
    for t in asset_tokens:
        if t.get("is_verified", True) and not t.get("is_scam", False):
            recalc += _token_value(t)

    recalc = max(0.0, recalc)
    if recalc == 0.0:
        return 0.0

    return min(api_value, recalc)


def _item_chain(item: dict) -> str:
    """Сеть позиции по токенам; '' для app-chain (Hyperliquid, Lighter, …)."""
    detail = item.get("detail") or {}
    for tk in (detail.get("supply_token_list") or []) + (item.get("asset_token_list") or []):
        if tk.get("chain"):
            return str(tk["chain"])
    return ""


def _write_log(address: str, snap: dict[str, Any], raw: dict[str, Any] | None) -> None:
    """Пишет запись о выборке в logs.txt в корне проекта."""
    if not RAW_LOG_ENABLED:
        return
    log_path = Path(__file__).parent.parent / "logs.txt"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    agg = snap.get("aggregate_usd")
    lines = [
        f"\n{'=' * 60}\n",
        f"[{timestamp}] {address}\n",
        f"  total_usd={snap['total_usd']:.2f}  tokens_usd={snap['tokens_usd']:.2f}"
        f"  protocols_usd={snap['protocols_usd']:.2f}"
        f"  aggregate_usd={('n/a' if agg is None else f'{agg:.2f}')}"
        f"  unverified_usd={snap['unverified_usd']:.2f}\n",
    ]
    lines += [f"  [!] {n}\n" for n in snap.get("notes", [])]
    if raw:
        blob = json.dumps(raw, ensure_ascii=False)
        if len(blob) > RAW_LOG_MAX_BYTES:
            blob = blob[:RAW_LOG_MAX_BYTES] + f"... <обрезано, {len(blob)} байт>"
        lines.append("  --- RAW ---\n" + blob + "\n")
    try:
        with _LOG_LOCK, open(log_path, "a", encoding="utf-8") as f:
            f.write("".join(lines))
    except OSError:
        pass


def _agree(a: float, b: float) -> bool:
    """Две суммы считаются согласованными в пределах относ./абс. допуска."""
    return _close(a, b, CORROBORATION_ABS_TOL, CORROBORATION_REL_TOL)


def _largest_agreeing_cluster(snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Наибольшая группа выборок, согласованных по total_usd (при равенстве — меньшая сумма)."""
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


def _build_tokens_data(tokens: list[dict]) -> list[dict]:
    """Из сырых токенов Rabby строит tokens_data для экспорта."""
    out = []
    for t in tokens:
        value = round(_token_value(t), 2)
        if value < MIN_VALUE_DISPLAY:
            continue
        out.append({
            "logo": t.get("logo_url") or "",
            "symbol": t.get("symbol", "?"),
            "chain": t.get("chain", "?"),
            "amount": t.get("amount", 0),
            "price": t.get("price", 0),
            "value": value,
        })
    return out


def _appchain_positions(address: str, proxy: str | None) -> dict[str, dict[str, Any]]:
    """Позиции адреса на app-chain через нативные API (все три — параллельно).

    Любая ошибка API → исключение (fail-closed): выборка повторяется целиком.
    Тесты подменяют эту функцию.
    """
    from debank_checker.api import hyperliquid_client, lighter_client, polymarket_client

    px = proxy if APPCHAIN_VIA_PROXY else None
    r = run_parallel({
        "hyperliquid": lambda: hyperliquid_client.get_positions(address, px),
        "lighter": lambda: lighter_client.get_positions(address, px),
        "polymarket": lambda: polymarket_client.get_positions(address, px),
    })
    return {k: unwrap(v) for k, v in r.items()}


def _fetch_nfts(client: Any, address: str) -> list[dict]:
    """NFT-коллекции одним запросом (is_all=true), без скама/непроверенных."""
    nft_data: list[dict] = []
    for col in client.get_collection_list(address, is_all=True):
        if col.get("is_scam") or col.get("is_suspicious"):
            continue
        if RABBY_IS_CORE and col.get("is_verified") is False:
            continue
        amount = col.get("amount") or len(col.get("nft_list") or [])
        if not amount:
            continue
        nft_data.append({"name": col.get("name", "?"), "chain": col.get("chain") or "", "amount": amount})
    nft_data.sort(key=lambda x: (x["chain"], x["name"]))
    return nft_data


def _aggregate_optional(err: Exception, proxy: str | None) -> bool:
    """Можно ли продолжить выборку без агрегата: 403 (бан эндпоинта для IP),
    а в прямом режиме ещё и 429. Через прокси 429 → выборка повторится с
    другого IP, чтобы не терять сверку по сетям."""
    if not AGGREGATE_OPTIONAL:
        return False
    s = str(err)
    return "403" in s or "Forbidden" in s or (proxy is None and "429" in s)


# ---------------------------------------------------------------- выборка

def _fetch_snapshot(address: str, proxy: str | None, shared: Memo | None = None,
                    with_nft: bool = True) -> dict[str, Any]:
    """Одна выборка баланса кошелька.

    total_usd = tokens_usd + protocols_usd, где protocols_usd — только
    проверенные позиции. Агрегат Rabby (total_usd_value) сохраняется в
    aggregate_usd и сравнивается с итогом, но НЕ используется как итог.

    shared — кэш кошелька (нативные app-chain позиции и on-chain количества
    общие для всех выборок). with_nft=False — NFT запрашивает check_wallet.
    """
    shared = shared if shared is not None else Memo()
    client = RabbyClient(proxy=proxy)
    notes: list[str] = []
    raw_evidence: dict[str, Any] = {}

    # 1) Все независимые запросы — одновременно.
    calls: dict[str, Any] = {
        "agg": lambda: client.get_total_balance(address, is_core=RABBY_IS_CORE),
        "tokens": lambda: client.get_cache_token_list(address),
        "apps": lambda: client.get_complex_app_list(address),
    }
    if ONCHAIN_VERIFY:
        calls["chain_map"] = lambda: _chain_map(client)
    if APPCHAIN_VERIFY:
        calls["native"] = lambda: shared.get("native", lambda: _appchain_positions(address, proxy))
    if with_nft:
        calls["nft"] = lambda: _fetch_nfts(client, address)
    r = run_parallel(calls)
    dead = next((v for v in r.values() if isinstance(v, ProxyDead)), None)
    if dead:
        raise dead  # прокси мёртв — фолбэки через него бессмысленны

    # Агрегат Rabby — только для контроля и списка сетей.
    aggregate_usd: float | None = None
    agg: dict = {}
    if isinstance(r["agg"], Exception):
        if not _aggregate_optional(r["agg"], proxy):
            raise r["agg"]
        notes.append(f"агрегат Rabby недоступен ({str(r['agg'])[:40]}) — контроль по агрегату пропущен")
    else:
        agg = r["agg"] or {}
        aggregate_usd = float(agg.get("total_usd_value") or 0.0)
    agg_chains: dict[str, float] = {}
    for c in agg.get("chain_list") or []:
        if c.get("id"):
            agg_chains[str(c["id"])] = float(c.get("usd_value") or 0.0)

    # 2) Токены: все сети одним запросом (cache_token_list); при сбое —
    #    по-сетевой token_list по сетям из агрегата (без агрегата — повтор).
    if isinstance(r["tokens"], Exception):
        nonzero_chains = [c for c, v in agg_chains.items() if v > 0]
        if not nonzero_chains and not agg:
            raise r["tokens"]
        per_chain = run_parallel({c: (lambda c=c: client.get_token_list(address, chain_id=c,
                                                                        is_all=not RABBY_IS_CORE))
                                  for c in nonzero_chains})
        tokens = [t for c in nonzero_chains for t in unwrap(per_chain[c])]
    else:
        tokens = r["tokens"]
    tokens = [t for t in tokens if _token_ok(t) and t.get("is_wallet", True)]
    portfolio = unwrap(r["apps"])
    native = unwrap(r["native"]) if APPCHAIN_VERIFY else {}
    chain_map = r.get("chain_map") or {}

    # 3) Протоколы. EVM-позиции (есть chain у токенов) — пересчёт по asset_token_list.
    #    App-chain позиции (chain нет) из Rabby НЕ принимаются: их подтверждает
    #    нативный API (Hyperliquid, Lighter, Polymarket); прочие — в unverified_usd.
    protocols_data: list[dict] = []
    protocols_usd = 0.0
    proto_by_chain: dict[str, float] = {}
    unverified_usd = 0.0
    claimed_appchain: dict[str, float] = {}
    unverified_apps: dict[str, float] = {}
    for proto in portfolio:
        app_id = str(proto.get("id") or "")
        items = proto.get("portfolio_item_list") or []
        evm_items = [it for it in items if _item_chain(it)]
        app_items = [it for it in items if not _item_chain(it)]

        if app_items:
            claimed = sum(max(0.0, float((it.get("stats") or {}).get("net_usd_value") or 0)) for it in app_items)
            claimed_appchain[app_id] = claimed_appchain.get(app_id, 0.0) + claimed
            if app_id not in APPCHAIN_NATIVE or not APPCHAIN_VERIFY:
                unverified_apps[app_id] = unverified_apps.get(app_id, 0.0) + claimed

        if not evm_items:
            continue
        proto_value = sum(_safe_position_value(item) for item in evm_items)
        if round(proto_value, 2) < MIN_VALUE_DISPLAY:
            continue
        protocols_usd += proto_value

        positions = []
        proto_chain = ""
        for item in evm_items:
            net = _safe_position_value(item)
            ch = _item_chain(item)
            proto_by_chain[ch] = proto_by_chain.get(ch, 0.0) + net
            if round(net, 2) < MIN_VALUE_DISPLAY:
                continue
            detail = item.get("detail") or {}
            proto_chain = proto_chain or ch
            supply = [f"{t.get('symbol', '?')} {float(t.get('amount') or 0):.4f}"
                      for t in detail.get("supply_token_list") or []]
            reward = [f"{t.get('symbol', '?')} {float(t.get('amount') or 0):.4f}"
                      for t in detail.get("reward_token_list") or []]
            positions.append({
                "type": item.get("name", ""),
                "value": round(net, 2),
                "supply": ", ".join(supply),
                "rewards": ", ".join(reward),
            })

        protocols_data.append({
            "logo": proto.get("logo_url") or "",
            "name": proto.get("name", "?"),
            "chain": proto_chain or proto.get("chain", ""),
            "value": round(proto_value, 2),
            "positions": positions,
        })

    # 2b) On-chain проверка токенов (фантомы отбрасываются, количества сверяются)
    #     и — одновременно с ней — свежий token_list по сетям, где агрегат выше
    #     токенов + EVM-позиций (устаревший кэш или фантом агрегата). Список
    #     таких сетей угадывается по непроверенным токенам и уточняется после
    #     проверки; кандидаты из token_list принимаются только после on-chain
    #     подтверждения.
    def fetch_fresh(chain: str) -> Any:
        return lambda: client.get_token_list(address, chain_id=chain, is_all=not RABBY_IS_CORE)

    def suspects_for(toks: list[dict]) -> list[str]:
        by = _by_chain(toks)
        return sorted((c for c, v in agg_chains.items()
                       if v > COMPONENT_TOL_ABS and v > by.get(c, 0.0) + proto_by_chain.get(c, 0.0)
                       and not _close(v, by.get(c, 0.0) + proto_by_chain.get(c, 0.0))),
                      key=lambda c: -agg_chains[c])

    stage: dict[Any, Any] = {("fresh", c): fetch_fresh(c) for c in suspects_for(tokens)[:CHAIN_REFETCH_MAX]}
    raw_tokens = tokens
    rabby_tokens_usd = sum(_token_value(t) for t in raw_tokens)
    stats: dict[str, Any] = {"rejected_usd": 0.0, "confirmed": set()}
    stage["verify"] = lambda: _verify_tokens(raw_tokens, address, chain_map, notes, require=False,
                                             proxy=proxy, memo=shared, stats=stats)
    fresh = run_parallel(stage)
    tokens = unwrap(fresh.pop("verify"))

    suspects = suspects_for(tokens)
    refetch = suspects[:CHAIN_REFETCH_MAX]
    missing = [c for c in refetch if ("fresh", c) not in fresh]
    fresh.update(run_parallel({("fresh", c): fetch_fresh(c) for c in missing}))

    have = {(str(t.get("chain")), str(t.get("id") or "").lower()) for t in tokens}
    candidates: dict[str, list[dict]] = {}
    for chain in refetch:
        candidates[chain] = [t for t in unwrap(fresh[("fresh", chain)])
                             if _token_ok(t) and t.get("is_wallet", True)
                             and (str(t.get("chain")), str(t.get("id") or "").lower()) not in have]
        if candidates[chain] and not ONCHAIN_VERIFY:
            candidates[chain] = []
            notes.append(f"{chain}: token_list даёт больше кэша, но без on-chain проверки не принимается")
    chain_notes: dict[str, list[str]] = {c: [] for c in candidates}
    chain_stats: dict[str, dict[str, Any]] = {c: {} for c in candidates}
    confirmed = run_parallel({
        c: (lambda c=c: _verify_tokens(candidates[c], address, chain_map, chain_notes[c], require=True,
                                       proxy=proxy, memo=shared, stats=chain_stats[c]))
        for c in refetch if candidates[c]
    })
    for st in chain_stats.values():
        stats["confirmed"] |= st.get("confirmed", set())
    by_chain = _by_chain(tokens)
    phantom_chains: dict[str, float] = {}
    for chain in refetch:
        notes.extend(chain_notes.get(chain, []))
        accepted = unwrap(confirmed[chain]) if chain in confirmed else []
        if accepted:
            add_usd = sum(_token_value(t) for t in accepted)
            tokens = tokens + accepted
            by_chain[chain] = by_chain.get(chain, 0.0) + add_usd
            notes.append(f"{chain}: кэш токенов устарел, on-chain подтверждены токены на ${add_usd:.2f}")
        explained = by_chain.get(chain, 0.0) + proto_by_chain.get(chain, 0.0)
        if not _close(agg_chains[chain], explained) and agg_chains[chain] > explained:
            phantom_chains[chain] = agg_chains[chain] - explained
    for chain in suspects[CHAIN_REFETCH_MAX:]:
        phantom_chains[chain] = agg_chains[chain] - by_chain.get(chain, 0.0) - proto_by_chain.get(chain, 0.0)
    tokens_usd = sum(_token_value(t) for t in tokens)

    # 3a) App-chain: нативные API — единственный источник значений.
    if APPCHAIN_VERIFY:
        for key, (label, chain) in APPCHAIN_LABELS.items():
            pos = native.get(key) or {}
            value = float(pos.get("total_usd") or 0.0)
            if round(value, 2) >= MIN_VALUE_DISPLAY:
                protocols_usd += value
                protocols_data.append({
                    "logo": "",
                    "name": f"{label} (native API)",
                    "chain": chain,
                    "value": round(value, 2),
                    "positions": [{
                        "type": d.get("type", ""),
                        "value": round(float(d.get("value") or 0), 2),
                        "supply": f"{d.get('symbol', '?')} {float(d.get('amount') or 0):.4f}",
                        "rewards": "",
                    } for d in pos.get("details") or []],
                })
        native_total = {k: float((v or {}).get("total_usd") or 0.0) for k, v in native.items()}
        for app_id, claimed in claimed_appchain.items():
            nat = APPCHAIN_NATIVE.get(app_id)
            if nat and not _close(claimed, native_total.get(nat, 0.0)):
                notes.append(f"Rabby приписывает {app_id} ${claimed:.2f}, нативный API даёт "
                             f"${native_total.get(nat, 0.0):.2f} — данные Rabby отклонены")
                raw_evidence.setdefault("rabby_appchain", []).append(
                    [p for p in portfolio if str(p.get("id") or "") == app_id])
    else:
        unverified_apps = dict(claimed_appchain)
    for app_id, claimed in unverified_apps.items():
        unverified_usd += claimed
        if claimed >= MIN_VALUE_DISPLAY:
            notes.append(f"app-chain позиция {app_id} ${claimed:.2f} не проверяется нативным API — в итог не входит")

    protocols_data.sort(key=lambda p: p["value"], reverse=True)

    # 4) NFT (в сумму не входят; сбой не срывает выборку).
    nft_data = r["nft"] if with_nft and not isinstance(r["nft"], Exception) else []

    # 5) Итог и контроль по агрегату.
    total_usd = tokens_usd + protocols_usd
    aggregate_agrees = True if aggregate_usd is None else _close(aggregate_usd, total_usd)
    if not aggregate_agrees:
        detail = ", ".join(f"{c}: +${v:.2f}" for c, v in sorted(phantom_chains.items(), key=lambda kv: -kv[1])[:5])
        notes.append(f"агрегат Rabby ${aggregate_usd:.2f} отклонён: проверенные компоненты дают ${total_usd:.2f}"
                     + (f" (необъяснённые сети: {detail})" if detail else ""))
        raw_evidence["aggregate"] = {"total_usd_value": aggregate_usd,
                                     "chain_list": [(c, v) for c, v in agg_chains.items() if v > 0]}
        raw_evidence["tokens_by_chain"] = {c: round(v, 4) for c, v in by_chain.items() if v > 0}

    tokens_data = _build_tokens_data(tokens)
    top_tokens = sorted(tokens_data, key=lambda t: t["value"], reverse=True)[:3]

    snap = {
        "address": address,
        "total_usd": total_usd,
        "tokens_usd": tokens_usd,
        "protocols_usd": protocols_usd,
        "nft_usd": 0.0,
        "aggregate_usd": aggregate_usd,
        "aggregate_agrees": aggregate_agrees,
        "unverified_usd": unverified_usd,
        "tokens": len(tokens_data),
        "chains": "",
        "top_tokens": ", ".join(f"{t['symbol']}(${t['value']:.2f})" for t in top_tokens),
        "tokens_data": tokens_data,
        "protocols_data": protocols_data,
        "nft_data": [],
        "proxy": proxy or "direct",
        "status": "OK",
        "error": "",
        "notes": notes,
        "corroborated": True,
        "snapshots": 1,
        "onchain_rejected_usd": round(stats["rejected_usd"], 2),
        "_rabby_tokens_usd": rabby_tokens_usd,
        "_confirmed": frozenset(stats["confirmed"]),
    }
    _attach_nfts(snap, nft_data)
    _write_log(address, snap, raw_evidence or None)
    return snap


def _attach_nfts(snap: dict[str, Any], nft_data: list[dict]) -> None:
    """NFT в результат. Список сетей (chains) — только сети с балансом
    (токены + протоколы): он не зависит от того, успели ли догрузиться NFT."""
    snap["nft_data"] = nft_data
    chains = {t["chain"] for t in snap["tokens_data"] if t.get("chain")}
    chains |= {p["chain"] for p in snap["protocols_data"] if p.get("chain")}
    snap["chains"] = ", ".join(sorted(chains))


def _wallet_nfts(address: str, proxy_manager: ProxyManager, first_proxy: str | None) -> list[dict]:
    """NFT кошелька: запрос через first_proxy; если он упал или не ответил за
    NFT_HEDGE_SEC — параллельно следующий через другой прокси (часть запросов
    collection_list зависает на стороне сервера). До NFT_ATTEMPTS попыток;
    сбой всех → []."""
    def attempt(first: bool) -> Any:
        def run() -> list[dict]:
            proxy = first_proxy if first else (proxy_manager.get_proxy() or first_proxy)
            try:
                return _fetch_nfts(RabbyClient(proxy=proxy), address)
            except ProxyDead as e:
                proxy_manager.report_dead(e.proxy)
                raise
        return run

    try:
        return first_success([attempt(i == 0) for i in range(NFT_ATTEMPTS)], NFT_HEDGE_SEC)
    except Exception:  # noqa: BLE001
        return []


def _load_nfts_background(result: dict[str, Any], address: str, proxy_manager: ProxyManager,
                          first_proxy: str | None) -> None:
    """Запускает загрузку NFT в фоне; по готовности они дописываются в result."""
    def job() -> None:
        _attach_nfts(result, _wallet_nfts(address, proxy_manager, first_proxy))

    fut = _NFT_POOL.submit(job)
    with _NFT_LOCK:
        _NFT_PENDING.add(fut)
    fut.add_done_callback(_forget_nft)


def _forget_nft(fut: Future) -> None:
    with _NFT_LOCK:
        _NFT_PENDING.discard(fut)


def pending_nfts() -> int:
    """Сколько кошельков ещё догружают NFT."""
    with _NFT_LOCK:
        return len(_NFT_PENDING)


def wait_nfts(timeout: float | None = None) -> int:
    """Ждёт фоновую загрузку NFT (перед экспортом). Возвращает, сколько не успело."""
    with _NFT_LOCK:
        futures = list(_NFT_PENDING)
    if futures:
        wait(futures, timeout=timeout)
    return pending_nfts()


# ---------------------------------------------------------------- кошелёк

def _is_transient(err: Exception) -> bool:
    if isinstance(err, OnchainCheckFailed):
        return True
    s = str(err).lower()
    return any(k in s for k in ("timeout", "timed out", "429", "connection", "reset", "502", "503", "504", "proxy",
                                "недоступ"))


def _is_rate_limited(err: Exception) -> bool:
    s = str(err).lower()
    return "429" in s or "403" in s or "too many" in s or "forbidden" in s


def _retry_delay(err: Exception, failures: int, proxy: str | None) -> float:
    """Пауза перед повтором выборки. Через прокси следующий запрос уходит с
    другого IP — ждать почти не нужно; в прямом режиме — нарастающая пауза."""
    if proxy is not None:
        return RETRY_PROXY_BACKOFF_SEC
    if _is_rate_limited(err):
        return min(RETRY_429_BACKOFF_SEC * failures, 30.0)
    if _is_transient(err):
        return min(RETRY_BACKOFF_SEC * failures, 10.0)
    return 0.0


def _delayed_snapshot(address: str, proxy: str | None, shared: Memo, delay: float) -> dict[str, Any]:
    if delay > 0:
        time.sleep(delay)
    return _fetch_snapshot(address, proxy, shared, with_nft=False)


def check_wallet(address: str, proxy_manager: ProxyManager, wallet_idx: int = -1) -> dict[str, Any]:
    """Проверяет баланс одного кошелька с защитой от «фантомных» балансов.

    CORROBORATION_MIN_AGREE выборок запускаются одновременно через разные
    прокси; упавшая выборка сразу заменяется новой (другой прокси), а если
    выборка не завершилась за SNAPSHOT_HEDGE_SEC, параллельно запускается
    замена (хедж) — берутся первые согласованные. NFT догружаются в фоне и
    дописываются в результат позже — перед экспортом вызвать wait_nfts().
    Баланс принимается (OK), когда
    набралось CORROBORATION_MIN_AGREE согласованных по total_usd выборок. Если
    бюджет выборок исчерпан без согласия — UNVERIFIED с консервативным
    значением (наибольшая согласованная группа, при равенстве — меньшая сумма).
    """
    total_start = time.perf_counter()
    need = max(1, CORROBORATION_MIN_AGREE) if CORROBORATION_ENABLED else 1
    max_fetches = max(1, CORROBORATION_MAX_FETCHES) if CORROBORATION_ENABLED else 1
    max_attempts = max(RETRY_ATTEMPTS, max_fetches * 3) if CORROBORATION_ENABLED else RETRY_ATTEMPTS

    snapshots: list[dict[str, Any]] = []
    pending: dict[Future, str | None] = {}
    started: dict[Future, float] = {}
    attempts = failures = 0
    last_error: Exception | None = None
    used_proxy: str | None = None
    # Кэш «поколения» выборок: нативные API и on-chain считаются один раз на
    # поколение. Хеджи зависших выборок начинают новое поколение, чтобы не
    # ждать тот же зависший запрос.
    memo = Memo()
    pool = ThreadPoolExecutor(max_workers=max_fetches)

    def budget_left() -> bool:
        return len(snapshots) + len(pending) < max_fetches and attempts < max_attempts

    def launch(delay: float = 0.0) -> bool:
        nonlocal attempts, used_proxy
        proxy = proxy_manager.get_proxy()
        if not proxy and not direct_allowed():
            return False
        attempts += 1
        used_proxy = proxy
        fut = pool.submit(_delayed_snapshot, address, proxy, memo, delay)
        pending[fut] = proxy
        started[fut] = time.monotonic() + delay
        return True

    def active() -> list[Future]:
        """Выборки в полёте, которые ещё не считаются зависшими."""
        now = time.monotonic()
        return [f for f in pending if now - started[f] < SNAPSHOT_HEDGE_SEC]

    def top_up(delay: float = 0.0) -> None:
        """Держит в полёте столько живых выборок, сколько не хватает до согласия."""
        want = max(1, need - len(_largest_agreeing_cluster(_eligible(snapshots))))
        while len(active()) < want and budget_left() and launch(delay):
            pass

    try:
        top_up()
        if not pending:
            _debug_log(wallet_idx, address, "NO_PROXY", attempts, 0, "—")
            return _error_result(address, "Нет доступных прокси", used_proxy)
        first_proxy = used_proxy

        result: dict[str, Any] | None = None
        while pending:
            now = time.monotonic()
            live = active()
            timeout = (max(0.05, min(started[f] + SNAPSHOT_HEDGE_SEC - now for f in live))
                       if live and budget_left() else None)
            done, _ = wait(list(pending), timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                memo = Memo()  # хедж: новое поколение, мимо зависших запросов
                top_up()
                continue
            retry_after = 0.0
            for fut in done:
                proxy = pending.pop(fut)
                started.pop(fut, None)
                try:
                    snapshots.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    failures += 1
                    err_str = str(e).lower()
                    if isinstance(e, ProxyDead):
                        proxy_manager.report_dead(e.proxy)
                    elif proxy and ("timeout" in err_str or "timed out" in err_str):
                        proxy_manager.report_timeout(proxy)
                    elif proxy and _is_rate_limited(e):
                        proxy_manager.report_rate_limited(proxy)
                    retry_after = _retry_delay(e, failures, proxy)
                    _debug_log(wallet_idx, address, "FAIL", attempts, time.perf_counter() - total_start,
                               _mask_proxy(proxy), str(e)[:80])

            cluster = _largest_agreeing_cluster(_eligible(snapshots))
            if len(cluster) >= need:
                result = _finalize(_rep(cluster), snapshots, cluster, corroborated=True)
                break
            top_up(retry_after)

        if result is None and snapshots:
            cluster = _largest_agreeing_cluster(_eligible(snapshots) or snapshots)
            result = _finalize(_rep(cluster), snapshots, cluster, corroborated=False)
        if result is None:
            _debug_log(wallet_idx, address, "ERROR", attempts, time.perf_counter() - total_start,
                       _mask_proxy(used_proxy), str(last_error)[:80])
            return _error_result(address, str(last_error) if last_error else "Unknown error", used_proxy)

        _load_nfts_background(result, address, proxy_manager, first_proxy)
        _debug_log(wallet_idx, address, result["status"], attempts, time.perf_counter() - total_start,
                   _mask_proxy(used_proxy))
        return result
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _is_tainted(snap: dict[str, Any]) -> bool:
    """On-chain проверка сняла заметную часть токенов → Rabby отдал для этого
    запроса чужой список (подмена целиком: наших токенов в нём может не быть)."""
    rejected = snap.get("onchain_rejected_usd", 0.0)
    return rejected > max(TAINT_ABS_USD, TAINT_REL * snap.get("_rabby_tokens_usd", 0.0))


def _eligible(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Выборки, которые могут участвовать в согласии: не заражённые и полные —
    содержат все токены, подтверждённые на цепочке в любой выборке кошелька.
    On-chain проверка снимает лишние токены, но пропавший из ответа Rabby
    токен можно заметить только по другим выборкам."""
    known: set = set()
    for s in snapshots:
        known |= s.get("_confirmed", frozenset())
    return [s for s in snapshots
            if not _is_tainted(s) and known <= s.get("_confirmed", frozenset())]


def _finalize(chosen: dict[str, Any], snapshots: list[dict[str, Any]],
              cluster: list[dict[str, Any]], corroborated: bool) -> dict[str, Any]:
    """Сводит примечания всех выборок в выбранный результат и ставит статус."""
    notes: list[str] = []
    for s in snapshots:
        for n in s.get("notes", []):
            if n not in notes:
                notes.append(n)
    rejected = sum(1 for s in snapshots if not s.get("aggregate_agrees", True))
    if rejected:
        notes.insert(0, f"агрегат Rabby отклонён в {rejected} из {len(snapshots)} выборок")
    chosen = {k: v for k, v in chosen.items() if not k.startswith("_")}
    eligible_ids = {id(s) for s in _eligible(snapshots)}
    skipped = sum(1 for s in snapshots if id(s) not in eligible_ids)
    if skipped:
        notes.insert(0, f"отброшено выборок с чужим/неполным списком токенов Rabby: {skipped}")
    chosen["notes"] = notes
    chosen["snapshots"] = len(snapshots)
    chosen["corroborated"] = corroborated
    if corroborated:
        chosen["status"] = "OK"
        chosen["error"] = "; ".join(notes)
    else:
        values = sorted(round(s["total_usd"], 2) for s in snapshots)
        chosen["status"] = "UNVERIFIED"
        chosen["error"] = (f"баланс не подтверждён: выборки {values}, "
                           f"взято консервативное ${chosen['total_usd']:.2f}"
                           + ("; " + "; ".join(notes) if notes else ""))
    return chosen


def _debug_log(idx: int, addr: str, status: str, attempt: int, sec: float, proxy: str, err: str = "") -> None:
    """Отладочный лог в stderr (config.DEBUG)."""
    if not DEBUG:
        return
    short = f"{addr[:10]}...{addr[-6:]}" if len(addr) > 20 else addr
    err_part = f" | {err}" if err else ""
    sys.stderr.write(f"[DEBUG] #{idx} {short} | {status} | попытка {attempt} | {sec:.2f}s | {proxy}{err_part}\n")
    sys.stderr.flush()


def _error_result(address: str, err_msg: str, proxy: str | None) -> dict[str, Any]:
    return {
        "address": address,
        "total_usd": 0.0,
        "tokens_usd": 0.0,
        "protocols_usd": 0.0,
        "nft_usd": 0.0,
        "aggregate_usd": 0.0,
        "aggregate_agrees": True,
        "unverified_usd": 0.0,
        "tokens": 0,
        "chains": "",
        "top_tokens": "",
        "tokens_data": [],
        "protocols_data": [],
        "nft_data": [],
        "proxy": proxy or "direct",
        "status": "ERROR",
        "error": err_msg,
        "notes": [],
        "corroborated": False,
        "snapshots": 0,
        "onchain_rejected_usd": 0.0,
    }
