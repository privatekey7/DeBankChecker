"""
Экспорт результатов в CSV
"""

import csv
import datetime
from pathlib import Path

from debank_checker.export.common import build_export_data
from debank_checker.export.config import ExportConfig


def export_to_csv(
    results: list[dict],
    output_dir: Path | str = ".",
    config: ExportConfig | None = None,
) -> Path:
    """
    Экспорт в CSV по ExportConfig.
    Создаёт один или несколько CSV-файлов в зависимости от config.
    """
    config = config or ExportConfig(summary=True, tokens=True, nft=True, protocols=True)
    output_dir = Path(output_dir)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = config.get_filename_suffix()
    base = f"debank_{suffix}_{ts}"

    data = build_export_data(results, config)
    written: list[Path] = []

    if config.total_only:
        path = output_dir / f"{base}.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Параметр", "Значение"])
            w.writerow(["Суммарный баланс", data["total"]["sum_usd"]])
            w.writerow(["Успешно", f"{data['total']['ok_count']}/{data['total']['total_count']}"])
        written.append(path)
    else:
        if data["summary"]:
            path = output_dir / f"{base}_summary.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["n", "address", "total_usd", "chains", "status"])
                w.writeheader()
                w.writerows(data["summary"])
            written.append(path)
        if data["tokens"]:
            path = output_dir / f"{base}_tokens.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["address", "symbol", "chain", "amount", "price", "value"])
                w.writeheader()
                w.writerows(data["tokens"])
            written.append(path)
        if data["protocols"]:
            path = output_dir / f"{base}_protocols.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["address", "name", "chain", "value"])
                w.writeheader()
                w.writerows(data["protocols"])
            written.append(path)
        if data["nft"]:
            path = output_dir / f"{base}_nft.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["address", "collection", "chain", "amount"])
                w.writeheader()
                w.writerows(data["nft"])
            written.append(path)

    return written[0] if written else output_dir / f"{base}.csv"
