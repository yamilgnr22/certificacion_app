from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from config_cpa import CpaProfile, load_cpa_profile
from document_generator import generar_documento_completo
from financial_model import build_financial_model
from tests.test_financial_model import _docx_text, sample_payload


class CpaProfileTest(unittest.TestCase):
    """F5-T1: los datos del CPA salen de configuracion, no del codigo."""

    def setUp(self):
        self._old_env = os.environ.get("CERTAPP_CPA_PROFILE")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("CERTAPP_CPA_PROFILE", None)
        else:
            os.environ["CERTAPP_CPA_PROFILE"] = self._old_env

    def test_defaults_match_historical_values(self):
        os.environ["CERTAPP_CPA_PROFILE"] = str(Path(tempfile.gettempdir()) / "no_existe_cpa.json")
        profile = load_cpa_profile()

        self.assertEqual(profile.nombre, "Yamil René García Laguna")
        self.assertEqual(profile.numero_cpa, "3314")
        self.assertEqual(profile.telefono, "+505 8966 5057")

    def test_partial_json_overrides_only_given_fields(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({"telefono": "+505 1111 2222", "campo_invalido": "x"}, tmp)
            path = tmp.name
        try:
            os.environ["CERTAPP_CPA_PROFILE"] = path
            profile = load_cpa_profile()

            self.assertEqual(profile.telefono, "+505 1111 2222")
            self.assertEqual(profile.nombre, "Yamil René García Laguna")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_generated_docx_reflects_profile_changes(self):
        # Criterio de aceptacion del plan: cambiar el telefono (y aqui tambien
        # nombre y numero CPA) en el JSON y regenerar -> el DOCX lo refleja
        # sin tocar codigo.
        custom = {
            "nombre": "Contadora de Prueba Pérez",
            "numero_cpa": "9999",
            "telefono": "+505 1234 5678",
            "fin_quinquenio": "1 de enero del 2099",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(custom, tmp)
            profile_path = tmp.name
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp_doc:
            out_path = Path(tmp_doc.name)
        try:
            os.environ["CERTAPP_CPA_PROFILE"] = profile_path
            result = build_financial_model(sample_payload())
            generar_documento_completo(
                result.df_esf_mensual,
                result.df_er,
                result.df_datos,
                result.df_certificacion,
                str(out_path),
                incluir_validacion=False,
                detener_si_error=False,
                esf_tipo="mensual",
            )
            text = _docx_text(out_path)

            self.assertIn("Contadora de Prueba Pérez", text)
            self.assertIn("9999", text)
            self.assertIn("1 de enero del 2099", text)
            self.assertNotIn("Yamil René García Laguna", text)
            self.assertNotIn("3314", text)
        finally:
            Path(profile_path).unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
