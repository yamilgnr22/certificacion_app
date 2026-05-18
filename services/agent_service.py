from __future__ import annotations

import uuid
from typing import Any, Mapping

from sqlalchemy.orm import Session

from llm import LLMProvider, LLMProviderError, get_llm_provider
from repositories import AgentRepository, PeriodoRepository
from services.agent_tools import AgentToolRegistry
from services.serializers import parse_json_object


PROMPT_VERSION = "agent-command-v1.0.0"


class AgentServiceError(ValueError):
    pass


class AgentConfigError(AgentServiceError):
    pass


class AgentValidationError(AgentServiceError):
    pass


class AgentNotFoundError(AgentServiceError):
    pass


class AgentCommandService:
    """Orquestador del asistente contable nuevo.

    Fase 2A: solo consultas y navegacion. Las mutaciones financieras se mantienen
    fuera de este servicio hasta que existan propuestas auditables.
    """

    def __init__(self, session: Session, *, provider: LLMProvider | None = None):
        self.session = session
        self.periodos = PeriodoRepository(session)
        self.agent_repo = AgentRepository(session)
        self.tools = AgentToolRegistry()
        self.provider = provider

    def handle_command(
        self,
        *,
        periodo_id: str,
        message: str,
        ui_context: Mapping[str, Any] | None = None,
        cpa_user: str = "system",
    ) -> dict[str, Any]:
        periodo_id = str(periodo_id or "").strip()
        message = str(message or "").strip()
        ui_context = dict(ui_context or {})
        if not periodo_id:
            raise AgentValidationError("Falta periodo_id para ejecutar el asistente.")
        if not message:
            raise AgentValidationError("Escriba una instruccion para el asistente.")

        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise AgentNotFoundError("Periodo no encontrado.")

        payload = parse_json_object(periodo.payload_json)
        command_id = f"cmd_{uuid.uuid4().hex[:12]}"
        try:
            provider = self.provider or self._provider_from_config()
            interpreted = provider.complete_json(
                system_prompt=_system_prompt(),
                user_prompt=_user_prompt(message=message, ui_context=ui_context),
                schema=AGENT_COMMAND_SCHEMA,
            )
        except LLMProviderError as exc:
            raise AgentConfigError(str(exc)) from exc
        except Exception as exc:
            raise AgentServiceError(f"No pude interpretar la instruccion: {type(exc).__name__}: {exc}") from exc

        intent = str(interpreted.get("intent") or "").strip()
        args = interpreted.get("args") if isinstance(interpreted.get("args"), dict) else {}
        if intent in {"", "clarification", "question"}:
            response = self._question_response(
                command_id=command_id,
                intent=intent or "question",
                message=str(interpreted.get("assistant_message") or interpreted.get("question") or "Necesito un poco mas de detalle para ayudarte."),
            )
        elif intent in self.tools.tools:
            tool_result = self.tools.run(intent, payload, args)
            response = self._tool_response(
                command_id=command_id,
                intent=intent,
                tool_result=tool_result,
            )
        else:
            response = self._question_response(
                command_id=command_id,
                intent=intent or "unknown",
                message=(
                    "Esa instruccion parece cambiar el modelo. En esta fase puedo consultar saldos, "
                    "mostrar mayores, abrir comprobantes y navegar. Para aplicar cambios usaremos propuestas auditables en la siguiente fase."
                ),
            )

        self.agent_repo.add_message(
            periodo_id=periodo.id,
            command_id=command_id,
            cpa_user=cpa_user,
            message=message,
            intent=response.get("intent"),
            response_type=response.get("response_type"),
            response=response,
        )
        self.session.commit()
        return response

    @staticmethod
    def _provider_from_config() -> LLMProvider:
        return get_llm_provider()

    def _tool_response(self, *, command_id: str, intent: str, tool_result: Mapping[str, Any]) -> dict[str, Any]:
        response_type = str(tool_result.get("response_type") or "answer")
        return {
            "ok": True,
            "command_id": command_id,
            "intent": intent,
            "response_type": response_type,
            "assistant_message": str(tool_result.get("assistant_message") or ""),
            "ui_actions": list(tool_result.get("ui_actions") or []),
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
        }

    def _question_response(self, *, command_id: str, intent: str, message: str) -> dict[str, Any]:
        return {
            "ok": True,
            "command_id": command_id,
            "intent": intent,
            "response_type": "question",
            "assistant_message": message,
            "ui_actions": [],
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
        }

    def _audit_metadata(self, command_id: str) -> dict[str, Any]:
        return {
            "command_id": command_id,
            "source": "agent_contable",
            "prompt_version": PROMPT_VERSION,
            "tool_versions": self.tools.versions(),
        }


AGENT_COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["explain_balance", "show_ledger", "show_voucher", "navigate", "question", "unsupported"],
        },
        "args": {"type": "object"},
        "assistant_message": {"type": "string"},
    },
    "required": ["intent", "args"],
}


def _system_prompt() -> str:
    return (
        "Sos un asistente contable dentro de una app de certificaciones. "
        "En esta fase solo podés interpretar consultas y navegacion, no cambios contables. "
        "Respondé exclusivamente JSON valido con intent y args. "
        "Intents permitidos: explain_balance(account, month), show_ledger(account), "
        "show_voucher(voucher_id), navigate(target), question. "
        "Si el usuario pide registrar, reversar, cambiar supuestos, finalizar o generar documentos, "
        "devolve intent unsupported o question; no inventes calculos."
    )


def _user_prompt(*, message: str, ui_context: Mapping[str, Any]) -> str:
    return (
        "Mensaje del usuario:\n"
        f"{message}\n\n"
        "Contexto UI disponible:\n"
        f"{dict(ui_context or {})}\n\n"
        "Ejemplos de cuentas validas: Efectivo y Equivalentes de Efectivo, "
        "Resultados Acumulados, Resultados del Ejercicio, Inventarios, Proveedores."
    )
