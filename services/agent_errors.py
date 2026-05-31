"""Excepciones del dominio del agente contable.

Vive en su propio modulo para que `agent_service.py` y
`agent_helpers.py` puedan compartir el mismo tipo sin caer en
imports circulares.
"""

from __future__ import annotations


class AgentServiceError(ValueError):
    pass


class AgentConfigError(AgentServiceError):
    pass


class AgentValidationError(AgentServiceError):
    pass


class AgentNotFoundError(AgentServiceError):
    pass


class AgentProposalConflictError(AgentServiceError):
    pass


class AgentCatalogChangedError(AgentServiceError):
    pass
