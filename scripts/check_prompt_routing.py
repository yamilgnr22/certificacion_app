"""Prueba de ruteo del system prompt contra el LLM real (F3-T1).

Ejecuta las frases ambiguas documentadas y compara el intent devuelto con
el esperado. Requiere OPENAI_API_KEY. Uso:

    python scripts/check_prompt_routing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from llm.provider import OpenAIProvider
from services.agent_helpers import _system_prompt, _user_prompt

CASES = [
    ("que el inventario oscile alrededor de USD 100k", {"plan_multi_target_balance"}),
    ("que la caja no quede negativa", {"plan_non_negative_account"}),
    ("caja promedio USD 5000 usando capital y que inventario cierre en USD 100k en junio 2026", {"plan_compound_constraints"}),
    ("ajusta inventario a USD 205k en mayo 2026", {"target_balance_adjustment"}),
    ("el primero", {"question"}),
    ("dale", {"question"}),
    ("ajusta la caja a C$ 50,000 en marzo 2026", {"question"}),
    ("CxC a 0 todos los meses contra capital", {"plan_multi_target_balance"}),
    (
        "inventario a USD 205k en mayo 2026 contra proveedores y cuentas por cobrar a USD 20k en junio 2026 contra inventario",
        {"plan_multi_account_target_balance", "plan_compound_constraints"},
    ),
    ("quiero una utilidad anual de USD 50,000", {"plan_target_utility"}),
    ("que proveedores no baje de C$ 80,000", {"plan_non_negative_account"}),
    ("fija los ingresos de febrero 2026 en 95,000 dolares", {"monthly_override"}),
]


def main() -> int:
    provider = OpenAIProvider()
    ui_context = {"period": {"start_month": "2026-01", "end_month": "2026-06"}}
    failures = 0
    for message, expected in CASES:
        try:
            data = provider.complete_json(
                system_prompt=_system_prompt(),
                user_prompt=_user_prompt(message=message, ui_context=ui_context),
            )
            intent = str(data.get("intent") or "")
        except Exception as exc:  # noqa: BLE001 - reporte de diagnostico
            intent = f"ERROR: {exc}"
        ok = intent in expected
        failures += 0 if ok else 1
        status = "OK " if ok else "FAIL"
        print(f"[{status}] '{message}' -> {intent} (esperado: {'/'.join(sorted(expected))})")
    print(f"\n{len(CASES) - failures}/{len(CASES)} casos correctos")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
