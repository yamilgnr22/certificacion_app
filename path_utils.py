from __future__ import annotations

import os
from glob import glob
from typing import Iterable, Optional


def _patterns(default: bool = True) -> list[str]:
    return ["*.jpg", "*.jpeg", "*.png", "*.tif", "*.tiff", "*.pdf"] if default else []


def find_first_file(directory: str, patterns: Optional[Iterable[str]] = None, pick: str = "latest") -> Optional[str]:
    """
    Busca el primer archivo en `directory` que coincida con `patterns`.
    - `pick='latest'` elige el más reciente por mtime; `pick='first'` el primero por orden alfabético.
    Retorna ruta absoluta o None si no hay coincidencias.
    """
    directory = os.path.expandvars(os.path.expanduser(directory))
    if not os.path.isabs(directory):
        directory = os.path.join(os.getcwd(), directory)
    pats = list(patterns or _patterns())
    files: list[str] = []
    for p in pats:
        files.extend(glob(os.path.join(directory, p)))
    if not files:
        return None
    if pick == "latest":
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    else:
        files.sort()
    return files[0]

