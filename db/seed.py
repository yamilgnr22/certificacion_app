from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from db.models import GiroNegocio


DEFAULT_BALANCE_ACCOUNTS = [
    "Efectivo y Equivalentes de Efectivo",
    "Cuentas por Cobrar Clientes",
    "Inventarios",
    "Bienes Inmuebles",
    "Mobiliario y Equipos",
    "Vehiculos",
    "Depreciacion Acumulada",
    "Tarjetas de Credito",
    "Proveedores",
    "Impuestos por Pagar",
    "Gastos Acumulados por pagar",
    "Creditos Personales",
    "Creditos Prendarios",
    "Creditos Comerciales",
    "Capital",
    "Resultados Acumulados",
    "Resultados del Ejercicio",
]


GIROS_SEED: list[dict[str, Any]] = [
    {
        "id": "pulperia",
        "nombre": "Pulperia / Tienda de barrio",
        "descripcion": "Comercio minorista de productos de consumo diario.",
        "cost_pct_min": 75.0,
        "cost_pct_max": 82.0,
        "variabilidad_ingresos_pct": 12.0,
        "variabilidad_costos_pct": 5.0,
        "plantilla_gastos": {
            "Sueldos y Salarios": 800,
            "Servicios Publicos": 200,
            "Alcaldia y DGI": 30,
            "Renta": 300,
            "Otros Gastos": 100,
        },
    },
    {
        "id": "ferreteria",
        "nombre": "Ferreteria",
        "descripcion": "Venta de materiales, herramientas y articulos de ferreteria.",
        "cost_pct_min": 65.0,
        "cost_pct_max": 75.0,
        "variabilidad_ingresos_pct": 15.0,
        "variabilidad_costos_pct": 5.0,
        "plantilla_gastos": {
            "Sueldos y Salarios": 2700,
            "Servicios Publicos": 600,
            "Alcaldia y DGI": 50,
            "Combustible": 500,
            "Publicidad": 1500,
            "Mantenimientos": 200,
            "Renta": 440,
            "Seguros": 100,
            "Otros Gastos": 350,
        },
    },
    {
        "id": "importadora",
        "nombre": "Importadora / Distribuidora",
        "descripcion": "Compra, importacion y distribucion de mercaderia.",
        "cost_pct_min": 60.0,
        "cost_pct_max": 72.0,
        "variabilidad_ingresos_pct": 15.0,
        "variabilidad_costos_pct": 6.0,
        "plantilla_gastos": {
            "Sueldos y Salarios": 3500,
            "Servicios Publicos": 800,
            "Alcaldia y DGI": 100,
            "Combustible": 900,
            "Publicidad": 1000,
            "Mantenimientos": 300,
            "Renta": 900,
            "Seguros": 250,
            "Otros Gastos": 500,
        },
    },
    {
        "id": "servicios_profesionales",
        "nombre": "Servicios profesionales",
        "descripcion": "Servicios tecnicos, profesionales o consultoria.",
        "cost_pct_min": 20.0,
        "cost_pct_max": 40.0,
        "variabilidad_ingresos_pct": 12.0,
        "variabilidad_costos_pct": 4.0,
        "plantilla_gastos": {
            "Sueldos y Salarios": 1200,
            "Servicios Publicos": 150,
            "Alcaldia y DGI": 50,
            "Publicidad": 250,
            "Renta": 350,
            "Otros Gastos": 200,
        },
    },
    {
        "id": "comercio_general",
        "nombre": "Comercio general",
        "descripcion": "Venta de mercaderia en general.",
        "cost_pct_min": 65.0,
        "cost_pct_max": 78.0,
        "variabilidad_ingresos_pct": 15.0,
        "variabilidad_costos_pct": 5.0,
        "plantilla_gastos": {
            "Sueldos y Salarios": 2700,
            "Servicios Publicos": 600,
            "Alcaldia y DGI": 50,
            "Combustible": 500,
            "Publicidad": 1500,
            "Renta": 440,
            "Otros Gastos": 350,
        },
    },
]


def seed_giros(session: Session) -> None:
    for item in GIROS_SEED:
        existing = session.get(GiroNegocio, item["id"])
        if existing:
            continue
        session.add(
            GiroNegocio(
                id=item["id"],
                nombre=item["nombre"],
                descripcion=item.get("descripcion"),
                cost_pct_min=float(item["cost_pct_min"]),
                cost_pct_max=float(item["cost_pct_max"]),
                variabilidad_ingresos_pct=float(item["variabilidad_ingresos_pct"]),
                variabilidad_costos_pct=float(item["variabilidad_costos_pct"]),
                plantilla_gastos_json=json.dumps(item["plantilla_gastos"], ensure_ascii=False, sort_keys=True),
                cuentas_balance_json=json.dumps(DEFAULT_BALANCE_ACCOUNTS, ensure_ascii=False),
            )
        )
