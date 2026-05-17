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
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()


