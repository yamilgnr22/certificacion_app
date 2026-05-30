from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy.orm import Session

from financial_model import DEFAULT_EXPENSES_USD
from repositories import AccountRepository, ClienteRepository, GiroRepository
from services.serializers import parse_json_object


class PlantillaService:
    def __init__(self, session: Session):
        self.clientes = ClienteRepository(session)
        self.giros = GiroRepository(session)
        self.accounts = AccountRepository(session)

    def effective_for_cliente(self, cliente_id: str) -> dict:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            return self._response("default", {}, dict(DEFAULT_EXPENSES_USD), [])

        giro = self.giros.get(cliente.giro_negocio_id)
        base_raw = parse_json_object(giro.plantilla_gastos_json) if giro and giro.activo else dict(DEFAULT_EXPENSES_USD)
        override_raw = parse_json_object(cliente.plantilla_gastos_json) if cliente.plantilla_gastos_json else {}

        base_items, base_warnings = self._normalize_template(base_raw, source="giro" if giro else "default")
        override_items, override_warnings = self._normalize_template(override_raw, source="cliente")
        merged = {**base_items, **override_items}

        if override_items:
            origen = "cliente"
        elif giro and giro.activo:
            origen = "giro"
        else:
            origen = "default"
        return self._response(origen, merged, {}, [*base_warnings, *override_warnings])

    def set_cliente_template(self, cliente_id: str, plantilla: dict) -> dict:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            raise KeyError("Cliente no encontrado")
        items, warnings = self._normalize_template(plantilla, source="cliente", strict=True)
        if not items:
            raise ValueError("La plantilla no puede estar vacia")
        data = {
            "version": 2,
            "items": [
                {"account_code": code, "amount_usd": item["amount_usd"]}
                for code, item in sorted(items.items(), key=lambda pair: pair[1].get("display_order", 0))
            ],
        }
        cliente.plantilla_gastos_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
        return self._response("cliente", items, {}, warnings)

    def engine_expenses_for_cliente(self, cliente_id: str) -> dict:
        effective = self.effective_for_cliente(cliente_id)
        return dict(effective.get("plantilla") or {})

    def engine_expenses_from_template(self, template: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
        items, warnings = self._normalize_template(template, source="periodo")
        return self._engine_expenses(items), warnings

    def recurring_accounts(self) -> list[dict[str, Any]]:
        return [
            {
                "code": account.code,
                "name": account.name,
                "legacy_payload_key": account.legacy_payload_key,
                "display_order": account.display_order,
            }
            for account in self.accounts.list_recurring_expenses()
        ]

    def _normalize_template(
        self,
        raw: Mapping[str, Any] | None,
        *,
        source: str,
        strict: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        warnings: list[str] = []
        items: dict[str, dict[str, Any]] = {}
        if not raw:
            return items, warnings
        if not isinstance(raw, Mapping):
            if strict:
                raise ValueError("La plantilla debe ser un objeto JSON")
            return items, [f"Plantilla {source} invalida: no es un objeto JSON."]

        if isinstance(raw.get("items"), list):
            for item in raw.get("items") or []:
                if not isinstance(item, Mapping):
                    warnings.append(f"Linea invalida en plantilla {source}.")
                    continue
                code = str(item.get("account_code") or "").strip()
                amount = self._amount(item.get("amount_usd"), code or "linea", strict=strict)
                if amount is None:
                    continue
                self._add_by_code(items, warnings, code, amount, source=source, strict=strict, merge_duplicate=False)
            return items, warnings

        for label, value in raw.items():
            amount = self._amount(value, str(label or ""), strict=strict)
            if amount is None:
                continue
            account = self.accounts.find_by_text(str(label or ""))
            if not account:
                msg = f"No se encontro cuenta de catalogo para '{label}' en plantilla {source}."
                if strict:
                    raise ValueError(msg)
                warnings.append(msg)
                continue
            self._add_account(items, warnings, account, amount, source=source, label=str(label), merge_duplicate=True)
        return items, warnings

    def _add_by_code(
        self,
        items: dict[str, dict[str, Any]],
        warnings: list[str],
        code: str,
        amount: float,
        *,
        source: str,
        strict: bool,
        merge_duplicate: bool,
    ) -> None:
        account = self.accounts.get_by_code_any(code)
        if not account:
            msg = f"La cuenta '{code}' no existe en el catalogo."
            if strict:
                raise ValueError(msg)
            warnings.append(msg)
            return
        if not account.active:
            warnings.append(f"La cuenta '{account.name}' esta inactiva y se omitio de la plantilla {source}.")
            return
        if not account.is_recurring_expense:
            msg = f"La cuenta '{account.name}' no esta marcada como gasto recurrente."
            if strict:
                raise ValueError(msg)
            warnings.append(msg)
            return
        self._add_account(items, warnings, account, amount, source=source, label=account.name, merge_duplicate=merge_duplicate)

    @staticmethod
    def _add_account(
        items: dict[str, dict[str, Any]],
        warnings: list[str],
        account,
        amount: float,
        *,
        source: str,
        label: str,
        merge_duplicate: bool,
    ) -> None:
        if int(getattr(account, "is_postable", 0) or 0) != 1:
            warnings.append(
                f"La cuenta '{account.name}' es un rubro; no se puede usar en la plantilla {source}."
            )
            return
        if account.code in items:
            if merge_duplicate:
                items[account.code]["amount_usd"] += amount
                warnings.append(
                    f"Se fusiono '{label}' con '{account.name}' en plantilla {source} porque apuntan a la misma cuenta."
                )
                return
            raise ValueError(f"La cuenta '{account.name}' esta duplicada en la plantilla.")
        items[account.code] = {
            "account_code": account.code,
            "account_name": account.name,
            "amount_usd": amount,
            "legacy_payload_key": account.legacy_payload_key,
            "display_order": account.display_order,
        }

    @staticmethod
    def _amount(value: Any, label: str, *, strict: bool) -> float | None:
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            if strict:
                raise ValueError(f"Monto invalido para {label}") from exc
            return None
        if amount < 0:
            if strict:
                raise ValueError(f"Monto negativo no permitido para {label}")
            return None
        return amount

    def _response(
        self,
        origen: str,
        items: dict[str, dict[str, Any]],
        fallback: dict[str, float],
        warnings: list[str],
    ) -> dict[str, Any]:
        engine = self._engine_expenses(items) if items else dict(fallback)
        ordered = sorted(items.values(), key=lambda item: (item.get("display_order", 0), item.get("account_name", "")))
        return {
            "origen": origen,
            "version": 2,
            "items": [
                {
                    "account_code": item["account_code"],
                    "account_name": item["account_name"],
                    "amount_usd": float(item["amount_usd"]),
                    "legacy_payload_key": item.get("legacy_payload_key"),
                }
                for item in ordered
            ],
            "warnings": warnings,
            "plantilla": engine,
        }

    @staticmethod
    def _engine_expenses(items: dict[str, dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for item in items.values():
            key = item.get("legacy_payload_key")
            if key:
                out[str(key)] = float(item.get("amount_usd") or 0)
        return out
