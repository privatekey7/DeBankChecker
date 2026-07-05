"""
Логирование в формате: YYYY-MM-DD HH:MM:SS | LEVEL | Сообщение
Цвета: INFO — белый, WARNING — оранжевый, SUCCESS — зелёный, ERROR — красный
"""

import sys
from datetime import datetime

# ANSI коды
RESET = "\033[0m"
WHITE = "\033[37m"
BRIGHT = "\033[1m"
ORANGE = "\033[38;5;208m"  # 256-color orange
GREEN = "\033[32m"
RED = "\033[31m"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(level: str, msg: str, color: str) -> None:
    line = f"{_timestamp()} | {level:<7} | {msg}"
    print(f"{color}{line}{RESET}", file=sys.stderr, flush=True)


def info(msg: str) -> None:
    _log("INFO", msg, WHITE + BRIGHT)


def warning(msg: str) -> None:
    _log("WARNING", msg, ORANGE + BRIGHT)


def success(msg: str) -> None:
    _log("SUCCESS", msg, GREEN + BRIGHT)


def error(msg: str) -> None:
    _log("ERROR", msg, RED + BRIGHT)
