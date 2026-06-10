"""
main.py — Lanzador de la nueva UI Web (Flask).

Este archivo ahora inicia el servidor de Flask definido en web_server.py.
"""

from __future__ import annotations

import os

from web_server import app


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("CERTAPP_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    # Por defecto solo localhost: la app no tiene autenticacion y maneja PII.
    # Para exponer a la red local: CERTAPP_HOST=0.0.0.0
    host = os.environ.get("CERTAPP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()


