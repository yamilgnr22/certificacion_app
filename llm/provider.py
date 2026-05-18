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

    def __init__(self, *, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL_AGENT", os.getenv("OPENAI_MODEL_CHAT", "gpt-4o-mini"))
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
        response = client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMProviderError(f"El proveedor LLM no devolvio JSON valido: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMProviderError("El proveedor LLM devolvio una estructura invalida.")
        return data


def get_llm_provider() -> LLMProvider:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider == "openai":
        return OpenAIProvider()
    raise LLMProviderError(f"Proveedor LLM no soportado: {provider}")
