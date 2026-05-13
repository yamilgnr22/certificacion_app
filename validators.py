"""
validators.py

Valida la consistencia aritmética de:
 - Estado de Resultados (ER)
 - Estado de Situación Financiera (ESF)

Retorna un dict con "ok" y listas de "errors" y "checks".
Las funciones son tolerantes a pequeñas diferencias por redondeo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import unicodedata
import re


def _norm(s: Any) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = " ".join(s.split())
    return s


def _num(x) -> float:
    try:
        n = pd.to_numeric(x, errors="coerce")
        return 0.0 if pd.isna(n) else float(n)
    except Exception:
        return 0.0


def _within(a: float, b: float, tol: float) -> bool:
    return abs((a or 0.0) - (b or 0.0)) <= tol


# ---------------------------------------------------------------------------
# ER VALIDATOR
# ---------------------------------------------------------------------------
def validate_er(df_er: pd.DataFrame, *, tolerance: float = 1.0) -> Dict[str, Any]:
    """
    Reglas básicas (si existen las filas correspondientes):
      - Total gastos operativos = suma del detalle entre las marcas
      - Utilidad (operativa o neta) = Ingresos Brutos - Total gastos operativos

    Nota: Si las filas ancla no existen, la regla se omite.
    """
    checks: List[Dict[str, Any]] = []

    if df_er is None or df_er.empty:
        return {"ok": False, "errors": ["Hoja ER vacía"], "checks": checks}

    # Replicar el preprocesamiento del generador para alinear columnas/filas
    try:
        df = (
            df_er.copy()
                 .drop(index=[0, 1, 2, 3, 4])
                 .reset_index(drop=True)
                 .drop(columns=[df_er.columns[1]])
        )
    except Exception:
        df = df_er.copy()

    df = df.fillna(0)
    # La primera columna es la descripción; el resto son valores (meses)
    if df.shape[1] <= 1:
        return {"ok": False, "errors": ["ER sin columnas numéricas"], "checks": checks}

    desc_col = df.columns[0]
    val_cols = list(df.columns[1:])

    # localizar filas ancla por etiqueta normalizada
    def find_row_idx(labels: List[str], *, in_desc: bool = True) -> Optional[int]:
        """
        Busca la primera coincidencia respetando la prioridad del listado
        de etiquetas. Evita usar set() para no perder el orden de
        especificidad (p.ej., '(=) ingresos brutos' antes que 'ingresos').
        """
        ordered = [_norm(x) for x in labels]
        for wanted in ordered:
            for i, row in df.iterrows():
                key = _norm(row[desc_col])
                if key == wanted:
                    return i
        return None

    idx_ing_brutos = find_row_idx(["(=) ingresos brutos", "ingresos brutos", "ingresos"])

    # Bloque COSTOS
    idx_cost_start = find_row_idx([
        "(-) costos", "costos", "costo de ventas", "costos de ventas",
        "costo de mercaderia vendida", "costo de mercancía vendida",
        "costo de ventas y servicios",
    ])
    idx_cost_total = find_row_idx([
        "total costos", "total costo", "total costo de ventas", "total costos de ventas",
    ])
    idx_util_bruta = find_row_idx(["utilidad bruta", "margen bruto", "resultado bruto"])  # si existe

    # Bloque GASTOS OPERATIVOS
    idx_gop_start  = find_row_idx(["(-) gastos operativos", "gastos operativos"])
    idx_gop_total  = find_row_idx(["total gastos operativos"])

    # Líneas de resultado
    idx_util_oper  = find_row_idx(["utilidad operativa", "resultado operativo"])
    idx_util_neta  = find_row_idx(["ingresos/utilidad neta", "utilidad neta", "resultado neto"])

    errors: List[str] = []
    err_cols: Dict[str, List[str]] = {}

    def _col_label(col: Any) -> str:
        label = str(col)
        return label if label and label.lower() != "nan" else str(col)

    def _add_err(msg: str, col_label: str) -> None:
        err_cols.setdefault(msg, []).append(col_label)

    def _finalize_errors() -> None:
        for msg, cols in err_cols.items():
            uniq: List[str] = []
            for c in cols:
                if c not in uniq:
                    uniq.append(c)
            suffix = f" [mes {', '.join(uniq)}]" if uniq else ""
            errors.append(f"{msg}{suffix}")

    # 1) Validar TOTAL COSTOS = suma del detalle (si existe bloque costos)
    if idx_cost_start is not None and idx_cost_total is not None and idx_cost_total > idx_cost_start:
        detail_c = df.iloc[idx_cost_start + 1: idx_cost_total]
        for c in val_cols:
            s = float(detail_c[c].apply(_num).sum())
            total = float(_num(df.iloc[idx_cost_total][c]))
            passed = _within(s, total, tolerance)
            checks.append({
                "rule": "ER: Total costos = suma detalle",
                "column": str(c),
                "expected": total,
                "computed": s,
                "ok": passed,
            })
            if not passed:
                _add_err("ER: Descuadre en 'Total costos' respecto al detalle", _col_label(c))

    # 2) Validar total gastos operativos = suma del detalle
    if idx_gop_start is not None and idx_gop_total is not None and idx_gop_total > idx_gop_start:
        # rango exclusivo (filas de detalle)
        detail = df.iloc[idx_gop_start + 1: idx_gop_total]
        for c in val_cols:
            s = float(detail[c].apply(_num).sum())
            total = float(_num(df.iloc[idx_gop_total][c]))
            passed = _within(s, total, tolerance)
            checks.append({
                "rule": "ER: Total gastos operativos = suma detalle",
                "column": str(c),
                "expected": total,
                "computed": s,
                "ok": passed,
            })
            if not passed:
                _add_err("ER: Descuadre en 'Total gastos operativos' respecto al detalle", _col_label(c))

    # 3) Validar UTILIDAD BRUTA = Ingresos Brutos - Total Costos (si existe fila)
    if (idx_ing_brutos is not None) and (idx_cost_total is not None) and (idx_util_bruta is not None):
        for c in val_cols:
            ing = float(_num(df.iloc[idx_ing_brutos][c]))
            cost_total = float(_num(df.iloc[idx_cost_total][c]))
            ub_calc = ing - cost_total
            ub_real = float(_num(df.iloc[idx_util_bruta][c]))
            passed = _within(ub_calc, ub_real, tolerance)
            checks.append({
                "rule": "ER: Utilidad Bruta = Ingresos Brutos - Total costos",
                "column": str(c),
                "expected": ub_real,
                "computed": ub_calc,
                "ok": passed,
            })
            if not passed:
                _add_err("ER: Descuadre en 'Utilidad Bruta' vs calculo (Ingresos Brutos - Total costos)", _col_label(c))

    # 4) Validar UTILIDAD OPERATIVA o NETA
    target_idx = idx_util_oper if idx_util_oper is not None else idx_util_neta
    if target_idx is not None:
        label = df.iloc[target_idx][desc_col]
        for c in val_cols:
            util_real = float(_num(df.iloc[target_idx][c]))
            util_calc = None

            if (idx_util_bruta is not None) and (idx_gop_total is not None):
                # Utilidad Operativa = Utilidad Bruta - Total GOP
                ub_real = float(_num(df.iloc[idx_util_bruta][c]))
                gop_total = float(_num(df.iloc[idx_gop_total][c]))
                util_calc = ub_real - gop_total
                rule = "ER: Utilidad Operativa = Utilidad Bruta - Total gastos operativos"
            elif (idx_ing_brutos is not None) and (idx_cost_total is not None) and (idx_gop_total is not None):
                # Si no hay fila de Utilidad Bruta, calcular desde Ingresos - Costos - GOP
                ing = float(_num(df.iloc[idx_ing_brutos][c]))
                cost_total = float(_num(df.iloc[idx_cost_total][c]))
                gop_total = float(_num(df.iloc[idx_gop_total][c]))
                util_calc = ing - cost_total - gop_total
                rule = "ER: Utilidad = Ingresos Brutos - Total costos - Total gastos operativos"
            elif (idx_ing_brutos is not None) and (idx_gop_total is not None):
                # Fallback original
                ing = float(_num(df.iloc[idx_ing_brutos][c]))
                gop_total = float(_num(df.iloc[idx_gop_total][c]))
                util_calc = ing - gop_total
                rule = "ER: Utilidad = Ingresos Brutos - Total gastos operativos"
            else:
                continue

            passed = _within(util_calc, util_real, tolerance)
            checks.append({
                "rule": rule,
                "column": str(c),
                "expected": util_real,
                "computed": util_calc,
                "ok": passed,
            })
            if not passed:
                if idx_util_bruta is not None:
                    _add_err("ER: Descuadre en 'Utilidad Operativa/Neta' vs calculo (Utilidad Bruta - Total GOP)", _col_label(c))
                else:
                    _add_err(f"ER: Descuadre en '{label}' vs calculo (Ingresos Brutos - Total costos - Total GOP)", _col_label(c))

    _finalize_errors()
    return {"ok": len(errors) == 0, "errors": errors, "checks": checks}


# ---------------------------------------------------------------------------
# ESF VALIDATOR
# ---------------------------------------------------------------------------
def _find_left_idx(df: pd.DataFrame, label: str) -> Optional[int]:
    """Busca etiqueta en columna 0 (lado izquierdo)"""
    for i, row in df.iterrows():
        if _norm(row.iloc[0]) == _norm(label):
            return i
    return None


def _find_right_idx(df: pd.DataFrame, label: str) -> Optional[int]:
    """Busca etiqueta en columna 3 (lado derecho), igualdad exacta normalizada"""
    if df.shape[1] < 5:
        return None
    tgt = _norm(label)
    for i, row in df.iterrows():
        if _norm(row.iloc[3]) == tgt:
            return i
    return None


def _find_right_idx_any(df: pd.DataFrame, labels: List[str]) -> Optional[int]:
    """Busca cualquiera de varias etiquetas; permite coincidencia parcial (contains)."""
    if df.shape[1] < 5:
        return None
    targets = [_norm(l) for l in labels]
    for i, row in df.iterrows():
        key = _norm(row.iloc[3])
        for cand in targets:
            if key == cand or cand in key or key.startswith(cand) or key.endswith(cand):
                return i
    return None


def validate_esf_corte(df_esf: pd.DataFrame, *, tolerance: float = 1.0) -> Dict[str, Any]:
    """
    Formato al corte (cinco columnas: desc izq, valor izq, separador, desc der, valor der)

    Reglas:
      - Total Corrientes (izq) = suma detalle Corrientes
      - Total No Corrientes (izq) = suma detalle No Corrientes
      - Total Activos = Total Corrientes + Total No Corrientes
      - Total Pasivos (der) = suma detalle Pasivos
      - Total Patrimonio (der) = suma detalle Patrimonio
      - Total Pasivo + Patrimonio = Total Pasivos + Total Patrimonio
      - Total Activos = Total Pasivo + Patrimonio
    """
    checks: List[Dict[str, Any]] = []
    errors: List[str] = []

    if df_esf is None or df_esf.empty:
        return {"ok": False, "errors": ["Hoja ESF vacía"], "checks": checks}

    df = df_esf.copy()
    df = df.fillna(0)

    # LEFT (Activos)
    i_corr = _find_left_idx(df, "Corrientes")
    i_tot_corr = _find_left_idx(df, "Total Corrientes")
    i_no = _find_left_idx(df, "No Corrientes")
    i_tot_no = _find_left_idx(df, "Total No Corrientes")
    i_tot_act = _find_left_idx(df, "Total Activos")

    def sum_left(r0: int, r1: int) -> float:
        if r0 is None or r1 is None or r1 <= r0:
            return 0.0
        seg = df.iloc[r0 + 1: r1]  # exclusivo
        return float(seg.iloc[:, 1].apply(_num).sum())

    if i_corr is not None and i_tot_corr is not None and i_tot_corr > i_corr:
        s = sum_left(i_corr, i_tot_corr)
        total = float(_num(df.iloc[i_tot_corr, 1]))
        passed = _within(s, total, tolerance)
        checks.append({
            "rule": "ESF: Total Corrientes = suma detalle",
            "side": "izq",
            "expected": total,
            "computed": s,
            "ok": passed,
        })
        if not passed:
            errors.append("ESF: Descuadre en Total Corrientes (izquierda)")

    if i_no is not None and i_tot_no is not None and i_tot_no > i_no:
        s = sum_left(i_no, i_tot_no)
        total = float(_num(df.iloc[i_tot_no, 1]))
        passed = _within(s, total, tolerance)
        checks.append({
            "rule": "ESF: Total No Corrientes = suma detalle",
            "side": "izq",
            "expected": total,
            "computed": s,
            "ok": passed,
        })
        if not passed:
            errors.append("ESF: Descuadre en Total No Corrientes (izquierda)")

    if i_tot_act is not None:
        tot_corr = float(_num(df.iloc[i_tot_corr, 1])) if i_tot_corr is not None else 0.0
        tot_no   = float(_num(df.iloc[i_tot_no, 1])) if i_tot_no is not None else 0.0
        tot_act  = float(_num(df.iloc[i_tot_act, 1]))
        passed   = _within(tot_corr + tot_no, tot_act, tolerance)
        checks.append({
            "rule": "ESF: Total Activos = Total Corrientes + Total No Corrientes",
            "expected": tot_act,
            "computed": tot_corr + tot_no,
            "ok": passed,
        })
        if not passed:
            errors.append("ESF: Descuadre en Total Activos")

    # RIGHT (Pasivos y Patrimonio)
    i_pas = _find_right_idx_any(df, ["Pasivos", "Pasivo"])
    i_tot_pas = _find_right_idx_any(df, [
        "Total Pasivos", "Pasivos Totales", "Total de Pasivos",
        "Total Pasivo", "Total de Pasivo"
    ])
    i_pat = _find_right_idx_any(df, ["Patrimonio"]) 
    i_tot_pat = _find_right_idx_any(df, [
        "Total Patrimonio", "Patrimonio Total", "Total de Patrimonio"
    ])
    i_tot_pp = _find_right_idx_any(df, [
        "Total Pasivo + Patrimonio", "Total Pasivo y Patrimonio",
        "Total Pasivos + Patrimonio", "Total de Pasivo y Patrimonio"
    ])

    def sum_right(r0: int, r1: int) -> float:
        if r0 is None or r1 is None or r1 <= r0:
            return 0.0
        seg = df.iloc[r0 + 1: r1]
        return float(seg.iloc[:, 4].apply(_num).sum())

    if i_pas is not None and i_tot_pas is not None and i_tot_pas > i_pas:
        s = sum_right(i_pas, i_tot_pas)
        total = float(_num(df.iloc[i_tot_pas, 4]))
        passed = _within(s, total, tolerance)
        checks.append({
            "rule": "ESF: Total Pasivos = suma detalle",
            "side": "der",
            "expected": total,
            "computed": s,
            "ok": passed,
        })
        if not passed:
            errors.append("ESF: Descuadre en Total Pasivos (derecha)")
    elif (i_pas is None) or (i_tot_pas is None):
        errors.append("ESF: No se encontraron anclas de Pasivos (revise etiquetas 'Pasivos' / 'Total Pasivos')")

    if i_pat is not None and i_tot_pat is not None and i_tot_pat > i_pat:
        s = sum_right(i_pat, i_tot_pat)
        total = float(_num(df.iloc[i_tot_pat, 4]))
        passed = _within(s, total, tolerance)
        checks.append({
            "rule": "ESF: Total Patrimonio = suma detalle",
            "side": "der",
            "expected": total,
            "computed": s,
            "ok": passed,
        })
        if not passed:
            errors.append("ESF: Descuadre en Total Patrimonio (derecha)")
    elif (i_pat is None) or (i_tot_pat is None):
        errors.append("ESF: No se encontraron anclas de Patrimonio (revise etiquetas 'Patrimonio' / 'Total Patrimonio')")

    if i_tot_pp is not None:
        tot_pas = float(_num(df.iloc[i_tot_pas, 4])) if i_tot_pas is not None else 0.0
        tot_pat = float(_num(df.iloc[i_tot_pat, 4])) if i_tot_pat is not None else 0.0
        tot_pp  = float(_num(df.iloc[i_tot_pp, 4]))
        passed  = _within(tot_pas + tot_pat, tot_pp, tolerance)
        checks.append({
            "rule": "ESF: Total Pasivo + Patrimonio = Total Pasivos + Total Patrimonio",
            "expected": tot_pp,
            "computed": tot_pas + tot_pat,
            "ok": passed,
        })
        if not passed:
            errors.append("ESF: Descuadre en Total Pasivo + Patrimonio")

    # Balance general
    if i_tot_act is not None and i_tot_pp is not None:
        tot_act = float(_num(df.iloc[i_tot_act, 1]))
        tot_pp  = float(_num(df.iloc[i_tot_pp, 4]))
        passed  = _within(tot_act, tot_pp, tolerance)
        checks.append({
            "rule": "ESF: Total Activos = Total Pasivo + Patrimonio",
            "expected": tot_act,
            "computed": tot_pp,
            "ok": passed,
        })
        if not passed:
            errors.append("ESF: Activo ≠ Pasivo + Patrimonio")

    return {"ok": len(errors) == 0, "errors": errors, "checks": checks}


def validate_esf_mensual(df_esf: pd.DataFrame, *, tolerance: float = 1.0) -> Dict[str, Any]:
    """
    Formato mensual (columna 0 = descripcion; columnas 1..N = valores por mes).

    Aplica reglas por cada columna de valores:
      - Total Corrientes = suma detalle Corrientes
      - Total No Corrientes = suma detalle No Corrientes
      - Total Activos = Total Corrientes + Total No Corrientes
      - Total Pasivos = suma detalle Pasivos
      - Total Patrimonio = suma detalle Patrimonio
      - Total Pasivo + Patrimonio = Total Pasivos + Total Patrimonio
      - Total Activos = Total Pasivo + Patrimonio
    """
    checks: List[Dict[str, Any]] = []
    errors: List[str] = []

    if df_esf is None or df_esf.empty:
        return {"ok": False, "errors": ["Hoja ESF/Mensual vacia"], "checks": checks}

    df = df_esf.copy().fillna(0)
    if df.shape[1] <= 1:
        return {"ok": False, "errors": ["ESF mensual sin columnas numericas"], "checks": checks}

    desc_col_idx = 0
    val_cols = list(range(1, df.shape[1]))

    err_cols: Dict[str, List[str]] = {}

    def _col_label(idx: int) -> str:
        label = df.columns[idx] if idx < len(df.columns) else idx
        label = str(label)
        return label if label and label.lower() != "nan" else str(idx)

    def _add_err(msg: str, col_label: str) -> None:
        err_cols.setdefault(msg, []).append(col_label)

    def _finalize_errors() -> None:
        for msg, cols in err_cols.items():
            uniq: List[str] = []
            for c in cols:
                if c not in uniq:
                    uniq.append(c)
            suffix = f" [mes {', '.join(uniq)}]" if uniq else ""
            errors.append(f"{msg}{suffix}")

    def _norm_key(s: Any) -> str:
        key = _norm(s)
        key = re.sub(r"[^a-z0-9]+", " ", key)
        return " ".join(key.split())

    keys = [_norm_key(v) for v in df.iloc[:, desc_col_idx].tolist()]
    n_rows = len(keys)

    rx_activos = re.compile(r"^activos?$")
    rx_pasivos = re.compile(r"^pasivos?$")
    rx_patrimonio_hdr = re.compile(r"^patrimonio(\s+neto)?$")
    rx_corr = re.compile(r"^corrientes?$")
    rx_no_corr = re.compile(r"^no\s+corrientes?$")
    rx_tot_corr = re.compile(r"^total\s+corrientes?$")
    rx_tot_no_corr = re.compile(r"^total\s+no\s+corrientes?$")
    rx_tot_act = re.compile(r"^total\s+activos?$")
    rx_tot_pas = re.compile(r"^total\s+pasivos?$")
    rx_tot_pat = re.compile(r"^(total\s+patrimonio|patrimonio\s+total)$")
    rx_tot_pp = re.compile(r"^total\s+pasivos?\s+(y\s+)?patrimonio$")

    def _find_idx(patterns: List[re.Pattern], start: int, end: int) -> Optional[int]:
        if start is None:
            return None
        if end is None:
            end = n_rows
        for i in range(max(start, 0), min(end, n_rows)):
            key = keys[i]
            for pat in patterns:
                if pat.match(key):
                    return i
        return None

    def _is_total_label(key: str) -> bool:
        return key.startswith("total ") or key.startswith("subtotal ")

    def _is_header_label(key: str) -> bool:
        return (
            rx_activos.match(key)
            or rx_pasivos.match(key)
            or rx_patrimonio_hdr.match(key)
            or rx_corr.match(key)
            or rx_no_corr.match(key)
        )

    i_act_hdr = _find_idx([rx_activos], 0, n_rows)
    i_pas_hdr = _find_idx([rx_pasivos], (i_act_hdr + 1) if i_act_hdr is not None else 0, n_rows)
    i_pat_hdr = _find_idx([rx_patrimonio_hdr], (i_pas_hdr + 1) if i_pas_hdr is not None else 0, n_rows)

    use_sections = i_act_hdr is not None and i_pas_hdr is not None
    if use_sections:
        act_start = i_act_hdr + 1
        act_end = i_pas_hdr
        pas_start = i_pas_hdr + 1
        pas_end = i_pat_hdr if i_pat_hdr is not None else n_rows
        pat_start = (i_pat_hdr + 1) if i_pat_hdr is not None else None
        pat_end = n_rows
    else:
        act_start, act_end = 0, n_rows
        pas_start, pas_end = 0, n_rows
        pat_start, pat_end = 0, n_rows

    def _find_in_range(patterns: List[re.Pattern], start: int, end: int) -> Optional[int]:
        return _find_idx(patterns, start, end)

    # Anclas Activos
    i_act_corr = _find_in_range([rx_corr], act_start, act_end)
    i_act_tot_corr = _find_in_range([rx_tot_corr], act_start, act_end)
    i_act_no = _find_in_range([rx_no_corr], act_start, act_end)
    i_act_tot_no = _find_in_range([rx_tot_no_corr], act_start, act_end)
    i_act_tot_act = _find_in_range([rx_tot_act], act_start, act_end)

    # Anclas Pasivos
    i_pas_tot_corr = _find_in_range([rx_tot_corr], pas_start, pas_end)
    i_pas_tot_no = _find_in_range([rx_tot_no_corr], pas_start, pas_end)
    i_pas_tot_pas = _find_in_range([rx_tot_pas], pas_start, pas_end)

    # Anclas Patrimonio y total general
    i_pat_tot = _find_in_range([rx_tot_pat], pat_start, pat_end)
    i_tot_pp = _find_in_range([rx_tot_pp], pat_start if pat_start is not None else 0, n_rows)

    def sum_range(r0: int, r1: int, col: int) -> float:
        if r0 is None or r1 is None or r1 <= r0:
            return 0.0
        total = 0.0
        for i in range(r0 + 1, r1):
            key = keys[i]
            if _is_total_label(key) or _is_header_label(key):
                continue
            total += float(_num(df.iloc[i, col]))
        return float(total)

    # Validaciones por columna (mes)
    for c in val_cols:
        col_label = _col_label(c)
        # Corrientes
        if i_act_corr is not None and i_act_tot_corr is not None and i_act_tot_corr > i_act_corr:
            s = sum_range(i_act_corr, i_act_tot_corr, c)
            total = float(_num(df.iloc[i_act_tot_corr, c]))
            passed = _within(s, total, tolerance)
            checks.append({"rule": "ESF(M): Total Corrientes = suma detalle", "column": col_label, "expected": total, "computed": s, "ok": passed})
            if not passed:
                _add_err("ESF(M): Descuadre en Total Corrientes", col_label)

        # No Corrientes
        if i_act_no is not None and i_act_tot_no is not None and i_act_tot_no > i_act_no:
            s = sum_range(i_act_no, i_act_tot_no, c)
            total = float(_num(df.iloc[i_act_tot_no, c]))
            passed = _within(s, total, tolerance)
            checks.append({"rule": "ESF(M): Total No Corrientes = suma detalle", "column": col_label, "expected": total, "computed": s, "ok": passed})
            if not passed:
                _add_err("ESF(M): Descuadre en Total No Corrientes", col_label)

        # Total Activos
        if i_act_tot_act is not None:
            tot_corr = float(_num(df.iloc[i_act_tot_corr, c])) if i_act_tot_corr is not None else 0.0
            tot_no = float(_num(df.iloc[i_act_tot_no, c])) if i_act_tot_no is not None else 0.0
            tot_act = float(_num(df.iloc[i_act_tot_act, c]))
            passed = _within(tot_corr + tot_no, tot_act, tolerance)
            checks.append({"rule": "ESF(M): Total Activos = Total Corrientes + Total No Corrientes", "column": col_label, "expected": tot_act, "computed": tot_corr + tot_no, "ok": passed})
            if not passed:
                _add_err("ESF(M): Descuadre en Total Activos", col_label)

        # Pasivos
        if i_pas_hdr is not None and i_pas_tot_pas is not None and i_pas_tot_pas > i_pas_hdr:
            total = float(_num(df.iloc[i_pas_tot_pas, c]))
            if i_pas_tot_corr is not None and i_pas_tot_no is not None:
                s = float(_num(df.iloc[i_pas_tot_corr, c])) + float(_num(df.iloc[i_pas_tot_no, c]))
                rule = "ESF(M): Total Pasivos = Total Corrientes + Total No Corrientes"
            else:
                s = sum_range(i_pas_hdr, i_pas_tot_pas, c)
                rule = "ESF(M): Total Pasivos = suma detalle"
            passed = _within(s, total, tolerance)
            checks.append({"rule": rule, "column": col_label, "expected": total, "computed": s, "ok": passed})
            if not passed:
                _add_err("ESF(M): Descuadre en Total Pasivos", col_label)

        # Patrimonio
        if i_pat_hdr is not None and i_pat_tot is not None and i_pat_tot > i_pat_hdr:
            s = sum_range(i_pat_hdr, i_pat_tot, c)
            total = float(_num(df.iloc[i_pat_tot, c]))
            passed = _within(s, total, tolerance)
            checks.append({"rule": "ESF(M): Total Patrimonio = suma detalle", "column": col_label, "expected": total, "computed": s, "ok": passed})
            if not passed:
                _add_err("ESF(M): Descuadre en Total Patrimonio", col_label)

        # Total Pasivo + Patrimonio
        if i_tot_pp is not None:
            tot_pas = float(_num(df.iloc[i_pas_tot_pas, c])) if i_pas_tot_pas is not None else 0.0
            tot_pat = float(_num(df.iloc[i_pat_tot, c])) if i_pat_tot is not None else 0.0
            tot_pp = float(_num(df.iloc[i_tot_pp, c]))
            passed = _within(tot_pas + tot_pat, tot_pp, tolerance)
            checks.append({"rule": "ESF(M): Total Pasivo + Patrimonio = Total Pasivos + Total Patrimonio", "column": col_label, "expected": tot_pp, "computed": tot_pas + tot_pat, "ok": passed})
            if not passed:
                _add_err("ESF(M): Descuadre en Total Pasivo + Patrimonio", col_label)

        # Balance general
        if i_act_tot_act is not None and i_tot_pp is not None:
            tot_act = float(_num(df.iloc[i_act_tot_act, c]))
            tot_pp = float(_num(df.iloc[i_tot_pp, c]))
            passed = _within(tot_act, tot_pp, tolerance)
            checks.append({"rule": "ESF(M): Total Activos = Total Pasivo + Patrimonio", "column": col_label, "expected": tot_act, "computed": tot_pp, "ok": passed})
            if not passed:
                _add_err("ESF(M): Activo != Pasivo + Patrimonio", col_label)

    _finalize_errors()
    return {"ok": len(errors) == 0, "errors": errors, "checks": checks}


def validate_esf(df_esf: pd.DataFrame, *, tolerance: float = 1.0, mode: Optional[str] = None) -> Dict[str, Any]:
    """Wrapper que enruta a la validación acorde al tipo de ESF.

    mode: "corte" | "mensual" | None (auto)
    """
    m = (mode or "").lower().strip()
    if m == "corte":
        return validate_esf_corte(df_esf, tolerance=tolerance)
    if m == "mensual":
        return validate_esf_mensual(df_esf, tolerance=tolerance)

    # auto: detección simple según etiquetas en la columna 3 (propia del formato corte)
    try:
        if df_esf is not None and not df_esf.empty and df_esf.shape[1] >= 4:
            col3 = df_esf.iloc[:, 3].astype(str).str.lower().fillna("")
            if col3.str.contains("pasivo|patrimonio").any():
                return validate_esf_corte(df_esf, tolerance=tolerance)
    except Exception:
        pass
    return validate_esf_mensual(df_esf, tolerance=tolerance)
