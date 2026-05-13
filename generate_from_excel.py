"""
CLI simple para generar el documento sin GUI.

Uso:
    python generate_from_excel.py --excel ruta.xlsx --out salida.docx [--plantilla plantilla.docx]
"""

from __future__ import annotations

import argparse
import sys

from excel_reader import ExcelData
from document_generator import generar_documento_completo
from validators import validate_er, validate_esf
from report_utils import build_report, save_report_json
from dotenv import load_dotenv


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Generar certificación CPA desde un Excel")
    parser.add_argument("--excel", required=True, help="Ruta al archivo Excel de entrada")
    parser.add_argument("--out", required=True, help="Ruta de salida .docx")
    parser.add_argument("--plantilla", required=False, help="Ruta plantilla SmartArt .docx")
    parser.add_argument("--strict", action="store_true", help="Detener si hay errores de validación")
    parser.add_argument("--tol", type=float, default=1.0, help="Tolerancia de validación (por defecto 1.0)")
    parser.add_argument("--cedula", help="Ruta a imagen/PDF de la cédula del cliente (visión)")
    parser.add_argument("--doc-strict", action="store_true", help="Detener si la validación de cédula por visión falla")
    parser.add_argument("--llm", action="store_true", help="Habilitar validación LLM")
    parser.add_argument("--llm-model", default="gpt-4o-mini", help="Modelo LLM a usar")
    parser.add_argument("--esf", choices=["corte", "mensual"], default="corte", help="Tipo de ESF a usar (hojas ESF_Corte/ESF_Mensual)")
    args = parser.parse_args(argv)

    data = ExcelData(args.excel)
    df_esf   = data.get_situacion_financiera(args.esf)
    df_er    = data.get_resultados()
    df_datos = data.get_datos()
    df_cert  = data.get_certificacion()

    # Validación de cédula con visión (opcional)
    validacion_documentos = None
    if args.cedula:
        from vision_validation import validate_cedula_vision
        validacion_documentos = validate_cedula_vision(
            df_cert,
            cedula_front=args.cedula,
            cedula_back=None,
            model=args.llm_model,
        )
        if args.doc_strict and not validacion_documentos.get("ok", True):
            raise SystemExit("Validación de cédula por visión fallida")

    validacion_llm = None
    if args.llm:
        from validators import validate_er, validate_esf
        from llm_validation import build_snapshot, llm_validate
        v_er = validate_er(df_er)
        v_esf = validate_esf(df_esf, mode=args.esf)
        snap = build_snapshot(df_er, df_esf, df_cert, v_er, v_esf, validacion_documentos)
        validacion_llm = llm_validate(snap, model=args.llm_model)

    generar_documento_completo(
        df_esf=df_esf,
        df_er=df_er,
        df_datos=df_datos,
        df_cert=df_cert,
        output_path=args.out,
        plantilla_path=args.plantilla,
        incluir_validacion=False,
        tolerancia_validacion=args.tol,
        detener_si_error=args.strict,
        validacion_documentos=validacion_documentos,
        validacion_llm=validacion_llm,
        esf_tipo=args.esf,
    )
    # Guardar reporte JSON junto al DOCX
    v_er = validate_er(df_er, tolerance=args.tol)
    v_esf = validate_esf(df_esf, tolerance=args.tol, mode=args.esf)
    report = build_report(v_er=v_er, v_esf=v_esf, v_docs=validacion_documentos, v_llm=validacion_llm)
    rep_path = save_report_json(report, args.out)
    print(f"Reporte de validación: {rep_path}")
    print(f"Documento generado en: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
