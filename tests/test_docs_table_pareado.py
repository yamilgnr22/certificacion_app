"""Tests del emparejado semantico en la seccion Documentos del cliente.

Layout esperado en el DOCX: fila 1 = cedula frente|reverso, luego cada
matricula_N junto a su soporte_N (constancia de tramite), y el resto de
a dos. None = celda vacia (matricula sin soporte todavia).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from generators.docs_table import _abrir_enderezada, _filas_pareadas, _normalizar


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


class OrientacionEXIFTest(unittest.TestCase):
    """Las fotos de celular guardan la rotacion en un tag EXIF en vez de rotar
    los pixeles. Windows la aplica al mostrarlas, Word NO: la cedula entraba
    acostada y, escalada por ancho, ocupaba media pagina.
    """

    def _jpeg(self, tmp, nombre, size, orientacion=None):
        from PIL import Image

        ruta = tmp / nombre
        im = Image.new("RGB", size, "white")
        if orientacion is None:
            im.save(ruta, "JPEG")
        else:
            exif = Image.Exif()
            exif[274] = orientacion  # 274 = Orientation
            im.save(ruta, "JPEG", exif=exif)
        return str(ruta)

    def test_endereza_la_foto_acostada(self):
        import tempfile
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            # 1004x1600 con EXIF "90 CCW" = una cedula horizontal
            ruta = self._jpeg(Path(d), "cedula.jpg", (1004, 1600), orientacion=8)
            r = _abrir_enderezada(ruta)
            self.assertNotIsInstance(r, str, "deberia devolver la imagen corregida")
            with Image.open(r) as im:
                self.assertGreater(im.size[0], im.size[1], "tiene que quedar horizontal")
                self.assertEqual(im.size, (1600, 1004))

    def test_sin_exif_devuelve_la_ruta_original(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ruta = self._jpeg(Path(d), "matricula.jpg", (1436, 989))
            self.assertEqual(_abrir_enderezada(ruta), ruta,
                             "sin rotacion pendiente no se reprocesa la imagen")

    def test_orientacion_normal_no_reprocesa(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ruta = self._jpeg(Path(d), "ok.jpg", (800, 600), orientacion=1)
            self.assertEqual(_abrir_enderezada(ruta), ruta)

    def test_archivo_ilegible_no_revienta(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "roto.jpg"
            ruta.write_bytes(b"no soy una imagen")
            # Cae al camino de siempre; el generador ya maneja el error.
            self.assertEqual(_abrir_enderezada(str(ruta)), str(ruta))

    def test_la_imagen_entra_horizontal_en_el_docx(self):
        import io
        import tempfile
        from docx import Document
        from generators.docs_table import generar_tabla_docs_cliente

        with tempfile.TemporaryDirectory() as d:
            imgs = [
                {"tipo": "cedula_front",
                 "path": self._jpeg(Path(d), "front.jpg", (1004, 1600), orientacion=8)},
                {"tipo": "cedula_back",
                 "path": self._jpeg(Path(d), "back.jpg", (930, 1485), orientacion=8)},
            ]
            doc = Document()
            generar_tabla_docs_cliente(doc, imgs)
            buf = io.BytesIO(); doc.save(buf); buf.seek(0)
            for shape in Document(buf).inline_shapes:
                self.assertLess(shape.height.cm, shape.width.cm,
                                "acostada ocuparia media pagina")
                self.assertLess(shape.height.cm, 7.0)


if __name__ == "__main__":
    unittest.main()
