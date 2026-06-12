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
                    "fecha_nacimiento": "06-04-1991",
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
        self.assertEqual(patch["fecha_nacimiento"], "1991-04-06")
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

    def test_build_client_patch_title_cases_uppercase_name_without_surname_rewrites(self):
        extraction = {
            "documents": {
                "cedula": {
                    "nombres_raw": "DAYANA MARILEN",
                    "apellidos_raw": "CENTENO LUQUEZ",
                    "nombre_completo": "DAYANA MARILEN CENTENO LUQUEZ",
                },
                "matricula": {},
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["nombre_completo"], "Dayana Marilen Centeno Luquez")
        self.assertEqual(patch["nombres_raw"], "Dayana Marilen")
        self.assertEqual(patch["apellidos_raw"], "Centeno Luquez")

    def test_build_client_patch_marks_review_when_focused_name_differs(self):
        extraction = {
            "documents": {
                "cedula": {
                    "nombres_raw": "DAYANA MARILEN",
                    "apellidos_raw": "CENTENO LIQUEZ",
                    "nombre_completo": "DAYANA MARILEN CENTENO LIQUEZ",
                    "name_review_required": False,
                    "name_review_reason": None,
                    "name_candidates": [],
                },
                "matricula": {},
            },
            "name_verification": {
                "nombres_raw": "DAYANA MARILEN",
                "apellidos_raw": "CENTENO LUQUEZ",
                "nombre_completo": "DAYANA MARILEN CENTENO LUQUEZ",
                "uncertain_characters": [],
            },
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["nombre_completo"], "Dayana Marilen Centeno Luquez")
        self.assertTrue(patch["name_review_required"])
        self.assertIn("verificacion", patch["name_review_reason"])
        self.assertGreaterEqual(len(patch["name_candidates"]), 2)

    def test_build_client_patch_marks_review_when_matricula_name_differs(self):
        extraction = {
            "documents": {
                "cedula": {
                    "nombres_raw": "DAYANA MARILEN",
                    "apellidos_raw": "CENTENO LUQUEZ",
                    "nombre_completo": "DAYANA MARILEN CENTENO LUQUEZ",
                },
                "matricula": {
                    "nombre_contribuyente": "Dayana Marilen Centeno Lopez",
                },
            }
        }

        patch = build_client_patch(extraction)

        self.assertTrue(patch["name_review_required"])
        self.assertIn("matricula", patch["name_review_reason"])

    def test_build_client_patch_does_not_rewrite_unknown_surnames(self):
        extraction = {
            "documents": {
                "cedula": {
                    "nombre_completo": "DAYANA MARILEN CENTENO LIQUEZ",
                },
                "matricula": {},
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["nombre_completo"], "Dayana Marilen Centeno Liquez")

    def test_build_client_patch_normalizes_visible_cedula_dates(self):
        extraction = {
            "documents": {
                "cedula": {
                    "nombre_completo": "DAYANA MARILEN CENTENO LUQUEZ",
                    "fecha_nacimiento": "28-01-1999",
                    "fecha_emision": "27/08/2019",
                    "fecha_expiracion": "2029-08-27",
                },
                "matricula": {},
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["nombre_completo"], "Dayana Marilen Centeno Luquez")
        self.assertEqual(patch["fecha_nacimiento"], "1999-01-28")
        self.assertEqual(patch["fecha_emision_cedula"], "2019-08-27")
        self.assertEqual(patch["fecha_expiracion_cedula"], "2029-08-27")

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

    def test_field_confidence_propagates_to_patch_keys(self):
        # F4-T1: la confianza por campo del documento llega al patch con la
        # clave del patch, y el resumen de matricula usa la peor de sus partes.
        extraction = {
            "documents": {
                "cedula": {
                    "nombre_completo": "Dayana Marilen Centeno Luquez",
                    "numero_cedula": "0012811860054R",
                    "domicilio_formal": "Municipio de Managua, Departamento de Managua",
                    "field_confidence": {
                        "nombre_completo": "alta",
                        "numero_cedula": "alta",
                        "domicilio_formal": "baja",
                    },
                },
                "matricula": {
                    "modalidad": "Cuota fija",
                    "codigo_interno": "RNVD-117331",
                    "roc": "138034303",
                    "field_confidence": {
                        "modalidad": "media",
                        "codigo_interno": "alta",
                        "roc": "baja",
                    },
                },
            }
        }

        patch = build_client_patch(extraction)

        confidence = patch["field_confidence"]
        self.assertEqual(confidence["nombre_completo"], "alta")
        self.assertEqual(confidence["cedula"], "alta")
        self.assertEqual(confidence["domicilio"], "baja")
        self.assertEqual(confidence["regimen"], "media")
        self.assertEqual(confidence["matricula"], "baja")  # peor de codigo/roc
        # Sin confianza para campos que no quedaron en el patch.
        self.assertNotIn("direccion_negocio", confidence)

    def test_patch_without_confidence_metadata_keeps_old_contract(self):
        extraction = {
            "documents": {
                "cedula": {"nombre_completo": "Cliente Sin Metadatos"},
                "matricula": {},
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["nombre_completo"], "Cliente Sin Metadatos")
        self.assertNotIn("field_confidence", patch)

    def test_cedula_confidence_falls_back_to_ruc_when_number_comes_from_matricula(self):
        extraction = {
            "documents": {
                "cedula": {},
                "matricula": {
                    "nombre_contribuyente": "Cliente Matricula",
                    "ruc": "0012811860054R",
                    "field_confidence": {"ruc": "media", "nombre_contribuyente": "alta"},
                },
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["field_confidence"]["cedula"], "media")
        self.assertEqual(patch["field_confidence"]["nombre_completo"], "alta")

    def test_matricula_summary_strips_image_label_noise(self):
        extraction = {
            "documents": {
                "cedula": {},
                "matricula": {
                    "codigo_interno": "RNVD-126096",
                    "roc": "138224421.Cedula de identidad - anverso/frente",
                    "resumen_linea": "RNVD-126096; ROC No. 138224421.Cedula de identidad - anverso/frente",
                },
            }
        }

        patch = build_client_patch(extraction)

        self.assertEqual(patch["matricula"], "RNVD-126096; ROC No. 138224421")


if __name__ == "__main__":
    unittest.main()
