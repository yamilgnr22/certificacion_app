"""Cache LRU de resultados de build_financial_model (F3-T3).

El modelo es deterministico: un payload identico (por hash estable, mismo
algoritmo que services.audit_service.stable_hash) produce siempre el mismo
resultado. Una sola instruccion del agente reconstruye el modelo 3-6 veces
(propuesta, impacto antes/despues, verificacion) y los planes multi-paso lo
hacen al menos una vez por paso, de modo que el cache corta el costo sin
cambiar semantica.

Contrato: el FinancialModelResult devuelto es COMPARTIDO entre callers.
Debe tratarse como inmutable; nunca mutar sus DataFrames ni sus dicts.
Los flujos del agente solo lo leen (serializan validations, consultan
saldos, copian vouchers con dict()).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Mapping

from financial_model import FinancialModelResult, build_financial_model

MAX_ENTRIES = 16

_lock = threading.Lock()
_cache: "OrderedDict[str, FinancialModelResult]" = OrderedDict()


def payload_hash(payload: Mapping[str, Any] | None) -> str:
    raw = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def cached_build_financial_model(payload: Mapping[str, Any]) -> FinancialModelResult:
    key = payload_hash(payload)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    result = build_financial_model(payload)
    with _lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)
    return result


def clear_model_cache() -> None:
    with _lock:
        _cache.clear()
