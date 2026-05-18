from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from repositories import AuditRepository


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class AuditService:
    def __init__(self, session: Session):
        self.repo = AuditRepository(session)

    def log(
        self,
        *,
        cpa_user: str,
        entity_type: str,
        entity_id: str,
        action: str,
        summary: str,
        before: Any = None,
        after: Any = None,
        metadata: dict[str, Any] | None = None,
    ):
        return self.repo.append(
            cpa_user=cpa_user or "system",
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            summary=summary,
            metadata=metadata or {},
            payload_before_hash=stable_hash(before) if before is not None else None,
            payload_after_hash=stable_hash(after) if after is not None else None,
        )

    def list_for(self, entity_type: str, entity_id: str) -> list[dict]:
        """Devuelve el historial de un entity en orden cronologico (mas viejo primero)."""
        entries = self.repo.list_for_entity(entity_type, entity_id)
        # list_for_entity ordena desc; invertimos para cronologico
        entries.reverse()
        return [
            {
                "id": e.id,
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "cpa_user": e.cpa_user,
                "action": e.action,
                "summary": e.summary,
                "payload_before_hash": e.payload_before_hash,
                "payload_after_hash": e.payload_after_hash,
                "metadata": json.loads(e.metadata_json or "{}"),
                "prev_entry_hash": e.prev_entry_hash,
            }
            for e in entries
        ]
