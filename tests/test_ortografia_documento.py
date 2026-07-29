"""Ortografia de lo que se imprime en el DOCX.

Las etiquetas del motor y de los generadores son texto que el banco lee y que
el CPA firma: van bien escritas, con tildes. Este test es la red que evita
que vuelvan a colarse sin acento al agregar una cuenta o una fila.

No revisa los DATOS del cliente (nombres, direcciones): esos vienen de la
base y son responsabilidad de quien los carga.
"""

from __future__ import annotations

import re
import unicodedata
import unittest

from motor import certificacion as mcert
from motor import er as mer
from motor import esf as mesf
from motor import mov as mmov
from motor import notas as mnotas
from motor import tipo_b as mtipo_b


def _sin_tildes(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


# Palabras que en el dominio contable SIEMPRE llevan tilde. Si aparecen sin
# ella en una etiqueta visible, es un error.
CON_TILDE = {
    "credito": "crédito", "creditos": "créditos",
    "descripcion": "descripción", "depreciacion": "depreciación",
    "vehiculos": "vehículos", "alcaldia": "alcaldía",
    "publicos": "públicos", "publico": "público",
    "situacion": "situación", "certificacion": "certificación",
    "cedula": "cédula", "matricula": "matrícula",
    "direccion": "dirección", "profesion": "profesión",
    "antiguedad": "antigüedad", "regimen": "régimen",
    "integracion": "integración", "adquisicion": "adquisición",
    "mercaderia": "mercadería", "numero": "número",
    "periodo": "período", "economica": "económica",
    "fotografias": "fotografías", "razon": "razón",
}


def _visible(texto: str) -> str:
    """Deja solo lo que el lector ve.

    Fuera: los marcadores de f-string ({cpa.numero_cpa}, {cedula}...) — son
    nombres de variable de Python, no palabras del documento — y los nombres
    entre comillas simples (hojas de Excel, claves)."""
    t = re.sub(r"\{[^}]*\}", " ", str(texto))
    t = re.sub(r"'[^']*'", " ", t)
    return t


def _revisar(textos: list[str]) -> list[str]:
    fallas = []
    for t in textos:
        for palabra in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", _visible(t)):
            base = _sin_tildes(palabra).lower()
            if base in CON_TILDE and palabra == _sin_tildes(palabra):
                fallas.append(f"{t!r} -> '{palabra}' deberia ser '{CON_TILDE[base]}'")
    return fallas


class EtiquetasDelMotorTest(unittest.TestCase):
    def _assert_ok(self, textos, contexto):
        fallas = _revisar(textos)
        self.assertFalse(fallas, f"{contexto}:\n  " + "\n  ".join(fallas))

    def test_etiquetas_del_er(self):
        labels = [v for k, v in vars(mer).items() if k.startswith("LABEL_")]
        self._assert_ok(labels, "motor/er.py")

    def test_filas_del_esf(self):
        # Las etiquetas viven en los literales de las funciones de armado.
        import inspect

        fuente = inspect.getsource(mesf)
        self._assert_ok(re.findall(r'fila\("([^"]+)"', fuente), "motor/esf.py (mensual)")
        self._assert_ok(re.findall(r'\(\s*"([^"]+)",\s*e\.', fuente), "motor/esf.py (corte)")

    def test_filas_del_flujo_de_caja(self):
        import inspect

        for modulo, nombre in ((mmov, "motor/mov.py"), (mtipo_b, "motor/tipo_b.py")):
            fuente = inspect.getsource(modulo)
            self._assert_ok(re.findall(r'_fila\("([^"]+)"', fuente), nombre)

    def test_titulos_y_filas_de_las_notas(self):
        import inspect

        fuente = inspect.getsource(mnotas)
        # titulo=, columnas=[...] y las etiquetas de fila
        textos = re.findall(r'titulo="([^"]+)"', fuente)
        textos += re.findall(r'columnas=\[([^\]]+)\]', fuente)
        textos += re.findall(r'\["([^"]+)",', fuente)
        self._assert_ok(textos, "motor/notas.py")

    def test_etiquetas_de_certificacion_y_datos(self):
        import inspect

        fuente = inspect.getsource(mcert)
        etiquetas = [
            t for t in re.findall(r'"([A-ZÁÉÍÓÚ][^"]{2,40})"', fuente)
            # Nombres de columna del DataFrame: son clave tecnica (los usan la
            # UI, los servicios y la ruta v1) y se traducen al imprimir, ver
            # TituloColumnaTest.
            if t not in {"Descripcion", "Datos", "Check List"}
        ]
        self._assert_ok(etiquetas, "motor/certificacion.py")


class TextosDeLosGeneradoresTest(unittest.TestCase):
    """Los parrafos y encabezados que escriben los generadores del DOCX."""

    ARCHIVOS = [
        "generators/certificacion.py", "generators/er_table.py",
        "generators/esf_table.py", "generators/esf_table_mensual.py",
        "generators/notas.py", "generators/docs_table.py",
        "generators/datos_table.py",
    ]

    def test_sin_palabras_sin_tilde(self):
        from pathlib import Path

        fallas = []
        for archivo in self.ARCHIVOS:
            fuente = Path(archivo).read_text(encoding="utf-8")
            # Solo lineas de texto visible: literales largos, sin comentarios.
            visibles = []
            for linea in fuente.splitlines():
                if linea.lstrip().startswith(("#", '"""', "'''")):
                    continue
                for t in re.findall(r'"([^"]{6,})"', linea):
                    if " " not in t or t.startswith(("w:", "http")):
                        continue
                    # Los literales todo-en-minusculas son claves de
                    # comparacion interna (ya normalizadas), no texto impreso.
                    if t == t.lower():
                        continue
                    visibles.append(t)
            for f in _revisar(visibles):
                fallas.append(f"{archivo}: {f}")
        self.assertFalse(fallas, "\n  " + "\n  ".join(fallas))


class TituloColumnaTest(unittest.TestCase):
    """El nombre tecnico de la columna se mantiene sin tilde (lo usan la UI,
    los servicios y v1); lo que se IMPRIME sale corregido."""

    def test_traduce_descripcion(self):
        from generators.utils import titulo_columna

        self.assertEqual(titulo_columna("Descripcion"), "Descripción")
        self.assertEqual(titulo_columna("Descripción"), "Descripción")
        self.assertEqual(titulo_columna("Acumulado del periodo"), "Acumulado del período")

    def test_columnas_sin_nombre_quedan_vacias(self):
        from generators.utils import titulo_columna

        self.assertEqual(titulo_columna("Unnamed: 3"), "")

    def test_no_toca_lo_demas(self):
        from generators.utils import titulo_columna

        self.assertEqual(titulo_columna("Promedio Mensual"), "Promedio Mensual")


if __name__ == "__main__":
    unittest.main()
