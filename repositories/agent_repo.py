from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import select

from db.models import AgentMessage, AgentPlan, AgentProposal, AgentSessionContext


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

    def add_proposal(
        self,
        *,
        periodo_id: str,
        command_id: str,
        payload_before_hash: str,
        proposal_json: str,
        projected_payload_json: str | None,
        expires_at,
    ) -> AgentProposal:
        proposal = AgentProposal(
            periodo_id=periodo_id,
            command_id=command_id,
            status="pending",
            payload_before_hash=payload_before_hash,
            proposal_json=proposal_json,
            projected_payload_json=projected_payload_json,
            expires_at=expires_at,
        )
        self.session.add(proposal)
        self.session.flush()
        return proposal

    def get_proposal(self, proposal_id: str) -> AgentProposal | None:
        return self.session.get(AgentProposal, proposal_id)

    def add_plan(
        self,
        *,
        periodo_id: str,
        cpa_user: str,
        kind: str,
        user_message: str,
        plan_summary: str,
        steps_json: str,
        aggregate_impact_json: str | None,
        payload_hash: str,
        expires_at,
    ) -> AgentPlan:
        plan = AgentPlan(
            periodo_id=periodo_id,
            cpa_user=cpa_user or "system",
            kind=kind,
            user_message=user_message,
            plan_summary=plan_summary,
            steps_json=steps_json,
            aggregate_impact_json=aggregate_impact_json,
            status="pending",
            payload_hash=payload_hash,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        self.session.add(plan)
        self.session.flush()
        return plan

    def get_plan(self, plan_id: str) -> AgentPlan | None:
        return self.session.get(AgentPlan, plan_id)

    def discard_pending_plans_for_periodo(self, *, periodo_id: str, cpa_user: str) -> int:
        records = list(
            self.session.scalars(
                select(AgentPlan).where(
                    AgentPlan.periodo_id == periodo_id,
                    AgentPlan.cpa_user == (cpa_user or "system"),
                    AgentPlan.status == "pending",
                )
            )
        )
        for plan in records:
            plan.status = "discarded"
            plan.failure_reason = "Descartado automaticamente por un plan nuevo."
            plan.applied_at = None
        if records:
            self.session.flush()
        return len(records)

    def recent_plans(self, *, periodo_id: str, cpa_user: str, limit: int = 5) -> list[AgentPlan]:
        stmt = (
            select(AgentPlan)
            .where(
                AgentPlan.periodo_id == periodo_id,
                AgentPlan.cpa_user == (cpa_user or "system"),
            )
            .order_by(AgentPlan.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def supersede_pending_for_command(self, command_id: str) -> int:
        command_id = str(command_id or "").strip()
        if not command_id:
            return 0
        records = list(
            self.session.scalars(
                select(AgentProposal).where(
                    AgentProposal.command_id == command_id,
                    AgentProposal.status == "pending",
                )
            )
        )
        for proposal in records:
            proposal.status = "superseded"
        if records:
            self.session.flush()
        return len(records)

    def recent_applied_proposals(self, *, periodo_id: str, limit: int = 10) -> list[AgentProposal]:
        stmt = (
            select(AgentProposal)
            .where(AgentProposal.periodo_id == periodo_id, AgentProposal.status == "applied")
            .order_by(AgentProposal.applied_at.desc(), AgentProposal.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def recent_messages(self, *, periodo_id: str, cpa_user: str, limit: int = 10) -> list[AgentMessage]:
        stmt = (
            select(AgentMessage)
            .where(AgentMessage.periodo_id == periodo_id, AgentMessage.cpa_user == (cpa_user or "system"))
            .order_by(AgentMessage.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def get_session_context(
        self,
        *,
        periodo_id: str,
        cpa_user: str,
        ttl_minutes: int = 30,
    ) -> AgentSessionContext | None:
        stmt = select(AgentSessionContext).where(
            AgentSessionContext.periodo_id == periodo_id,
            AgentSessionContext.cpa_user == (cpa_user or "system"),
        )
        record = self.session.scalar(stmt)
        if not record:
            return None
        updated = record.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated < datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes):
            return None
        return record

    def upsert_session_context(
        self,
        *,
        periodo_id: str,
        cpa_user: str,
        **changes: Any,
    ) -> AgentSessionContext:
        stmt = select(AgentSessionContext).where(
            AgentSessionContext.periodo_id == periodo_id,
            AgentSessionContext.cpa_user == (cpa_user or "system"),
        )
        record = self.session.scalar(stmt)
        if record is None:
            record = AgentSessionContext(periodo_id=periodo_id, cpa_user=cpa_user or "system")
            self.session.add(record)
        for key, value in changes.items():
            if hasattr(record, key):
                setattr(record, key, value)
        record.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return record
