from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


MAX_PLANNER_STEPS = 3
TECHNICAL_TOOLS = {"find_account", "validate_account", "estimate_entry_impact"}
MUTATING_TOOLS = {"create_account", "journal_entry"}
ALLOWED_TOOLS = TECHNICAL_TOOLS | MUTATING_TOOLS


class AgentPlanError(ValueError):
    pass


@dataclass(frozen=True)
class AgentPlanStep:
    tool: str
    args: dict[str, Any]


class AgentPlanner:
    """Validador local de planes multi-paso producidos por el LLM.

    El planner no llama al LLM ni muta datos. Su trabajo es convertir el JSON
    del modelo en una secuencia segura y acotada de pasos.
    """

    def validate(self, interpreted: Mapping[str, Any]) -> list[AgentPlanStep]:
        steps_raw = interpreted.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise AgentPlanError("Necesito una instruccion completa con cuenta, monto, mes y asiento a registrar.")
        if len(steps_raw) > MAX_PLANNER_STEPS:
            raise AgentPlanError("Esa instruccion requiere demasiados pasos. Dividila en partes mas pequenas.")

        steps: list[AgentPlanStep] = []
        for raw in steps_raw:
            if not isinstance(raw, Mapping):
                raise AgentPlanError("El plan contiene un paso invalido.")
            tool = str(raw.get("tool") or "").strip()
            if tool not in ALLOWED_TOOLS:
                raise AgentPlanError(f"La herramienta {tool or '(vacia)'} no esta habilitada para planes compuestos.")
            args = raw.get("args") if isinstance(raw.get("args"), Mapping) else {}
            steps.append(AgentPlanStep(tool=tool, args=dict(args)))

        if not any(step.tool == "journal_entry" for step in steps):
            raise AgentPlanError("El plan compuesto debe incluir el asiento contable a registrar.")
        if sum(1 for step in steps if step.tool in MUTATING_TOOLS) > 2:
            raise AgentPlanError("Por ahora solo puedo crear una cuenta y registrar un asiento en una misma propuesta.")
        return steps

