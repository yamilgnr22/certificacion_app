"""Motor de certificaciones financieras.

Reemplaza la construccion manual del Excel. Recibe inputs estructurados
(InputsTipoA / InputsTipoB) y produce ModeloCertificacion (ER, ESF, Mov,
Planes, Notas, Certificacion). Los DataFrames de salida alimentan
generar_documento_completo() sin modificaciones.

Reglas duras del motor (ver Decisiones_y_Reglas_Motor.md seccion C):
  1. Saldo de cada credito en mes de corte = saldo_reportado (exacto).
  2. Tipo A: ESF_Corte = saldos finales dados (exacto).
  3. Tipo B: cuentas objetivo en banda +-20%.
  4. Cada mes Total Activos = Total Pasivo + Patrimonio (sin plug Capital).
  5. ESF Resultados del Ejercicio = utilidad acumulada del ER.
  6. ER Gastos Financieros = suma intereses de planes vivos ese mes.
  7. ESF Efectivo = saldo final de Mov.
  8. ESF Depr. Acumulada = suma depreciaciones del ER; depr. no sale en Mov.
  9. Capital constante = Activos0 - Pasivos0.
"""

from motor.inputs import (
    CuentaObjetivo,
    CuotaPlan,
    DatosCliente,
    DeudaInput,
    ER_LineaMes,
    Bandas,
    ESF_Saldos,
    Minimos,
    Estrategia,
    InputsTipoA,
    InputsTipoB,
    Moneda,
    PeriodoSpec,
    PlanResuelto,
)
from motor.orquestador import ModeloCertificacion, certificar_tipo_a, certificar_tipo_b

__all__ = [
    "CuentaObjetivo",
    "CuotaPlan",
    "DatosCliente",
    "DeudaInput",
    "ER_LineaMes",
    "Bandas",
    "ESF_Saldos",
    "Minimos",
    "Estrategia",
    "InputsTipoA",
    "InputsTipoB",
    "Moneda",
    "ModeloCertificacion",
    "PeriodoSpec",
    "PlanResuelto",
    "certificar_tipo_a",
    "certificar_tipo_b",
]
