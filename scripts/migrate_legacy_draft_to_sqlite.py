from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.engine import get_engine, session_factory
from db.models import Cliente, PeriodoCertificacion
from db.runtime import require_alembic_version
from financial_model import build_financial_model, result_to_json
from services.audit_service import AuditService, stable_hash
from services.serializers import cliente_to_dict


TARGET_RECORD_ID = "draft_20260515003709_7de28b54"
TARGET_GIRO_ID = "comercio_general"
LEGACY_ROOT = ROOT / "data" / "models"


class LegacyMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    ok: bool
    mode: str
    legacy_record_id: str
    legacy_path: str
    payload_hash: str
    cliente_id: str | None
    cliente_action: str
    periodo_id: str | None
    periodo_action: str
    mes_inicial: str
    mes_final: str
    validation_ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "legacy_record_id": self.legacy_record_id,
            "legacy_path": self.legacy_path,
            "payload_hash": self.payload_hash,
            "cliente_id": self.cliente_id,
            "cliente_action": self.cliente_action,
            "periodo_id": self.periodo_id,
            "periodo_action": self.periodo_action,
            "mes_inicial": self.mes_inicial,
            "mes_final": self.mes_final,
            "validation_ok": self.validation_ok,
        }


def migrate_legacy_draft(
    session: Session,
    *,
    legacy_path: Path | None = None,
    record_id: str = TARGET_RECORD_ID,
    apply: bool = False,
    cpa_user: str = "system",
) -> MigrationResult:
    path = legacy_path or find_legacy_draft(record_id)
    record = read_legacy_record(path)
    if record.get("id") != record_id:
        raise LegacyMigrationError(
            f"El JSON tiene id {record.get('id')!r}, pero se esperaba {record_id!r}"
        )
    if record.get("type") != "draft":
        raise LegacyMigrationError("Solo se migran borradores legacy (type='draft')")

    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise LegacyMigrationError("El JSON legacy no contiene payload valido")

    payload_hash = canonical_payload_hash(payload)
    period_data = period_fields_from_payload(payload)
    rendered = preview_payload(payload)
    validation_ok = bool(rendered.get("ok"))

    cliente = find_active_cliente_by_cedula(session, str((payload.get("client") or {}).get("cedula") or record.get("cedula") or ""))
    cliente_action = "reuse" if cliente else "create"
    periodo = None
    periodo_action = "dry_run"

    if cliente:
        periodo = find_periodo_for_range(
            session,
            cliente.id,
            period_data["mes_inicial"],
            period_data["mes_final"],
        )
        if periodo:
            existing_hash = canonical_payload_hash(json.loads(periodo.payload_json or "{}"))
            if existing_hash != payload_hash:
                raise LegacyMigrationError(
                    "Ya existe un periodo para este cliente/rango con payload distinto. "
                    f"periodo_id={periodo.id}"
                )
            periodo_action = "reuse"
        else:
            periodo_action = "create"
    else:
        periodo_action = "create"

    if not apply:
        return MigrationResult(
            ok=True,
            mode="dry-run",
            legacy_record_id=record_id,
            legacy_path=str(path),
            payload_hash=payload_hash,
            cliente_id=cliente.id if cliente else None,
            cliente_action=cliente_action,
            periodo_id=periodo.id if periodo else None,
            periodo_action=periodo_action,
            mes_inicial=period_data["mes_inicial"],
            mes_final=period_data["mes_final"],
            validation_ok=validation_ok,
        )

    metadata = migration_metadata(record, path, payload_hash, period_data, mode="apply")
    try:
        audit = AuditService(session)
        if not cliente:
            cliente = Cliente(**cliente_fields_from_payload(payload))
            session.add(cliente)
            session.flush()
            audit.log(
                cpa_user=cpa_user,
                entity_type="cliente",
                entity_id=cliente.id,
                action="legacy_import",
                summary=f"Importo cliente legacy {cliente.nombre_completo}",
                after=cliente_to_dict(cliente, include_giro=False),
                metadata=metadata,
            )

        if not periodo:
            periodo = PeriodoCertificacion(
                cliente_id=cliente.id,
                periodo_meses=period_data["periodo_meses"],
                mes_inicial=period_data["mes_inicial"],
                mes_final=period_data["mes_final"],
                estado="borrador",
                tasa_cambio=period_data["tasa_cambio"],
                ingresos_base_usd=period_data["ingresos_base_usd"],
                variabilidad_ingresos_pct=period_data["variabilidad_ingresos_pct"],
                cost_pct=period_data["cost_pct"],
                variabilidad_costos_pct=period_data["variabilidad_costos_pct"],
                cash_sales_pct=period_data["cash_sales_pct"],
                seed=period_data["seed"],
                saldos_iniciales_origen="legacy_json",
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                period_blocks_json=json.dumps(rendered.get("period_blocks") or [], ensure_ascii=False, sort_keys=True, default=str),
                validation_json=json.dumps(rendered.get("validations") or {}, ensure_ascii=False, sort_keys=True, default=str),
                created_by=cpa_user or "system",
            )
            session.add(periodo)
            session.flush()
            audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="legacy_import",
                summary=f"Importo borrador legacy {periodo.mes_inicial}..{periodo.mes_final}",
                after={
                    "id": periodo.id,
                    "cliente_id": periodo.cliente_id,
                    "mes_inicial": periodo.mes_inicial,
                    "mes_final": periodo.mes_final,
                    "estado": periodo.estado,
                    "payload_hash": payload_hash,
                },
                metadata=metadata,
            )
            periodo_action = "create"
        else:
            periodo_action = "reuse"

        session.commit()
    except Exception:
        session.rollback()
        raise

    return MigrationResult(
        ok=True,
        mode="apply",
        legacy_record_id=record_id,
        legacy_path=str(path),
        payload_hash=payload_hash,
        cliente_id=cliente.id,
        cliente_action=cliente_action,
        periodo_id=periodo.id,
        periodo_action=periodo_action,
        mes_inicial=period_data["mes_inicial"],
        mes_final=period_data["mes_final"],
        validation_ok=validation_ok,
    )


def find_legacy_draft(record_id: str = TARGET_RECORD_ID) -> Path:
    matches = list(LEGACY_ROOT.glob(f"*/drafts/{record_id}.json"))
    if not matches:
        raise LegacyMigrationError(f"No se encontro el borrador legacy {record_id}")
    if len(matches) > 1:
        raise LegacyMigrationError(f"Se encontro mas de un borrador legacy {record_id}")
    return matches[0]


def read_legacy_record(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LegacyMigrationError(f"No se pudo leer JSON legacy: {path}") from exc
    if not isinstance(data, dict):
        raise LegacyMigrationError("El archivo legacy no contiene un objeto JSON")
    return data


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    return stable_hash(json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)))


def find_active_cliente_by_cedula(session: Session, cedula: str) -> Cliente | None:
    cedula = str(cedula or "").strip()
    if not cedula:
        return None
    stmt = select(Cliente).where(Cliente.cedula == cedula, Cliente.activo == 1).limit(1)
    return session.scalar(stmt)


def find_periodo_for_range(
    session: Session,
    cliente_id: str,
    mes_inicial: str,
    mes_final: str,
) -> PeriodoCertificacion | None:
    stmt = (
        select(PeriodoCertificacion)
        .where(
            PeriodoCertificacion.cliente_id == cliente_id,
            PeriodoCertificacion.mes_inicial == mes_inicial,
            PeriodoCertificacion.mes_final == mes_final,
        )
        .limit(1)
    )
    return session.scalar(stmt)


def cliente_fields_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    client = dict(payload.get("client") or {})
    expenses = dict(payload.get("expenses") or {})
    nombre = _clean_text(client.get("nombre_completo") or client.get("name"))
    cedula = _clean_text(client.get("cedula"))
    giro_nombre = _clean_text(client.get("giro_negocio"))
    negocio = _clean_text(client.get("nombre_negocio") or giro_nombre or nombre)
    direccion_negocio = _clean_text(client.get("direccion_negocio") or "No visible en el borrador legacy")
    if not nombre or not cedula:
        raise LegacyMigrationError("El payload legacy debe incluir nombre_completo y cedula")
    return {
        "nombre_completo": nombre,
        "cedula": cedula,
        "direccion_domicilio": _clean_text(client.get("direccion_personal")),
        "telefono": _clean_text(client.get("contacto")),
        "email": None,
        "nombre_negocio": negocio,
        "ruc": None,
        "matricula_roc": _clean_text(client.get("matricula")),
        "direccion_negocio": direccion_negocio,
        "giro_negocio_id": TARGET_GIRO_ID,
        "plantilla_gastos_json": json.dumps(expenses, ensure_ascii=False, sort_keys=True) if expenses else None,
        "sexo": _clean_text(client.get("sexo")),
        "estado_civil": _clean_text(client.get("estado_civil")),
        "profesion": _clean_text(client.get("profesion")),
        "banco": _clean_text(client.get("banco")),
        "regimen": _clean_text(client.get("regimen")),
        "antiguedad": _clean_text(client.get("antiguedad")),
        "empleados": _clean_text(client.get("empleados")),
        "domicilio": _clean_text(client.get("domicilio")),
        "created_by": "legacy_import",
        "activo": 1,
    }


def period_fields_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    period = dict(payload.get("period") or {})
    income = dict(payload.get("income") or {})
    mes_inicial = _month_key(period.get("start_month") or period.get("mes_inicio"), "start_month")
    mes_final = _month_key(period.get("end_month") or period.get("mes_final"), "end_month")
    start = pd.to_datetime(f"{mes_inicial}-01")
    end = pd.to_datetime(f"{mes_final}-01")
    if start > end:
        raise LegacyMigrationError("El rango legacy tiene mes inicial posterior al mes final")
    return {
        "mes_inicial": mes_inicial,
        "mes_final": mes_final,
        "periodo_meses": (end.year - start.year) * 12 + (end.month - start.month) + 1,
        "tasa_cambio": _float_or_default(period.get("exchange_rate"), 36.6243),
        "seed": _clean_text(period.get("seed")),
        "ingresos_base_usd": _float_or_none(income.get("base_income_usd")),
        "variabilidad_ingresos_pct": _float_or_none(income.get("income_variability_pct")),
        "cost_pct": _float_or_none(income.get("cost_pct")),
        "variabilidad_costos_pct": _float_or_none(income.get("cost_variability_pct")),
        "cash_sales_pct": _float_or_none(income.get("cash_sales_pct")),
    }


def preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = build_financial_model(payload)
    return result_to_json(result)


def migration_metadata(
    record: dict[str, Any],
    path: Path,
    payload_hash: str,
    period_data: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    try:
        rel_path = str(path.resolve().relative_to(ROOT))
    except ValueError:
        rel_path = str(path)
    return {
        "legacy_record_id": record.get("id"),
        "legacy_path": rel_path,
        "payload_hash": payload_hash,
        "mes_inicial": period_data["mes_inicial"],
        "mes_final": period_data["mes_final"],
        "mode": mode,
    }


def _month_key(value: Any, label: str) -> str:
    text = str(value or "").strip()[:7]
    if len(text) != 7 or text[4] != "-":
        raise LegacyMigrationError(f"Falta mes valido en payload.period.{label}")
    try:
        pd.to_datetime(f"{text}-01")
    except Exception as exc:
        raise LegacyMigrationError(f"Mes invalido en payload.period.{label}: {text}") from exc
    return text


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _float_or_default(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _print_result(result: MigrationResult) -> None:
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migra el draft legacy de Kitiel a Cliente + Periodo SQLite."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Valida y muestra acciones sin escribir en DB.")
    mode.add_argument("--apply", action="store_true", help="Ejecuta la migracion.")
    parser.add_argument("--path", type=Path, help="Ruta opcional al JSON legacy.")
    parser.add_argument("--record-id", default=TARGET_RECORD_ID, help="ID del draft legacy esperado.")
    parser.add_argument("--cpa-user", default="system", help="Usuario para audit_log.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    engine = get_engine()
    require_alembic_version(engine)
    session = session_factory(engine)()
    try:
        result = migrate_legacy_draft(
            session,
            legacy_path=args.path,
            record_id=args.record_id,
            apply=bool(args.apply),
            cpa_user=args.cpa_user,
        )
        _print_result(result)
        return 0
    except Exception as exc:
        session.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
