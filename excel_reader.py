"""excel_reader.py
Carga las hojas necesarias desde un archivo de Excel y las expone
mediante una clase sencilla.
"""

import pandas as pd


class ExcelData:
    """Envuelve la lectura de las hojas ESF, ER, Datos y Certificacion.

    Soporta dos variantes de ESF:
      - "ESF" o "ESF_Corte": formato al corte (lado izquierdo Activos; derecho Pasivo+Patrimonio)
      - "ESF_Mensual": formato mensual (múltiples columnas para meses)

    Métodos de acceso:
      - get_situacion_financiera(tipo="auto|corte|mensual")
      - get_resultados(), get_datos(), get_certificacion()
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        # ESF variantes
        self.df_esf_corte = None
        self.df_esf_mensual = None
        # Hojas restantes
        self.df_er = None
        self.df_datos = None
        self.df_certificacion = None
        self._read_excel()

    # ------------------------------------------------------------------ #
    # API pública                                                         #
    # ------------------------------------------------------------------ #
    def get_situacion_financiera(self, tipo: str = "auto"):
        """Obtiene la hoja ESF en función del tipo.

        - tipo="corte": retorna ESF (o ESF_Corte)
        - tipo="mensual": retorna ESF_Mensual
        - tipo="auto": preferir "ESF_Corte"/"ESF" si existe; si no, usar "ESF_Mensual".
        """
        t = (tipo or "auto").lower()
        if t == "mensual":
            return self.df_esf_mensual
        if t == "corte":
            return self.df_esf_corte
        # auto
        return self.df_esf_corte or self.df_esf_mensual

    def get_resultados(self):
        return self.df_er

    def get_datos(self):
        return self.df_datos

    def get_certificacion(self):
        return self.df_certificacion

    # ------------------------------------------------------------------ #
    # Interno                                                             #
    # ------------------------------------------------------------------ #
    def _read_excel(self) -> None:
        """Lee las hojas requeridas; intenta variantes de ESF con detección robusta de nombres.

        Acepta nombres de hojas:
          - Corte: ESF_Corte, ESF, ESF Corte, ESF-Corte
          - Mensual: ESF_Mensual, ESF Mensual, ESF-Mensual, Mensual
        """
        try:
            # Inspeccionar nombres de hojas para encontrar coincidencias robustas
            xls = pd.ExcelFile(self.file_path)
            sheets = [str(s) for s in xls.sheet_names]

            def _norm(s: str) -> str:
                import unicodedata, re
                s = s.strip().lower()
                s = unicodedata.normalize("NFD", s)
                s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
                s = re.sub(r"[^a-z0-9]", "", s)
                return s

            def _find_sheet(candidates: list[str]) -> str | None:
                cands = [_norm(c) for c in candidates]
                for name in sheets:
                    n = _norm(name)
                    if n in cands:
                        return name
                return None

            corte_name = _find_sheet(["ESF_Corte", "ESF", "ESF Corte", "ESF-Corte"]) or None
            mensual_name = _find_sheet(["ESF_Mensual", "ESF Mensual", "ESF-Mensual", "Mensual"]) or None

            self.df_esf_corte = pd.read_excel(self.file_path, sheet_name=corte_name) if corte_name else None
            self.df_esf_mensual = pd.read_excel(self.file_path, sheet_name=mensual_name) if mensual_name else None

            # Otras hojas
            self.df_er = pd.read_excel(self.file_path, sheet_name="ER", header=0)
            self.df_datos = pd.read_excel(self.file_path, sheet_name="Datos")
            self.df_certificacion = pd.read_excel(self.file_path, sheet_name="Certificacion")
        except Exception as exc:
            raise RuntimeError(f"Error al leer el archivo de Excel → {exc}") from exc
