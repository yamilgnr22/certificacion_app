from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from db.models import AgentMessage, LegacyCallCounter


class AgentRepository:
    def __init__(self, session: Session):
        self.session = session

    def add_message(
        self,
        *,
        periodo_id: str,
        command_id: str,
        cpa_user: str,
        message: str,
        intent: str,
        response_type: str,
        response: dict[str, Any],
    ) -> AgentMessage:
        record = AgentMessage(
            periodo_id=periodo_id,
            command_id=command_id,
            cpa_user=cpa_user or "system",
            message=message,
            intent=intent,
            response_type=response_type,
            response_json=json.dumps(response, ensure_ascii=False, sort_keys=True, default=str),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def increment_legacy_counter(self, endpoint: str) -> LegacyCallCounter:
        counter = self.session.get(LegacyCallCounter, endpoint)
        if counter is None:
            counter = LegacyCallCounter(endpoint=endpoint, call_count=0, updated_at=datetime.now(timezone.utc))
            self.session.add(counter)
        counter.call_count += 1
        counter.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return counter
