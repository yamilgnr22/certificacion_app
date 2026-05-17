from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditLog


class AuditRepository:
    def __init__(self, session: Session):
        self.session = session

    def append(
        self,
        *,
        cpa_user: str,
        entity_type: str,
        entity_id: str,
        action: str,
        summary: str,
        metadata: dict | None = None,
        payload_before_hash: str | None = None,
        payload_after_hash: str | None = None,
    ) -> AuditLog:
        previous = self.latest()
        prev_hash = self.entry_hash(previous) if previous else None
        entry = AuditLog(
            cpa_user=cpa_user,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            summary=summary,
            payload_before_hash=payload_before_hash,
            payload_after_hash=payload_after_hash,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            prev_entry_hash=prev_hash,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def latest(self) -> AuditLog | None:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        return self.session.scalar(stmt)

    def list_for_entity(self, entity_type: str, entity_id: str) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        )
        return list(self.session.scalars(stmt))

    @staticmethod
    def entry_hash(entry: AuditLog) -> str:
        payload = {
            "id": entry.id,
            "timestamp": entry.timestamp.isoformat() if entry.timestamp else "",
            "cpa_user": entry.cpa_user,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "action": entry.action,
            "summary": entry.summary,
            "payload_before_hash": entry.payload_before_hash,
            "payload_after_hash": entry.payload_after_hash,
            "metadata_json": entry.metadata_json,
            "prev_entry_hash": entry.prev_entry_hash,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
