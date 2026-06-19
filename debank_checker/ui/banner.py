"""
ASCII-заставка в стиле Season7
"""

import sys

# ANSI цвета
RESET = "\033[0m"
BRIGHT = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
GREEN = "\033[32m"

_BANNER_INDENT = "    "
_BANNER_LINES = [
    "██████╗ ███████╗██████╗  █████╗ ███╗   ██╗██╗  ██╗",
    "██╔══██╗██╔════╝██╔══██╗██╔══██╗████╗  ██║██║ ██╔╝",
    "██║  ██║█████╗  ██████╔╝███████║██╔██╗ ██║█████╔╝",
    "██║  ██║██╔══╝  ██╔══██╗██╔══██║██║╚██╗██║██╔═██╗",
    "██████╔╝███████╗██████╔╝██║  ██║██║ ╚████║██║  ██╗",
    "╚═════╝ ╚══════╝╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝",
]
_BANNER_WIDTH = max(len(l) for l in _BANNER_LINES)


def _center_text_in_banner(s: str) -> str:
    """Центрировать строку относительно ширины ASCII-баннера (не экрана)."""
    s = s.rstrip()
    pad = max(0, (_BANNER_WIDTH - len(s)) // 2)
    return f"{_BANNER_INDENT}{' ' * pad}{s}"


def show() -> None:
    """Показать главную заставку."""
    if sys.platform == "win32":
        try:
            import os
            os.system("cls")
        except Exception:
            pass
    else:
        print("\033[2J\033[H", end="")

    print(create_main_banner())
    print(create_subtitle())
    print(create_separator())
    print()


def create_main_banner() -> str:
    """Главный баннер DeBank Checker."""
    banner = "\n".join(f"{_BANNER_INDENT}{l}" for l in _BANNER_LINES)
    return f"\n{CYAN}{BRIGHT}{banner}{RESET}"


def create_subtitle() -> str:
    """Подзаголовок."""
    return "\n".join(
        [
            _center_text_in_banner(f"{YELLOW}{BRIGHT}BALANCE CHECKER v1.1.0{RESET}"),
            _center_text_in_banner(f"{DIM}EVM · Tokens · DeFi · NFT{RESET}"),
            _center_text_in_banner(f"{DIM}TG: https://t.me/privatekey_ai{RESET}"),
        ]
    )


def create_separator() -> str:
    """Разделитель."""
    return f"{_BANNER_INDENT}{BLUE}{'=' * _BANNER_WIDTH}{RESET}"


def create_progress_bar(current: int, total: int, width: int = 50) -> str:
    """Прогресс-бар в стиле Season7."""
    if total <= 0:
        pct = 0
        filled = 0
        empty = width
    else:
        pct = round((current / total) * 100)
        filled = round((current / total) * width)
        empty = width - filled
    bar = f"{GREEN}{'█' * filled}{DIM}{'░' * empty}{RESET}"
    return f"[{bar}] {pct}% ({current}/{total})"
