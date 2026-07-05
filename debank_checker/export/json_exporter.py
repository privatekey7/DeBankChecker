"""
Экспорт результатов в JSON
"""

import datetime
import json
from pathlib import Path

from debank_checker.export.common import build_export_data
from debank_checker.export.config import ExportConfig


def _json_serial(obj):
    """Сериализация для JSON (float, etc)."""
    if isinstance(obj, float):
        return round(obj, 6) if obj == obj else None  # NaN check
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def export_to_json(
    results: list[dict],
    output_dir: Path | str = ".",
    config: ExportConfig | None = None,
) -> Path:
    """
    Экспорт в JSON по ExportConfig.
    Один файл со структурой { total, summary, tokens, protocols, nft }.
    """
    config = config or ExportConfig(summary=True, tokens=True, nft=True, protocols=True)
    output_dir = Path(output_dir)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = config.get_filename_suffix()
    filepath = output_dir / f"debank_{suffix}_{ts}.json"

    data = build_export_data(results, config)
    out = {
        "exported_at": datetime.datetime.now().isoformat(),
        "total": data["total"],
        "summary": data["summary"],
        "tokens": data["tokens"],
        "protocols": data["protocols"],
        "nft": data["nft"],
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=_json_serial)

    return filepath
