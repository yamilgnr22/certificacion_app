"""
report_utils.py

Construcción y guardado de reportes JSON de validación.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, date
from typing import Any, Dict, Optional
from pathlib import Path
import pandas as pd


def build_report(
    *,
    v_er: Optional[Dict[str, Any]] = None,
    v_esf: Optional[Dict[str, Any]] = None,
    v_docs: Optional[Dict[str, Any]] = None,
    v_llm: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "validations": {
            "er": v_er or {},
            "esf": v_esf or {},
            "documents": v_docs or {},
            "llm": v_llm or {},
        },
    }
    if meta:
        report["meta"] = meta
    return report


def _sanitize(obj: Any) -> Any:
    # Convierte objetos no-JSON (Path, Timestamp, datetime) a str
    if isinstance(obj, (Path,)):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def save_report_json(report: Dict[str, Any], output_docx_path: str | Path) -> str:
    base, _ = os.path.splitext(output_docx_path)
    out_path = base + ".validation.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(_sanitize(report), f, ensure_ascii=False, indent=2)
    return out_path
