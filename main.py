"""
DeBank Balance Checker — точка входа
"""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from debank_checker.checker import check_wallet
from debank_checker.config import (
    DEBUG,
    MAX_WORKERS,
    OUTPUT_DIR,
    PROXIES_FILE,
    PROXY_MULTIPLIER,
    WALLETS_FILE,
)
from debank_checker.export.csv_exporter import export_to_csv
from debank_checker.export.excel import export_to_excel
from debank_checker.export.json_exporter import export_to_json
from debank_checker.proxy.manager import load_proxies, ProxyManager
from debank_checker.ui.banner import create_progress_bar, show
from debank_checker.ui.logger import error, info, success
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
    """Читает адреса из файла."""
    path = Path(path)
    wallets = []
    if not path.exists():
        return wallets
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                wallets.append(line)
    return wallets


def main() -> None:
    setup_encoding()
    show()

    wallets = load_wallets()
    proxies = load_proxies()

    if not wallets:
        error("Нет кошельков для проверки. Добавь адреса в wallets.txt")
        sys.exit(1)

    if not proxies:
        error("Прокси обязательны. Добавь прокси в proxy.txt")
        sys.exit(1)

    info(f"Кошельков: {len(wallets)}  |  Прокси: {len(proxies)}")

    max_workers = min(MAX_WORKERS, len(wallets), len(proxies) * PROXY_MULTIPLIER)
    max_workers = max(1, max_workers)
    info(f"Параллельных воркеров: {max_workers}")

    proxy_manager = ProxyManager(proxies)
    results: list[dict] = [None] * len(wallets)
    completed = [0]
    lock = threading.Lock()

    def process(idx: int, address: str) -> None:
        start = time.perf_counter()
        row = check_wallet(address, proxy_manager, wallet_idx=idx)
        results[idx] = row
        if DEBUG:
            elapsed = time.perf_counter() - start
            sys.stderr.write(f"[DEBUG] #{idx} DONE | {elapsed:.2f}s total | status={row['status']}\n")
            sys.stderr.flush()
        with lock:
            completed[0] += 1
            done = completed[0]
            total = len(wallets)
            bar = create_progress_bar(done, total)
            print(f"\r{bar}", end="", flush=True)

    print("\r" + create_progress_bar(0, len(wallets)), end="", flush=True)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process, i, w): i
            for i, w in enumerate(wallets)
        }
        for future in as_completed(futures):
            future.result()

    print()

    ok_count = sum(1 for r in results if r and r["status"] == "OK")
    total_sum = sum(r["total_usd"] for r in results if r and r["status"] == "OK")

    info(f"Итого: {ok_count}/{len(results)} успешно  |  Суммарный баланс: ${total_sum:,.2f}")

    output_path = Path(OUTPUT_DIR)
    output_path.mkdir(parents=True, exist_ok=True)

    while True:
        export_config = show_menu(results)
        fmt = ask_format()

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        info("Прервано пользователем")
        sys.exit(0)
    except Exception as e:
        error(f"Критическая ошибка: {e}")
        sys.exit(1)
