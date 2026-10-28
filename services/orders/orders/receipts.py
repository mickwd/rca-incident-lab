"""Comprobantes de órdenes escritos en disco (volumen de uploads)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def write_receipt(base_dir: str, payload: dict) -> Path:
    """Agrega el comprobante al archivo JSONL del día.

    Si el volumen está lleno ``write`` levanta ``OSError`` con errno ENOSPC y
    la orden no se confirma (se hace rollback en el handler).
    """
    folder = Path(base_dir) / "receipts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")
        fh.flush()
    return path
