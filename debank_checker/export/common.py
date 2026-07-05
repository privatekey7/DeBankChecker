"""
Общая логика фильтрации и построения данных для экспорта
"""

from debank_checker.export.config import ExportConfig


def filter_tokens(tokens_data: list[dict], token_filter: str | None) -> list[dict]:
    """Фильтр по symbol:chain (например ETH:eth)."""
    if not token_filter or ":" not in token_filter:
        return tokens_data
    parts = token_filter.split(":", 1)
    symbol, chain = (parts[0].strip().upper(), parts[1].strip().lower()) if len(parts) == 2 else ("", "")
    return [
        t for t in tokens_data
        if (not symbol or (t.get("symbol") or "").upper() == symbol)
        and (not chain or (t.get("chain") or "").lower() == chain)
    ]


def filter_protocols(protocols_data: list[dict], protocol_filter: str | None) -> list[dict]:
    """Фильтр по name:chain. :chain — все в сети."""
    if not protocol_filter or ":" not in protocol_filter:
        return protocols_data
    parts = protocol_filter.split(":", 1)
    name, chain = (parts[0].strip(), parts[1].strip().lower()) if len(parts) == 2 else ("", "")
    return [
        p for p in protocols_data
        if (not name or (p.get("name") or "").strip() == name)
        and (not chain or (p.get("chain") or "").lower() == chain)
    ]


def filter_nft(nft_data: list[dict], nft_filter: str | None) -> list[dict]:
    """Фильтр по collection:chain."""
    if not nft_filter or ":" not in nft_filter:
        return nft_data
    parts = nft_filter.split(":", 1)
    collection, chain = (parts[0].strip(), parts[1].strip().lower()) if len(parts) == 2 else ("", "")
    return [
        n for n in nft_data
        if (not collection or collection.lower() in (n.get("name") or "").lower())
        and (not chain or (n.get("chain") or "").lower() == chain)
    ]


def build_export_data(results: list[dict], config: ExportConfig) -> dict:
    """
    Построить данные для экспорта по config.
    Возвращает: { "total": {...}, "summary": [...], "tokens": [...], "protocols": [...], "nft": [...] }
    """
    ok = [r for r in results if r and r.get("status") == "OK"]
    total_sum = sum(r.get("total_usd", 0) for r in ok)
    data = {
        "total": {"sum_usd": total_sum, "ok_count": len(ok), "total_count": len(results)},
        "summary": [],
        "tokens": [],
        "protocols": [],
        "nft": [],
    }

    if config.summary and not config.total_only:
        for i, r in enumerate(results, 1):
            data["summary"].append({
                "n": i,
                "address": r.get("address", ""),
                "total_usd": r.get("total_usd") if r.get("status") == "OK" else None,
                "chains": r.get("chains", ""),
                "status": r.get("status", ""),
            })

    if config.tokens:
        for r in results:
            if r.get("status") != "OK":
                continue
            for t in filter_tokens(r.get("tokens_data", []), config.token_filter):
                data["tokens"].append({
                    "address": r["address"],
                    "symbol": t.get("symbol", ""),
                    "chain": t.get("chain", ""),
                    "amount": t.get("amount", 0),
                    "price": t.get("price", 0),
                    "value": t.get("value", 0),
                })

    if config.protocols:
        for r in results:
            if r.get("status") != "OK":
                continue
            for p in filter_protocols(r.get("protocols_data", []), config.protocol_filter):
                data["protocols"].append({
                    "address": r["address"],
                    "name": p.get("name", ""),
                    "chain": p.get("chain", ""),
                    "value": p.get("value", 0),
                })

    if config.nft:
        for r in results:
            if r.get("status") != "OK":
                continue
            for n in filter_nft(r.get("nft_data", []), config.nft_filter):
                data["nft"].append({
                    "address": r["address"],
                    "collection": n.get("name", ""),
                    "chain": n.get("chain", ""),
                    "amount": n.get("amount", 0),
                })

    return data
