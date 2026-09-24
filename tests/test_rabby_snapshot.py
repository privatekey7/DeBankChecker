"""Тесты сборки снапшота баланса через Rabby (_fetch_snapshot) и корроборации.

Мокаем ТОЛЬКО внешние клиенты (RabbyClient, нативные app-chain API) — границу
системы. Логика маппинга ответов в result-dict тестируется как есть.
"""
from __future__ import annotations

import itertools
import time

import debank_checker.checker as checker


class FakeRabbyClient:
    """Заглушка Rabby API с заранее заданными ответами эндпоинтов."""

    def __init__(self, proxy: str | None):
        self.proxy = proxy

    def get_total_balance(self, address: str, is_core: bool = True) -> dict:
        # честный агрегат: 371.32 (USDC eth) + 8.68 (ETH arb) + 8.68 (позиция на arb)
        return {
            "total_usd_value": 388.68,
            "chain_list": [
                {"id": "eth", "usd_value": 371.32},
                {"id": "arb", "usd_value": 17.36},
                {"id": "bsc", "usd_value": 0.0},  # нулевую сеть пропускаем
            ],
        }

    def get_cache_token_list(self, address: str) -> list:
        """Кэш всех сетей одним запросом (как реальный endpoint)."""
        return [
            {"symbol": "USDC", "chain": "eth", "amount": 371.32, "price": 1.0,
             "is_verified": True, "is_core": True, "is_scam": False,
             "logo_url": "u"},
            # скам-токен — должен быть отфильтрован
            {"symbol": "SCAM", "chain": "eth", "amount": 1e9, "price": 1.0,
             "is_verified": True, "is_core": True, "is_scam": True},
            # не-core токен — должен быть отфильтрован при is_core
            {"symbol": "RANDOM", "chain": "eth", "amount": 100, "price": 5.0,
             "is_verified": True, "is_core": False, "is_scam": False},
            {"symbol": "ETH", "chain": "arb", "amount": 0.004, "price": 2170.0,
             "is_verified": True, "is_core": True, "is_scam": False},
        ]

    def get_token_list(self, address: str, chain_id: str, is_all: bool = False) -> list:
        """Фолбэк / свежий по-сетевой список."""
        data = {
            "eth": [
                {"symbol": "USDC", "chain": "eth", "amount": 371.32, "price": 1.0,
                 "is_verified": True, "is_core": True, "is_scam": False,
                 "logo_url": "u"},
            ],
            "arb": [
                {"symbol": "ETH", "chain": "arb", "amount": 0.004, "price": 2170.0,
                 "is_verified": True, "is_core": True, "is_scam": False},
            ],
        }
        return data.get(chain_id, [])

    def get_complex_app_list(self, address: str) -> list:
        return [
            {
                "id": "gmx", "name": "GMX", "logo_url": "l",
                "portfolio_item_list": [
                    {
                        "name": "Rewards",
                        "stats": {"net_usd_value": 8.68},
                        "asset_token_list": [
                            {"symbol": "USDC", "amount": 8.68, "price": 1.0,
                             "is_verified": True, "is_scam": False, "chain": "arb"}
                        ],
                        "detail": {"supply_token_list": [
                            {"symbol": "USDC", "amount": 8.68, "chain": "arb"}
                        ]},
                    }
                ],
            }
        ]

    def get_collection_list(self, address: str, is_all: bool = True) -> list:
        return [
            {"name": "GMCards", "chain": "soneium", "nft_list": [{}, {}],
             "is_scam": False, "is_verified": True},
            # скам-коллекция — отфильтровать
            {"name": "SCAMNFT", "chain": "eth", "nft_list": [{}],
             "is_scam": True, "is_verified": False},
        ]


def _no_appchain(address, proxy):
    return {"hyperliquid": {"total_usd": 0.0, "details": []},
            "lighter": {"total_usd": 0.0, "details": []},
            "polymarket": {"total_usd": 0.0, "details": []}}


def _onchain_matches(token, address, chain_map, proxy=None):
    """On-chain количество совпадает с Rabby."""
    return float(token.get("amount") or 0)


def _patch(monkeypatch, client=FakeRabbyClient, appchain=_no_appchain, onchain=_onchain_matches):
    monkeypatch.setattr(checker, "RabbyClient", client)
    monkeypatch.setattr(checker, "_appchain_positions", appchain)
    monkeypatch.setattr(checker, "_onchain_verify_token", onchain)
    monkeypatch.setattr(checker, "_chain_map", lambda client: {"eth": {"evm": 1, "native": "eth"},
                                                                "arb": {"evm": 42161, "native": "arb"}})


def test_total_is_sum_of_verified_components(monkeypatch):
    """total_usd = токены + проверенные позиции; агрегат согласован."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["total_usd"], 2) == 388.68
    assert snap["aggregate_usd"] == 388.68
    assert snap["aggregate_agrees"] is True
    assert snap["notes"] == []


def test_contaminated_aggregate_is_rejected(monkeypatch):
    """Агрегат Rabby с чужой суммой НЕ становится итогом; расхождение — в примечании."""
    class Contaminated(FakeRabbyClient):
        def get_total_balance(self, address, is_core=True):
            return {"total_usd_value": 246977.52,
                    "chain_list": [{"id": "eth", "usd_value": 371.32},
                                   {"id": "arb", "usd_value": 246606.2}]}

    _patch(monkeypatch, Contaminated)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["total_usd"], 2) == 388.68
    assert snap["aggregate_agrees"] is False
    assert any("отклонён" in n for n in snap["notes"])
    assert "arb" in snap["notes"][-1]


def test_stale_token_cache_refreshed_per_chain(monkeypatch):
    """Агрегат по сети выше кэша токенов, свежий token_list подтверждает → берём свежие токены."""
    class StaleCache(FakeRabbyClient):
        def get_total_balance(self, address, is_core=True):
            return {"total_usd_value": 488.68,
                    "chain_list": [{"id": "eth", "usd_value": 471.32}, {"id": "arb", "usd_value": 17.36}]}

        def get_token_list(self, address, chain_id, is_all=False):
            if chain_id == "eth":
                return [{"id": "0xdai", "symbol": "DAI", "chain": "eth", "amount": 100.0, "price": 1.0,
                         "is_verified": True, "is_core": True, "is_scam": False}]
            return super().get_token_list(address, chain_id, is_all)

    _patch(monkeypatch, StaleCache)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["tokens_usd"], 2) == 480.0
    assert snap["aggregate_agrees"] is True
    assert any("устарел" in n and "on-chain подтверждены" in n for n in snap["notes"])


def test_phantom_token_from_cache_dropped_by_onchain(monkeypatch):
    """Токен из cache_token_list с on-chain балансом 0 отбрасывается."""
    def onchain(token, address, chain_map, proxy=None):
        return 0.0 if token["symbol"] == "USDC" else float(token["amount"])

    _patch(monkeypatch, onchain=onchain)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["tokens_usd"], 2) == 8.68
    assert any("фантомный токен" in n for n in snap["notes"])
    assert snap["aggregate_agrees"] is False


def test_token_amount_corrected_from_chain(monkeypatch):
    """Расхождение количества исправляется по данным сети."""
    def onchain(token, address, chain_map, proxy=None):
        return 100.0 if token["symbol"] == "USDC" else float(token["amount"])

    _patch(monkeypatch, onchain=onchain)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["tokens_usd"], 2) == 108.68
    assert any("по данным сети" in n for n in snap["notes"])


def test_unverifiable_token_fails_snapshot(monkeypatch):
    """RPC сети недоступен → выборка не засчитывается (OnchainCheckFailed), а не тихий приём."""
    def onchain(token, address, chain_map, proxy=None):
        raise checker.OnchainCheckFailed("все ноды недоступны")

    _patch(monkeypatch, onchain=onchain)
    try:
        checker._fetch_snapshot("0xabc", "http://proxy")
    except checker.OnchainCheckFailed as e:
        assert "USDC@eth" in str(e)
        return
    raise AssertionError("ожидалось OnchainCheckFailed")


def test_unverifiable_chain_ends_as_unverified(monkeypatch):
    """Если RPC сети не поднимаются за все попытки — статус UNVERIFIED, не OK и не ноль."""
    def onchain(token, address, chain_map, proxy=None):
        raise checker.OnchainCheckFailed("все ноды недоступны")

    _patch(monkeypatch, onchain=onchain)
    monkeypatch.setattr(checker, "RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(checker, "CORROBORATION_MAX_FETCHES", 1)
    monkeypatch.setattr(checker, "RETRY_BACKOFF_SEC", 0.0)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "ERROR"
    assert "USDC@eth" in res["error"]


def test_refetched_token_list_candidates_need_onchain_confirmation(monkeypatch):
    """Кандидат из token_list без on-chain подтверждения не принимается (фантом $1017 на arb)."""
    class Inflated(FakeRabbyClient):
        def get_total_balance(self, address, is_core=True):
            return {"total_usd_value": 1405.0,
                    "chain_list": [{"id": "eth", "usd_value": 371.32}, {"id": "arb", "usd_value": 1034.0}]}

        def get_token_list(self, address, chain_id, is_all=False):
            if chain_id == "arb":
                return [{"id": "0xusdc", "symbol": "USDC", "chain": "arb", "amount": 1017.0, "price": 1.0,
                         "is_verified": True, "is_core": True, "is_scam": False}]
            return []

    def onchain(token, address, chain_map, proxy=None):
        return 0.0 if token.get("id") == "0xusdc" else float(token["amount"])

    _patch(monkeypatch, Inflated, onchain=onchain)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["total_usd"], 2) == 388.68
    assert snap["aggregate_agrees"] is False
    assert all(t["symbol"] != "USDC" or t["chain"] != "arb" for t in snap["tokens_data"])


def test_aggregate_403_does_not_fail_snapshot(monkeypatch):
    """403 на total_balance → выборка без агрегата, итог из компонентов."""
    class Banned(FakeRabbyClient):
        def get_total_balance(self, address, is_core=True):
            raise RuntimeError("HTTP Error 403: Forbidden")

    _patch(monkeypatch, Banned)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert snap["status"] == "OK"
    assert snap["aggregate_usd"] is None
    assert round(snap["total_usd"], 2) == 388.68
    assert any("недоступен" in n for n in snap["notes"])


def test_phantom_appchain_positions_from_rabby_ignored(monkeypatch):
    """Chainless Hyperliquid-позиция из Rabby ($18k) не входит в итог, если нативный API даёт 0."""
    class PhantomHL(FakeRabbyClient):
        def get_complex_app_list(self, address):
            return super().get_complex_app_list(address) + [{
                "id": "hyperliquid", "name": "Hyperliquid",
                "portfolio_item_list": [{
                    "name": "Deposit", "detail_types": ["common"],
                    "base": {"app_id": "hyperliquid", "user_addr": address},
                    "stats": {"net_usd_value": 18668.08, "asset_usd_value": 18668.08},
                    "asset_token_list": [{"symbol": "HYPE", "amount": 400, "price": 46.67,
                                          "is_verified": True, "is_scam": False}],
                    "detail": {"supply_token_list": [{"symbol": "HYPE", "amount": 400}]},
                }],
            }]

    _patch(monkeypatch, PhantomHL)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["total_usd"], 2) == 388.68
    assert all("Hyperliquid" not in p["name"] for p in snap["protocols_data"])
    assert any("hyperliquid" in n and "отклонены" in n for n in snap["notes"])


def test_native_appchain_positions_counted(monkeypatch):
    """Позиции, подтверждённые нативным API Hyperliquid, входят в итог."""
    def hl(address, proxy):
        return {"hyperliquid": {"total_usd": 100.0, "details": [
                    {"type": "Perpetuals", "symbol": "USDC", "amount": 100.0, "value": 100.0}]},
                "lighter": {"total_usd": 0.0, "details": []},
                "polymarket": {"total_usd": 0.0, "details": []}}

    _patch(monkeypatch, appchain=hl)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["total_usd"], 2) == 488.68
    names = [p["name"] for p in snap["protocols_data"]]
    assert "Hyperliquid (native API)" in names
    # агрегат 388.68 теперь ниже проверенного итога → расхождение отмечено, но итог не режется
    assert snap["aggregate_agrees"] is False


def test_polymarket_claim_rejected_by_native_api(monkeypatch):
    """Polymarket-позиция из Rabby при нативном 0 отклоняется (не unverified, а фантом)."""
    class Poly(FakeRabbyClient):
        def get_complex_app_list(self, address):
            return [{
                "id": "polymarket", "name": "Polymarket",
                "portfolio_item_list": [{
                    "name": "Prediction", "stats": {"net_usd_value": 857.05},
                    "asset_token_list": [{"symbol": "USDC", "amount": 857.05, "price": 1.0}],
                    "detail": {},
                }],
            }]

    _patch(monkeypatch, Poly)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["total_usd"], 2) == 380.0
    assert snap["unverified_usd"] == 0.0
    assert any("polymarket" in n and "отклонены" in n for n in snap["notes"])


def test_unknown_appchain_app_excluded_and_noted(monkeypatch):
    """App-chain позиция без нативной проверки (opinion) — в unverified_usd, не в итоге."""
    class Poly(FakeRabbyClient):
        def get_complex_app_list(self, address):
            return [{
                "id": "opinion", "name": "Opinion",
                "portfolio_item_list": [{
                    "name": "Prediction", "stats": {"net_usd_value": 55.0},
                    "asset_token_list": [{"symbol": "USDC", "amount": 55, "price": 1.0}],
                    "detail": {},
                }],
            }]

    _patch(monkeypatch, Poly)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert round(snap["total_usd"], 2) == 380.0
    assert snap["unverified_usd"] == 55.0
    assert any("opinion" in n for n in snap["notes"])


def test_appchain_api_failure_fails_snapshot(monkeypatch):
    """Недоступность нативного API → исключение (fail-closed), а не молчаливый ноль."""
    def broken(address, proxy):
        raise RuntimeError("Hyperliquid недоступен")

    _patch(monkeypatch, appchain=broken)
    try:
        checker._fetch_snapshot("0xabc", "http://proxy")
    except RuntimeError:
        return
    raise AssertionError("ожидалось исключение")


def test_scam_and_non_core_tokens_filtered(monkeypatch):
    """SCAM (is_scam) и RANDOM (не core) не попадают в tokens_data."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    symbols = {t["symbol"] for t in snap["tokens_data"]}
    assert "SCAM" not in symbols
    assert "RANDOM" not in symbols
    assert "USDC" in symbols and "ETH" in symbols


def test_zero_value_chains_skipped(monkeypatch):
    """Сеть bsc с usd_value=0 не запрашивается → её токенов нет."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert "bsc" not in snap["chains"]


def test_protocols_mapped_with_chain_from_token(monkeypatch):
    """EVM DeFi-протокол попадает в protocols_data, chain берётся из токена позиции."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert len(snap["protocols_data"]) == 1
    proto = snap["protocols_data"][0]
    assert proto["name"] == "GMX"
    assert proto["chain"] == "arb"
    assert proto["value"] == 8.68


def test_scam_nft_collection_filtered(monkeypatch):
    """Скам-коллекция отфильтрована, валидная — с количеством из nft_list."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    names = {n["name"] for n in snap["nft_data"]}
    assert names == {"GMCards"}
    assert snap["nft_data"][0]["amount"] == 2


def test_result_dict_schema_matches_debank(monkeypatch):
    """result-dict содержит все ключи, которые ждут экспортёры/меню."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    required = {
        "address", "total_usd", "tokens_usd", "protocols_usd", "nft_usd",
        "tokens", "chains", "top_tokens", "tokens_data", "protocols_data",
        "nft_data", "proxy", "status", "error", "aggregate_usd", "aggregate_agrees",
        "unverified_usd", "notes",
    }
    assert required <= set(snap.keys())
    assert snap["status"] == "OK"


def test_empty_wallet_returns_zero_total(monkeypatch):
    """Пустой кошелёк: total=0, пустые списки, статус OK."""
    class EmptyClient(FakeRabbyClient):
        def get_total_balance(self, address, is_core=True):
            return {"total_usd_value": 0.0, "chain_list": []}

        def get_cache_token_list(self, address):
            return []

        def get_complex_app_list(self, address):
            return []

        def get_collection_list(self, address, is_all=True):
            return []

    _patch(monkeypatch, EmptyClient)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert snap["total_usd"] == 0.0
    assert snap["tokens_data"] == []
    assert snap["status"] == "OK"


def test_token_fallback_on_cache_failure(monkeypatch):
    """Сбой cache_token_list → токены собираются по-сетевым token_list."""
    class CacheBrokenClient(FakeRabbyClient):
        def get_cache_token_list(self, address):
            raise RuntimeError("HTTP Error 429: ")

    _patch(monkeypatch, CacheBrokenClient)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    symbols = {t["symbol"] for t in snap["tokens_data"]}
    assert symbols == {"USDC", "ETH"}


def test_wallet_tokens_fetched_by_single_cache_request(monkeypatch):
    """Основной путь: токены всех сетей берутся из cache_token_list, без по-сетевых запросов."""
    calls = {"cache": 0, "per_chain": 0}

    class CountingClient(FakeRabbyClient):
        def get_cache_token_list(self, address):
            calls["cache"] += 1
            return super().get_cache_token_list(address)

        def get_token_list(self, address, chain_id, is_all=False):
            calls["per_chain"] += 1
            return super().get_token_list(address, chain_id, is_all)

    _patch(monkeypatch, CountingClient)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert calls == {"cache": 1, "per_chain": 0}
    assert len(snap["tokens_data"]) == 2


# ---------------------------------------------------------------- check_wallet

class _PM:
    def __init__(self, proxies=("http://p1", "http://p2", "http://p3")):
        self._p = list(proxies)
        self.i = 0

    def get_proxy(self):
        p = self._p[self.i % len(self._p)]
        self.i += 1
        return p

    def report_timeout(self, proxy):
        pass

    def report_rate_limited(self, proxy):
        pass

    def report_dead(self, proxy):
        pass


def test_check_wallet_requires_two_agreeing_snapshots(monkeypatch):
    """Баланс принят (OK) после двух согласованных выборок."""
    _patch(monkeypatch)
    monkeypatch.setattr(checker, "CORROBORATION_MIN_AGREE", 2)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "OK"
    assert res["snapshots"] == 2
    assert round(res["total_usd"], 2) == 388.68


def test_check_wallet_unverified_when_snapshots_disagree(monkeypatch):
    """Каждая выборка даёт разную EVM-позицию (фантом complex_app_list) → UNVERIFIED
    с консервативным значением."""
    counter = itertools.count(1)

    class Drift(FakeRabbyClient):
        def get_complex_app_list(self, address):
            n = next(counter)
            return [{"id": "gmx", "name": "GMX", "portfolio_item_list": [{
                "name": "Rewards", "stats": {"net_usd_value": 100.0 * n},
                "asset_token_list": [{"symbol": "USDC", "amount": 100.0 * n, "price": 1.0, "chain": "arb"}],
                "detail": {"supply_token_list": [{"symbol": "USDC", "amount": 100.0 * n, "chain": "arb"}]},
            }]}]

    _patch(monkeypatch, Drift)
    monkeypatch.setattr(checker, "CORROBORATION_MIN_AGREE", 2)
    monkeypatch.setattr(checker, "CORROBORATION_MAX_FETCHES", 3)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "UNVERIFIED"
    assert res["snapshots"] == 3
    assert round(res["total_usd"], 2) == 480.0  # минимальная выборка: 380 токенов + 100
    assert "не подтверждён" in res["error"]


def test_check_wallet_retries_transient_errors(monkeypatch):
    """Сетевая ошибка первой выборки → повтор; итог OK."""
    calls = {"n": 0}

    class Flaky(FakeRabbyClient):
        def get_total_balance(self, address, is_core=True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Connection reset by peer")
            return super().get_total_balance(address, is_core)

    _patch(monkeypatch, Flaky)
    monkeypatch.setattr(checker, "CORROBORATION_MIN_AGREE", 2)
    monkeypatch.setattr(checker, "RETRY_BACKOFF_SEC", 0.0)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "OK"
    assert calls["n"] == 3


def test_check_wallet_no_proxy_without_direct_is_error(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(checker, "direct_allowed", lambda: False)
    res = checker.check_wallet("0xabc", _PM(proxies=(None,)))
    assert res["status"] == "ERROR"


# ---------------------------------------------------------------- скорость без потери проверок

class _RecordingPM(_PM):
    def __init__(self, proxies=("http://p1", "http://p2", "http://p3", "http://p4")):
        super().__init__(proxies)
        self.dead = []

    def report_dead(self, proxy):
        self.dead.append(proxy)


def test_check_wallet_hedges_stuck_snapshots(monkeypatch):
    """Обе первые выборки зависли → хедж через SNAPSHOT_HEDGE_SEC даёт результат, не дожидаясь их."""
    counter = itertools.count(1)

    class Stuck(FakeRabbyClient):
        def get_complex_app_list(self, address):
            if next(counter) <= 2:
                time.sleep(3)
            return super().get_complex_app_list(address)

    _patch(monkeypatch, Stuck)
    monkeypatch.setattr(checker, "SNAPSHOT_HEDGE_SEC", 0.1)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    started = time.perf_counter()
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "OK"
    assert round(res["total_usd"], 2) == 388.68
    assert time.perf_counter() - started < 2.0


def test_native_api_called_once_per_wallet(monkeypatch):
    """Нативные app-chain API запрашиваются один раз на кошелёк, а не в каждой выборке."""
    calls = []

    def appchain(address, proxy):
        calls.append(proxy)
        return _no_appchain(address, proxy)

    _patch(monkeypatch, appchain=appchain)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "OK" and res["snapshots"] == 2
    assert len(calls) == 1


def test_nfts_loaded_in_background(monkeypatch):
    """Баланс возвращается сразу, NFT дописываются в результат фоновой загрузкой."""
    _patch(monkeypatch)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert checker.wait_nfts(timeout=5) == 0
    assert [n["name"] for n in res["nft_data"]] == ["GMCards"]
    # chains — только сети с балансом: не меняется от того, догрузились ли NFT
    assert "soneium" not in res["chains"] and "eth" in res["chains"]


def test_dead_proxy_is_reported(monkeypatch):
    """Прокси, отвергший подключение (407), исключается из ротации."""
    from debank_checker.api.http import ProxyDead

    class DeadFirst(FakeRabbyClient):
        def get_cache_token_list(self, address):
            if self.proxy == "http://p1":
                raise ProxyDead(self.proxy, "CONNECT tunnel failed, response 407")
            return super().get_cache_token_list(address)

    _patch(monkeypatch, DeadFirst)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    monkeypatch.setattr(checker, "RETRY_PROXY_BACKOFF_SEC", 0.0)
    pm = _RecordingPM()
    res = checker.check_wallet("0xabc", pm)
    assert res["status"] == "OK"
    assert pm.dead == ["http://p1"]


# ---------------------------------------------------------------- подмена списка токенов Rabby

_TRUE_AMOUNTS = {"USDC": 371.32, "ETH": 0.004}  # что реально лежит на кошельке (on-chain)


def _onchain_truth(token, address, chain_map, proxy=None):
    return _TRUE_AMOUNTS.get(token["symbol"], 0.0)


_FOREIGN_LIST = [  # чужой кошелёк: наших USDC нет, есть чужой USDG и «чужое» количество ETH
    {"symbol": "USDG", "chain": "eth", "amount": 2428.78, "price": 1.0, "is_core": True},
    {"symbol": "ETH", "chain": "arb", "amount": 0.05, "price": 2170.0, "is_core": True},
]
_OUR_LIST_WITHOUT_USDC = [  # список без нашего крупного токена, остальное честно
    {"symbol": "ETH", "chain": "arb", "amount": 0.004, "price": 2170.0, "is_core": True},
]


def _client_with_lists(lists, aggregate=True):
    counter = itertools.count()

    class Swapping(FakeRabbyClient):
        def get_total_balance(self, address, is_core=True):
            if not aggregate:
                raise RuntimeError("HTTP Error 403: Forbidden")
            return super().get_total_balance(address, is_core)

        def get_cache_token_list(self, address):
            n = next(counter)
            return lists[n] if n < len(lists) else lists[-1]

    return Swapping


def test_foreign_token_list_pair_is_not_accepted(monkeypatch):
    """Инцидент 0x9Cd7…: две выборки с одинаковым ЧУЖИМ списком сошлись на пыли ($1.44).
    Такие выборки заражены (on-chain сняла >10%) и в согласии не участвуют."""
    good = FakeRabbyClient(None).get_cache_token_list("0xabc")
    _patch(monkeypatch, _client_with_lists([_FOREIGN_LIST, _FOREIGN_LIST, _FOREIGN_LIST, good, good]),
           onchain=_onchain_truth)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "OK"
    assert round(res["total_usd"], 2) == 388.68
    assert any("чужим/неполным" in n for n in res["notes"])
    assert not any(k.startswith("_") for k in res)


def test_list_missing_confirmed_token_is_not_accepted(monkeypatch):
    """Выборка без токена, подтверждённого on-chain в другой выборке, — неполная
    (агрегат Rabby недоступен, восстановить пропажу через сверку по сетям нельзя)."""
    good = FakeRabbyClient(None).get_cache_token_list("0xabc")
    _patch(monkeypatch, _client_with_lists([good, _OUR_LIST_WITHOUT_USDC, _OUR_LIST_WITHOUT_USDC, good],
                                           aggregate=False),
           onchain=_onchain_truth)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    monkeypatch.setattr(checker, "SNAPSHOT_HEDGE_SEC", 60)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "OK"
    assert round(res["total_usd"], 2) == 388.68


def test_only_foreign_lists_end_unverified(monkeypatch):
    """Rabby всё время отдаёт чужой список → UNVERIFIED, а не «подтверждённая» пыль."""
    _patch(monkeypatch, _client_with_lists([_FOREIGN_LIST]), onchain=_onchain_truth)
    monkeypatch.setattr(checker, "RAW_LOG_ENABLED", False)
    res = checker.check_wallet("0xabc", _PM())
    assert res["status"] == "UNVERIFIED"
