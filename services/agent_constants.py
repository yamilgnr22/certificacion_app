"""Constantes compartidas entre AgentCommandService y sus mixins.

Extraidas para evitar ciclos de import entre `agent_service.py` y los
modulos de mixins (`agent_plan_builders.py`, etc.).
"""

from __future__ import annotations


MAX_PLAN_ACCOUNTS = 4

TARGET_BALANCE_ACCOUNTS = {
    "inventory": {"label": "Inventarios", "normal_balance": "debit"},
    "cash": {"label": "Efectivo y Equivalentes de Efectivo", "normal_balance": "debit"},
    "accounts_receivable": {"label": "Cuentas por Cobrar Clientes", "normal_balance": "debit"},
    "suppliers": {"label": "Proveedores", "normal_balance": "credit"},
}

TARGET_COUNTER_DEFAULTS = {
    # El motor actual no postea journals contra P&L como COGS/Compras; para objetivo
    # de inventario se usa caja/proveedores hasta que el engine soporte P&L manual.
    "inventory": {"increase": ("cash", "suppliers"), "decrease": ("cash",)},
    "cash": {"increase": ("accounts_receivable", "loans_personal"), "decrease": ("suppliers", "exp_other")},
    "accounts_receivable": {"increase": ("current_earnings",), "decrease": ("cash",)},
    "suppliers": {"increase": ("inventory",), "decrease": ("cash",)},
}
