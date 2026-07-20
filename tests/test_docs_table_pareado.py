"""Tests del emparejado semantico en la seccion Documentos del cliente.

Layout esperado en el DOCX: fila 1 = cedula frente|reverso, luego cada
matricula_N junto a su soporte_N (constancia de tramite), y el resto de
a dos. None = celda vacia (matricula sin soporte todavia).
"""

from __future__ import annotations

import unittest

from generators.docs_table import _filas_pareadas, _normalizar


def _d(tipo: str, path: str) -> dict:
    return {"tipo": tipo, "path": path}


class NormalizarTest(unittest.TestCase):
    def test_acepta_rutas_dicts_y_tuplas(self):
        items = ["a.png", _d("matricula_1", "b.png"), ("cedula_front", "c.png")]
        self.assertEqual(
            _normalizar(items),
            [("otro", "a.png"), ("matricula_1", "b.png"), ("cedula_front", "c.png")],
        )

    def test_vacio(self):
        self.assertEqual(_normalizar(None), [])
        self.assertEqual(_normalizar([]), [])


class FilasPareadasTest(unittest.TestCase):
    def test_layout_completo(self):
        items = [
            _d("soporte_2", "s2"), _d("matricula_1", "m1"), _d("cedula_back", "cb"),
            _d("matricula_3", "m3"), _d("soporte_1", "s1"), _d("cedula_front", "cf"),
            _d("matricula_2", "m2"), _d("soporte_3", "s3"),
        ]
        self.assertEqual(
            _filas_pareadas(items),
            [("cf", "cb"), ("m1", "s1"), ("m2", "s2"), ("m3", "s3")],
        )

    def test_matricula_sin_soporte_deja_celda_vacia(self):
        items = [_d("cedula_front", "cf"), _d("cedula_back", "cb"), _d("matricula_1", "m1")]
        self.assertEqual(_filas_pareadas(items), [("cf", "cb"), ("m1", None)])

    def test_soporte_sin_matricula_tambien_sale(self):
        items = [_d("soporte_2", "s2")]
        self.assertEqual(_filas_pareadas(items), [(None, "s2")])

    def test_solo_una_cedula(self):
        self.assertEqual(_filas_pareadas([_d("cedula_back", "cb")]), [(None, "cb")])

    def test_resto_va_de_a_dos_al_final(self):
        items = [
            _d("otro", "o1"), _d("matricula", "leg"), _d("cedula_front", "cf"),
            _d("otro", "o2"),
        ]
        self.assertEqual(
            _filas_pareadas(items),
            [("cf", None), ("o1", "leg"), ("o2", None)],
        )

    def test_compat_lista_de_rutas_planas(self):
        # Certificaciones viejas pasaban rutas sueltas: orden original de a dos.
        self.assertEqual(
            _filas_pareadas(["a", "b", "c"]),
            [("a", "b"), ("c", None)],
        )


if __name__ == "__main__":
    unittest.main()
