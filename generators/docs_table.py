# generators/docs_table.py
"""
Sección “Documentos del cliente” con tabla vacía:

• 3 columnas × 5 filas
• Anchuras: 8.5 cm – 1 cm – 8.5 cm
• Alturas : 5 cm – 1 cm – 5 cm – 1 cm – 5 cm
• Sin ningún borde
"""

from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.shared import Cm
from typing import List

from word_helpers import apply_paragraph_style, set_row_height, set_cell_border


def generar_tabla_docs_cliente(doc: Document) -> None:
    # Nueva página
    doc.add_page_break()

    # ---------- Encabezado ----------
    titulo = doc.add_paragraph("Documentos del cliente")
    apply_paragraph_style(
        titulo,
        font_name="Arial",
        font_size=12,
        bold=True,
        alignment=WD_PARAGRAPH_ALIGNMENT.CENTER,
        line_spacing=1.15,
    )
    doc.add_paragraph()  # pequeño espacio

    # ---------- Tabla vacía ----------
    table = doc.add_table(rows=5, cols=3)
    table.autofit = False

    # Anchuras de columna
    col_w = [8.5, 1.0, 8.5]  # cm
    for j, w in enumerate(col_w):
        for row in table.rows:
            row.cells[j].width = Cm(w)

    # Alturas de fila
    row_h = [5.0, 1.0, 5.0, 1.0, 5.0]  # cm
    for row, h in zip(table.rows, row_h):
        set_row_height(row, int(h * 1440 / 2.54))

    # Eliminar TODOS los bordes celda-por-celda
    no_border = {"sz": "0", "val": "nil"}
    for row in table.rows:
        for cell in row.cells:
            set_cell_border(
                cell,
                top=no_border,
                bottom=no_border,
                left=no_border,
                right=no_border,
            )


    # ── 4) Salto de página después de la tabla ────────────────────────
    doc.add_page_break()