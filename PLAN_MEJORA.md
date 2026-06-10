# PLAN DE MEJORA — certificacion_app

> Hoja de ruta derivada de la auditoría técnica del 2026-06-10.
> Uso: en cada sesión de trabajo, referenciar tareas por ID (ej. "implementá F1-T1").
> Marcar `[x]` al completar. No iniciar una fase que dependa de otra incompleta.

---

## 1. Resumen ejecutivo

**Estado actual.** App Flask + SQLite que genera certificaciones de estados financieros (contexto Nicaragua). El modelo financiero ([financial_model.py](financial_model.py)) y el motor de comprobantes ([accounting_model.py](accounting_model.py)) generan ER/ESF/flujo mensual; un agente contable conversacional ([services/agent_service.py](services/agent_service.py) + mixins) traduce instrucciones en lenguaje natural a propuestas/planes auditables con verificación post-aplicación; la extracción de cédula/matrícula por visión LLM ya funciona; el DOCX final tiene calidad de uso bancario.

**Veredicto de la auditoría: ENCAMINADO, ~70% de avance.** La decisión arquitectónica clave ya está tomada correctamente: **la IA solo clasifica intención (JSON con schema, temperature=0); todos los números los calcula Python determinista y se re-verifican tras aplicar (tolerancia C$1)**. No hay que migrar a un motor determinista: hay que *endurecerlo*.

**Los 3 problemas más graves que este plan resuelve:**

1. **Capital es un residuo (plug), no una cuenta** — [financial_model.py:400](financial_model.py:400). La ecuación A = P + C se cumple *por construcción*, así que el `balance_check` no puede fallar nunca: cualquier bug de descuadre se esconde como movimiento fantasma de Capital. → Fase 1.
2. **Clamps silenciosos y motores sin conciliar** — pagos que exceden el saldo del pasivo se recortan sin alerta ([financial_model.py:289-295](financial_model.py:289)) y nadie verifica que el libro mayor (accounting_model) cuadre contra el ESF (financial_model). → Fase 1.
3. **Cero autenticación con PII expuesta** — servidor en `0.0.0.0` sin login ([web_server.py:1198](web_server.py:1198)), cédulas y datos financieros legibles desde toda la red local. → Fase 6 (con quick win inmediato QW-1).

---

## 2. Arquitectura objetivo

```
┌─────────────────────────────────────────────────────────────────────┐
│  UI (webui/)                                                        │
│  Formularios cliente/período · Editor del modelo · Chat del agente  │
│  Confirmación humana de: datos extraídos, propuestas y planes       │
└────────────┬──────────────────────────────────────┬─────────────────┘
             │ HTTP/JSON                            │
┌────────────▼─────────────┐          ┌─────────────▼─────────────────┐
│  EXTRACCIÓN POR IMAGEN   │          │  CAPA DE IA (solo lenguaje)   │
│  document_extraction.py  │          │  llm/provider.py              │
│  llm_vision.py           │          │  Intent + args JSON c/schema  │
│  → client_patch con      │          │  Explica resultados.          │
│    confianza por campo,  │          │  NUNCA calcula cifras.        │
│    SIEMPRE revisado por  │          └─────────────┬─────────────────┘
│    humano antes de DB    │                        │ restricciones
└──────────────────────────┘                        │ estructuradas
                                      ┌─────────────▼─────────────────┐
                                      │  SOLVER (services/solver/)    │
                                      │  agent_plan_builders +        │
                                      │  agent_proposal_builders      │
                                      │  + nuevo constraint_solver.py │
                                      │  Dado {metas}, encuentra      │
                                      │  ajustes; reporta IMPOSIBLE   │
                                      │  con la razón numérica.       │
                                      └─────────────┬─────────────────┘
                                                    │ payload proyectado
┌───────────────────────────────────────────────────▼─────────────────┐
│  MOTOR CONTABLE DETERMINISTA (código puro, sin IA, sin red)         │
│  financial_model.py  → estados desde parámetros                     │
│  accounting_model.py → comprobantes / mayor / trazas                │
│  accounting_accounts.py → ÚNICA fuente del catálogo y alias         │
│  validators.py + nuevo invariants.py:                               │
│    I1: A = P + C con Capital TRANSACCIONAL (no residual)            │
│    I2: utilidad del ER == Resultados del Ejercicio del ESF          │
│    I3: saldo final del mayor == fila del ESF (cuenta×mes)           │
│    I4: ningún pasivo/activo operativo clampeado en silencio         │
│    I5: todo comprobante balanceado (debe == haber)                  │
│  Cualquier violación = error visible, nunca absorción silenciosa.   │
└───────────────────────────────────────────────────┬─────────────────┘
                                                    │ resultado validado
                                      ┌─────────────▼─────────────────┐
                                      │  GENERACIÓN DOCX              │
                                      │  document_generator.py        │
                                      │  generators/ (datos del CPA   │
                                      │  desde config, no hardcode)   │
                                      └───────────────────────────────┘
```

**Módulos que se MANTIENEN tal cual:** `db/` (modelos + migraciones), `repositories/`, `services/audit_service.py`, `services/periodo_service.py`, flujo de propuestas/planes de `agent_service.py` (hash optimista, expiración, estados, atomicidad), `document_extraction.py` (núcleo), `generators/` (estructura).

**Módulos que se REFACTORIZAN:** `financial_model.py` (capital transaccional, clamps→warnings, cobranza configurable), `accounting_model.py` y `financial_model.py` (des-triplicar alias importando de `accounting_accounts.py`), `validators.py` (suma I2-I3), `web_server.py` (bind/auth), `generators/certificacion.py` (datos CPA a config), `webui/static/app.js` (partición gradual).

**Módulos que se CREAN:** `invariants.py` (raíz), `services/solver/constraint_solver.py`, `config_cpa.py` (o tabla en DB), `tests/test_invariants.py`, `tests/test_solver.py`.

**Módulos que se ELIMINAN (cuando el contador legacy llegue a 0):** `model_chat.py`, `chat_controller.py`, endpoints `/api/model/chat/*`, `tests/test_chat_controller.py`, `tests/test_chat_legacy_snapshot.py`.

---

## 3. Plan por fases

### FASE 1 — Fundamentos: invariantes reales en el motor contable
*Objetivo: que la ecuación contable no PUEDA romperse sin que la app lo grite. Es prerequisito de todo lo demás: el solver y la IA confían en estas validaciones.*

- [x] **F1-T1 — Capital transaccional + check del residuo** · **CRÍTICO · M** *(hecho 2026-06-10; suite completa verde: 197 passed. El test del descuadre usa el clamp de sobrepago — al hacer F1-T2 cambiarlo a monkeypatch, ver nota en CapitalInvariantTest)*
  - Qué: en el loop mensual de `build_financial_model`, acumular `capital_transactional = capital_apertura + aportes − retiros ± reclasificaciones (± delta de asientos del chat contra capital)`. Mantener el cálculo residual actual, pero agregar a `validations` un bloque `capital`: `{ok, errors:[{month, residual, transactional, diff}]}` cuando `|residual − transactional| > 1.0`. Dejar de ignorar asientos contra capital en `_apply_journal_side` ([financial_model.py:1328](financial_model.py:1328)): registrarlos en el acumulador transaccional.
  - Archivos: `financial_model.py` (líneas ~183-400, ~1328), `tests/test_financial_model.py`.
  - Aceptación: test nuevo que inyecta un descuadre artificial (p.ej. duplicar una salida de caja vía monkeypatch o evento inválido) y verifica que `validations["capital"]["ok"] == False` con el mes y monto correctos. Todos los tests existentes siguen verdes (modelos sanos no disparan el check).
  - Riesgo si no: descuadres reales invisibles para siempre; el resto del plan construye sobre arena.

- [x] **F1-T2 — Eliminar clamps silenciosos en pasivos** · **CRÍTICO · S** *(hecho 2026-06-10; helper `_apply_liability_payment` recorta al disponible del mes y reporta en `validations["overpayments"]`. El test de F1-T1 pasó a usar `credit_card_new` sin contrapartida como descuadre inyectado. Suite: 199 passed)*
  - Qué: en [financial_model.py:289-295](financial_model.py:289) (credit_cards, suppliers, taxes_payable, accrued_expenses, y el loop de loans en ~294), cuando `pago > saldo disponible`: recortar el pago efectivo al saldo (afectando también la salida de caja, no solo el pasivo) y registrar warning en `validations["overpayments"]` con `{month, account, requested, applied}`.
  - Archivos: `financial_model.py`, `tests/test_financial_model.py`.
  - Aceptación: test con `credit_card_payment` mayor al saldo → el pasivo queda en 0, la caja solo baja por el monto aplicado, y aparece el warning. El `balance_check` y el check de F1-T1 quedan en 0.
  - Riesgo si no: la fuente más probable de los descuadres que F1-T1 va a empezar a detectar.

- [x] **F1-T3 — Conciliación mayor↔ESF (invariante I3)** · **CRÍTICO · M** *(hecho 2026-06-10; `invariants.validate_ledger_vs_esf` integrado como `validations["ledger_esf"]`. Bonus: se detectaron y corrigieron 2 bugs — faltaba el voucher de venta de activos, y el running balance del mayor se corrompía con vouchers guardados fuera de orden cronológico. Suite: 203 passed)*
  - Qué: nueva función `validate_ledger_vs_esf(accounting, df_esf_full, months, tolerance=1.0)` en un nuevo módulo `invariants.py` (raíz): para cada cuenta de balance del mayor (`accounting["trace"]`), comparar `closing_balance` del último mes contra la fila correspondiente del ESF, por mes. Integrarla en `build_financial_model` → `validations["ledger_esf"]`. Mapear cuentas con `accounting_accounts.LEDGER_ACCOUNT_LABELS`.
  - Archivos: `invariants.py` (nuevo), `financial_model.py` (integración), `tests/test_invariants.py` (nuevo).
  - Aceptación: test que construye un modelo estándar y verifica `validations["ledger_esf"]["ok"] == True`; test que agrega un voucher guardado desbalanceado a mano al payload y verifica que el check lo detecta con cuenta y mes.
  - Riesgo si no: los dos motores (estado vs. mayor) pueden divergir tras cualquier refactor sin que ningún test lo note.

- [x] **F1-T4 — Unificar el catálogo de cuentas (una sola fuente)** · **IMPORTANTE · M** *(hecho 2026-06-10; `financial_model._normalize_ledger_account` y `accounting_model._account_label` ahora importan de `accounting_accounts`. Suite: 199 passed)*
  - Qué: `accounting_accounts.py` pasa a ser la única fuente. Eliminar el dict inline duplicado de `_normalize_ledger_account` ([financial_model.py:1362-1395](financial_model.py:1362)) y el de `_account_label` ([accounting_model.py:627-661](accounting_model.py:627)); ambos importan `LEDGER_ACCOUNT_ALIASES` / `LEDGER_ACCOUNT_LABELS` / `normalize_account`.
  - Archivos: `financial_model.py`, `accounting_model.py`, `accounting_accounts.py`.
  - Aceptación: `grep` de "cuentas por cobrar clientes" devuelve UNA sola definición en código (fuera de tests). Suite completa verde (especialmente `test_financial_model.py` y `test_agent_api.py`).
  - Riesgo si no: un alias agregado en un dict y no en los otros produce cuentas mal normalizadas que F1-T3 reportará como falsos positivos.

- [x] **F1-T5 — Invariante ER↔ESF explícito (I2)** · **IMPORTANTE · S** *(hecho 2026-06-10; `invariants.validate_er_vs_esf` integrado como `validations["er_esf"]`. Suite: 206 passed)*
  - Qué: en `invariants.py`, check de que la utilidad acumulada del ER (fila "Ingresos/Utilidad Neta") coincide con `result_accum` mostrado en el ESF del último mes (± ajustes de asientos a `current_earnings`, que ya están en `result_accum_adjustment`). Integrar a `validations["er_esf"]`.
  - Archivos: `invariants.py`, `financial_model.py`, `tests/test_invariants.py`.
  - Aceptación: test con asiento del chat contra `current_earnings` → el check sigue OK (considera el ajuste); test con corrupción manual del payload → falla con monto.
  - Riesgo si no: la coherencia ER↔BG queda implícita "por construcción", igual que estaba el capital.

- [ ] **F1-T6 — Cobranza configurable** · **DESEABLE · S**
  - Qué: parámetro `collections_pct` en `payload["movements"]` (default 1.0 = comportamiento actual) aplicado en [financial_model.py:273](financial_model.py:273). Exponer el campo en el formulario del período.
  - Archivos: `financial_model.py`, `webui/static/app.js`, `webui/templates/index.html`, `tests/test_financial_model.py`.
  - Aceptación: con `collections_pct=0.5`, la cartera crece mes a mes y el flujo de caja refleja la cobranza parcial; balance y F1-T1/T3 en verde.
  - Riesgo si no: cartera siempre artificial (se cobra el 100% cada mes), poco realista para certificaciones multi-mes.

### FASE 2 — Solver de restricciones determinista
*Depende de Fase 1 (el solver necesita invariantes confiables para validar sus soluciones). Construye SOBRE las recetas existentes en `agent_plan_builders.py` — no las reemplaza.*

- [ ] **F2-T1 — Extraer núcleo del solver a `services/solver/constraint_solver.py`** · **IMPORTANTE · M**
  - Qué: crear el paquete `services/solver/` con una API pura: `solve(payload, constraints: list[Constraint]) -> SolveResult`. `Constraint` = dataclass `{kind: "target"|"average"|"floor"|"utility", account, months, amount, currency, counter_account, variability_pct}`. `SolveResult` = `{feasible: bool, steps: [...], infeasible_reason: str|None, aggregate_impact}`. Internamente delega en la lógica ya probada de `_simulate_target_plan`, `compute_target_distribution` y `_build_target_utility_plan` (mover, no duplicar; los mixins pasan a llamar al solver).
  - Archivos: `services/solver/__init__.py`, `services/solver/constraint_solver.py` (nuevos), `services/agent_plan_builders.py` (delega), `tests/test_solver.py` (nuevo).
  - Aceptación: `test_solver.py` ejercita el solver SIN tocar Flask ni LLM (payload de fixture → constraints → verifica saldos resultantes con `build_financial_model`). `test_agent_api.py` completo sigue verde.
  - Riesgo si no: la lógica del solver queda soldada a los mixins del agente; imposible testearla o extenderla aislada.

- [ ] **F2-T2 — Diagnóstico de infactibilidad con números** · **IMPORTANTE · M**
  - Qué: cuando una meta no se puede cumplir (hoy: errores genéricos como "delta demasiado alto" en [agent_plan_builders.py:190](services/agent_plan_builders.py:190)), `SolveResult.infeasible_reason` debe explicar el conflicto con cifras: meta pedida, valor máximo/mínimo alcanzable, y qué restricción lo limita (p.ej. "llevar caja a C$5,000 en marzo requiere retirar C$120,000 pero la contrapartida capital quedaría en C$−15,000"). El agente lo muestra tal cual.
  - Archivos: `services/solver/constraint_solver.py`, `services/agent_plan_builders.py`, `tests/test_solver.py`.
  - Aceptación: test con meta imposible (inventario negativo requerido) → `feasible=False` y `infeasible_reason` contiene los tres números.
  - Riesgo si no: el usuario recibe "no se pudo" sin saber si reformular la meta o cambiar la contrapartida.

- [ ] **F2-T3 — Metas combinadas en una instrucción** · **IMPORTANTE · L**
  - Qué: soportar listas de constraints heterogéneas en un solo `solve()` (hoy el prompt obliga "un objetivo por vez", regla A en [agent_helpers.py:87](services/agent_helpers.py:87)). Estrategia: resolver secuencialmente en orden de dependencia (utilidad → balances no-caja → caja al final, porque caja absorbe contrapartidas), re-simulando tras cada constraint y verificando que las anteriores sigan cumplidas; si una re-rompe otra, reportar infactible con el par en conflicto. Relajar la regla A del prompt para mapear multi-objetivo a `compound_constraints`.
  - Archivos: `services/solver/constraint_solver.py`, `services/agent_helpers.py` (prompt), `services/agent_service.py` (intent nuevo o extensión de `plan_multi_account_target_balance`), `tests/test_solver.py`, `tests/test_agent_api.py`.
  - Aceptación: instrucción "caja promedio 5,000 USD y que inventario cierre en 100,000 USD en junio" produce UN plan multi-paso aplicable; tras aplicar, ambas metas verificadas dentro de tolerancia.
  - Riesgo si no: el flujo principal de tu visión (varias metas en una frase) sigue requiriendo N mensajes.

- [ ] **F2-T4 — Ampliar cuentas objetivo** · **DESEABLE · M**
  - Qué: extender `TARGET_BALANCE_ACCOUNTS` en [services/agent_constants.py](services/agent_constants.py) a tarjetas, préstamos y gastos acumulados, con `TARGET_COUNTER_DEFAULTS` coherentes (y manteniendo contrapartida obligatoria para caja).
  - Archivos: `services/agent_constants.py`, `services/agent_helpers.py` (prompt), `tests/test_agent_api.py`.
  - Aceptación: "que tarjetas cierre en C$50,000 en abril" genera propuesta válida con verificación post-apply en verde.
  - Riesgo si no: el agente rechaza metas legítimas sobre la mitad del balance.

### FASE 3 — Capa de IA: robustez de interpretación
*Depende de F2-T1 (la IA emite constraints hacia el solver). El patrón base ya es correcto; esto es endurecimiento.*

- [ ] **F3-T1 — Migrar reglas de ruteo del mega-prompt a few-shot estructurado** · **IMPORTANTE · M**
  - Qué: el system prompt de [agent_helpers.py:45-91](services/agent_helpers.py:45) creció a un bloque de reglas frágil. Reorganizarlo: (a) lista compacta de intents con schema, (b) 10-15 ejemplos `usuario → JSON esperado` cubriendo los casos ambiguos documentados (oscile vs. piso, multi-objetivo, referenciales "el primero/dale"), (c) snapshot-test del prompt para detectar regresiones accidentales.
  - Archivos: `services/agent_helpers.py`, `tests/test_agent_api.py` (los tests con provider fake ya cubren el contrato; agregar casos de ruteo con fakes).
  - Aceptación: suite verde + prueba manual de las 6 frases ambiguas del prompt actual contra el LLM real (documentar resultados en el PR).
  - Riesgo si no: cada intent nuevo degrada el ruteo de los existentes sin forma de notarlo.

- [ ] **F3-T2 — Reintento estructurado ante JSON inválido del LLM** · **IMPORTANTE · S**
  - Qué: en `OpenAIProvider.complete_json` ([llm/provider.py:34](llm/provider.py:34)), ante `LLMProviderError` por JSON inválido o schema incumplido, un (1) reintento agregando el error como mensaje. Registrar el reintento en la respuesta (`audit.llm_retries`).
  - Archivos: `llm/provider.py`, `services/agent_service.py`.
  - Aceptación: test unitario con provider fake que falla una vez y acierta la segunda → el comando se completa y `llm_retries == 1`.
  - Riesgo si no: fallos transitorios del proveedor se vuelven errores de usuario.

- [ ] **F3-T3 — Cache de `build_financial_model` por hash de payload** · **IMPORTANTE · M**
  - Qué: el modelo se reconstruye 3-6 veces por propuesta y N veces por plan (un build por mes en `_build_non_negative_account_plan`, [agent_plan_builders.py:121-164](services/agent_plan_builders.py:121)). Crear `model_cache.py` con LRU (~16 entradas) keyed por `stable_hash(payload)` (ya existe en `services/audit_service.py`). Usarlo en `agent_service`, plan builders y proposal builders. Invalidación: no hace falta — el payload es inmutable por valor; cada proyección tiene hash distinto.
  - Archivos: `model_cache.py` (nuevo), `services/agent_service.py`, `services/agent_plan_builders.py`, `services/agent_proposal_builders.py`, `tests/test_agent_api.py`.
  - Aceptación: test que cuenta invocaciones (monkeypatch sobre `build_financial_model`) en un `handle_command` de propuesta: las llamadas con el mismo hash se resuelven de cache. Tiempo de un plan de 12 meses medido antes/después en el PR.
  - Riesgo si no: planes sobre períodos de 24-36 meses tardan decenas de segundos y chocan con `MAX_TURN_DURATION_S = 30`.

- [ ] **F3-T4 — Retirar el chat legacy** · **IMPORTANTE · M**
  - Qué: verificar `legacy_call_counters` en la DB; si los endpoints `/api/model/chat/*` tienen uso ~0, eliminar `model_chat.py`, `chat_controller.py`, sus endpoints en [web_server.py:948-981](web_server.py:948), sus tests (`test_chat_controller.py`, `test_chat_legacy_snapshot.py`) y el código del UI que los llama.
  - Archivos: eliminar los citados; `web_server.py`, `webui/static/app.js`.
  - Aceptación: suite verde, `grep -r "model_chat\|chat_controller"` sin resultados fuera de git history, la UI del asistente funciona end-to-end.
  - Riesgo si no: ~2,500 líneas muertas-vivas que confunden cada sesión de trabajo futura y duplican lógica contable.

### FASE 4 — Extracción por imagen: confirmación humana de primera clase
*Independiente de F2/F3; puede intercalarse. El núcleo ya funciona.*

- [ ] **F4-T1 — Confianza por campo en la extracción** · **IMPORTANTE · M**
  - Qué: ampliar el JSON schema de `_document_schema()` en [document_extraction.py](document_extraction.py) para que cada campo devuelva `{value, confidence: "alta"|"media"|"baja", visible: bool}`. Propagar a `client_patch` como `{value, confidence}`.
  - Archivos: `document_extraction.py`, `tests/test_document_extraction.py`.
  - Aceptación: tests existentes adaptados + prueba manual con las imágenes de `input_docs/cedula/` mostrando confidencias coherentes.
  - Riesgo si no: el usuario no sabe qué campo revisar con cuidado; un dígito de cédula mal leído llega al documento legal.

- [ ] **F4-T2 — UI de confirmación campo por campo** · **IMPORTANTE · M**
  - Qué: tras extraer, el formulario de cliente marca cada campo autocompletado con badge de origen ("extraído — confianza baja/media/alta") y exige un click "Confirmar datos extraídos" antes de habilitar guardar. Campos de confianza baja resaltados.
  - Archivos: `webui/static/app.js` (sección "Extraccion de documentos"), `webui/static/styles.css`, `webui/templates/index.html`.
  - Aceptación: manual — subir cédula, verificar badges, verificar que guardar está bloqueado hasta confirmar.
  - Riesgo si no: la "confirmación humana" de la visión queda implícita y salteable.

- [ ] **F4-T3 — Persistir el JSON extraído como documento soporte** · **DESEABLE · S**
  - Qué: al confirmar, guardar imagen + `extracted_json` en la tabla `documentos_soporte` (ya existe en [db/models.py:113](db/models.py:113), hoy subutilizada), vía `ClienteService`.
  - Archivos: `services/cliente_service.py`, `web_server.py` (endpoint extract ya existente), `tests/test_cliente_extension.py`.
  - Aceptación: tras confirmar una extracción, `GET /api/clientes/<id>` lista el documento con su JSON.
  - Riesgo si no: sin evidencia de qué se extrajo y cuándo, ante cualquier disputa.

### FASE 5 — Documento de certificación
*Independiente; requiere F1 solo para confiar en las cifras.*

- [ ] **F5-T1 — Datos del CPA a configuración** · **IMPORTANTE · S**
  - Qué: extraer nombre, cédula, No. CPA, acuerdo/quinquenio, teléfono y email hardcodeados en [generators/certificacion.py:99-155](generators/certificacion.py:99) a un `config_cpa.py` (dataclass cargada de `cpa_profile.json` con default actual) o a una tabla `cpa_profile`. El generador recibe el perfil como parámetro.
  - Archivos: `config_cpa.py` + `cpa_profile.json` (nuevos), `generators/certificacion.py`, `document_generator.py`.
  - Aceptación: cambiar el teléfono en el JSON y regenerar → el DOCX refleja el cambio sin tocar código.
  - Riesgo si no: la renovación del quinquenio (2028) o un cambio de contacto exige editar código en 6 lugares.

- [ ] **F5-T2 — Test E2E del documento generado** · **IMPORTANTE · M**
  - Qué: test que genera un DOCX desde un payload fixture, lo reabre con `python-docx` y verifica: (a) los montos en texto coinciden con `result.summary` (ingresos brutos, utilidad, promedios), (b) nombre y cédula del cliente presentes, (c) las tablas ER/ESF tienen las filas de totales correctas.
  - Archivos: `tests/test_document_e2e.py` (nuevo).
  - Aceptación: el test corre en la suite y falla si se rompe el mapeo cifras→texto.
  - Riesgo si no: única pieza sin red de seguridad automática; un bug de formato se descubre en el banco.

- [ ] **F5-T3 — Fallback de fuentes** · **DESEABLE · S**
  - Qué: "Abadi"/"Abadi Extra Light" solo renderizan si están instaladas. Definir las fuentes en `config_cpa.py` con fallback (Calibri) y documentar el requisito en README.
  - Archivos: `config_cpa.py`, `generators/certificacion.py`, `word_helpers.py`, `README.md`.
  - Aceptación: manual — abrir el DOCX en una máquina sin Abadi y verificar presentación digna.
  - Riesgo si no: documento con sustitución de fuentes fea en máquinas ajenas.

### FASE 6 — Seguridad y pulido
*QW-1 (bind localhost) se hace YA como quick win; el resto aquí.*

- [ ] **F6-T1 — Autenticación mínima** · **CRÍTICO · M**
  - Qué: token compartido en `.env` (`CERTAPP_AUTH_TOKEN`); middleware `before_request` que exige `Authorization: Bearer <token>` para `/api/*` (excepto estáticos); pantalla de login simple que lo guarda en `localStorage`; el header `X-CPA-User` pasa a derivarse de la sesión autenticada, no del cliente.
  - Archivos: `web_server.py`, `webui/static/app.js`, `webui/templates/index.html`, `.env.example` (nuevo).
  - Aceptación: request sin token → 401; con token → 200; el audit log registra el usuario de la sesión.
  - Riesgo si no: PII de terceros (cédulas) accesible a cualquiera en la red; audit log falsificable.

- [ ] **F6-T2 — Crear `.env.example` y documentar manejo de la key** · **IMPORTANTE · S**
  - Qué: `.env.example` con todas las vars (`OPENAI_API_KEY`, `OPENAI_MODEL_AGENT`, `OPENAI_MODEL_DOCUMENTS`, `CERTAPP_HOST`, `CERTAPP_AUTH_TOKEN`, `POPPLER_PATH`, etc.) y sección en README. Confirmado en auditoría: `.env` nunca estuvo en git — mantenerlo así.
  - Archivos: `.env.example` (nuevo), `README.md`.
  - Aceptación: clonar en limpio + copiar `.env.example` → `.env` + poner key → la app arranca.
  - Riesgo si no: onboarding a otra máquina por adivinación; tentación de commitear el `.env` real.

- [ ] **F6-T3 — Aviso de envío de PII a OpenAI** · **IMPORTANTE · S**
  - Qué: texto visible en la UI de extracción: "Las imágenes se procesan con OpenAI (servidor externo)". Documentar en README la política de datos (las imágenes no se persisten en disco más allá del TTL; F4-T3 cambia eso con consentimiento).
  - Archivos: `webui/templates/index.html`, `README.md`.
  - Aceptación: manual — el aviso es visible antes de subir imágenes.
  - Riesgo si no: responsabilidad profesional ante el cliente final por tratamiento de datos no informado.

- [ ] **F6-T4 — Límite y limpieza de `JOBS` en memoria** · **DESEABLE · S**
  - Qué: cap de tamaño (p.ej. 50 jobs) en el dict `JOBS` de [web_server.py:85](web_server.py:85) con evicción del más viejo en `prune_uploads()`.
  - Archivos: `web_server.py`.
  - Aceptación: test o verificación manual de que el job 51 desaloja al más antiguo.
  - Riesgo si no: memoria creciente en sesiones largas (menor, pero gratis de arreglar).

- [ ] **F6-T5 — Partir `app.js` en módulos ES** · **DESEABLE · L**
  - Qué: dividir [webui/static/app.js](webui/static/app.js) (4,360 líneas) siguiendo su propio TOC: `api.js`, `clientes.js`, `periodos.js`, `asistente.js`, `modelo.js`, `documentos.js`, con `<script type="module">`. Sin framework ni build. Hacerlo en 2-3 PRs por sección, no de un golpe.
  - Archivos: `webui/static/*.js`, `webui/templates/index.html`.
  - Aceptación: cada PR deja la UI completamente funcional (smoke test manual del flujo afectado).
  - Riesgo si no: cada cambio de UI es arqueología; riesgo creciente de regresiones.

---

## 4. Quick wins (menos de 1 hora cada uno, hacer YA)

- [x] **QW-1 — Bind a localhost por defecto** *(hecho 2026-06-10)*: en [web_server.py:1198](web_server.py:1198) y también en [main.py](main.py) (tenía su propio `app.run` con `0.0.0.0`), `host = os.environ.get("CERTAPP_HOST", "127.0.0.1")`. Verificado: servidor responde HTTP 200 en 127.0.0.1:8000.
- [x] **QW-2 — Logging de excepciones tragadas en impactos** *(hecho 2026-06-10)*: `logger.warning(..., exc_info=True)` en `_compute_impact` y `_compute_assumption_impact` ([services/agent_service.py](services/agent_service.py)) y en `_compute_plan_aggregate_impact` ([services/agent_plan_builders.py](services/agent_plan_builders.py), mismo patrón). Suite del agente verde (62 passed).
- [x] **QW-3 — `.env.example`** *(hecho 2026-06-10)*: creado con todas las vars detectadas por grep (`OPENAI_*`, `CERTAPP_*`, `POPPLER_PATH`, `PORT`, `LLM_PROVIDER`) y defaults verificados contra el código.
- [x] **QW-4 — Borrar logs de servidor de la raíz** *(hecho 2026-06-10)*: inspeccionados (solo access logs, sin PII) y borrados. Hallazgo: confirmaban escucha en `0.0.0.0` con IP pública 200.201.222.166 asignada a la máquina — QW-1 era aún más urgente de lo estimado.
- [x] **QW-5 — Limpiar `FlujoValidacion.ipynb.bak`** *(hecho 2026-06-10)*: borrado; el notebook vigente está en `notebooks/`.

---

## 5. Qué NO tocar (funciona bien; preservar en los refactors)

1. **El contrato propose→simulate→verify→apply del agente** ([services/agent_service.py](services/agent_service.py)): hash optimista del payload, expiración de propuestas/planes, estados (`pending/applied/stale/expired/discarded/failed`), aplicación atómica con rollback y falla honesta (`failed_step_order` + `failure_reason`). Es la columna vertebral correcta.
2. **La separación LLM-clasifica / Python-calcula**: el LLM nunca produce cifras finales y la verificación post-aplicación con tolerancia C$1 (`_verify_target_balance_after_apply`, `_verify_plan_step_against_result`). Cualquier feature nueva del agente DEBE seguir este patrón.
3. **AuditLog encadenado** ([db/models.py:127](db/models.py:127)) y `AuditService` con before/after.
4. **Migraciones Alembic** (`db/migrations/`): seguir el patrón incremental; nunca editar migraciones aplicadas.
5. **La suite de tests del agente** (`tests/test_agent_api.py`, 1,778 líneas con provider fake): es el arnés que permite todo lo demás. Solo extender.
6. **El `.gitignore`**: ya cubre `.env`, DBs, uploads, documentos y logs. Verificado limpio en historial.
7. **El flujo de `compute_target_distribution`** ([services/agent_tools.py:408](services/agent_tools.py:408)): seed determinístico + normalización a promedio exacto + clamp de variabilidad. Moverlo al solver (F2-T1) sin cambiar su semántica.
8. **El texto legal del DOCX** ([generators/certificacion.py](generators/certificacion.py)): el contenido y formato están validados en uso real; F5-T1 solo parametriza datos, no redacción.

---

## 6. Orden de ejecución recomendado

Pensado para que el proyecto quede funcional entre tarea y tarea (cada ID = una sesión o menos):

```
Sesión 0 (hoy):    QW-1 → QW-4 → QW-5 → QW-3 → QW-2
Sesiones 1-2:      F1-T1  (capital transaccional — el corazón del plan)
Sesión  3:         F1-T2  (clamps → warnings; F1-T1 ya los detecta)
Sesión  4:         F1-T4  (unificar catálogo ANTES de conciliar, evita falsos positivos)
Sesiones 5-6:      F1-T3  (conciliación mayor↔ESF)
Sesión  7:         F1-T5  (invariante ER↔ESF)
Sesión  8:         F3-T3  (cache del modelo — habilita planes largos antes del solver)
Sesiones 9-10:     F2-T1  (extraer solver puro)
Sesión  11:        F2-T2  (infactibilidad con números)
Sesiones 12-14:    F2-T3  (metas combinadas — la feature estrella de la visión)
Sesión  15:        F3-T2  (reintento LLM)
Sesiones 16-17:    F3-T1  (prompt few-shot)
Sesión  18:        F3-T4  (retirar legacy — revisar contador antes)
Sesión  19:        F4-T1  (confianza por campo)
Sesión  20:        F4-T2  (UI de confirmación)
Sesión  21:        F5-T1 + F5-T3 (config CPA + fuentes)
Sesión  22:        F5-T2  (E2E del documento)
Sesiones 23-24:    F6-T1  (auth) → F6-T3 (aviso PII) → F6-T2 (cerrar docs)
Backlog flexible:  F1-T6, F2-T4, F4-T3, F6-T4, F6-T5 (intercalar cuando convenga)
```

**Regla de oro entre sesiones:** después de cada tarea, suite de tests completa en verde (`python -m pytest tests -q`) + smoke test manual del flujo tocado. Si una tarea no cabe en la sesión, se parte en sub-PRs que dejen todo funcionando — nunca se mergea a `main` un estado intermedio roto.
