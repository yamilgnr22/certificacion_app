import json
import sys
from pathlib import Path


def load_nb(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_nb(path: Path, nb: dict) -> None:
    tmp = path.with_suffix(".ipynb.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)
        f.write("\n")
    tmp.replace(path)


def cell_text(cell: dict) -> str:
    src = cell.get("source") or []
    if isinstance(src, list):
        text = "".join(src)
    else:
        text = str(src)
    return text


def priority_for_cell(cell: dict) -> int:
    """Assign a sort priority based on heuristic markers.
    Lower number -> earlier in the notebook.
    """
    t = cell_text(cell).lower()
    if cell.get("cell_type") == "markdown":
        if "# flujo de valid" in t:
            return 0
        if "llm_vision_extract" in t or "vision_extract" in t or "llm visi" in t:
            return 63
        if "debug_ocr_cedula" in t:
            return 61
    # Code cells
    # Prefer explicit id mapping where available
    cid = str(cell.get("id") or "")
    explicit = {
        # Config / environment
        "b215222e": 10,  # config with paths
        "48437a0e": 15,  # from pathlib import Path (stray import)
        "6e775dec": 20,  # imports + env
        "2a199423": 45,  # import pandas as pd
        # Data load & extraction
        "0483ee21": 30,  # cargar excel
        "6d745b0f": 40,  # extraer campos certificacion
        # Deterministic validations
        "624c5d58": 50,  # validacion ER/ESF
        "6f191e99": 50,  # res_esf validate call (keep right after)
        # Docs and OCR
        "97e3ab11": 60,  # validacion cedula
        # LLM
        "4828bed5": 70,  # validacion LLM
        # Output
        "edebde92": 80,  # generar documento
        "6734db96": 90,  # guardar reporte
        # Misc / placeholders
        "702a8694": 65,  # matricula placeholder
        "c3c238e0": 62,  # debug OCR cedula (code)
        "f8b3e54d": 61,  # debug OCR cedula (markdown)
    }
    if cid in explicit:
        return explicit[cid]
    if "excel_path" in t and "output_dir" in t:
        return 10  # config
    if "imports y prepar" in t or "load_dotenv" in t:
        return 20
    if "import pandas as pd" in t:
        return 25
    if "cargar excel" in t:
        return 30
    if "extraer campos" in t and "certificacion" in t:
        return 40
    if "validaci" in t and ("er" in t or "esf" in t):
        return 50
    if "validate_esf(" in t or "validate_er(" in t:
        return 50
    # Validación de cédula (evitar que cualquier uso de la palabra 'cedula' active esta rama)
    if "validaci" in t and ("c\u00e9dula" in t or "cedula" in t):
        return 60
    if "debug: inspecci" in t and "ocr" in t and "cedula" in t:
        return 62
    # LLM Visión (celdas de import, ejecución y normalización)
    if "from llm_vision" in t or "extract_cedula_with_vision" in t or "campos normalizados" in t or "fields = (vision" in t:
        return 64
    if "matr" in t and "validaci" in t:
        return 65
    if "validaci" in t and "llm" in t:
        return 70
    if "generar el documento" in t or "documento" in t and "docx" in t:
        return 80
    if "guardar reporte" in t or "save_report_json" in t:
        return 90
    # Fallback: keep relative order at the end
    return 1000


def reorder_cells(nb: dict) -> dict:
    cells = nb.get("cells", [])
    # Stable sort: include original index as tie-breaker
    indexed = list(enumerate(cells))
    sorted_cells = [c for _, c in sorted(indexed, key=lambda ic: (priority_for_cell(ic[1]), ic[0]))]
    nb2 = dict(nb)
    nb2["cells"] = sorted_cells
    return nb2


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/reorder_notebook.py <path-to-ipynb>")
        return 2
    p = Path(sys.argv[1]).resolve()
    if not p.exists():
        print(f"File not found: {p}")
        return 2
    nb = load_nb(p)
    nb2 = reorder_cells(nb)
    # Backup
    bak = p.with_suffix(".ipynb.bak")
    bak.write_text(json.dumps(nb, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    save_nb(p, nb2)
    print(f"Reordered: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
