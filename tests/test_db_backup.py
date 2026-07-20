"""Tests del respaldo automatico de la DB (db/backup.py).

Best-effort: copia consistente via la API de backup de sqlite3, no-op para
:memory: (tests) y retencion de los ultimos RETENCION archivos.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine

from db.backup import backup_sqlite


class BackupSqliteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "certificacion_app.db"
        con = sqlite3.connect(str(self.db_path))
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (42)")
        con.commit()
        con.close()
        self.engine = create_engine(f"sqlite:///{self.db_path}", future=True)
        self.addCleanup(self.engine.dispose)

    def test_crea_copia_con_los_datos(self):
        dest = backup_sqlite(self.engine, motivo="pre_finalizar")
        self.assertIsNotNone(dest)
        self.assertTrue(dest.exists())
        self.assertEqual(dest.parent, self.db_path.parent / "backups")
        self.assertIn("pre_finalizar", dest.name)
        con = sqlite3.connect(str(dest))
        self.assertEqual(con.execute("SELECT x FROM t").fetchone(), (42,))
        con.close()

    def test_memoria_es_noop(self):
        eng = create_engine("sqlite:///:memory:", future=True)
        self.assertIsNone(backup_sqlite(eng))
        eng.dispose()

    def test_override_de_carpeta_por_env(self):
        otra = Path(self.tmp.name) / "otra_carpeta"
        os.environ["CERTAPP_BACKUPS_DIR"] = str(otra)
        self.addCleanup(os.environ.pop, "CERTAPP_BACKUPS_DIR", None)
        dest = backup_sqlite(self.engine)
        self.assertEqual(dest.parent, otra)

    def test_retencion_conserva_los_ultimos(self):
        from db import backup as mod

        out_dir = self.db_path.parent / "backups"
        out_dir.mkdir()
        # Simular respaldos viejos con mtimes crecientes
        for i in range(mod.RETENCION + 5):
            p = out_dir / f"certificacion_app_viejo{i:02d}.db"
            p.write_bytes(b"x")
            os.utime(p, (1000 + i, 1000 + i))
        dest = backup_sqlite(self.engine)
        respaldos = list(out_dir.glob("certificacion_app_*.db"))
        self.assertEqual(len(respaldos), mod.RETENCION)
        self.assertIn(dest, respaldos)
        # Los mas viejos se fueron
        self.assertFalse((out_dir / "certificacion_app_viejo00.db").exists())

    def test_nunca_lanza_si_algo_falla(self):
        class EngineRoto:
            @property
            def url(self):
                raise RuntimeError("boom")

        self.assertIsNone(backup_sqlite(EngineRoto()))


if __name__ == "__main__":
    unittest.main()
