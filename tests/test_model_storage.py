from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import model_storage


def sample_payload():
    return {
        "client": {
            "nombre_completo": "Cliente Prueba",
            "cedula": "001-010101-0000A",
            "banco": "BAC",
        },
        "period": {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "storage-test",
        },
        "movements": {
            "events": [
                {"month": "2026-02", "account": "owner_withdrawal", "amount": 1000, "currency": "nio"}
            ]
        },
    }


class ModelStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = model_storage.MODEL_STORE_DIR
        model_storage.MODEL_STORE_DIR = Path(self.tmp.name) / "models"

    def tearDown(self):
        model_storage.MODEL_STORE_DIR = self.old_dir
        self.tmp.cleanup()

    def test_save_and_load_draft_preserves_payload(self):
        payload = sample_payload()
        record = model_storage.save_draft(payload)
        loaded = model_storage.get_draft(record["id"])

        self.assertEqual(loaded["payload"], payload)
        self.assertEqual(loaded["status"], "draft")
        self.assertEqual(loaded["client_name"], "Cliente Prueba")

    def test_update_draft_replaces_same_record(self):
        payload = sample_payload()
        record = model_storage.save_draft(payload)
        updated_payload = sample_payload()
        updated_payload["period"]["end_month"] = "2026-12"
        updated = model_storage.save_draft(updated_payload, draft_id=record["id"])
        drafts = model_storage.list_drafts()

        self.assertEqual(updated["id"], record["id"])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(model_storage.get_draft(record["id"])["payload"], updated_payload)

    def test_save_final_copies_document_and_is_listed(self):
        payload = sample_payload()
        source_doc = Path(self.tmp.name) / "source.docx"
        source_doc.write_bytes(b"docx")

        final = model_storage.save_final(
            payload,
            result_json={
                "summary": {"income_total": 1},
                "full_summary": {"months": ["2025-01"]},
                "period_blocks": [{"id": "full_range"}],
                "validations": {"balance": {"ok": True}},
            },
            document_path=str(source_doc),
            filename="certificacion.docx",
        )
        listed = model_storage.list_finals()
        doc_path, filename = model_storage.final_document_path(final["id"])
        loaded = model_storage.get_final(final["id"])

        self.assertEqual(len(listed), 1)
        self.assertEqual(filename, "certificacion.docx")
        self.assertTrue(doc_path.exists())
        self.assertEqual(loaded["payload"], payload)
        self.assertEqual(loaded["summary"], {"income_total": 1})

    def test_duplicate_final_creates_editable_draft(self):
        payload = sample_payload()
        source_doc = Path(self.tmp.name) / "source.docx"
        source_doc.write_bytes(b"docx")
        final = model_storage.save_final(
            payload,
            result_json={},
            document_path=str(source_doc),
            filename="certificacion.docx",
        )
        draft = model_storage.duplicate_final(final["id"])

        self.assertEqual(draft["status"], "draft")
        self.assertNotEqual(draft["id"], final["id"])
        self.assertEqual(model_storage.get_draft(draft["id"])["payload"], payload)

    def test_delete_draft_does_not_delete_final(self):
        payload = sample_payload()
        draft = model_storage.save_draft(payload)
        source_doc = Path(self.tmp.name) / "source.docx"
        source_doc.write_bytes(b"docx")
        final = model_storage.save_final(payload, result_json={}, document_path=str(source_doc), filename="final.docx")

        model_storage.delete_draft(draft["id"])

        self.assertEqual(model_storage.list_drafts(), [])
        self.assertEqual(model_storage.get_final(final["id"])["id"], final["id"])


if __name__ == "__main__":
    unittest.main()
