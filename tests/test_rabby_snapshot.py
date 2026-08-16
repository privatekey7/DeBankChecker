"""Тесты сборки снапшота баланса через Rabby (_fetch_snapshot).

Мокаем ТОЛЬКО внешний HTTP-клиент (RabbyClient) — граница системы. Логика
маппинга ответов в result-dict тестируется как есть.
"""

import debank_checker.checker as checker


class FakeRabbyClient:
    """Заглушка Rabby API с заранее заданными ответами эндпоинтов."""

    def __init__(self, proxy: str):
        self.proxy = proxy

    def get_total_balance(self, address: str, is_core: bool = True) -> dict:
        return {
            "total_usd_value": 380.0,
            "chain_list": [
                {"id": "eth", "usd_value": 371.32},
                {"id": "arb", "usd_value": 8.68},
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
        """Фолбэк: по-сетевой список (используется при сбое кэш-эндпоинта)."""
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
                "name": "Hyperliquid", "logo_url": "l",
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


def _patch(monkeypatch):
    monkeypatch.setattr(checker, "RabbyClient", FakeRabbyClient)


def test_total_usd_comes_from_authoritative_total_balance(monkeypatch):
    """total_usd берётся из total_usd_value напрямую, НЕ суммируется вручную."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert snap["total_usd"] == 380.0


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
    """DeFi-протокол попадает в protocols_data, chain берётся из токена позиции."""
    _patch(monkeypatch)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert len(snap["protocols_data"]) == 1
    proto = snap["protocols_data"][0]
    assert proto["name"] == "Hyperliquid"
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
        "nft_data", "proxy", "status", "error",
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

    monkeypatch.setattr(checker, "RabbyClient", EmptyClient)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert snap["total_usd"] == 0.0
    assert snap["tokens_data"] == []
    assert snap["status"] == "OK"


def test_token_fallback_on_cache_failure(monkeypatch):
    """Сбой cache_token_list → токены собираются по-сетевым token_list."""
    class CacheBrokenClient(FakeRabbyClient):
        def get_cache_token_list(self, address):
            raise RuntimeError("HTTP Error 429: ")

    monkeypatch.setattr(checker, "RabbyClient", CacheBrokenClient)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    symbols = {t["symbol"] for t in snap["tokens_data"]}
    assert symbols == {"USDC", "ETH"}


def test_wallet_tokens_fetched_by_single_cache_request(monkeypatch):
    """Основной путь: токены всех сетей берутся из cache_token_list."""
    calls = {"cache": 0, "per_chain": 0}

    class CountingClient(FakeRabbyClient):
        def get_cache_token_list(self, address):
            calls["cache"] += 1
            return super().get_cache_token_list(address)

        def get_token_list(self, address, chain_id, is_all=False):
            calls["per_chain"] += 1
            return super().get_token_list(address, chain_id, is_all)

    monkeypatch.setattr(checker, "RabbyClient", CountingClient)
    snap = checker._fetch_snapshot("0xabc", "http://proxy")
    assert calls == {"cache": 1, "per_chain": 0}
    assert len(snap["tokens_data"]) == 2
