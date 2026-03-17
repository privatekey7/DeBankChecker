"""
Консольное меню выбора экспорта.
Вызывается после проверки кошельков — данные уже получены.
Двухшаговый выбор: сначала сеть, затем токен/NFT/протокол в этой сети.
"""

import questionary

from debank_checker.export.config import ExportConfig

# Разделитель для фильтра (symbol:chain)
_SEP = ":"
_ALL_IN_CHAIN = "← Все в этой сети"


def _get_chains_from_tokens(results: list[dict] | None) -> list[str]:
    """Уникальные сети из токенов."""
    chains: set[str] = set()
    if not results:
        return []
    for r in results:
        if r.get("status") != "OK":
            continue
        for t in r.get("tokens_data", []):
            ch = (t.get("chain") or "").strip().lower()
            if ch:
                chains.add(ch)
    return sorted(chains)


def _get_chains_from_nft(results: list[dict] | None) -> list[str]:
    """Уникальные сети из NFT."""
    chains: set[str] = set()
    if not results:
        return []
    for r in results:
        if r.get("status") != "OK":
            continue
        for n in r.get("nft_data", []):
            ch = (n.get("chain") or "").strip().lower()
            if ch:
                chains.add(ch)
    return sorted(chains)


def _get_chains_from_protocols(results: list[dict] | None) -> list[str]:
    """Уникальные сети из DeFi-протоколов."""
    chains: set[str] = set()
    if not results:
        return []
    for r in results:
        if r.get("status") != "OK":
            continue
        for p in r.get("protocols_data", []):
            ch = (p.get("chain") or "").strip().lower()
            if ch:
                chains.add(ch)
    return sorted(chains)


def _build_token_options_for_chain(results: list[dict] | None, chain: str) -> list[str]:
    """Токены в указанной сети: SYMBOL (из реальных данных)."""
    seen: set[str] = set()
    options: list[str] = []
    if not results:
        return options
    chain_l = chain.lower()
    for r in results:
        if r.get("status") != "OK":
            continue
        for t in r.get("tokens_data", []):
            if (t.get("chain") or "").lower() != chain_l:
                continue
            symbol = (t.get("symbol") or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                options.append(symbol)
    return sorted(options)


def _build_nft_options_for_chain(results: list[dict] | None, chain: str) -> list[str]:
    """NFT-коллекции в указанной сети (из реальных данных)."""
    seen: set[str] = set()
    options: list[str] = []
    if not results:
        return options
    chain_l = chain.lower()
    for r in results:
        if r.get("status") != "OK":
            continue
        for n in r.get("nft_data", []):
            if (n.get("chain") or "").lower() != chain_l:
                continue
            name = (n.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                options.append(name)
    return sorted(options)


def _build_protocol_options_for_chain(results: list[dict] | None, chain: str) -> list[str]:
    """DeFi-протоколы в указанной сети (из реальных данных)."""
    seen: set[str] = set()
    options: list[str] = []
    if not results:
        return options
    chain_l = chain.lower()
    for r in results:
        if r.get("status") != "OK":
            continue
        for p in r.get("protocols_data", []):
            if (p.get("chain") or "").lower() != chain_l:
                continue
            name = (p.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                options.append(name)
    return sorted(options)


def _select(title: str, options: list[str]) -> str | None:
    """Выбор из списка с помощью questionary."""
    if not options:
        return None
    result = questionary.select(title, choices=options).ask()
    return result


FORMAT_OPTIONS = [
    "CSV",
    "Excel",
    "JSON",
]

AGAIN_OPTIONS = [
    "1. Продолжить",
    "2. Выйти",
]

MAIN_OPTIONS = [
    "1. Summary (адрес, баланс, chains, статус)",
    "2. Конкретный токен (сеть → токен)",
    "3. Все токены",
    "4. Все токены в конкретной сети",
    "5. Все NFT",
    "6. Конкретный NFT (сеть → коллекция)",
    "7. Все NFT в конкретной сети",
    "8. Все DeFi-позиции",
    "9. Конкретный DeFi (сеть → протокол)",
    "10. Все DeFi в конкретной сети",
    "11. Всё (Tokens + Protocols + NFT)",
]


def show_menu(results: list[dict] | None = None) -> ExportConfig:
    """
    Интерактивное меню: двухшаговый выбор.
    Шаг 1: выбор сети. Шаг 2: выбор токена/NFT/протокола в этой сети.
    """
    choice_str = _select("Что экспортировать? (↑↓ — выбор, Enter — подтвердить)", MAIN_OPTIONS)
    if not choice_str:
        choice_str = MAIN_OPTIONS[0]
    choice = int(choice_str.split(".")[0].strip())

    token_filter = None
    nft_filter = None
    protocol_filter = None

    if choice == 2:
        chains = _get_chains_from_tokens(results)
        if not chains:
            print("  Нет токенов в данных. Экспорт всех токенов (п.3).")
            choice = 3
        else:
            chain = _select("Шаг 1: Выберите сеть", chains)
            if not chain:
                choice = 3
            else:
                tokens = _build_token_options_for_chain(results, chain)
                if not tokens:
                    print("  Нет токенов в этой сети. Экспорт всех токенов (п.3).")
                    choice = 3
                else:
                    symbol = _select("Шаг 2: Выберите токен", tokens)
                    if symbol:
                        token_filter = f"{symbol}{_SEP}{chain}"

    elif choice == 4:
        chains = _get_chains_from_tokens(results)
        if not chains:
            print("  Нет токенов в данных. Экспорт всех токенов (п.3).")
            choice = 3
        else:
            chain = _select("Выберите сеть", chains)
            if chain:
                token_filter = f"{_SEP}{chain}"

    elif choice == 6:
        chains = _get_chains_from_nft(results)
        if not chains:
            print("  Нет NFT в данных. Экспорт всех NFT (п.5).")
            choice = 5
        else:
            chain = _select("Шаг 1: Выберите сеть", chains)
            if not chain:
                choice = 5
            else:
                nfts = _build_nft_options_for_chain(results, chain)
                if not nfts:
                    print("  Нет NFT в этой сети. Экспорт всех NFT (п.5).")
                    choice = 5
                else:
                    collection = _select("Шаг 2: Выберите NFT-коллекцию", nfts)
                    if collection:
                        nft_filter = f"{collection}{_SEP}{chain}"

    elif choice == 7:
        chains = _get_chains_from_nft(results)
        if not chains:
            print("  Нет NFT в данных. Экспорт всех NFT (п.5).")
            choice = 5
        else:
            chain = _select("Выберите сеть", chains)
            if chain:
                nft_filter = f"{_SEP}{chain}"

    elif choice == 9:
        chains = _get_chains_from_protocols(results)
        if not chains:
            print("  Нет DeFi-позиций в данных. Экспорт всех DeFi (п.8).")
            choice = 8
        else:
            chain = _select("Шаг 1: Выберите сеть", chains)
            if not chain:
                choice = 8
            else:
                protocols = _build_protocol_options_for_chain(results, chain)
                if not protocols:
                    print("  Нет протоколов в этой сети. Экспорт всех DeFi (п.8).")
                    choice = 8
                else:
                    options = [_ALL_IN_CHAIN] + protocols
                    selected = _select("Шаг 2: Выберите протокол или «Все в этой сети»", options)
                    if selected == _ALL_IN_CHAIN:
                        protocol_filter = f"{_SEP}{chain}"
                    elif selected:
                        protocol_filter = f"{selected}{_SEP}{chain}"

    elif choice == 10:
        chains = _get_chains_from_protocols(results)
        if not chains:
            print("  Нет DeFi-позиций в данных. Экспорт всех DeFi (п.8).")
            choice = 8
        else:
            chain = _select("Выберите сеть", chains)
            if chain:
                protocol_filter = f"{_SEP}{chain}"

    return ExportConfig.from_menu_choice(choice, token_filter, nft_filter, protocol_filter)


def ask_format() -> str:
    """Выбор формата экспорта: csv, excel, json."""
    choice = _select("В какой формат экспортировать?", FORMAT_OPTIONS)
    if not choice:
        return "excel"
    return choice.lower()


def ask_continue() -> bool:
    """Спросить: продолжить или выйти. True = продолжить."""
    choice = _select("Продолжить?", AGAIN_OPTIONS)
    return choice == AGAIN_OPTIONS[0] if choice else False
