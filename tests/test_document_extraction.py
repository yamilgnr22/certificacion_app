from __future__ import annotations

import unittest

from document_extraction import build_client_patch


class DocumentExtractionMappingTest(unittest.TestCase):
    def test_build_client_patch_maps_cedula_and_matricula_fields(self):
        extraction = {
            "documents": {
                "cedula": {
                    "nombre_completo": "Kitiel Rosibel Montiel Gonzalez",
                    "numero_cedula": "3610604910000X",
                    "sexo": "F",
                    "direccion_formal": "Residencial Daniel Chavarria, Econs III, 1 cuadra norte, media cuadra este",
                    "domicilio_formal": "Municipio de Managua, Departamento de Managua",
                },
                "matricula": {
                    "modalidad": "Cuota fija",
                    "codigo_interno": "RNVD-117331",
                    "roc": "138034303",
                    "direccion_negocio_formal": "Altamira, semaforos de la Vicky, 1 cuadra y media abajo",
                    "actividad_economica": "Venta de mercaderia en general",
                    "resumen_linea": "",
                },
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["nombre_completo"], "Kitiel Rosibel Montiel González")
        self.assertEqual(patch["cedula"], "361-060491-0000X")
        self.assertEqual(patch["sexo"], "Femenino")
        self.assertEqual(patch["domicilio"], "Municipio de Managua, Departamento de Managua.")
        self.assertEqual(patch["regimen"], "Cuota fija")
        self.assertEqual(patch["matricula"], "RNVD-117331; ROC No. 138034303")
        self.assertEqual(patch["giro_negocio"], "Venta de mercaderia en general")

    def test_build_client_patch_ignores_not_visible_values(self):
        extraction = {
            "documents": {
                "cedula": {
                    "nombre_completo": "No visible en la imagen",
                    "numero_cedula": None,
                    "sexo": "No visible en la imagen",
                },
                "matricula": {
                    "nombre_contribuyente": "Cliente Visible",
                    "resumen_linea": "RNVD-1000; ROC No. 2000",
                },
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["nombre_completo"], "Cliente Visible")
        self.assertEqual(patch["matricula"], "RNVD-1000; ROC No. 2000")
        self.assertNotIn("cedula", patch)
        self.assertNotIn("sexo", patch)

    def test_matricula_summary_ignores_invalid_llm_summary(self):
        extraction = {
            "documents": {
                "cedula": {},
                "matricula": {
                    "codigo_interno": "RNVD-117331",
                    "roc": "R.O.C.:138034303",
                    "resumen_linea": "3610604910000X; ROC No. 138034303.Cedula de identidad",
                },
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["matricula"], "RNVD-117331; ROC No. 138034303")


if __name__ == "__main__":
    unittest.main()
