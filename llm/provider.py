from __future__ import annotations

import json
import os
from typing import Any, Mapping, Protocol


class LLMProviderError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class OpenAIProvider:
    name = "openai"
    MAX_RETRIES = 1

    def __init__(self, *, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL_AGENT", os.getenv("OPENAI_MODEL_CHAT", "gpt-4o-mini"))
        # Reintentos efectuados en la ultima llamada (F3-T2); el agente lo
        # reporta en la respuesta como llm_retries.
        self.last_retries = 0
        if not self.api_key:
            raise LLMProviderError("No hay clave OpenAI configurada para ejecutar el asistente contable.")

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        from openai import OpenAI  # type: ignore

        client = OpenAI(api_key=self.api_key)
        self.last_retries = 0
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            response = client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=messages,
            )
            content = response.choices[0].message.content or "{}"
            try:
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("la respuesta no es un objeto JSON")
                return data
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if attempt >= self.MAX_RETRIES:
                    break
                # F3-T2: un reintento estructurado con el error como contexto.
                self.last_retries = attempt + 1
                messages = [
                    *messages,
                    {"role": "assistant", "content": content[:2000]},
                    {
                        "role": "user",
                        "content": (
                            f"Tu respuesta anterior no fue valida ({exc}). "
                            "Responde SOLO un objeto JSON valido con intent y args, sin texto adicional."
                        ),
                    },
                ]
        raise LLMProviderError(f"El proveedor LLM no devolvio JSON valido tras reintentar: {last_error}")


def get_llm_provider() -> LLMProvider:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider == "openai":
        return OpenAIProvider()
    raise LLMProviderError(f"Proveedor LLM no soportado: {provider}")
