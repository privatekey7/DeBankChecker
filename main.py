"""
DeBank Checker — точка входа (балансы через Rabby API)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from debank_checker import __version__
from debank_checker.checker import check_wallet, pending_nfts, wait_nfts
from debank_checker.config import (
    ALLOW_DIRECT,
    DEBUG,
    DIRECT_WORKERS,
    MAX_WORKERS,
    OUTPUT_DIR,
    PROXY_MULTIPLIER,
    WALLETS_FILE,
)
from debank_checker.export.csv_exporter import export_to_csv
from debank_checker.export.excel import export_to_excel
from debank_checker.export.json_exporter import export_to_json
from debank_checker.proxy.manager import ProxyManager, drop_dead_proxies, load_proxies
from debank_checker.ui.banner import create_progress_bar, show
from debank_checker.ui.logger import error, info, success, warning
from debank_checker.ui.menu import ask_continue, ask_format, show_menu


def setup_encoding() -> None:
    """UTF-8 и colorama для Windows."""
    if sys.platform == "win32":
        try:
            import colorama
            colorama.init()
        except ImportError:
            pass
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_wallets(path: str | Path = WALLETS_FILE) -> list[str]:
    """Читает адреса из файла (пустые строки и # — пропускаются)."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def worker_count(wallets: int, proxies: int) -> int:
    """min(MAX_WORKERS, кошельков, прокси × PROXY_MULTIPLIER); без прокси — DIRECT_WORKERS."""
    return max(1, min(MAX_WORKERS, wallets, proxies * PROXY_MULTIPLIER if proxies else DIRECT_WORKERS))


def check_all(wallets: list[str], proxy_manager: ProxyManager, workers: int,
              on_done=None) -> list[dict]:
    """Проверяет все кошельки параллельно; on_done(done, total) — после каждого."""
    results: list[dict] = [None] * len(wallets)  # type: ignore[list-item]
    completed = [0]
    lock = threading.Lock()

    def process(idx: int, address: str) -> None:
        start = time.perf_counter()
        row = check_wallet(address, proxy_manager, wallet_idx=idx)
        results[idx] = row
        if DEBUG:
            sys.stderr.write(f"[DEBUG] #{idx} DONE | {time.perf_counter() - start:.2f}s total"
                             f" | status={row['status']}\n")
            sys.stderr.flush()
        with lock:
            completed[0] += 1
            if on_done:
                on_done(completed[0], len(wallets))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for fut in [executor.submit(process, i, w) for i, w in enumerate(wallets)]:
            fut.result()
    return results


def ensure_nfts() -> None:
    """Дождаться фоновой загрузки NFT (нужны меню NFT и экспорту)."""
    left = pending_nfts()
    if left:
        info(f"Догружаю NFT: осталось {left} кошельков...")
        if wait_nfts(timeout=120):
            warning(f"NFT не догружены у {pending_nfts()} кошельков — в экспорте их NFT будут пустыми")


def main() -> None:
    setup_encoding()
    show()

    wallets = load_wallets()
    proxies = load_proxies()

    if not wallets:
        error("Нет кошельков для проверки. Добавь адреса в wallets.txt")
        sys.exit(1)

    if not proxies and not ALLOW_DIRECT:
        error("Прокси обязательны. Добавь прокси в proxy.txt (или DBC_DIRECT=1 для прямого режима)")
        sys.exit(1)

    if proxies:
        total_proxies = len(proxies)
        proxies, _ = drop_dead_proxies(proxies)  # мёртвые исключаются молча
        if not proxies:
            error("Ни один прокси не работает — проверь proxy.txt")
            sys.exit(1)
    info(f"v{__version__}  |  Кошельков: {len(wallets)}  |  "
         + (f"Рабочие прокси: {len(proxies)} из {total_proxies}" if proxies else "Прокси: нет (прямой режим)"))
    workers = worker_count(len(wallets), len(proxies))
    info(f"Параллельных воркеров: {workers}")

    started = time.perf_counter()
    print("\r" + create_progress_bar(0, len(wallets)), end="", flush=True)
    results = check_all(wallets, ProxyManager(proxies), workers,
                        on_done=lambda done, total: print(f"\r{create_progress_bar(done, total)}",
                                                          end="", flush=True))
    print()

    ok_count = sum(1 for r in results if r["status"] == "OK")
    unverified = sum(1 for r in results if r["status"] == "UNVERIFIED")
    total_sum = sum(r["total_usd"] for r in results if r["status"] == "OK")

    info(f"Итого: {ok_count}/{len(results)} подтверждено  |  Суммарный баланс: ${total_sum:,.2f}"
         f"  |  {time.perf_counter() - started:.1f} с")
    if unverified:
        info(f"Не подтверждено (UNVERIFIED, в сумму не входят): {unverified}")

    output_path = Path(OUTPUT_DIR)
    output_path.mkdir(parents=True, exist_ok=True)

    while True:
        export_config = show_menu(results, ensure_nfts=ensure_nfts)
        fmt = ask_format()
        if export_config.nft:  # без NFT в экспорте ждать их догрузки не нужно
            ensure_nfts()

        if fmt == "csv":
            out_path = export_to_csv(results, output_path, config=export_config)
            success(f"CSV сохранён: {out_path.resolve()}")
        elif fmt == "json":
            out_path = export_to_json(results, output_path, config=export_config)
            success(f"JSON сохранён: {out_path.resolve()}")
        else:
            out_path = export_to_excel(results, output_path, config=export_config)
            success(f"Excel сохранён: {out_path.resolve()}")

        if not ask_continue():
            break

    info("Готово.")


def _exit(code: int) -> None:
    """Немедленный выход. sys.exit ждал бы фоновые потоки (догрузка NFT,
    запасные запросы к зависшим выборкам) — Ctrl+C «зависал» бы до их конца."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        info("Прервано пользователем")
        _exit(0)
    except Exception as e:
        error(f"Критическая ошибка: {e}")
        _exit(1)
    _exit(0)
