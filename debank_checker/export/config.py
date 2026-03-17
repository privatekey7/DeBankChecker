"""
Конфигурация экспорта — что включать в результат
"""

from dataclasses import dataclass


@dataclass
class ExportConfig:
    """Настройки экспорта по выбору пользователя в меню."""

    # 1 — только общий баланс
    total_only: bool = False
    # 2 — Summary по кошелькам
    summary: bool = True
    # 3 — конкретный токен (symbol:chain)
    token_filter: str | None = None  # "ETH:eth" или None
    # 4 — все токены
    tokens: bool = True
    # 5 — все NFT
    nft: bool = True
    # 6 — конкретный NFT (collection:chain)
    nft_filter: str | None = None  # "Bored Ape:eth" или None
    # 7 — DeFi-позиции (name:chain или :chain для всех в сети)
    protocol_filter: str | None = None  # "Uniswap:eth" или ":eth"
    protocols: bool = True

    @classmethod
    def from_menu_choice(
        cls,
        choice: int,
        token_filter: str | None = None,
        nft_filter: str | None = None,
        protocol_filter: str | None = None,
    ) -> "ExportConfig":
        """Создать конфиг по номеру пункта меню (1–11)."""
        configs = {
            1: cls(summary=True, tokens=False, nft=False, protocols=False),
            2: cls(summary=False, token_filter=token_filter or None, tokens=True, nft=False, protocols=False),
            3: cls(summary=False, tokens=True, nft=False, protocols=False),
            4: cls(summary=False, token_filter=token_filter or None, tokens=True, nft=False, protocols=False),
            5: cls(summary=False, tokens=False, nft=True, protocols=False),
            6: cls(summary=False, tokens=False, nft_filter=nft_filter or None, nft=True, protocols=False),
            7: cls(summary=False, tokens=False, nft_filter=nft_filter or None, nft=True, protocols=False),
            8: cls(summary=False, tokens=False, nft=False, protocol_filter=None, protocols=True),
            9: cls(summary=False, tokens=False, nft=False, protocol_filter=protocol_filter, protocols=True),
            10: cls(summary=False, tokens=False, nft=False, protocol_filter=protocol_filter or None, protocols=True),
            11: cls(summary=False, tokens=True, nft=True, protocols=True),
        }
        return configs.get(choice, configs[11])

    def get_filename_suffix(self) -> str:
        """Суффикс для имени файла: Total, Summary, Tokens, NFT, DeFi или комбинация."""
        if self.total_only:
            return "Total"
        if self.summary and not any([self.tokens, self.protocols, self.nft]):
            return "Summary"
        parts: list[str] = []
        if self.tokens:
            parts.append("Tokens")
        if self.protocols:
            parts.append("DeFi")
        if self.nft:
            parts.append("NFT")
        return "_".join(parts) if parts else "Summary"
