"""Perfil del CPA que firma las certificaciones (F5-T1).

Los datos personales del contador (nombre, cedula, numero CPA, quinquenio,
contacto) ya no viven hardcodeados en los generadores: se cargan de
``cpa_profile.json`` en la raiz del proyecto (o de la ruta indicada en la
variable de entorno ``CERTAPP_CPA_PROFILE``). Cualquier campo ausente o
vacio usa el default historico, asi que el JSON puede contener solo lo que
se quiera sobreescribir.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class CpaProfile:
    nombre: str = "Yamil René García Laguna"
    nombre_plano: str = "Yamil Rene Garcia Laguna"  # variante sin tildes para tablas planas
    titulo_corto: str = "Licenciado"
    titulo: str = "Licenciado en Contaduría Pública y Auditoría"
    estado_civil: str = "soltero"
    domicilio: str = "Managua"
    ciudad_emision: str = "Managua"
    cedula: str = "001-281186-0054R"
    numero_cpa: str = "3314"
    acuerdo_cpa: str = "C.P.A. No. 315-2023"
    fecha_acuerdo: str = "22 de diciembre del 2023"
    fin_quinquenio: str = "21 de diciembre del 2028"
    telefono: str = "+505 8966 5057"
    email: str = "yamilgnr22@gmail.com"
    # Fuentes del encabezado/pie. "Abadi" requiere estar instalada en la
    # maquina que abre el documento; si no lo esta, Word sustituye con una
    # similar. Para maxima portabilidad usar "Calibri" en el JSON.
    font_encabezado: str = "Abadi"
    font_secundaria: str = "Abadi Extra Light"


def _profile_path() -> Path:
    override = os.getenv("CERTAPP_CPA_PROFILE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).parent / "cpa_profile.json"


def load_cpa_profile() -> CpaProfile:
    """Carga el perfil desde JSON; sin archivo (o invalido) usa defaults.

    Se lee en cada generacion (el archivo es minusculo) para que editar el
    JSON surta efecto sin reiniciar el servidor.
    """
    path = _profile_path()
    if not path.exists():
        return CpaProfile()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return CpaProfile()
    if not isinstance(data, dict):
        return CpaProfile()
    valid = {field.name for field in fields(CpaProfile)}
    clean = {
        key: str(value).strip()
        for key, value in data.items()
        if key in valid and str(value or "").strip()
    }
    return CpaProfile(**clean)
