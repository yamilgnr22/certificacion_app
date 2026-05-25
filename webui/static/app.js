(() => {
  const qs = (sel) => document.querySelector(sel);
  const qsa = (sel) => Array.from(document.querySelectorAll(sel));

  let currentToken = null;
  let es = null; // EventSource
  const lastResults = { er: null, esf: null, docs_cedula: null, docs_matricula: null, llm: null };

  const steps = ["excel", "er", "esf", "docs_cedula", "docs_matricula", "llm", "done"];
  const previewUrls = new Map();

  let lastUploadOpts = null; // recuerda opciones del último token

  function resetStepper() {
    steps.forEach((s) => setStepState(s, null));
    setStepState("excel", "running");
    qs('#messages').innerHTML = '';
    clearResults();
  }

  function setStepState(step, state) {
    const el = qs(`.step[data-step="${step}"]`);
    if (!el) return;
    el.classList.remove('running', 'done', 'error');
    if (state) el.classList.add(state);
  }

  function addMsg(text, type = 'info') {
    if (type !== 'error') return; // Solo mostrar errores
    const wrap = qs('#messages');
    const div = document.createElement('div');
    div.className = `msg ${type}`;
    div.textContent = text;
    wrap.appendChild(div);
  }

  function showER(res) {
    renderValidationCard('#res-er', {
      key: 'er',
      title: 'ER',
      full: 'Estado de Resultados',
      icon: '<svg class="result-icon" viewBox="0 0 24 24"><path fill="currentColor" d="M3 4h18v2H3V4zm0 4h10v2H3V8zm0 4h14v2H3v-2zm0 4h8v2H3v-2z"/></svg>',
      ok: !!(res && res.ok),
      summary: res ? `${res.ok ? 'Sin errores' : `${(res.errors||[]).length} errores`}` : '—',
      items: buildErItems(res)
    });
  }

  function showESF(res) {
    renderValidationCard('#res-esf', {
      key: 'esf',
      title: 'ESF',
      full: 'Estado de Situación Financiera',
      icon: '<svg class="result-icon" viewBox="0 0 24 24"><path fill="currentColor" d="M4 4h7v7H4V4zm9 0h7v7h-7V4zM4 13h7v7H4v-7zm9 3h7v4h-7v-4z"/></svg>',
      ok: !!(res && res.ok),
      summary: res ? `${res.ok ? 'Sin errores' : `${(res.errors||[]).length} errores`}` : '—',
      items: buildEsfItems(res)
    });
  }

  function showDocs(id, res) {
    const countBad = res && Array.isArray(res.checks) ? res.checks.filter(c => !c.ok).length : 0;
    const iconCed = '<svg class="result-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5zm2 3h14v10H5V8zm3.8 1.8a2.3 2.3 0 1 0 0 4.6a2.3 2.3 0 0 0 0-4.6zM12 11h6v1.5h-6V11zm0 3h4.5v1.5H12V14z"/></svg>';
    const iconMat = '<svg class="result-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 3l9 5v2H3V8l9-5zm-7 7h14v9h-3v-6h-3v6H9v-6H6v6H5v-9z"/></svg>';
    renderValidationCard(`#res-${id}`, {
      key: id,
      title: id === 'docs-cedula' ? 'Cédula' : 'Matrícula',
      full: id === 'docs-cedula' ? 'Documentos — Cédula' : 'Documentos — Matrícula',
      icon: id === 'docs-cedula' ? iconCed : iconMat,
      ok: !!(res && res.ok),
      summary: res ? (res.ok ? 'Campos verificados' : `${countBad} diferencias`) : '—',
      items: buildDocsItems(res)
    });
  }

  function showLLM(res) {
    const ok = res && !(res.ok === false || (Array.isArray(res.issues) && res.issues.length));
    const count = res && Array.isArray(res.issues) ? res.issues.length : 0;
    renderValidationCard('#res-llm', {
      key: 'llm',
      title: 'LLM',
      full: 'Validación LLM',
      icon: '<svg class="result-icon" viewBox="0 0 24 24"><path fill="currentColor" d="M12 2a7 7 0 0 1 7 7c0 1.88-.72 3.59-1.9 4.86l2.17 3.76-1.73 1L15.5 15a7 7 0 1 1-3.5-13z"/></svg>',
      ok: !!ok,
      summary: ok ? 'Sin observaciones' : `${count} observaciones`,
      items: buildLlmItems(res)
    });
  }

  function buildErItems(res) {
    if (!res) return [];
    const items = [];
    (res.errors || []).forEach(e => items.push({ text: e, bad: true }));
    (res.checks || []).filter(c => !c.ok).forEach(c => {
      const t = `${c.rule || 'Regla'}${c.column ? ` [mes ${c.column}]` : ''} · esperado=${c.expected ?? ''} · calculado=${c.computed ?? ''}`;
      items.push({ text: t, bad: true });
    });
    return items;
  }

  function buildEsfItems(res) {
    if (!res) return [];
    const items = [];
    (res.errors || []).forEach(e => items.push({ text: e, bad: true }));
    (res.checks || []).filter(c => c.ok === false).forEach(c => {
      const t = `${c.rule || 'Regla'}${c.column ? ` [mes ${c.column}]` : ''} · esperado=${c.expected ?? ''} · calculado=${c.computed ?? ''}`;
      items.push({ text: t, bad: true });
    });
    return items;
  }

  function buildDocsItems(res) {
    if (!res) return [];
    const items = [];
    (res.checks || []).forEach(c => {
      const t = `${c.doc || ''} ${c.field || ''}: '${c.got ?? ''}' vs '${c.expected ?? ''}'`;
      items.push({ text: t, bad: !c.ok, ok: !!c.ok });
    });
    return items;
  }

  function formatEvidence(evidence) {
    if (!evidence) return '';
    if (typeof evidence === 'string') return evidence;
    try { return JSON.stringify(evidence); } catch { return String(evidence); }
  }

  function buildLlmItems(res) {
    if (!res || !Array.isArray(res.issues)) return [];
    return res.issues.map(it => ({
      text: `[${it.severity}] ${it.description} · ${formatEvidence(it.evidence)}`,
      bad: true
    }));
  }

  function renderValidationCard(containerSel, cfg) {
    const wrap = qs(containerSel);
    if (!wrap) return;
    const parent = wrap.closest('.result-group');
    if (parent) {
      const h = parent.querySelector('h3');
      if (h) {
        const titleInner = cfg.titleHtml || `<span class="abbr" title="${cfg.full || cfg.title}">${cfg.title}</span>`;
        h.innerHTML = `<span class="result-title">${cfg.icon || ''}${titleInner}</span>` +
                      `<span class="badge ${cfg.ok ? 'ok' : 'bad'}">${cfg.ok ? 'OK' : 'Revisar'}</span>`;
      }
    }
    const items = Array.isArray(cfg.items) ? cfg.items : [];
    const first = items.slice(0, 6);
    const rest = items.slice(6);
    const listHtml = first.map(it => `<li class="${it.bad ? 'bad' : (it.ok ? 'ok' : '')}">${truncate(it.text, 180)}</li>`).join('');
    const moreHtml = rest.map(it => `<li class="${it.bad ? 'bad' : (it.ok ? 'ok' : '')}">${truncate(it.text, 180)}</li>`).join('');

    wrap.innerHTML = `
      <div class="section-body">
        <div class="summary">${cfg.summary || ''}
          ${items.length > 6 ? '<button class="toggle-btn" data-toggle>Ver detalles</button>' : ''}
        </div>
        <div class="details${items.length > 6 ? '' : ' open'}">
          <ul class="list">${listHtml}${moreHtml ? `<span class="more" hidden></span>` : ''}</ul>
        </div>
      </div>`;

    if (items.length > 6) {
      const btn = wrap.querySelector('[data-toggle]');
      const details = wrap.querySelector('.details');
      if (btn && details) {
        let opened = false;
        btn.addEventListener('click', () => {
          opened = !opened;
          details.classList.toggle('open', opened);
          // Render full list when open
          if (opened && wrap.querySelector('.more')) {
            wrap.querySelector('.list').insertAdjacentHTML('beforeend', moreHtml);
            wrap.querySelector('.more').remove();
          }
          btn.textContent = opened ? 'Ocultar' : 'Ver detalles';
        });
      }
    }
  }

  function clearResults() {
    qs('#res-er').innerHTML = '';
    qs('#res-esf').innerHTML = '';
    qs('#res-docs-cedula').innerHTML = '';
    qs('#res-docs-matricula').innerHTML = '';
    qs('#res-llm').innerHTML = '';
    lastResults.er = lastResults.esf = lastResults.docs_cedula = lastResults.docs_matricula = lastResults.llm = null;
  }

  function formatSize(bytes) {
    if (!Number.isFinite(bytes)) return '';
    const mb = bytes / (1024 * 1024);
    if (mb >= 1) return `${mb.toFixed(2)} MB`;
    const kb = bytes / 1024;
    return `${Math.max(1, Math.round(kb))} KB`;
  }

  function setPreviewEmpty(el) {
    if (!el) return;
    el.classList.add('empty');
    el.innerHTML = '<span class="meta">Sin archivo</span>';
  }

  function revokePrevUrl(key) {
    const url = previewUrls.get(key);
    if (url) {
      try { URL.revokeObjectURL(url); } catch {}
      previewUrls.delete(key);
    }
  }

  function previewFile(inputSel, previewSel, key, dzSel) {
    const input = qs(inputSel);
    const preview = qs(previewSel);
    if (!input || !preview) return;
    const file = input.files && input.files[0];
    if (!file) {
      revokePrevUrl(key);
      setPreviewEmpty(preview);
      // si no hay archivo, mostrar de nuevo la dropzone
      const dz = dzSel ? qs(dzSel) : null;
      if (dz) dz.classList.remove('hidden');
      return;
    }
    preview.classList.remove('empty');
    const name = (file.name || '').toLowerCase();
    const isImg = /^image\//.test(file.type) || /\.(png|jpe?g|bmp|tiff?)$/.test(name);
    const isPdf = file.type === 'application/pdf' || /\.pdf$/.test(name);
    const isXls = /\.xlsx?$/.test(name) || file.type === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' || file.type === 'application/vnd.ms-excel';
    const meta = `<div class="meta">${file.name} · ${formatSize(file.size)}</div>`;
    // ocultar dropzone
    const dz = dzSel ? qs(dzSel) : null;
    if (dz) dz.classList.add('hidden');
    if (isImg) {
      revokePrevUrl(key);
      const url = URL.createObjectURL(file);
      previewUrls.set(key, url);
      preview.innerHTML = `<img class="thumb" src="${url}" alt="preview">${meta}`;
    } else if (isPdf) {
      revokePrevUrl(key);
      preview.innerHTML = `<div class="pdf-chip">PDF</div>${meta}`;
    } else if (isXls) {
      revokePrevUrl(key);
      const label = name.endsWith('.xls') ? 'XLS' : 'XLSX';
      preview.innerHTML = `<div class="excel-chip">${label}</div>${meta}`;
    } else {
      preview.innerHTML = meta;
    }
  }

  function truncate(text, max = 48) {
    if (!text) return '';
    return text.length > max ? text.slice(0, max - 3) + '...' : text;
  }

  function wireDropzone(inputSel, dzSel, previewSel, key) {
    const input = qs(inputSel);
    const dz = qs(dzSel);
    if (!input || !dz) return;
    const textEl = dz.querySelector('.dz-text');
    const hintEl = dz.querySelector('.dz-hint');

    const update = () => {
      const f = input.files && input.files[0];
      if (textEl) textEl.innerHTML = f ? truncate(f.name) : 'Arrastra y suelta aquí o <span class="u">haz clic</span>';
      if (hintEl) hintEl.textContent = f ? formatSize(f.size) : (hintEl.getAttribute('data-default') || hintEl.textContent);
      previewFile(inputSel, previewSel, key, dzSel);
    };

    if (hintEl) hintEl.setAttribute('data-default', hintEl.textContent || '');

    dz.addEventListener('click', () => input.click());
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault(); dz.classList.remove('dragover');
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) {
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        input.dispatchEvent(new Event('change'));
      }
    });
    input.addEventListener('change', update);
  }

  async function uploadForToken() {
    const excel = qs('#excel').files[0];
    if (!excel) throw new Error('Seleccione primero un Excel');
    const cedulaFront = qs('#cedula_front').files[0];
    const cedulaBack = qs('#cedula_back').files[0];
    const matricula = qs('#matricula').files[0];
    const esfTipo = (document.querySelector('input[name="esf_tipo"]:checked')?.value || 'corte');
    const allowErrors = !!qs('#allow_errors')?.checked;
    const form = new FormData();
    form.append('excel', excel);
    if (cedulaFront) form.append('cedula_front', cedulaFront);
    if (cedulaBack) form.append('cedula_back', cedulaBack);
    if (matricula) form.append('matricula', matricula);
    form.append('esf_tipo', esfTipo);
    // Validaciones siempre activas por defecto
    // Si el usuario permite errores, entonces NO estricto
    form.append('strict_contable', allowErrors ? 'false' : 'true');
    form.append('strict_docs', 'true');
    form.append('use_llm', 'true');
    form.append('tolerancia', '1');

    const resp = await fetch('/api/upload', { method: 'POST', body: form });
    if (!resp.ok) throw new Error('Error subiendo archivos');
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'Error en upload');
    lastUploadOpts = { esf_tipo: esfTipo, strict_contable: !allowErrors };
    return data.token;
  }

  function startStream(token) {
    if (es) { es.close(); es = null; }
    es = new EventSource(`/api/validate/stream?token=${encodeURIComponent(token)}`);
    es.onmessage = () => {};
    es.addEventListener('status', (ev) => {
      // Mensajes verbosos ocultos; solo actualizamos stepper
      setStepState('excel', 'done');
      if (genBtn) genBtn.disabled = true;
    });
    es.addEventListener('progress', (ev) => {
      try {
        const d = JSON.parse(ev.data);
        setStepState(d.step, 'running');
      } catch {}
    });
    es.addEventListener('result', (ev) => {
      try {
        const d = JSON.parse(ev.data);
        setStepState(d.step, 'done');
        if (d.step === 'er') { lastResults.er = d.result; showER(d.result); }
        if (d.step === 'esf') { lastResults.esf = d.result; showESF(d.result); }
        if (d.step === 'docs_cedula') { lastResults.docs_cedula = d.result; showDocs('docs-cedula', d.result); }
        if (d.step === 'docs_matricula') { lastResults.docs_matricula = d.result; showDocs('docs-matricula', d.result); }
        if (d.step === 'llm') { lastResults.llm = d.result; showLLM(d.result); }
      } catch {}
    });
    es.addEventListener('done', (ev) => {
      setStepState('done', 'done');
      // Ocultamos mensajes informativos finales
      es.close(); es = null;
      if (genBtn) genBtn.disabled = false;
    });
    es.addEventListener('error', (ev) => {
      setStepState('done', 'error');
      try { const d = JSON.parse(ev.data); addMsg(`${d.type}: ${d.message}`, 'error'); } catch { addMsg('Error en validación', 'error'); }
      es.close(); es = null;
      if (genBtn) genBtn.disabled = false; // permite reintentar generación si se desea
    });
  }

  async function onValidate() {
    try {
      resetStepper();
      currentToken = await uploadForToken();
      startStream(currentToken);
    } catch (e) {
      addMsg(String(e.message || e), 'error');
      setStepState('done', 'error');
    }
  }

  async function onGenerate() {
    try {
      const esfTipo = (document.querySelector('input[name="esf_tipo"]:checked')?.value || 'corte');
      const allowErrors = !!qs('#allow_errors')?.checked;
      const desired = { esf_tipo: esfTipo, strict_contable: !allowErrors };
      const needNewUpload = !currentToken || !lastUploadOpts || (lastUploadOpts.esf_tipo !== desired.esf_tipo) || (lastUploadOpts.strict_contable !== desired.strict_contable);
      if (needNewUpload) {
        addMsg('Subiendo archivos para generar...');
        currentToken = await uploadForToken();
      }
      // Si ya validamos y hay fallos en documentos, confirmar
      const c = lastResults.docs_cedula;
      const m = lastResults.docs_matricula;
      const docsOk = (c ? !!c.ok : true) && (m ? !!m.ok : true);
      if (c || m) {
        if (!docsOk) {
          const proceed = confirm('La validación de documentos presenta fallos. ¿Desea continuar y generar el documento igualmente?');
          if (!proceed) return;
        }
      }
      const resp = await fetch(`/api/generate?token=${encodeURIComponent(currentToken)}`);
      if (!resp.ok) {
        // Intentar leer el error real del backend (JSON)
        let detail = 'Error generando el documento';
        try {
          const data = await resp.json();
          if (data && data.error) detail = data.error;
        } catch (_) { /* respuesta no es JSON */ }
        throw new Error(detail);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `certificacion_${currentToken.slice(0,8)}.docx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      // No se muestra mensaje detallado; descarga directa
    } catch (e) {
      addMsg(String(e.message || e), 'error');
    }
  }

  const genBtn = qs('#btnGenerarFinal');
  qs('#btnValidar').addEventListener('click', onValidate);
  genBtn?.addEventListener('click', onGenerate);

  // Inicializar previsualizaciones
  setPreviewEmpty(qs('#prev-excel'));
  setPreviewEmpty(qs('#prev-cedula-front'));
  setPreviewEmpty(qs('#prev-cedula-back'));
  setPreviewEmpty(qs('#prev-matricula'));
  qs('#excel')?.addEventListener('change', () => previewFile('#excel', '#prev-excel', 'excel', '#dz-excel'));
  qs('#cedula_front')?.addEventListener('change', () => previewFile('#cedula_front', '#prev-cedula-front', 'cedula_front', '#dz-ced-front'));
  qs('#cedula_back')?.addEventListener('change', () => previewFile('#cedula_back', '#prev-cedula-back', 'cedula_back', '#dz-ced-back'));
  qs('#matricula')?.addEventListener('change', () => previewFile('#matricula', '#prev-matricula', 'matricula', '#dz-matricula'));

  // Drag & drop zones
  wireDropzone('#excel', '#dz-excel', '#prev-excel', 'excel');
  wireDropzone('#cedula_front', '#dz-ced-front', '#prev-cedula-front', 'cedula_front');
  wireDropzone('#cedula_back', '#dz-ced-back', '#prev-cedula-back', 'cedula_back');
  wireDropzone('#matricula', '#dz-matricula', '#prev-matricula', 'matricula');

  // Click en previsualización para cambiar archivo
  qsa('.preview.clickable').forEach((el) => {
    const inputSel = el.getAttribute('data-input');
    if (!inputSel) return;
    el.addEventListener('click', () => {
      const input = qs(inputSel);
      if (input) input.click();
    });
  });

  let lastModelPayload = null;
  let lastModelPreviewData = null;
  let selectedModelBlockId = '';
  let lastModelRenderedEsf = false;
  let lastAccountMovementPreview = null;
  let pendingChatPayload = null;
  let pendingChatData = null;
  let pendingDocExtraction = null;
  let currentDraftId = null;
  let savedModelsCache = { drafts: [], finals: [] };
  let modelMonthlyOverrides = [];
  let modelJournalEntries = [];
  let modelAccountingVouchers = [];
  let modelDynamicAccounts = [];
  let modelChatCommands = [];
  let selectedChatVoucherId = '';
  let clienteGiros = [];
  let clientesCache = [];
  let clientesLoadedOnce = false;
  let currentClienteDetail = null;
  let currentClienteId = null;
  let currentClienteOriginalCedula = '';
  let selectedClienteId = '';
  let selectedClienteName = '';
  let selectedGiroId = '';
  let clientesSearchTimer = null;

  function setModelMessage(text, type = 'info') {
    const wrap = qs('#modelMessages');
    if (!wrap) return;
    wrap.innerHTML = '';
    if (!text) return;
    const div = document.createElement('div');
    div.className = `msg ${type}`;
    div.textContent = text;
    wrap.appendChild(div);
  }

  function setDocExtractMessage(text, type = 'info') {
    const wrap = qs('#modelDocExtractMessage');
    if (!wrap) return;
    wrap.innerHTML = '';
    if (!text) return;
    const div = document.createElement('div');
    div.className = `msg ${type}`;
    div.textContent = text;
    wrap.appendChild(div);
  }

  function setScopedMessage(selector, text, type = 'info') {
    const wrap = qs(selector);
    if (!wrap) return;
    wrap.innerHTML = '';
    if (!text) return;
    const div = document.createElement('div');
    div.className = `msg ${type}`;
    div.textContent = text;
    wrap.appendChild(div);
  }

  function setClientesMessage(text, type = 'info') {
    setScopedMessage('#clientesMessages', text, type);
  }

  function setClienteFormMessage(text, type = 'info') {
    setScopedMessage('#clienteFormMessages', text, type);
  }

  async function fetchJson(url, options = {}) {
    const resp = await fetch(url, options);
    let data = {};
    try { data = await resp.json(); } catch {}
    if (!resp.ok || data.ok === false) {
      const err = new Error(data.error || `Error HTTP ${resp.status}`);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function clearDocExtraction() {
    pendingDocExtraction = null;
    const wrap = qs('#modelDocExtractResult');
    if (wrap) {
      wrap.classList.add('hidden');
      wrap.replaceChildren();
    }
  }

  function docFieldLabel(key) {
    const labels = {
      nombre_completo: 'Nombre completo',
      cedula: 'Cedula',
      sexo: 'Sexo',
      domicilio: 'Domicilio',
      direccion_personal: 'Direccion personal',
      regimen: 'Regimen',
      matricula: 'Matricula/ROC',
      direccion_negocio: 'Direccion negocio',
      giro_negocio: 'Giro del negocio',
    };
    return labels[key] || key;
  }

  function renderDocExtraction(data) {
    const wrap = qs('#modelDocExtractResult');
    if (!wrap) return;
    wrap.replaceChildren();
    wrap.classList.remove('hidden');
    const patch = data?.client_patch || {};
    const title = document.createElement('h3');
    title.textContent = 'Datos extraidos';
    wrap.appendChild(title);

    const fields = document.createElement('div');
    fields.className = 'doc-extract-fields';
    Object.entries(patch).forEach(([key, value]) => {
      const box = document.createElement('div');
      box.className = 'doc-extract-field';
      const label = document.createElement('span');
      label.textContent = docFieldLabel(key);
      const strong = document.createElement('strong');
      strong.textContent = String(value || '');
      box.append(label, strong);
      fields.appendChild(box);
    });
    if (!Object.keys(patch).length) {
      const empty = document.createElement('div');
      empty.className = 'doc-extract-field';
      empty.textContent = 'No se extrajeron campos aplicables al formulario.';
      fields.appendChild(empty);
    }
    wrap.appendChild(fields);

    const actions = document.createElement('div');
    actions.className = 'doc-extract-actions';
    const apply = document.createElement('button');
    apply.className = 'btn primary';
    apply.type = 'button';
    apply.textContent = 'Aplicar al formulario';
    const discard = document.createElement('button');
    discard.className = 'btn';
    discard.type = 'button';
    discard.textContent = 'Descartar';
    apply.addEventListener('click', applyDocExtraction);
    discard.addEventListener('click', () => {
      clearDocExtraction();
      setDocExtractMessage('Extraccion descartada.', 'info');
    });
    actions.append(apply, discard);
    wrap.appendChild(actions);
  }

  function setFieldValue(id, value) {
    if (value === undefined || value === null || value === '') return;
    const el = qs(`#${id}`);
    if (!el) {
      console.warn(`Campo destino no encontrado: ${id}`);
      return;
    }
    el.value = String(value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function fillSelect(select, items, { emptyLabel = '', valueKey = 'id', labelKey = 'nombre' } = {}) {
    if (!select) return;
    const current = select.value;
    select.replaceChildren();
    if (emptyLabel) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = emptyLabel;
      select.appendChild(opt);
    }
    (items || []).forEach(item => {
      const opt = document.createElement('option');
      opt.value = String(item[valueKey] || '');
      opt.textContent = String(item[labelKey] || item[valueKey] || '');
      select.appendChild(opt);
    });
    if ([...select.options].some(opt => opt.value === current)) select.value = current;
  }

  function clienteFieldValue(id) {
    const el = qs(`#${id}`);
    return el ? el.value.trim() : '';
  }

  function setClienteField(id, value) {
    const el = qs(`#${id}`);
    if (!el || value === undefined || value === null || value === '') return;
    el.value = String(value);
  }

  function markClienteInvalid(fields = []) {
    qsa('#clienteFormPanel .field').forEach(field => {
      field.classList.remove('invalid');
      const err = field.querySelector('.field-error');
      if (err) err.remove();
    });
    fields.forEach(({ key, message }) => {
      const field = qs(`#clienteFormPanel [data-client-field="${key}"]`);
      if (!field) return;
      field.classList.add('invalid');
      const err = document.createElement('div');
      err.className = 'field-error';
      err.textContent = message;
      field.appendChild(err);
    });
  }

  async function loadGiros() {
    const data = await fetchJson('/api/giros');
    clienteGiros = data.giros || [];
    fillSelect(qs('#clientesGiroFilter'), clienteGiros, { emptyLabel: 'Todos' });
    fillSelect(qs('#c_giro_negocio_id'), clienteGiros, { emptyLabel: 'Seleccione giro' });
    return clienteGiros;
  }

  async function loadClientes() {
    const params = new URLSearchParams();
    const q = clienteFieldValue('clientesSearch');
    const giro = clienteFieldValue('clientesGiroFilter');
    if (q) params.set('q', q);
    if (giro) params.set('giro', giro);
    const url = `/api/clientes${params.toString() ? `?${params}` : ''}`;
    const data = await fetchJson(url);
    clientesCache = data.clientes || [];
    clientesLoadedOnce = true;
    renderClientes();
  }

  async function refreshClientes() {
    try {
      setClientesMessage('Cargando clientes...', 'info');
      await loadGiros();
      await loadClientes();
      setClientesMessage('', 'info');
    } catch (e) {
      renderClientes([]);
      setClientesMessage(String(e.message || e), 'error');
    }
  }

  function renderClientes(records = clientesCache) {
    const wrap = qs('#clientesList');
    if (!wrap) return;
    wrap.replaceChildren();
    if (!records.length) {
      wrap.className = 'client-list empty-state';
      wrap.textContent = clientesLoadedOnce ? 'Sin clientes para mostrar.' : 'Sin clientes cargados.';
      return;
    }
    wrap.className = 'client-list';
    records.forEach(cliente => {
      const item = document.createElement('div');
      item.className = 'client-item';
      const info = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = cliente.nombre_completo || 'Cliente sin nombre';
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = [
        cliente.cedula,
        cliente.nombre_negocio,
        cliente.giro?.nombre || cliente.giro_negocio_id,
        cliente.updated_at ? `Actualizado ${String(cliente.updated_at).slice(0, 10)}` : '',
      ].filter(Boolean).join(' · ');
      info.append(title, meta);
      const actions = document.createElement('div');
      actions.className = 'client-actions';
      const open = document.createElement('button');
      open.className = 'btn';
      open.type = 'button';
      open.textContent = 'Abrir';
      open.addEventListener('click', () => loadClienteDetail(cliente.id, { openForm: true, readonly: true }));
      const edit = document.createElement('button');
      edit.className = 'btn';
      edit.type = 'button';
      edit.textContent = 'Editar';
      edit.addEventListener('click', () => loadClienteDetail(cliente.id, { openForm: true }));
      const use = document.createElement('button');
      use.className = 'btn primary';
      use.type = 'button';
      use.textContent = 'Usar en modelo';
      use.addEventListener('click', () => loadClienteDetail(cliente.id, { useInModel: true }));
      actions.append(open, edit, use);
      item.append(info, actions);
      wrap.appendChild(item);
    });
  }

  function clearClienteForm() {
    currentClienteDetail = null;
    currentClienteId = null;
    currentClienteOriginalCedula = '';
    ['c_nombre_completo', 'c_cedula', 'c_telefono', 'c_email', 'c_nombre_negocio', 'c_ruc',
      'c_matricula_roc', 'c_direccion_domicilio', 'c_direccion_negocio', 'c_fecha_nacimiento',
      'c_fecha_inicio_negocio', 'c_giro_negocio_id',
      'c_sexo', 'c_estado_civil', 'c_profesion', 'c_banco', 'c_regimen',
      'c_antiguedad', 'c_empleados', 'c_domicilio'].forEach(id => {
      const el = qs(`#${id}`);
      if (el) el.value = '';
    });
    ['c_doc_cedula_front', 'c_doc_cedula_back', 'c_doc_matricula'].forEach(id => {
      const el = qs(`#${id}`);
      if (el) el.value = '';
    });
    markClienteInvalid([]);
    setClienteFormMessage('', 'info');
    renderClienteTemplate(null);
    renderClientePeriodos([]);
    updateClienteFormUiState();
  }

  function updateClienteFormUiState() {
    const hasClient = !!currentClienteId;
    const periodos = currentClienteDetail?.periodos || [];
    const hasPeriodos = periodos.length > 0;

    const setStep = (el, state) => {
      if (!el) return;
      el.classList.remove('active', 'completed', 'locked');
      if (state) el.classList.add(state);
    };
    setStep(qs('#clienteStepper .step[data-step="cliente"]'), hasClient ? 'completed' : 'active');
    if (!hasClient) {
      setStep(qs('#clienteStepper .step[data-step="periodos"]'), 'locked');
      setStep(qs('#clienteStepper .step[data-step="plantilla"]'), 'locked');
      setStep(qs('#clienteStepper .step[data-step="editor"]'), 'locked');
    } else if (!hasPeriodos) {
      setStep(qs('#clienteStepper .step[data-step="periodos"]'), 'active');
      setStep(qs('#clienteStepper .step[data-step="plantilla"]'), null);
      setStep(qs('#clienteStepper .step[data-step="editor"]'), 'locked');
    } else {
      setStep(qs('#clienteStepper .step[data-step="periodos"]'), 'completed');
      setStep(qs('#clienteStepper .step[data-step="plantilla"]'), null);
      setStep(qs('#clienteStepper .step[data-step="editor"]'), 'active');
    }

    const btnSave = qs('#btnSaveCliente');
    if (btnSave) btnSave.textContent = hasClient ? 'Guardar cambios' : 'Guardar cliente';
    qs('#btnUseClienteInModel')?.classList.toggle('hidden', !hasClient);
    qs('#btnDeleteCliente')?.classList.toggle('hidden', !hasClient);

    const templatePanel = qs('#clienteTemplatePanel');
    const periodosPanel = qs('#clientePeriodosPanel');
    if (templatePanel) {
      templatePanel.classList.toggle('locked', !hasClient);
      templatePanel.classList.toggle('unlocked', hasClient);
    }
    if (periodosPanel) {
      periodosPanel.classList.toggle('locked', !hasClient);
      periodosPanel.classList.toggle('unlocked', hasClient);
    }
  }

  function openClienteForm({ mode = 'new', detail = null } = {}) {
    clearClienteForm();
    const panel = qs('#clienteFormPanel');
    if (panel) panel.classList.remove('hidden');
    qs('#clienteFormTitle').textContent = mode === 'new' ? 'Nuevo cliente' : 'Ficha del cliente';
    if (detail) fillClienteForm(detail);
    panel?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function fillClienteForm(detail) {
    currentClienteDetail = detail;
    const cliente = detail.cliente || detail;
    currentClienteId = cliente.id || null;
    currentClienteOriginalCedula = cliente.cedula || '';
    const mapping = {
      c_nombre_completo: cliente.nombre_completo,
      c_cedula: cliente.cedula,
      c_telefono: cliente.telefono,
      c_email: cliente.email,
      c_nombre_negocio: cliente.nombre_negocio,
      c_ruc: cliente.ruc,
      c_matricula_roc: cliente.matricula_roc,
      c_direccion_domicilio: cliente.direccion_domicilio,
      c_direccion_negocio: cliente.direccion_negocio,
      c_fecha_nacimiento: cliente.fecha_nacimiento ? String(cliente.fecha_nacimiento).slice(0, 10) : '',
      c_fecha_inicio_negocio: cliente.fecha_inicio_negocio ? String(cliente.fecha_inicio_negocio).slice(0, 10) : '',
      c_giro_negocio_id: cliente.giro_negocio_id,
      // Campos de certificacion
      c_sexo: cliente.sexo || '',
      c_estado_civil: cliente.estado_civil,
      c_profesion: cliente.profesion,
      c_banco: cliente.banco,
      c_regimen: cliente.regimen,
      c_antiguedad: cliente.antiguedad,
      c_empleados: cliente.empleados,
      c_domicilio: cliente.domicilio,
    };
    Object.entries(mapping).forEach(([id, value]) => setClienteField(id, value));
    renderClienteTemplate(detail.plantilla_gastos || null);
    renderClientePeriodos(detail.periodos || []);
    updateClienteFormUiState();
  }

  function collectClientePayload() {
    return {
      nombre_completo: clienteFieldValue('c_nombre_completo'),
      cedula: clienteFieldValue('c_cedula'),
      telefono: clienteFieldValue('c_telefono'),
      email: clienteFieldValue('c_email'),
      nombre_negocio: clienteFieldValue('c_nombre_negocio'),
      ruc: clienteFieldValue('c_ruc'),
      matricula_roc: clienteFieldValue('c_matricula_roc'),
      direccion_domicilio: clienteFieldValue('c_direccion_domicilio'),
      direccion_negocio: clienteFieldValue('c_direccion_negocio'),
      fecha_nacimiento: clienteFieldValue('c_fecha_nacimiento'),
      fecha_inicio_negocio: clienteFieldValue('c_fecha_inicio_negocio'),
      giro_negocio_id: clienteFieldValue('c_giro_negocio_id'),
      // Campos de certificacion
      sexo: clienteFieldValue('c_sexo'),
      estado_civil: clienteFieldValue('c_estado_civil'),
      profesion: clienteFieldValue('c_profesion'),
      banco: clienteFieldValue('c_banco'),
      regimen: clienteFieldValue('c_regimen'),
      antiguedad: clienteFieldValue('c_antiguedad'),
      empleados: clienteFieldValue('c_empleados'),
      domicilio: clienteFieldValue('c_domicilio'),
    };
  }

  function validateClientePayload(payload) {
    const required = [
      ['nombre_completo', 'Nombre completo es requerido.'],
      ['cedula', 'Cedula es requerida.'],
      ['nombre_negocio', 'Nombre del negocio es requerido.'],
      ['direccion_negocio', 'Direccion del negocio es requerida.'],
      ['giro_negocio_id', 'Seleccione un giro.'],
    ];
    const errors = required.filter(([key]) => !payload[key]).map(([key, message]) => ({ key, message }));
    markClienteInvalid(errors);
    return errors;
  }

  async function saveCliente() {
    const payload = collectClientePayload();
    const errors = validateClientePayload(payload);
    if (errors.length) {
      setClienteFormMessage('Revise los campos requeridos.', 'error');
      return;
    }
    if (currentClienteId && payload.cedula !== currentClienteOriginalCedula) {
      const ok = window.confirm('Cambiar la cedula afecta los datos legales del cliente. ¿Continuar?');
      if (!ok) return;
    }
    try {
      const wasNew = !currentClienteId;
      setClienteFormMessage('Guardando cliente...', 'info');
      const url = currentClienteId ? `/api/clientes/${encodeURIComponent(currentClienteId)}` : '/api/clientes';
      const method = currentClienteId ? 'PUT' : 'POST';
      const data = await fetchJson(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const cliente = data.cliente;
      currentClienteId = cliente.id;
      currentClienteOriginalCedula = cliente.cedula;
      await loadClientes();
      await loadClienteDetail(cliente.id, { openForm: true });
      setClienteFormMessage(wasNew ? 'Cliente creado.' : 'Cliente guardado.', 'success');
    } catch (e) {
      const msg = String(e.message || e);
      if (/cedula/i.test(msg)) markClienteInvalid([{ key: 'cedula', message: msg }]);
      if (/giro/i.test(msg)) markClienteInvalid([{ key: 'giro_negocio_id', message: msg }]);
      setClienteFormMessage(msg, 'error');
    }
  }

  async function loadClienteDetail(clienteId, { openForm = false, useInModel = false } = {}) {
    try {
      const data = await fetchJson(`/api/clientes/${encodeURIComponent(clienteId)}`);
      currentClienteDetail = data;
      if (openForm) openClienteForm({ mode: 'edit', detail: data });
      if (useInModel) useClienteInModel(data);
      return data;
    } catch (e) {
      setClientesMessage(String(e.message || e), 'error');
      return null;
    }
  }

  async function deleteCliente() {
    if (!currentClienteId || !currentClienteDetail?.cliente) return;
    const name = currentClienteDetail.cliente.nombre_completo || 'este cliente';
    const ok = window.confirm(`Esto desactivara el cliente ${name}. Sus periodos historicos permanecen accesibles.`);
    if (!ok) return;
    try {
      await fetchJson(`/api/clientes/${encodeURIComponent(currentClienteId)}`, { method: 'DELETE' });
      setClientesMessage('Cliente desactivado.', 'success');
      qs('#clienteFormPanel')?.classList.add('hidden');
      clearClienteForm();
      await loadClientes();
    } catch (e) {
      setClienteFormMessage(String(e.message || e), 'error');
    }
  }

  function applyClientePatchToForm(patch) {
    const mapping = {
      nombre_completo: 'c_nombre_completo',
      cedula: 'c_cedula',
      direccion_personal: 'c_direccion_domicilio',
      direccion_negocio: 'c_direccion_negocio',
      matricula: 'c_matricula_roc',
      // Campos de certificacion extraidos por vision IA
      sexo: 'c_sexo',
      estado_civil: 'c_estado_civil',
      profesion: 'c_profesion',
      domicilio: 'c_domicilio',
    };
    Object.entries(mapping).forEach(([key, id]) => {
      let value = patch[key];
      // Normalizar sexo a las opciones del select
      if (key === 'sexo' && typeof value === 'string') {
        const n = value.trim().toLowerCase();
        if (n.startsWith('f')) value = 'Femenino';
        else if (n.startsWith('m')) value = 'Masculino';
        else if (n) value = 'Otro';
      }
      setClienteField(id, value);
    });
  }

  async function extractClienteDocs() {
    const front = qs('#c_doc_cedula_front')?.files?.[0];
    const back = qs('#c_doc_cedula_back')?.files?.[0];
    const matricula = qs('#c_doc_matricula')?.files?.[0];
    if (!front && !back && !matricula) {
      setClienteFormMessage('Adjunte al menos una imagen de cedula o matricula.', 'error');
      return;
    }
    const form = new FormData();
    if (front) form.append('cedula_front', front);
    if (back) form.append('cedula_back', back);
    if (matricula) form.append('matricula', matricula);
    const btn = qs('#btnClienteExtractDocs');
    if (btn) btn.disabled = true;
    try {
      setClienteFormMessage('Extrayendo datos desde documentos...', 'info');
      const data = await fetchJson('/api/clientes/extract-from-docs', { method: 'POST', body: form });
      applyClientePatchToForm(data.client_patch || {});
      setClienteFormMessage('Datos extraidos y cargados al formulario. Revise antes de guardar.', 'success');
    } catch (e) {
      setClienteFormMessage(`${String(e.message || e)} Puede continuar en modo manual.`, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderClientePeriodos(periodos) {
    const wrap = qs('#clientePeriodosList');
    if (!wrap) return;
    wrap.replaceChildren();
    const btnNew = qs('#btnNewPeriodo');
    if (btnNew) btnNew.disabled = !currentClienteId;
    if (!periodos || !periodos.length) {
      wrap.className = 'saved-list empty-state';
      wrap.textContent = currentClienteId
        ? 'Sin periodos. Pulse "Nuevo periodo" para crear el primero.'
        : 'Abra o guarde un cliente para revisar sus periodos.';
      return;
    }
    wrap.className = 'saved-list';
    periodos.forEach(periodo => {
      const item = document.createElement('div');
      item.className = 'saved-item periodo-item';
      const estado = periodo.estado || 'borrador';
      const recompute = periodo.recompute_required
        ? '<span class="periodo-status recompute">requiere recalcular</span>'
        : '';
      const finalizedAt = periodo.finalized_at ? new Date(periodo.finalized_at).toLocaleDateString() : '';
      item.innerHTML = `
        <div>
          <strong>${periodo.mes_inicial} a ${periodo.mes_final}</strong>
          <div class="periodo-badges">
            <span class="periodo-status ${estado}">${estado}</span>
            ${recompute}
          </div>
          <div class="meta">${finalizedAt ? 'Finalizado: ' + finalizedAt + ' - ' : ''}saldos: ${periodo.saldos_iniciales_origen || 'manual'}</div>
        </div>
        <div class="actions"></div>
      `;
      const actions = item.querySelector('.actions');
      const openEditor = document.createElement('button');
      openEditor.className = 'btn primary';
      openEditor.textContent = 'Abrir en editor';
      openEditor.addEventListener('click', () => openPeriodoInEditor(periodo.id));
      actions.appendChild(openEditor);
      if (periodo.estado === 'borrador') {
        const finalize = document.createElement('button');
        finalize.className = 'btn';
        finalize.textContent = 'Finalizar';
        finalize.addEventListener('click', () => finalizePeriodo(periodo.id, periodo));
        actions.appendChild(finalize);
        const del = document.createElement('button');
        del.className = 'btn danger';
        del.textContent = 'Eliminar';
        del.addEventListener('click', () => deletePeriodo(periodo.id, periodo));
        actions.appendChild(del);
      }
      // Boton de auditoria siempre disponible
      const audit = document.createElement('button');
      audit.className = 'btn';
      audit.textContent = 'Ver auditoria';
      audit.addEventListener('click', () => showPeriodoAudit(periodo.id, periodo));
      actions.appendChild(audit);
      if (periodo.estado !== 'borrador') {
        // Estado finalizado o certificado
        if (periodo.documento_generado_at) {
          const download = document.createElement('button');
          download.className = 'btn primary';
          download.textContent = 'Descargar documento';
          download.addEventListener('click', () => downloadPeriodoDocument(periodo.id));
          actions.appendChild(download);
          const regen = document.createElement('button');
          regen.className = 'btn';
          regen.textContent = 'Regenerar';
          regen.addEventListener('click', () => generatePeriodoDocument(periodo.id, true, periodo));
          actions.appendChild(regen);
        } else {
          const gen = document.createElement('button');
          gen.className = 'btn primary';
          gen.textContent = 'Generar documento';
          gen.addEventListener('click', () => generatePeriodoDocument(periodo.id, false, periodo));
          actions.appendChild(gen);
        }
        const dup = document.createElement('button');
        dup.className = 'btn';
        dup.textContent = 'Duplicar como borrador';
        dup.addEventListener('click', () => duplicatePeriodo(periodo.id));
        actions.appendChild(dup);
      }
      wrap.appendChild(item);
    });
  }

  async function openPeriodoInEditor(periodoId) {
    activateMode('modelMode');
    await loadEditablePeriodos();
    const selector = qs('#editableSelector');
    if (selector) selector.value = periodoId;
    await selectEditablePeriodo(periodoId);
    qs('#editorPeriodoHeader')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function confirmIfRecomputeRequired(periodo, actionLabel) {
    if (!periodo?.recompute_required) return true;
    return window.confirm(`Este periodo requiere recalcular. Revise los saldos antes de ${actionLabel}. Desea continuar?`);
  }

  async function generatePeriodoDocument(periodoId, isRegen, periodo = null) {
    if (!confirmIfRecomputeRequired(periodo, isRegen ? 'regenerar el documento' : 'generar el documento')) return;
    if (isRegen && !window.confirm('Regenerar documento sobreescribira el archivo anterior. Continuar?')) return;
    setPeriodosMessage('Generando documento...', 'info');
    try {
      await fetchJson(`/api/periodos/${periodoId}/generar-documento`, { method: 'POST' });
      setPeriodosMessage('Documento generado correctamente.', 'success');
      await loadClienteDetail(currentClienteId, { openForm: true });
    } catch (e) {
      setPeriodosMessage(String(e.message || e), 'error');
    }
  }

  function downloadPeriodoDocument(periodoId) {
    // Navegacion directa al endpoint binario; el navegador maneja la descarga.
    window.open(`/api/periodos/${periodoId}/documento`, '_blank');
  }

  async function showPeriodoAudit(periodoId, periodo) {
    const overlay = document.createElement('div');
    overlay.className = 'audit-overlay';
    overlay.innerHTML = `
      <div class="audit-modal">
        <div class="audit-header">
          <h3>Auditoria del periodo ${periodo.mes_inicial}..${periodo.mes_final}</h3>
          <button class="btn" id="auditClose">Cerrar</button>
        </div>
        <div class="audit-body"><div class="loading">Cargando historial...</div></div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.querySelector('#auditClose').addEventListener('click', close);
    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) close(); });
    try {
      const data = await fetchJson(`/api/audit?entity_type=periodo&entity_id=${encodeURIComponent(periodoId)}`);
      const body = overlay.querySelector('.audit-body');
      const records = data.records || [];
      if (!records.length) {
        body.innerHTML = '<p class="meta">Sin entradas de auditoria.</p>';
        return;
      }
      const rows = records.map((r, i) => {
        const ts = r.timestamp ? new Date(r.timestamp).toLocaleString() : '';
        const meta = r.metadata || {};
        const changed = meta.changed_fields || meta.changed_blocks || [];
        const changedStr = Array.isArray(changed) && changed.length ? ` · campos: ${changed.join(', ')}` : '';
        const invalidados = (meta.invalidated_descendants || []).length;
        const invalidStr = invalidados ? ` · ${invalidados} hijos invalidados` : '';
        return `<tr>
          <td>${i + 1}</td>
          <td>${ts}</td>
          <td><strong>${r.action}</strong></td>
          <td>${r.cpa_user || 'system'}</td>
          <td>${r.summary || ''}${changedStr}${invalidStr}</td>
        </tr>`;
      }).join('');
      body.innerHTML = `
        <table class="audit-table">
          <thead><tr><th>#</th><th>Fecha</th><th>Accion</th><th>Usuario</th><th>Detalle</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } catch (e) {
      overlay.querySelector('.audit-body').innerHTML = `<p class="error">${String(e.message || e)}</p>`;
    }
  }

  // ====================== Periodos: formulario y acciones ======================
  function setPeriodosMessage(text, type = 'info') {
    setScopedMessage('#periodosMessages', text, type);
  }
  function setPeriodoFormMessage(text, type = 'info') {
    setScopedMessage('#periodoFormMessages', text, type);
  }

  function openPeriodoForm() {
    if (!currentClienteId) {
      setPeriodosMessage('Abra o guarde un cliente antes de crear un periodo.', 'error');
      return;
    }
    const panel = qs('#periodoFormPanel');
    if (!panel) return;
    panel.classList.remove('hidden');
    const subtitle = qs('#periodoFormSubtitle');
    if (subtitle) subtitle.textContent = `Cliente: ${selectedClienteName || ''}`;
    setPeriodoFormMessage('', 'info');
    qs('#rollforwardPreview')?.classList.add('hidden');
    const rfChk = qs('#periodo_rollforward');
    if (rfChk) rfChk.checked = false;
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function closePeriodoForm() {
    qs('#periodoFormPanel')?.classList.add('hidden');
  }

  async function refreshRollforwardPreview() {
    const wrap = qs('#rollforwardPreview');
    if (!wrap) return;
    const checked = qs('#periodo_rollforward')?.checked;
    if (!checked || !currentClienteId) {
      wrap.classList.add('hidden');
      wrap.replaceChildren();
      return;
    }
    const mesInicial = qs('#periodo_mes_inicial')?.value || '';
    if (!mesInicial) {
      wrap.classList.remove('hidden');
      wrap.textContent = 'Indique mes inicial para previsualizar saldos.';
      return;
    }
    try {
      const data = await fetchJson(`/api/clientes/${currentClienteId}/rollforward-preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mes_inicial: mesInicial.slice(0, 7) }),
      });
      const rf = data.rollforward || {};
      wrap.classList.remove('hidden');
      if (!rf.has_anterior) {
        wrap.innerHTML = '<div class="info">No hay periodo anterior finalizado. Se usaran saldos manuales (todos en 0).</div>';
        return;
      }
      const cuentas = Object.entries(rf.saldos || {}).slice(0, 6)
        .map(([k, v]) => `<li>${k}: ${Number(v).toLocaleString()}</li>`).join('');
      const warningHtml = rf.warning ? `<div class="warning">${rf.warning}</div>` : '';
      wrap.innerHTML = `
        ${warningHtml}
        <div><strong>Saldos heredados del periodo ${rf.mes_anterior_final}:</strong></div>
        <ul class="rollforward-saldos">${cuentas}${Object.keys(rf.saldos || {}).length > 6 ? '<li>...</li>' : ''}</ul>
      `;
    } catch (e) {
      wrap.classList.remove('hidden');
      wrap.innerHTML = `<div class="error">${String(e.message || e)}</div>`;
    }
  }

  async function savePeriodo() {
    if (!currentClienteId) return;
    const body = {
      mes_inicial: (qs('#periodo_mes_inicial')?.value || '').slice(0, 7),
      mes_final: (qs('#periodo_mes_final')?.value || '').slice(0, 7),
      tasa_cambio: numberValue('periodo_tasa_cambio', 36.6243),
      ingresos_base_usd: numberValue('periodo_ingresos_base_usd', 0),
      variabilidad_ingresos_pct: numberValue('periodo_var_ingresos', 12),
      cost_pct: numberValue('periodo_cost_pct', 70),
      variabilidad_costos_pct: numberValue('periodo_var_costos', 5),
      cash_sales_pct: numberValue('periodo_cash_sales_pct', 85),
      rollforward: !!qs('#periodo_rollforward')?.checked,
    };
    const seed = qs('#periodo_seed')?.value?.trim();
    if (seed) body.seed = seed;
    if (!body.mes_inicial || !body.mes_final) {
      setPeriodoFormMessage('Indique mes inicial y mes final.', 'error');
      return;
    }
    const btn = qs('#btnSavePeriodo');
    if (btn) btn.disabled = true;
    try {
      const data = await fetchJson(`/api/clientes/${currentClienteId}/periodos`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const warning = data.rollforward?.warning;
      setPeriodoFormMessage(`Periodo creado (${data.periodo.mes_inicial}..${data.periodo.mes_final}).${warning ? ' ' + warning : ''}`, warning ? 'warning' : 'success');
      closePeriodoForm();
      await loadClienteDetail(currentClienteId, { openForm: true });
    } catch (e) {
      setPeriodoFormMessage(String(e.message || e), 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function finalizePeriodo(periodoId, periodo = null) {
    if (!confirmIfRecomputeRequired(periodo, 'finalizarlo')) return;
    if (!window.confirm('Finalizar este periodo? Los saldos finales se calcularan y guardaran para el roll-forward del proximo.')) return;
    try {
      await fetchJson(`/api/periodos/${periodoId}/finalizar`, { method: 'POST' });
      setPeriodosMessage('Periodo finalizado.', 'success');
      await loadClienteDetail(currentClienteId, { openForm: true });
    } catch (e) {
      setPeriodosMessage(String(e.message || e), 'error');
    }
  }

  async function duplicatePeriodo(periodoId) {
    try {
      await fetchJson(`/api/periodos/${periodoId}/duplicar`, { method: 'POST' });
      setPeriodosMessage('Periodo duplicado como nuevo borrador.', 'success');
      await loadClienteDetail(currentClienteId, { openForm: true });
    } catch (e) {
      setPeriodosMessage(String(e.message || e), 'error');
    }
  }

  async function deletePeriodo(periodoId, periodo) {
    if (!window.confirm(`Eliminar borrador ${periodo.mes_inicial}..${periodo.mes_final}? Esta accion es permanente.`)) return;
    try {
      await fetchJson(`/api/periodos/${periodoId}`, { method: 'DELETE' });
      setPeriodosMessage('Borrador eliminado.', 'success');
      await loadClienteDetail(currentClienteId, { openForm: true });
    } catch (e) {
      setPeriodosMessage(String(e.message || e), 'error');
    }
  }

  function renderClienteTemplate(data) {
    const wrap = qs('#clienteTemplateEditor');
    const origin = qs('#clienteTemplateOrigin');
    if (!wrap) return;
    wrap.replaceChildren();
    const plantilla = data?.plantilla || {};
    if (origin) origin.textContent = data ? `Origen: ${data.origen || 'default'}` : 'Sin plantilla cargada.';
    if (!Object.keys(plantilla).length) {
      wrap.className = 'template-editor empty-state';
      wrap.textContent = 'Abra o guarde un cliente para revisar la plantilla.';
      return;
    }
    wrap.className = 'template-editor';
    Object.entries(plantilla).forEach(([name, amount]) => addTemplateRow(name, amount));
  }

  function addTemplateRow(name = '', amount = 0) {
    const wrap = qs('#clienteTemplateEditor');
    if (!wrap) return;
    if (wrap.classList.contains('empty-state')) {
      wrap.className = 'template-editor';
      wrap.replaceChildren();
    }
    const row = document.createElement('div');
    row.className = 'template-row';
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.value = name;
    nameInput.placeholder = 'Concepto';
    const amountInput = document.createElement('input');
    amountInput.type = 'number';
    amountInput.step = '0.01';
    amountInput.min = '0';
    amountInput.value = Number(amount || 0);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn danger';
    remove.textContent = 'Eliminar';
    remove.addEventListener('click', () => row.remove());
    row.append(nameInput, amountInput, remove);
    wrap.appendChild(row);
  }

  function collectTemplateRows() {
    const rows = qsa('#clienteTemplateEditor .template-row');
    const out = {};
    for (const row of rows) {
      const inputs = row.querySelectorAll('input');
      const key = (inputs[0]?.value || '').trim();
      const amount = Number(inputs[1]?.value || 0);
      if (!key) continue;
      if (!Number.isFinite(amount) || amount < 0) {
        throw new Error(`Monto invalido para ${key}`);
      }
      out[key] = amount;
    }
    if (!Object.keys(out).length) throw new Error('La plantilla no puede estar vacia.');
    return out;
  }

  async function saveClienteTemplate() {
    if (!currentClienteId) {
      setClienteFormMessage('Guarde el cliente antes de guardar una plantilla personalizada.', 'error');
      return;
    }
    try {
      const plantilla = collectTemplateRows();
      const data = await fetchJson(`/api/clientes/${encodeURIComponent(currentClienteId)}/plantilla-gastos`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plantilla }),
      });
      renderClienteTemplate(data.plantilla_gastos);
      await loadClienteDetail(currentClienteId, { openForm: false });
      setClienteFormMessage('Plantilla personalizada guardada.', 'success');
    } catch (e) {
      setClienteFormMessage(String(e.message || e), 'error');
    }
  }

  function resetClienteTemplateToGiro() {
    const cliente = currentClienteDetail?.cliente;
    const giroId = cliente?.giro_negocio_id || clienteFieldValue('c_giro_negocio_id');
    const giro = clienteGiros.find(item => item.id === giroId);
    if (!giro) {
      setClienteFormMessage('Seleccione un giro para resetear la plantilla.', 'error');
      return;
    }
    renderClienteTemplate({ origen: 'giro', plantilla: giro.plantilla_gastos || {} });
    setClienteFormMessage('Plantilla restablecida desde el giro. Guarde plantilla para conservar el cambio.', 'info');
  }

  function applyClienteTemplateToModel(templateData) {
    const template = templateData?.plantilla || {};
    const mapping = {
      'Sueldos y Salarios': 'm_g_sueldos',
      'Servicios Publicos': 'm_g_servicios',
      'Servicios Públicos': 'm_g_servicios',
      'Alcaldia y DGI': 'm_g_alcaldia',
      'Alcaldía y DGI': 'm_g_alcaldia',
      'Combustible': 'm_g_combustible',
      'Publicidad': 'm_g_publicidad',
      'Mantenimientos': 'm_g_mantenimientos',
      'Renta': 'm_g_renta',
      'Seguros': 'm_g_seguros',
      'Otros Gastos': 'm_g_otros',
      'Otros gastos': 'm_g_otros',
    };
    const unmatched = [];
    Object.entries(template).forEach(([key, value]) => {
      const id = mapping[key];
      if (id) setFieldValue(id, value);
      else unmatched.push(key);
    });
    return unmatched;
  }

  function useClienteInModel(detail = currentClienteDetail) {
    if (!detail?.cliente) return;
    const cliente = detail.cliente;
    selectedClienteId = cliente.id || '';
    selectedClienteName = cliente.nombre_completo || '';
    selectedGiroId = cliente.giro_negocio_id || '';
    const modelMapping = {
      m_nombre_completo: cliente.nombre_completo,
      m_cedula: cliente.cedula,
      m_contacto: cliente.telefono,
      m_direccion_personal: cliente.direccion_domicilio,
      m_direccion_negocio: cliente.direccion_negocio,
      m_matricula: cliente.matricula_roc,
      m_giro_negocio: cliente.giro?.nombre || cliente.giro_negocio_id,
    };
    Object.entries(modelMapping).forEach(([id, value]) => setFieldValue(id, value));
    const unmatched = applyClienteTemplateToModel(detail.plantilla_gastos || {});
    const badge = qs('#selectedClienteBadge');
    if (badge) {
      badge.textContent = `Usando cliente: ${selectedClienteName} (snapshot)`;
      badge.classList.remove('hidden');
    }
    activateMode('modelMode');
    lastModelPayload = null;
    setGenerateEnabled(false);
    if (unmatched.length) {
      setModelMessage(`Cliente cargado. Revise categorias de plantilla no aplicadas al formulario: ${unmatched.join(', ')}.`, 'warning');
    } else {
      setModelMessage('Cliente cargado al modelo como snapshot.', 'info');
    }
  }

  function applyDocExtraction() {
    const patch = pendingDocExtraction?.client_patch || {};
    const mapping = {
      nombre_completo: 'm_nombre_completo',
      cedula: 'm_cedula',
      sexo: 'm_sexo',
      domicilio: 'm_domicilio',
      direccion_personal: 'm_direccion_personal',
      regimen: 'm_regimen',
      matricula: 'm_matricula',
      direccion_negocio: 'm_direccion_negocio',
      giro_negocio: 'm_giro_negocio',
    };
    Object.entries(mapping).forEach(([key, id]) => setFieldValue(id, patch[key]));
    lastModelPayload = null;
    setGenerateEnabled(false);
    clearDocExtraction();
    setDocExtractMessage('Datos cargados al formulario. Revise antes de generar.', 'info');
  }

  async function onExtractClientDocs() {
    clearDocExtraction();
    const front = qs('#m_doc_cedula_front')?.files?.[0];
    const back = qs('#m_doc_cedula_back')?.files?.[0];
    const matricula = qs('#m_doc_matricula')?.files?.[0];
    if (!front && !back && !matricula) {
      setDocExtractMessage('Adjunte al menos una imagen de cedula o matricula.', 'error');
      return;
    }
    const btn = qs('#btnExtractClientDocs');
    if (btn) btn.disabled = true;
    setDocExtractMessage('Extrayendo datos desde imagenes...', 'info');
    const form = new FormData();
    if (front) form.append('cedula_front', front);
    if (back) form.append('cedula_back', back);
    if (matricula) form.append('matricula', matricula);
    try {
      const resp = await fetch('/api/model/documents/extract', { method: 'POST', body: form });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'No se pudo extraer datos.');
      pendingDocExtraction = data;
      renderDocExtraction(data);
      setDocExtractMessage('Extraccion lista. Revise los campos y aplique si estan correctos.', 'info');
    } catch (e) {
      setDocExtractMessage(String(e.message || e), 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function appendChatMessage(text, type = 'app') {
    const wrap = qs('#modelChatMessages');
    if (!wrap || !text) return;
    const div = document.createElement('div');
    div.className = `chat-bubble ${type}`;
    div.textContent = text;
    wrap.appendChild(div);
    div.scrollIntoView({ block: 'nearest' });
  }

  function renderAgentToolData(data) {
    const wrap = qs('#modelChatMessages');
    if (!wrap || !data || typeof data !== 'object') return;
    const kind = data.kind || '';
    if (!['balance_explanation', 'ledger', 'voucher'].includes(kind)) return;
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble app chat-tool-data';
    if (kind === 'balance_explanation') {
      const entries = (data.entries || []).slice(0, 8);
      bubble.appendChild(compactTable(
        ['Comprobante', 'Descripcion', 'Debe', 'Haber', 'Saldo'],
        entries.map(row => [
          row.voucher_id || '',
          row.description || '',
          formatMoney(row.debit || 0),
          formatMoney(row.credit || 0),
          formatMoney(row.running_balance || 0),
        ]),
        'Movimientos que explican el saldo'
      ));
    } else if (kind === 'ledger') {
      const rows = (data.rows || []).slice(0, 12);
      bubble.appendChild(compactTable(
        ['Fecha', 'Comprobante', 'Descripcion', 'Debe', 'Haber', 'Saldo'],
        rows.map(row => [
          row.date || row.month || '',
          row.voucher_id || '',
          row.description || '',
          formatMoney(row.debit || 0),
          formatMoney(row.credit || 0),
          formatMoney(row.running_balance || 0),
        ]),
        `Mayor de ${data.account_label || data.account || ''}`
      ));
    } else if (kind === 'voucher' && data.voucher) {
      const voucher = data.voucher;
      bubble.appendChild(compactTable(
        ['Cuenta', 'Debe', 'Haber', 'Referencia'],
        (voucher.lines || []).map(line => [
          line.account || '',
          formatMoney(line.debit || 0),
          formatMoney(line.credit || 0),
          line.reference || '',
        ]),
        `${voucher.voucher_id || ''} · ${voucher.description || ''}`
      ));
    }
    wrap.appendChild(bubble);
    bubble.scrollIntoView({ block: 'nearest' });
  }

  function compactTable(headers, rows, titleText) {
    const box = document.createElement('div');
    box.className = 'agent-data-card';
    if (titleText) {
      const title = document.createElement('div');
      title.className = 'agent-data-title';
      title.textContent = titleText;
      box.appendChild(title);
    }
    const table = document.createElement('table');
    table.className = 'journal-proposal-table';
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    headers.forEach(header => {
      const th = document.createElement('th');
      th.textContent = header;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    const tbody = document.createElement('tbody');
    (rows.length ? rows : [['Sin movimientos']]).forEach(row => {
      const tr = document.createElement('tr');
      row.forEach(value => {
        const td = document.createElement('td');
        td.textContent = value;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    box.appendChild(table);
    return box;
  }

  function clearPendingChatProposal() {
    if (pendingChatData?.proposalElement) {
      markProposalCardStatus(pendingChatData.proposalElement, 'superseded', 'Propuesta reemplazada por una nueva instruccion.');
    }
    pendingChatPayload = null;
    pendingChatData = null;
  }

  function markProposalCardStatus(bubble, kind, message) {
    if (!bubble) return;
    const actions = bubble.querySelector('.proposal-actions');
    const status = document.createElement('div');
    status.className = `proposal-status ${kind}`;
    status.textContent = message;
    if (actions) {
      actions.replaceWith(status);
    } else {
      const existing = bubble.querySelector('.proposal-status');
      if (existing) existing.replaceWith(status);
      else bubble.appendChild(status);
    }
  }

  function inputValue(id) {
    const el = qs(`#${id}`);
    return el ? el.value.trim() : '';
  }

  function numberValue(id, fallback = 0) {
    const raw = inputValue(id);
    const n = Number(raw);
    return Number.isFinite(n) ? n : fallback;
  }

  function parseModelEvents() {
    const raw = inputValue('m_eventos');
    if (!raw) return [];
    return raw.split(/\r?\n/)
      .map(line => line.trim())
      .filter(Boolean)
      .map(line => {
        if (line.startsWith('{')) {
          try {
            const parsed = JSON.parse(line);
            parsed.amount = Number(parsed.amount || parsed.amount_nio || 0);
            return parsed;
          } catch {}
        }
        const parts = line.split(',').map(x => (x || '').trim());
        const [month, account, amount, currency, source, instructionId, locked, createdAt] = parts;
        const event = { month, account, amount: Number(amount || 0), currency: currency || 'nio' };
        if (source) event.source = source;
        if (instructionId) event.instruction_id = instructionId;
        if (locked) event.locked = locked === 'true' || locked === '1' || locked === 'locked';
        if (createdAt) event.created_at = createdAt;
        const message = parts.slice(8).join(',').trim();
        if (message) event.message = message;
        return event;
      })
      .filter(ev => ev.month && ev.account && Number.isFinite(ev.amount));
  }

  function buildModelPayload() {
    return {
      client: {
        cliente_id: selectedClienteId || undefined,
        giro_negocio_id: selectedGiroId || undefined,
        nombre_completo: inputValue('m_nombre_completo'),
        cedula: inputValue('m_cedula'),
        banco: inputValue('m_banco'),
        estado_civil: inputValue('m_estado_civil'),
        profesion: inputValue('m_profesion'),
        sexo: inputValue('m_sexo'),
        domicilio: inputValue('m_domicilio'),
        direccion_personal: inputValue('m_direccion_personal'),
        direccion_negocio: inputValue('m_direccion_negocio'),
        fecha_certificacion: inputValue('m_fecha_certificacion'),
        contacto: inputValue('m_contacto'),
        regimen: inputValue('m_regimen'),
        matricula: inputValue('m_matricula'),
        giro_negocio: inputValue('m_giro_negocio'),
        antiguedad: inputValue('m_antiguedad'),
        empleados: numberValue('m_empleados', 0),
      },
      period: {
        start_month: inputValue('m_mes_inicio'),
        end_month: inputValue('m_mes_final'),
        months: numberValue('m_cantidad_meses', 0) || undefined,
        exchange_rate: numberValue('m_tasa_cambio', 36.6243),
        seed: inputValue('m_semilla'),
      },
      income: {
        base_income_usd: numberValue('m_ingresos_base', 100000),
        income_variability_pct: numberValue('m_var_ingresos', 15),
        cost_pct: numberValue('m_costo_pct', 70),
        cost_variability_pct: numberValue('m_var_costo', 5),
        cash_sales_pct: numberValue('m_contado_pct', 85),
        monthly_overrides: modelMonthlyOverrides,
      },
      expenses: {
        'Sueldos y Salarios': numberValue('m_g_sueldos', 0),
        'Servicios Publicos': numberValue('m_g_servicios', 0),
        'Alcaldia y DGI': numberValue('m_g_alcaldia', 0),
        'Combustible': numberValue('m_g_combustible', 0),
        'Publicidad': numberValue('m_g_publicidad', 0),
        'Mantenimientos': numberValue('m_g_mantenimientos', 0),
        'Renta': numberValue('m_g_renta', 0),
        'Seguros': numberValue('m_g_seguros', 0),
        'Otros Gastos': numberValue('m_g_otros', 0),
      },
      balances: {
        cash: numberValue('m_b_cash', 0),
        accounts_receivable: numberValue('m_b_ar', 0),
        inventory: numberValue('m_b_inventory', 0),
        ppe_real_estate: numberValue('m_b_real_estate', 0),
        ppe_equipment: numberValue('m_b_equipment', 0),
        ppe_vehicles: numberValue('m_b_vehicles', 0),
        accum_depreciation: numberValue('m_b_accum_dep', 0),
        credit_cards: numberValue('m_b_cards', 0),
        suppliers: numberValue('m_b_suppliers', 0),
        taxes_payable: numberValue('m_b_taxes', 0),
        accrued_expenses: numberValue('m_b_accrued', 0),
        loans_personal: numberValue('m_b_personal', 0),
        loans_pledge: numberValue('m_b_pledge', 0),
        loans_commercial: numberValue('m_b_commercial', 0),
        loans_mortgage: numberValue('m_b_mortgage', 0),
        retained_earnings: numberValue('m_b_retained', 0),
      },
      movements: {
        purchase_base_usd: numberValue('m_compras_base', 0),
        purchase_variability_pct: numberValue('m_var_compras', 0),
        loan_interest_monthly_pct: numberValue('m_interes_creditos', 0),
        events: parseModelEvents(),
        journal_entries: modelJournalEntries,
      },
      assets: {
        life_real_estate_years: numberValue('m_life_real_estate', 40),
        life_equipment_years: numberValue('m_life_equipment', 8),
        life_vehicles_years: numberValue('m_life_vehicles', 5),
      },
      accounting: {
        vouchers: modelAccountingVouchers,
        dynamic_accounts: modelDynamicAccounts,
      },
      chat: {
        commands: modelChatCommands,
      },
    };
  }

  // ====================== Editor avanzado del Periodo ======================
  let activePeriodoId = null;
  let activePeriodoDetail = null;
  let editorDirty = false;
  let suppressEditorDirty = false;

  function setEditorMessage(text, type = 'info') {
    setScopedMessage('#editorMessages', text, type);
  }

  function markEditorDirty(dirty = true) {
    if (suppressEditorDirty) return;
    editorDirty = !!(dirty && activePeriodoId && activePeriodoDetail?.periodo?.estado === 'borrador');
    const badge = qs('#epHeaderDirty');
    if (badge) badge.classList.toggle('hidden', !editorDirty);
    const saveBtn = qs('#btnSavePeriodoChanges');
    if (saveBtn) saveBtn.disabled = !editorDirty;
  }

  function runWithoutEditorDirty(fn) {
    suppressEditorDirty = true;
    try {
      return fn();
    } finally {
      suppressEditorDirty = false;
    }
  }

  function updateSidebarContext(cli, per) {
    const wrap = qs('#sidebarContext');
    if (!wrap) return;
    const clienteEl = qs('#sidebarContextCliente');
    const periodoEl = qs('#sidebarContextPeriodo');
    const estadoEl = qs('#sidebarContextEstado');
    if (!cli && !per) {
      wrap.classList.add('empty');
      if (clienteEl) clienteEl.textContent = 'Sin selección';
      if (periodoEl) periodoEl.textContent = '';
      if (estadoEl) { estadoEl.textContent = ''; estadoEl.className = 'sidebar-context-estado'; }
      return;
    }
    wrap.classList.remove('empty');
    const nombre = cli?.nombre_completo || cli?.nombre_negocio || 'Cliente';
    if (clienteEl) clienteEl.textContent = nombre;
    if (periodoEl) periodoEl.textContent = per ? `${per.mes_inicial} → ${per.mes_final}` : '';
    if (estadoEl && per?.estado) {
      estadoEl.textContent = per.estado;
      estadoEl.className = `sidebar-context-estado ${per.estado === 'borrador' ? 'ok' : 'warn'}`;
    } else if (estadoEl) {
      estadoEl.textContent = '';
      estadoEl.className = 'sidebar-context-estado';
    }
  }

  function updateEditorHeader() {
    const wrap = qs('#editorPeriodoHeader');
    const banner = qs('#editorReadonlyBanner');
    if (!wrap) return;
    if (!activePeriodoDetail) {
      wrap.classList.add('hidden');
      banner?.classList.add('hidden');
      updateSidebarContext(null, null);
      return;
    }
    wrap.classList.remove('hidden');
    const per = activePeriodoDetail.periodo || {};
    const cli = activePeriodoDetail.cliente || {};
    qs('#epHeaderCliente').textContent = cli.nombre_completo || cli.nombre_negocio || 'Cliente';
    qs('#epHeaderRango').textContent = `${per.mes_inicial} a ${per.mes_final}`;
    updateSidebarContext(cli, per);
    const estadoBadge = qs('#epHeaderEstado');
    estadoBadge.textContent = per.estado || '';
    estadoBadge.classList.toggle('ok', per.estado === 'borrador');
    estadoBadge.classList.toggle('warn', per.estado !== 'borrador');
    qs('#epHeaderRecompute').classList.toggle('hidden', !per.recompute_required);

    const isBorrador = per.estado === 'borrador';
    qs('#btnSavePeriodoChanges').classList.toggle('hidden', !isBorrador);
    qs('#btnDuplicateActivePeriodo').classList.toggle('hidden', isBorrador);
    if (banner) {
      banner.classList.toggle('hidden', isBorrador);
      const erbEstado = qs('#erbEstado');
      if (erbEstado) erbEstado.textContent = per.estado || '';
    }
    // Deshabilitar inputs del modelMode si no es borrador
    setModelModeReadonly(!isBorrador);
    // Chat asistente solo en borrador
    setChatEnabled(isBorrador);
  }

  function setModelModeReadonly(readonly) {
    const panel = qs('#modelMode');
    if (!panel) return;
    qsa('#modelMode input, #modelMode select, #modelMode textarea, #modelMode button').forEach(el => {
      // Excluir controles del editor mismo y del chat (chat se maneja aparte en setChatEnabled)
      if (el.id === 'editableSelector' || el.id === 'btnRefreshEditablePeriodos'
          || el.id === 'btnSavePeriodoChanges' || el.id === 'btnDuplicateActivePeriodo'
          || el.id === 'btnModelChatSend' || el.id === 'modelChatInput'
          || el.id === 'modelChatScopeSelect' || el.id === 'modelAccountSelect'
          || el.id === 'modelBlockSelectSummary' || el.id === 'modelVoucherTypeFilter'
          || el.id === 'modelVoucherAccountFilter'
          || el.classList.contains('chat-chip') || el.classList.contains('model-block-select')) {
        return;
      }
      el.disabled = readonly;
    });
  }

  function setChatEnabled(enabled) {
    const input = qs('#modelChatInput');
    const send = qs('#btnModelChatSend');
    const scope = qs('#modelChatScopeSelect');
    const undo = qs('#btnUndoLastChatAdjustment');
    const chips = qsa('.chat-chip');
    if (input) input.disabled = !enabled;
    if (send) send.disabled = !enabled;
    if (scope) scope.disabled = !enabled;
    if (undo) undo.disabled = !enabled;
    chips.forEach(c => { c.disabled = !enabled; });
    // Banner explicativo arriba del chat
    let banner = qs('#chatDisabledBanner');
    const card = qs('#modelChatCard');
    if (!enabled && card && !banner) {
      banner = document.createElement('div');
      banner.id = 'chatDisabledBanner';
      banner.className = 'readonly-banner';
      banner.textContent = 'El asistente contable solo edita periodos en estado borrador. Duplique este periodo como borrador para usarlo.';
      card.insertBefore(banner, card.firstChild?.nextSibling || null);
    } else if (enabled && banner) {
      banner.remove();
    }
  }

  async function loadEditablePeriodos() {
    try {
      const data = await fetchJson('/api/periodos/editables');
      const select = qs('#editableSelector');
      if (!select) return;
      const current = activePeriodoId || '';
      select.replaceChildren();
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'Seleccione un periodo...';
      select.appendChild(placeholder);
      (data.periodos || []).forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        const dirty = p.recompute_required ? ' ⟳' : '';
        opt.textContent = `${p.cliente_nombre} · ${p.mes_inicial}..${p.mes_final} · ${p.estado}${dirty}`;
        select.appendChild(opt);
      });
      if (current) select.value = current;
    } catch (e) {
      setEditorMessage(`No se pudieron cargar los periodos editables: ${e.message || e}`, 'error');
    }
  }

  async function selectEditablePeriodo(periodoId) {
    if (!periodoId) {
      activePeriodoId = null;
      activePeriodoDetail = null;
      updateEditorHeader();
      markEditorDirty(false);
      setModelModeReadonly(false);
      return;
    }
    // Confirmar si hay cambios sin guardar
    if (editorDirty && !window.confirm('Tiene cambios sin guardar en el periodo actual. ¿Descartarlos?')) {
      const select = qs('#editableSelector');
      if (select) select.value = activePeriodoId || '';
      return;
    }
    try {
      const data = await fetchJson(`/api/periodos/${periodoId}`);
      activePeriodoId = periodoId;
      activePeriodoDetail = data;
      editorDirty = false;
      // Aplicar el payload del periodo a los inputs del modelo
      if (data.periodo?.payload) {
        runWithoutEditorDirty(() => applyModelPayload(data.periodo.payload, { draftId: null }));
      }
      updateEditorHeader();
      markEditorDirty(false);
      setEditorMessage(`Periodo cargado: ${data.cliente?.nombre_completo} ${data.periodo.mes_inicial}..${data.periodo.mes_final}`, 'success');
    } catch (e) {
      setEditorMessage(`Error cargando periodo: ${e.message || e}`, 'error');
    }
  }

  async function saveActivePeriodoChanges() {
    if (!activePeriodoId) return;
    if (activePeriodoDetail?.periodo?.estado !== 'borrador') {
      setEditorMessage('Solo se pueden guardar cambios en borradores.', 'error');
      return;
    }
    const payload = buildModelPayload();
    // Preservar el bloque client del periodo (no viene del form)
    if (activePeriodoDetail?.periodo?.payload?.client) {
      payload.client = activePeriodoDetail.periodo.payload.client;
    }
    const btn = qs('#btnSavePeriodoChanges');
    if (btn) btn.disabled = true;
    try {
      const data = await fetchJson(`/api/periodos/${activePeriodoId}/payload`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload }),
      });
      const warn = data.invalidated_descendants?.length
        ? ` ⚠ Se marcaron ${data.invalidated_descendants.length} periodo(s) hijo(s) para recalcular.`
        : '';
      setEditorMessage(`Cambios guardados. Bloques: ${(data.changed_blocks || []).join(', ') || 'sin cambios detectados'}.${warn}`, 'success');
      // Recargar el detail
      activePeriodoDetail = await fetchJson(`/api/periodos/${activePeriodoId}`);
      markEditorDirty(false);
      updateEditorHeader();
      await loadEditablePeriodos();
    } catch (e) {
      setEditorMessage(`Error al guardar: ${e.message || e}`, 'error');
    } finally {
      if (btn) btn.disabled = !editorDirty;
    }
  }

  async function duplicateActivePeriodo() {
    if (!activePeriodoId) return;
    try {
      const data = await fetchJson(`/api/periodos/${activePeriodoId}/duplicar`, { method: 'POST' });
      setEditorMessage(`Borrador duplicado creado.`, 'success');
      await loadEditablePeriodos();
      await selectEditablePeriodo(data.periodo.id);
    } catch (e) {
      setEditorMessage(`Error al duplicar: ${e.message || e}`, 'error');
    }
  }

  function applyModelPayload(payload, { draftId = null } = {}) {
    payload = payload || {};
    const client = payload.client || {};
    const period = payload.period || {};
    const income = payload.income || {};
    const expenses = payload.expenses || {};
    const balances = payload.balances || {};
    const movements = payload.movements || {};
    const accounting = payload.accounting || {};
    const chat = payload.chat || {};
    const assets = payload.assets || {};
    modelMonthlyOverrides = Array.isArray(income.monthly_overrides) ? income.monthly_overrides.map(item => ({ ...item })) : [];
    modelJournalEntries = Array.isArray(movements.journal_entries) ? movements.journal_entries.map(item => ({ ...item })) : [];
    modelAccountingVouchers = Array.isArray(accounting.vouchers) ? accounting.vouchers.map(item => ({ ...item })) : [];
    modelDynamicAccounts = Array.isArray(accounting.dynamic_accounts) ? accounting.dynamic_accounts.map(item => ({ ...item })) : [];
    modelChatCommands = Array.isArray(chat.commands) ? chat.commands.map(item => ({ ...item })) : [];

    const mapping = {
      m_nombre_completo: client.nombre_completo,
      m_cedula: client.cedula,
      m_banco: client.banco,
      m_estado_civil: client.estado_civil,
      m_profesion: client.profesion,
      m_sexo: client.sexo,
      m_domicilio: client.domicilio,
      m_direccion_personal: client.direccion_personal,
      m_direccion_negocio: client.direccion_negocio,
      m_fecha_certificacion: client.fecha_certificacion,
      m_contacto: client.contacto,
      m_regimen: client.regimen,
      m_matricula: client.matricula,
      m_giro_negocio: client.giro_negocio,
      m_antiguedad: client.antiguedad,
      m_empleados: client.empleados,
      m_mes_inicio: period.start_month,
      m_mes_final: period.end_month,
      m_tasa_cambio: period.exchange_rate,
      m_semilla: period.seed,
      m_ingresos_base: income.base_income_usd,
      m_var_ingresos: income.income_variability_pct,
      m_costo_pct: income.cost_pct,
      m_var_costo: income.cost_variability_pct,
      m_contado_pct: income.cash_sales_pct,
      m_g_sueldos: expenses['Sueldos y Salarios'],
      m_g_servicios: expenses['Servicios Publicos'],
      m_g_alcaldia: expenses['Alcaldia y DGI'],
      m_g_combustible: expenses.Combustible,
      m_g_publicidad: expenses.Publicidad,
      m_g_mantenimientos: expenses.Mantenimientos,
      m_g_renta: expenses.Renta,
      m_g_seguros: expenses.Seguros,
      m_g_otros: expenses['Otros Gastos'],
      m_b_cash: balances.cash,
      m_b_ar: balances.accounts_receivable,
      m_b_inventory: balances.inventory,
      m_b_real_estate: balances.ppe_real_estate,
      m_b_equipment: balances.ppe_equipment,
      m_b_vehicles: balances.ppe_vehicles,
      m_b_accum_dep: balances.accum_depreciation,
      m_b_cards: balances.credit_cards,
      m_b_suppliers: balances.suppliers,
      m_b_taxes: balances.taxes_payable,
      m_b_accrued: balances.accrued_expenses,
      m_b_personal: balances.loans_personal,
      m_b_pledge: balances.loans_pledge,
      m_b_commercial: balances.loans_commercial,
      m_b_mortgage: balances.loans_mortgage,
      m_b_retained: balances.retained_earnings,
      m_compras_base: movements.purchase_base_usd,
      m_var_compras: movements.purchase_variability_pct,
      m_interes_creditos: movements.loan_interest_monthly_pct,
      m_life_real_estate: assets.life_real_estate_years ?? 40,
      m_life_equipment: assets.life_equipment_years ?? 8,
      m_life_vehicles: assets.life_vehicles_years ?? 5,
    };
    Object.entries(mapping).forEach(([id, value]) => setFieldValue(id, value));
    setModelEventsFromPayload(payload);
    currentDraftId = draftId;
    lastModelPayload = payload;
    lastModelPreviewData = null;
    clearPendingChatProposal();
    setGenerateEnabled(false);
  }

  function savedSearchText(record) {
    return [
      record.client_name,
      record.cedula,
      record.bank,
      record.period_label,
      record.start_month,
      record.end_month,
      record.id,
    ].filter(Boolean).join(' ').toLowerCase();
  }

  function renderSavedModels() {
    const filter = inputValue('savedModelsFilter').toLowerCase();
    renderSavedList('#draftsList', savedModelsCache.drafts, 'draft', filter);
    renderSavedList('#finalsList', savedModelsCache.finals, 'final', filter);
  }

  function renderSavedList(containerSel, records, type, filter) {
    const wrap = qs(containerSel);
    if (!wrap) return;
    wrap.replaceChildren();
    const visible = (records || []).filter(record => !filter || savedSearchText(record).includes(filter));
    if (!visible.length) {
      wrap.classList.add('empty-state');
      wrap.textContent = type === 'draft' ? 'Sin borradores.' : 'Sin historicos.';
      return;
    }
    wrap.classList.remove('empty-state');
    visible.forEach(record => {
      const item = document.createElement('div');
      item.className = 'saved-item';
      const title = document.createElement('strong');
      title.textContent = record.client_name || 'Cliente sin nombre';
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${record.period_label || 'Sin periodo'} | ${record.bank || 'Sin banco'} | ${record.updated_at || record.created_at || ''}`;
      const actions = document.createElement('div');
      actions.className = 'saved-actions';

      const open = document.createElement('button');
      open.className = 'btn';
      open.type = 'button';
      open.textContent = type === 'draft' ? 'Abrir' : 'Ver';
      open.addEventListener('click', () => type === 'draft' ? loadDraft(record.id) : viewFinal(record.id));
      actions.appendChild(open);

      if (type === 'draft') {
        const del = document.createElement('button');
        del.className = 'btn';
        del.type = 'button';
        del.textContent = 'Eliminar';
        del.addEventListener('click', () => deleteDraftRecord(record.id));
        actions.appendChild(del);
      } else {
        const dup = document.createElement('button');
        dup.className = 'btn';
        dup.type = 'button';
        dup.textContent = 'Duplicar';
        dup.addEventListener('click', () => duplicateFinalRecord(record.id));
        const doc = document.createElement('button');
        doc.className = 'btn';
        doc.type = 'button';
        doc.textContent = 'DOCX';
        doc.addEventListener('click', () => { window.location.href = `/api/model/finals/${encodeURIComponent(record.id)}/document`; });
        actions.append(dup, doc);
      }

      item.append(title, meta, actions);
      wrap.appendChild(item);
    });
  }

  async function refreshSavedModels() {
    const [draftsResp, finalsResp] = await Promise.all([
      fetch('/api/model/drafts'),
      fetch('/api/model/finals'),
    ]);
    const drafts = await draftsResp.json();
    const finals = await finalsResp.json();
    if (!draftsResp.ok || !drafts.ok) throw new Error(drafts.error || 'No se pudieron cargar borradores.');
    if (!finalsResp.ok || !finals.ok) throw new Error(finals.error || 'No se pudieron cargar historicos.');
    savedModelsCache = { drafts: drafts.records || [], finals: finals.records || [] };
    renderSavedModels();
  }

  async function showSavedModelsPanel() {
    const panel = qs('#savedModelsPanel');
    if (panel) panel.classList.toggle('hidden', false);
    try {
      await refreshSavedModels();
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function saveCurrentDraft() {
    try {
      const payload = buildModelPayload();
      const resp = await fetch('/api/model/drafts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ draft_id: currentDraftId, payload }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'No se pudo guardar el borrador.');
      currentDraftId = data.record?.id || currentDraftId;
      lastModelPayload = payload;
      setModelMessage('Borrador guardado.', 'info');
      await showSavedModelsPanel();
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function saveCurrentFinal() {
    try {
      const payload = buildModelPayload();
      setModelMessage('Generando y guardando version final...');
      const resp = await fetch('/api/model/finals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'No se pudo guardar final.');
      setModelMessage('Version final guardada en historico.', 'info');
      await showSavedModelsPanel();
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function loadDraft(recordId) {
    try {
      const resp = await fetch(`/api/model/drafts/${encodeURIComponent(recordId)}`);
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'No se pudo cargar el borrador.');
      applyModelPayload(data.record.payload, { draftId: data.record.id });
      setModelMessage('Borrador cargado. Recalculando vista previa...', 'info');
      await onModelPreview();
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function viewFinal(recordId) {
    try {
      const resp = await fetch(`/api/model/finals/${encodeURIComponent(recordId)}`);
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'No se pudo cargar el historico.');
      applyModelPayload(data.record.payload, { draftId: null });
      setModelMessage('Historico cargado solo para revision. Use Duplicar para editarlo como borrador.', 'info');
      await onModelPreview();
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function duplicateFinalRecord(recordId) {
    try {
      const resp = await fetch(`/api/model/finals/${encodeURIComponent(recordId)}/duplicate`, { method: 'POST' });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'No se pudo duplicar el historico.');
      applyModelPayload(data.record.payload, { draftId: data.record.id });
      setModelMessage('Historico duplicado como nuevo borrador. Recalculando vista previa...', 'info');
      await refreshSavedModels();
      await onModelPreview();
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function deleteDraftRecord(recordId) {
    if (!confirm('Eliminar este borrador?')) return;
    try {
      const resp = await fetch(`/api/model/drafts/${encodeURIComponent(recordId)}`, { method: 'DELETE' });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'No se pudo eliminar el borrador.');
      if (currentDraftId === recordId) currentDraftId = null;
      await refreshSavedModels();
      setModelMessage('Borrador eliminado.', 'info');
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  function formatMoney(value) {
    const n = Number(value || 0);
    return n.toLocaleString('es-NI', { maximumFractionDigits: 0 });
  }

  function formatSignedMoney(value) {
    const n = Number(value || 0);
    const sign = n > 0 ? '+' : '';
    return `${sign}${formatMoney(n)}`;
  }

  function formatEventAmount(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return '0';
    if (Math.abs(n - Math.round(n)) < 0.0001) return String(Math.round(n));
    return n.toFixed(2).replace(/\.?0+$/, '');
  }

  function formatModelEvents(events) {
    return (events || [])
      .map(ev => {
        const base = `${ev.month || ''},${ev.account || ''},${formatEventAmount(ev.amount)},${ev.currency || 'nio'}`;
        if (!ev.source && !ev.instruction_id) return base;
        const source = ev.source || '';
        const instructionId = ev.instruction_id || '';
        const locked = ev.locked === undefined ? '' : String(!!ev.locked);
        const createdAt = ev.created_at || '';
        const message = String(ev.message || '').replace(/[\r\n]+/g, ' ').replace(/,/g, ';');
        return `${base},${source},${instructionId},${locked},${createdAt},${message}`;
      })
      .join('\n');
  }

  function setModelEventsFromPayload(payload) {
    const textarea = qs('#m_eventos');
    if (!textarea) return;
    const events = payload?.movements?.events || payload?.events || [];
    modelJournalEntries = Array.isArray(payload?.movements?.journal_entries)
      ? payload.movements.journal_entries.map(item => ({ ...item }))
      : [];
    textarea.value = formatModelEvents(events);
    renderAdjustmentHistory();
  }

  function leverLabel(lever) {
    const labels = {
      purchase_adjustment: 'Ajuste de compras',
      supplier_financing: 'Financiamiento de proveedores',
      loan_commercial_new: 'Nuevo credito comercial',
      capital_contribution: 'Aporte de capital',
      owner_withdrawal: 'Retiro contra capital',
      retained_earnings_distribution: 'Retiro contra resultados acumulados',
      capital_reclassification: 'Reclasificacion patrimonial',
      undo_last_adjustment: 'Deshacer ultimo ajuste',
    };
    return labels[lever] || lever || '';
  }

  function journalAccountLabel(account) {
    const labels = {
      cash: 'Efectivo y Equivalentes de Efectivo',
      accounts_receivable: 'Cuentas por Cobrar Clientes',
      inventory: 'Inventarios',
      ppe_real_estate: 'Bienes Inmuebles',
      ppe_equipment: 'Mobiliario y Equipos',
      ppe_vehicles: 'Vehiculos',
      accum_depreciation: 'Depreciacion Acumulada',
      credit_cards: 'Tarjetas de Credito',
      suppliers: 'Proveedores',
      taxes_payable: 'Impuestos por Pagar',
      accrued_expenses: 'Gastos Acumulados por pagar',
      loans_mortgage: 'Creditos Hipotecarios',
      loans_consumo: 'Creditos Consumo',
      loans_personal: 'Creditos Personales',
      loans_pledge: 'Creditos Prendarios',
      loans_commercial: 'Creditos Comerciales',
      capital: 'Capital',
      retained_earnings: 'Resultados Acumulados',
      current_earnings: 'Resultados del Ejercicio',
    };
    return labels[account] || account || '';
  }

  function renderChatProposal(data) {
    const thread = qs('#modelChatMessages');
    if (!thread) return;
    const wrap = document.createElement('div');
    wrap.className = 'chat-bubble app chat-bubble-proposal';
    thread.appendChild(wrap);
    if (data) data.proposalElement = wrap;
    const proposal = data?.proposal || {};
    const event = proposal.event;
    const events = data?.new_events || proposal.events || (event ? [event] : []);
    const journalEntries = data?.new_journal_entries || (proposal.journal_entry ? [proposal.journal_entry] : []);
    const removedEvents = data?.removed_events || [];
    const removedJournalEntries = data?.removed_journal_entries || [];
    const preservedEvents = data?.existing_events_preserved || [];
    const proposalKind = proposal.kind || ((events.length && proposal.target_cash === undefined && proposal.adjusted_cash === undefined) ? 'compound_events' : '');

    const title = document.createElement('h3');
    title.textContent = proposalTitle(proposalKind);
    wrap.appendChild(title);

    const messageText = String(data?.assistant_message || proposal.assistant_message || proposal.explanation || '').trim();
    if (messageText) {
      const messageEl = document.createElement('p');
      messageEl.className = 'proposal-message';
      messageEl.textContent = messageText;
      wrap.appendChild(messageEl);
    }

    const grid = document.createElement('div');
    grid.className = 'proposal-grid';
    let items = [];
    if (proposalKind === 'workflow') {
      items = [
        ['Accion', proposal.confirm_label || 'Confirmar'],
        ['Requiere confirmacion', 'Si'],
      ];
    } else if (proposalKind === 'period_change') {
      items = [
        ['Nuevo periodo', proposal.target_month || ''],
        ['Impacto caja', formatSignedMoney(proposal.impact?.cash || 0)],
        ['Impacto activos', formatSignedMoney(proposal.impact?.assets || 0)],
        ['Impacto pasivos', formatSignedMoney(proposal.impact?.liabilities || 0)],
        ['Impacto patrimonio', formatSignedMoney(proposal.impact?.equity || 0)],
      ];
    } else if (proposalKind === 'assumption_change' || proposalKind === 'assumption_change_proposal') {
      items = [
        ['Supuesto', proposal.field || proposal.assumption_label || proposal.technical_records?.[0]?.assumption || 'Supuesto'],
        ['Antes', proposal.before !== undefined ? String(proposal.before) : '-'],
        ['Despues', proposal.after !== undefined ? String(proposal.after) : (proposal.assumption_value !== undefined ? `${formatMoney(proposal.assumption_value)}%` : `${formatMoney(proposal.technical_records?.[0]?.value)}%`)],
        ['Alcance', proposal.scope || proposal.scope_label || 'periodo completo'],
      ];
    } else if (proposalKind === 'create_account') {
      const account = proposal.account || proposal.technical_records?.[0] || {};
      items = [
        ['Cuenta', account.name || ''],
        ['Tipo', account.account_type || ''],
        ['Seccion', account.section || ''],
        ['Requiere confirmacion', 'Si'],
      ];
    } else if (proposalKind === 'compound_voucher_correction') {
      items = [
        ['Original', proposal.original_voucher_id || ''],
        ['Reverso', proposal.reversal_voucher_id || ''],
        ['Nuevo asiento', proposal.correction_entry_id || ''],
        ['Mes', proposal.target_month || proposal.month || ''],
      ];
    } else if (proposalKind === 'voucher_reversal') {
      items = [
        ['Original', proposal.original_voucher_id || proposal.reference_voucher_id || ''],
        ['Reverso', proposal.reversal_voucher_id || ''],
        ['Mes', proposal.target_month || proposal.month || ''],
        ['Alcance', proposal.scope_label || 'bloque seleccionado'],
      ];
    } else if (proposalKind === 'journal_entry' || proposalKind === 'journal_entry_proposal') {
      items = [
        ['Mes', proposal.target_month || proposal.month || ''],
        ['Descripcion', proposal.description || proposal.title || 'Partida doble'],
        ['Debe total', formatMoney(proposal.totals?.debit || proposal.amount || 0)],
        ['Haber total', formatMoney(proposal.totals?.credit || proposal.amount || 0)],
        ['Cuadra', proposal.totals?.balanced === false ? 'No' : 'Si'],
      ];
    } else if (proposalKind === 'compound_events') {
      items = [
        ['Mes', proposal.target_month || ''],
        ['Eventos nuevos', formatMoney(events.length)],
        ['Impacto caja', formatSignedMoney(proposal.impact?.cash || 0)],
        ['Impacto activos', formatSignedMoney(proposal.impact?.assets || 0)],
        ['Impacto pasivos', formatSignedMoney(proposal.impact?.liabilities || 0)],
        ['Impacto patrimonio', formatSignedMoney(proposal.impact?.equity || 0)],
      ];
    } else {
      items = [
        ['Mes objetivo', proposal.target_month || ''],
        ['Caja objetivo', formatMoney(proposal.target_cash)],
        ['Caja ajustada', formatMoney(proposal.adjusted_cash)],
        ['Diferencia', formatSignedMoney(proposal.difference)],
        ['Palanca', leverLabel(proposal.lever)],
      ];
    }
    if (!['journal_entry', 'journal_entry_proposal', 'compound_events', 'compound_voucher_correction'].includes(proposalKind) && proposal.cash_variability_pct !== undefined && proposal.cash_variability_pct !== null) {
      items.push(['Variabilidad caja', `+/- ${formatMoney(proposal.cash_variability_pct)}%`]);
    }
    if (!['journal_entry', 'journal_entry_proposal', 'compound_events', 'compound_voucher_correction'].includes(proposalKind) && proposal.target_min_cash !== undefined && proposal.target_max_cash !== undefined) {
      items.push(['Rango objetivo', `${formatMoney(proposal.target_min_cash)} - ${formatMoney(proposal.target_max_cash)}`]);
    }
    if (!['journal_entry', 'journal_entry_proposal', 'compound_events', 'compound_voucher_correction'].includes(proposalKind) && proposal.purchase_average_nio !== undefined) items.push(['Compras promedio C$', formatMoney(proposal.purchase_average_nio)]);
    if (!['journal_entry', 'journal_entry_proposal', 'compound_events', 'compound_voucher_correction'].includes(proposalKind) && proposal.purchase_average_usd !== undefined) items.push(['Compras promedio USD', formatMoney(proposal.purchase_average_usd)]);
    if (proposal.adjusted_min_cash !== undefined) items.push(['Caja mínima ajustada', formatMoney(proposal.adjusted_min_cash)]);
    if (proposal.adjusted_max_cash !== undefined) items.push(['Caja máxima ajustada', formatMoney(proposal.adjusted_max_cash)]);
    if (!['journal_entry', 'journal_entry_proposal', 'compound_events', 'compound_voucher_correction'].includes(proposalKind) && events.length) items.push(['Eventos nuevos', formatMoney(events.length)]);
    if (!['journal_entry', 'journal_entry_proposal', 'compound_events', 'compound_voucher_correction'].includes(proposalKind) && removedEvents.length) items.push(['Eventos removidos', formatMoney(removedEvents.length)]);
    items.forEach(([label, value]) => {
      const box = document.createElement('div');
      box.className = 'proposal-item';
      const span = document.createElement('span');
      span.textContent = label;
      const strong = document.createElement('strong');
      strong.textContent = value;
      box.append(span, strong);
      grid.appendChild(box);
    });
    wrap.appendChild(grid);

    const appendJournalTable = (heading, rows) => {
      if (!Array.isArray(rows)) return;
      if (heading) {
        const sub = document.createElement('div');
        sub.className = 'proposal-impact-title';
        sub.textContent = heading;
        wrap.appendChild(sub);
      }
      const journalTable = document.createElement('table');
      journalTable.className = 'journal-proposal-table';
      journalTable.innerHTML = '<thead><tr><th>Cuenta</th><th>Debe</th><th>Haber</th><th>Ref</th></tr></thead>';
      const tbody = document.createElement('tbody');
      rows.forEach(row => {
        const tr = document.createElement('tr');
        const account = document.createElement('td');
        account.textContent = row.account || '';
        const debit = document.createElement('td');
        debit.textContent = row.debit ? formatMoney(row.debit) : '-';
        const credit = document.createElement('td');
        credit.textContent = row.credit ? formatMoney(row.credit) : '-';
        const ref = document.createElement('td');
        ref.textContent = row.reference || '';
        tr.append(account, debit, credit, ref);
        tbody.appendChild(tr);
      });
      journalTable.appendChild(tbody);
      wrap.appendChild(journalTable);
    };
    if (proposalKind === 'compound_voucher_correction') {
      appendJournalTable(`1. Reverso de ${proposal.original_voucher_id || ''}`, proposal.reversal_rows);
      appendJournalTable('2. Nuevo asiento corregido', proposal.correction_rows);
    } else if ((proposalKind === 'journal_entry' || proposalKind === 'journal_entry_proposal' || proposalKind === 'compound_events' || proposalKind === 'voucher_reversal' || proposalKind === 'create_account') && Array.isArray(proposal.journal_rows)) {
      appendJournalTable('', proposal.journal_rows);
    }
    if (proposalKind === 'compound_events' && Array.isArray(proposal.event_labels)) {
      const list = document.createElement('ul');
      list.className = 'proposal-event-list';
      proposal.event_labels.forEach(label => {
        const li = document.createElement('li');
        li.textContent = label;
        list.appendChild(li);
      });
      wrap.appendChild(list);
    }

    const impact = proposal.impact;
    if (impact && Array.isArray(impact.items) && impact.items.length) {
      const impactSection = document.createElement('div');
      impactSection.className = 'proposal-impact';
      const impactTitle = document.createElement('div');
      impactTitle.className = 'proposal-impact-title';
      impactTitle.textContent = impact.month ? `Impacto al cierre de ${impact.month}` : 'Impacto en el modelo';
      impactSection.appendChild(impactTitle);
      const impactGrid = document.createElement('div');
      impactGrid.className = 'proposal-impact-grid';
      impact.items.forEach(item => {
        const cell = document.createElement('div');
        cell.className = 'proposal-impact-item';
        const label = document.createElement('span');
        label.textContent = item.label;
        const delta = document.createElement('strong');
        const value = Number(item.delta || 0);
        if (value === 0) {
          delta.textContent = 'sin cambio';
          delta.dataset.direction = 'neutral';
        } else if (value > 0) {
          delta.textContent = `+${formatMoney(value)}`;
          delta.dataset.direction = 'up';
        } else {
          delta.textContent = `-${formatMoney(Math.abs(value))}`;
          delta.dataset.direction = 'down';
        }
        cell.append(label, delta);
        impactGrid.appendChild(cell);
      });
      impactSection.appendChild(impactGrid);
      wrap.appendChild(impactSection);
    }
    if (proposalKind === 'assumption_change_proposal' && proposal.assumption_impact) {
      const impactSection = document.createElement('div');
      impactSection.className = 'proposal-impact';
      const impactTitle = document.createElement('div');
      impactTitle.className = 'proposal-impact-title';
      impactTitle.textContent = 'Impacto estimado';
      impactSection.appendChild(impactTitle);
      const impactGrid = document.createElement('div');
      impactGrid.className = 'proposal-impact-grid';
      [
        ['Ingresos', proposal.assumption_impact.revenue_total_delta],
        ['Costos totales', proposal.assumption_impact.cost_total_delta],
        ['Utilidad neta', proposal.assumption_impact.net_income_delta],
        ['Caja final', proposal.assumption_impact.cash_final_delta],
        ['Patrimonio final', proposal.assumption_impact.equity_final_delta],
      ].forEach(([labelText, rawValue]) => {
        const cell = document.createElement('div');
        cell.className = 'proposal-impact-item';
        const label = document.createElement('span');
        label.textContent = labelText;
        const delta = document.createElement('strong');
        const value = Number(rawValue || 0);
        delta.textContent = value === 0 ? 'sin cambio' : formatSignedMoney(value);
        delta.dataset.direction = value > 0 ? 'up' : value < 0 ? 'down' : 'neutral';
        cell.append(label, delta);
        impactGrid.appendChild(cell);
      });
      impactSection.appendChild(impactGrid);
      wrap.appendChild(impactSection);
    }

    if (proposal.explanation && !messageText) {
      const note = document.createElement('p');
      note.className = 'proposal-note';
      note.textContent = proposal.explanation;
      wrap.appendChild(note);
    }

    const technicalLines = [];
    const technicalRecords = Array.isArray(proposal.technical_records) ? proposal.technical_records : [];
    if (events.length) technicalLines.push(events.map(ev => formatEventLine(ev, { includeMessage: true })).join('\n'));
    if (journalEntries.length) technicalLines.push(journalEntries.map(entry => formatJournalLine(entry, { includeMessage: true })).join('\n'));
    if (technicalRecords.length) technicalLines.push(technicalRecords.map(record => JSON.stringify(record, null, 2)).join('\n\n'));
    if (removedEvents.length) technicalLines.push(`Eventos a remover:\n${removedEvents.map(ev => formatEventLine(ev, { includeMessage: true })).join('\n')}`);
    if (removedJournalEntries.length) technicalLines.push(`Partidas a remover:\n${removedJournalEntries.map(entry => formatJournalLine(entry, { includeMessage: true })).join('\n')}`);
    if (technicalLines.length) {
      const details = document.createElement('details');
      details.className = 'proposal-technical';
      const summary = document.createElement('summary');
      summary.textContent = `Ver registros tecnicos (${events.length + removedEvents.length + journalEntries.length + removedJournalEntries.length + technicalRecords.length})`;
      const eventLine = document.createElement('div');
      eventLine.className = 'proposal-event';
      eventLine.textContent = technicalLines.join('\n\n');
      details.append(summary, eventLine);
      wrap.appendChild(details);
    }

    const actions = document.createElement('div');
    actions.className = 'proposal-actions';
    const apply = document.createElement('button');
    apply.id = 'btnModelChatApply';
    apply.className = 'btn primary';
    apply.type = 'button';
    apply.textContent = proposal.confirm_label || (
      proposalKind === 'workflow'
        ? 'Confirmar'
        : proposalKind === 'voucher_reversal'
          ? 'Aplicar reverso'
          : proposalKind === 'create_account'
            ? 'Crear cuenta'
            : proposalKind === 'compound_voucher_correction'
              ? 'Aplicar correccion'
              : ['journal_entry', 'journal_entry_proposal', 'compound_events'].includes(proposalKind)
              ? 'Aplicar registro'
              : 'Aplicar propuesta'
    );
    const discard = document.createElement('button');
    discard.id = 'btnModelChatDiscard';
    discard.className = 'btn';
    discard.type = 'button';
    discard.textContent = 'Descartar';
    actions.append(apply, discard);
    wrap.appendChild(actions);

    if (proposal.expires_at) {
      const timer = document.createElement('div');
      timer.className = 'proposal-note';
      wrap.insertBefore(timer, actions);
      const expiresAt = new Date(proposal.expires_at).getTime();
      const updateTimer = () => {
        const left = Math.max(0, expiresAt - Date.now());
        const minutes = Math.floor(left / 60000);
        const seconds = Math.floor((left % 60000) / 1000);
        timer.textContent = left > 0 ? `Vence en ${minutes}:${String(seconds).padStart(2, '0')}` : 'Propuesta vencida';
        if (left <= 0) {
          apply.disabled = true;
          markProposalCardStatus(wrap, 'discarded', 'Propuesta vencida. Pedi una nueva.');
          clearInterval(intervalId);
        }
      };
      const intervalId = setInterval(updateTimer, 1000);
      updateTimer();
    }

    apply.addEventListener('click', onModelChatApply);
    discard.addEventListener('click', onModelChatDiscard);
    wrap.scrollIntoView({ block: 'nearest' });
  }

  function proposalTitle(kind) {
    if (kind === 'compound_voucher_correction') return 'Correccion contable propuesta';
    if (kind === 'journal_entry' || kind === 'journal_entry_proposal' || kind === 'compound_events' || kind === 'voucher_reversal') return 'Propuesta contable';
    if (kind === 'create_account') return 'Cuenta contable propuesta';
    if (kind === 'assumption_change' || kind === 'assumption_change_proposal') return 'Propuesta de supuesto';
    if (kind === 'workflow') return 'Accion propuesta';
    if (kind === 'period_change') return 'Cambio de periodo propuesto';
    return 'Propuesta del asistente';
  }

  function formatEventLine(ev, { includeMessage = false } = {}) {
    const base = `${ev.month || ''},${ev.account || ''},${formatEventAmount(ev.amount)},${ev.currency || 'nio'}`;
    if (!ev.source && !ev.instruction_id) return base;
    const metadata = `${base},${ev.source || ''},${ev.instruction_id || ''},${ev.locked === undefined ? '' : String(!!ev.locked)},${ev.created_at || ''}`;
    if (!includeMessage || !ev.message) return metadata;
    return `${metadata},${String(ev.message).replace(/[\r\n]+/g, ' ').replace(/,/g, ';')}`;
  }

  function formatJournalLine(entry, { includeMessage = false } = {}) {
    const base = `${entry.month || ''},${entry.debit_account || ''},${entry.credit_account || ''},${formatEventAmount(entry.amount)},${entry.currency || 'nio'}`;
    const metadata = `${base},${entry.source || ''},${entry.instruction_id || ''},${entry.locked === undefined ? '' : String(!!entry.locked)},${entry.created_at || ''}`;
    if (!includeMessage || !entry.message) return metadata;
    return `${metadata},${String(entry.message).replace(/[\r\n]+/g, ' ').replace(/,/g, ';')}`;
  }

  function renderAdjustmentHistory() {
    const wrap = qs('#modelChatHistory');
    if (!wrap) return;
    const events = parseModelEvents().filter(ev => ev.source === 'chat_financiero');
    const journalEntries = (modelJournalEntries || []).filter(entry => entry.source === 'chat_financiero');
    wrap.replaceChildren();
    if (!events.length && !journalEntries.length) {
      wrap.classList.add('empty-state');
      wrap.textContent = 'Sin ajustes aplicados por chat.';
      return;
    }
    wrap.classList.remove('empty-state');
    const groups = [];
    events.forEach(ev => {
      const id = ev.instruction_id || 'sin_id';
      let group = groups.find(item => item.id === id);
      if (!group) {
        group = {
          id,
          message: ev.message || 'Ajuste aplicado por chat',
          created_at: ev.created_at || '',
          events: [],
        };
        groups.push(group);
      }
      group.events.push(ev);
    });
    journalEntries.forEach(entry => {
      const id = entry.instruction_id || 'sin_id';
      let group = groups.find(item => item.id === id);
      if (!group) {
        group = {
          id,
          message: entry.message || 'Partida aplicada por chat',
          created_at: entry.created_at || '',
          events: [],
          journalEntries: [],
        };
        groups.push(group);
      }
      if (!group.journalEntries) group.journalEntries = [];
      group.journalEntries.push(entry);
    });
    groups.reverse().forEach(group => {
      const item = document.createElement('div');
      item.className = 'history-item';
      const strong = document.createElement('strong');
      strong.textContent = group.message;
      const meta = document.createElement('span');
      const journalCount = (group.journalEntries || []).length;
      meta.textContent = `${group.created_at || 'sin fecha'} | ${group.events.length} evento(s), ${journalCount} partida(s) | ${group.id}`;
      const lines = document.createElement('div');
      lines.className = 'history-events';
      lines.textContent = [
        ...group.events.map(formatEventLine),
        ...(group.journalEntries || []).map(formatJournalLine),
      ].join('\n');
      item.append(strong, meta, lines);
      wrap.appendChild(item);
    });
  }

  function formatAccountingMoney(value, { zeroAsDash = false, zeroDecimals = false } = {}) {
    const n = Number(value || 0);
    if (Math.abs(n) < 0.5) return zeroAsDash ? '-' : (zeroDecimals ? '0.00' : '0');
    const abs = Math.abs(n).toLocaleString('es-NI', { maximumFractionDigits: 0 });
    return n < 0 ? `(${abs})` : abs;
  }

  function formatDecimal(value, digits) {
    const n = Number(value || 0);
    return n.toLocaleString('es-NI', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function formatPercent(value) {
    const n = Number(value || 0) * 100;
    return `${n.toLocaleString('es-NI', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
  }

  function rowLabel(row) {
    const raw = row?.Descripcion ?? row?.Concepto ?? row?.Movimiento ?? row?.Cuenta ?? '';
    return String(raw ?? '').trim();
  }

  function isMonthColumn(col) {
    return /^\d{4}-\d{2}$/.test(String(col || ''));
  }

  function displayColumnHeader(col) {
    const text = String(col || '');
    if (isMonthColumn(text)) {
      const match = /^(\d{4})-(\d{2})$/.exec(text);
      const year = match?.[1] || '';
      const month = match?.[2] || '';
      const monthNames = {
        '01': 'ene', '02': 'feb', '03': 'mar', '04': 'abr',
        '05': 'may', '06': 'jun', '07': 'jul', '08': 'ago',
        '09': 'sept', '10': 'oct', '11': 'nov', '12': 'dic',
      };
      return `${monthNames[month] || month}-${year.slice(2)}`;
    }
    if (text === 'Descripcion') return 'Descripción';
    if (text === 'Acumulado del periodo') return 'Acumulado del período';
    return text;
  }

  function displayStatementLabel(value, rowIndex, tableKind) {
    if (tableKind === 'er') {
      if (rowIndex === 3) return `Contado ${formatPercent(value)}`;
      if (rowIndex === 4) return `Crédito ${formatPercent(value)}`;
    }
    return value ?? '';
  }

  function classifyStatementRow(row, rowIndex, tableKind) {
    const label = rowLabel(row);
    const plain = label.toLowerCase();
    const classes = [];
    if (!label) classes.push('stmt-empty-row');
    if (tableKind === 'er') {
      if (rowIndex <= 4) classes.push('stmt-assumption-row');
      if (plain === '(-) gastos operativos') classes.push('stmt-section-row', 'stmt-negative-label');
      if (plain.startsWith('(-)')) classes.push('stmt-negative-label');
      if (plain.startsWith('(=)')) classes.push('stmt-formula-row');
      if (plain === 'ingresos' || plain === '(=) ingresos brutos' || plain === 'total gastos operativos' || plain === 'ingresos/utilidad neta') {
        classes.push('stmt-total-row');
      }
      if (plain === 'ingresos/utilidad neta') classes.push('stmt-grand-total-row');
      const expenseRows = [
        'sueldos y salarios', 'servicios publicos', 'alcaldia y dgi', 'combustible', 'publicidad',
        'gastos financieros', 'mantenimientos', 'renta', 'gasto por depreciacion', 'seguros', 'otros gastos',
      ];
      if (expenseRows.includes(plain)) classes.push('stmt-subitem-row');
    }
    if (tableKind === 'esf') {
      const sectionRows = ['activos', 'pasivos', 'patrimonio', 'corrientes', 'no corrientes', 'propiedad planta y equipos'];
      if (sectionRows.includes(plain)) classes.push('stmt-section-row');
      if (plain.startsWith('(-)')) classes.push('stmt-negative-label');
      if (plain.startsWith('total ')) classes.push('stmt-total-row');
      if (['total activos', 'total pasivos', 'total patrimonio', 'total pasivo + patrimonio'].includes(plain)) {
        classes.push('stmt-grand-total-row');
      }
      if (label && !sectionRows.includes(plain) && !plain.startsWith('total ')) classes.push('stmt-subitem-row');
    }
    if (tableKind === 'movement') {
      if (['saldo inicial', 'aumentos', 'disminuciones', 'saldo final'].includes(plain)) classes.push('stmt-total-row');
    }
    return classes;
  }

  function formatTableValue(value, col, row, rowIndex, colIndex, tableKind) {
    if (value === null || value === undefined || value === '') return '';
    if (colIndex === 0) return displayStatementLabel(value, rowIndex, tableKind);
    if (tableKind === 'er') {
      if (isMonthColumn(col)) {
        if (rowIndex === 0) return formatDecimal(value, 2);
        if (rowIndex === 1) return formatPercent(value);
        if (rowIndex === 2) return formatDecimal(value, 4);
      }
      if (col === 'Base' && typeof value === 'number') return formatDecimal(value, 2);
      if (typeof value === 'number') return formatAccountingMoney(value, { zeroAsDash: true });
    }
    if (tableKind === 'esf') {
      if (typeof value === 'number') return formatAccountingMoney(value, { zeroDecimals: true });
    }
    if (typeof value === 'number') return formatMoney(value);
    return value ?? '';
  }

  function renderModelSummary(summary) {
    summary = summary || {};
    const wrap = qs('#modelSummary');
    if (!wrap) return;
    wrap.replaceChildren();
    const items = [
      ['Ingresos acumulados', formatMoney(summary.income_total)],
      ['Ingreso promedio', formatMoney(summary.income_average)],
      ['Utilidad acumulada', formatMoney(summary.net_income_total)],
      ['Utilidad promedio', formatMoney(summary.net_income_average)],
      ['Activos finales', formatMoney(summary.ending_assets)],
      ['Pasivos finales', formatMoney(summary.ending_liabilities)],
      ['Patrimonio final', formatMoney(summary.ending_equity)],
      ['Semilla', summary.seed || ''],
    ];
    items.forEach(([label, value]) => {
      const box = document.createElement('div');
      box.className = 'metric';
      const span = document.createElement('span');
      span.textContent = label;
      const strong = document.createElement('strong');
      strong.textContent = value;
      box.append(span, strong);
      wrap.appendChild(box);
    });
  }

  function getModelBlock(data) {
    const blocks = data?.period_blocks || [];
    const previews = data?.preview?.blocks || {};
    let blockId = selectedModelBlockId;
    if (!blockId || !previews[blockId]) {
      blockId = blocks[0]?.id || '';
      selectedModelBlockId = blockId;
    }
    const preview = blockId && previews[blockId] ? previews[blockId] : (data?.preview || {});
    const meta = blocks.find(block => block.id === blockId) || null;
    const summary = preview?.summary || data?.summary || {};
    return { blockId, preview, meta, summary };
  }

  function syncModelBlockSelector(data) {
    const toolbars = qsa('.model-block-toolbar');
    const selects = qsa('.model-block-select');
    if (!selects.length) return;
    const blocks = data?.period_blocks || [];
    selects.forEach(select => select.replaceChildren());
    if (blocks.length <= 1) {
      toolbars.forEach(toolbar => toolbar.classList.add('hidden'));
      selectedModelBlockId = blocks[0]?.id || '';
      return;
    }
    selects.forEach(select => {
      blocks.forEach(block => {
        const opt = document.createElement('option');
        opt.value = block.id;
        opt.textContent = block.label || block.id;
        select.appendChild(opt);
      });
    });
    if (!selectedModelBlockId || !blocks.some(block => block.id === selectedModelBlockId)) {
      selectedModelBlockId = blocks[0].id;
    }
    selects.forEach(select => { select.value = selectedModelBlockId; });
    toolbars.forEach(toolbar => toolbar.classList.remove('hidden'));
  }

  function renderModelData(data, { includeEsf = false, preferredAccount = '' } = {}) {
    lastModelPreviewData = data;
    lastModelRenderedEsf = !!includeEsf;
    syncModelBlockSelector(data);
    const block = getModelBlock(data);
    renderModelSummary(block.summary || data.summary || {});
    renderPreviewTable('#modelErPreview', block.preview?.er || {}, 'Sin Estado de Resultados.', 'er');
    if (includeEsf) {
      const negativeCash = block.summary?.negative_cash_months || [];
      populateAccountSelector(
        block.preview?.movimiento_cuentas || {},
        preferredAccount || (negativeCash.length ? 'Efectivo y Equivalentes de Efectivo' : '')
      );
      renderPreviewTable('#modelEsfPreview', block.preview?.esf_mensual || {}, 'Sin Estado de Situacion Financiera.', 'esf');
    }
    renderAccounting(data, preferredAccount);
    qs('#modelPreviewCard')?.classList.remove('hidden');
  }

  function buildModelChatScope() {
    const mode = inputValue('modelChatScopeSelect') || 'block';
    if (mode === 'global') return { mode: 'global' };
    if (!lastModelPreviewData) return { mode: 'block' };
    const block = getModelBlock(lastModelPreviewData);
    return {
      mode: 'block',
      block_id: block.meta?.id || block.blockId || '',
      label: block.meta?.label || '',
      months: block.meta?.months || [],
    };
  }

  function buildModelChatUiContext() {
    const block = lastModelPreviewData ? getModelBlock(lastModelPreviewData) : null;
    const months = block?.meta?.months || lastModelPreviewData?.summary?.months || [];
    return {
      scope: buildModelChatScope(),
      selected_block_id: block?.meta?.id || block?.blockId || '',
      selected_block_label: block?.meta?.label || '',
      selected_account: selectedAccountingAccount() || qs('#modelAccountSelect')?.value || '',
      selected_month: months[months.length - 1] || '',
      selected_voucher_type: selectedAccountingType(),
      selected_voucher: selectedChatVoucherId,
    };
  }

  function renderPreviewTable(containerSel, tableData, emptyText = 'Sin datos para mostrar.', tableKind = 'generic') {
    const wrap = qs(containerSel);
    if (!wrap) return;
    wrap.replaceChildren();
    wrap.classList.remove('empty-state');
    const columns = tableData?.columns || [];
    const rows = tableData?.rows || [];
    if (!columns.length || !rows.length) {
      wrap.classList.add('empty-state');
      wrap.textContent = emptyText;
      return;
    }
    const table = document.createElement('table');
    table.className = `statement-table statement-${tableKind}`;
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    columns.forEach(col => {
      const th = document.createElement('th');
      th.textContent = displayColumnHeader(col);
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    const tbody = document.createElement('tbody');
    rows.forEach((row, rowIndex) => {
      const tr = document.createElement('tr');
      classifyStatementRow(row, rowIndex, tableKind).forEach(cls => tr.classList.add(cls));
      columns.forEach((col, colIndex) => {
        const td = document.createElement('td');
        const value = row[col];
        td.textContent = formatTableValue(value, col, row, rowIndex, colIndex, tableKind);
        if (typeof value === 'number' && value < 0) td.classList.add('negative');
        if (colIndex === 0) td.classList.add('label-cell');
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    wrap.appendChild(table);
  }

  function populateAccountSelector(tableData, preferredAccount = '') {
    const select = qs('#modelAccountSelect');
    if (!select) return;
    lastAccountMovementPreview = tableData || null;
    select.replaceChildren();
    const rows = tableData?.rows || [];
    const accounts = [];
    rows.forEach(row => {
      const account = row.Cuenta;
      if (account && !accounts.includes(account)) accounts.push(account);
    });
    if (!accounts.length) {
      renderPreviewTable('#modelAccountMovementPreview', {}, 'Sin movimientos de cuentas.');
      return;
    }
    accounts.forEach(account => {
      const opt = document.createElement('option');
      opt.value = account;
      opt.textContent = account;
      select.appendChild(opt);
    });
    if (preferredAccount && accounts.includes(preferredAccount)) {
      select.value = preferredAccount;
    } else if (accounts.length) {
      select.value = accounts[0];
    }
    renderSelectedAccountMovement();
  }

  function renderSelectedAccountMovement() {
    const select = qs('#modelAccountSelect');
    if (!select || !lastAccountMovementPreview) {
      renderPreviewTable('#modelAccountMovementPreview', {}, 'Sin movimientos de cuentas.');
      return;
    }
    const account = select.value;
    const filtered = {
      columns: lastAccountMovementPreview.columns || [],
      rows: (lastAccountMovementPreview.rows || []).filter(row => row.Cuenta === account),
    };
    renderPreviewTable('#modelAccountMovementPreview', filtered, 'Sin movimientos para la cuenta seleccionada.', 'movement');
    renderAccounting(lastModelPreviewData, account);
  }

  function renderAccounting(data, preferredAccount = '') {
    const accounting = data?.accounting || {};
    const vouchers = accounting.vouchers || [];
    const ledger = accounting.ledger || [];
    if (!vouchers.length) {
      setEmpty('#modelVoucherPreview', 'Sin comprobantes.');
      setEmpty('#modelLedgerPreview', 'Sin mayor contable.');
      setEmpty('#modelTracePreview', 'Sin trazabilidad.');
      return;
    }
    populateAccountingFilters(vouchers, ledger, preferredAccount);
    renderVoucherTable(vouchers);
    renderLedgerTable(ledger);
    renderTracePanel(accounting);
  }

  function setEmpty(sel, text) {
    const el = qs(sel);
    if (!el) return;
    el.classList.add('empty-state');
    el.replaceChildren();
    el.textContent = text;
  }

  function populateAccountingFilters(vouchers, ledger, preferredAccount = '') {
    const typeSelect = qs('#modelVoucherTypeFilter');
    const accountSelect = qs('#modelVoucherAccountFilter');
    if (typeSelect && !typeSelect.options.length) {
      const types = ['Todos', ...Array.from(new Set(vouchers.map(v => v.type).filter(Boolean))).sort()];
      types.forEach(type => {
        const opt = document.createElement('option');
        opt.value = type === 'Todos' ? '' : type;
        opt.textContent = type;
        typeSelect.appendChild(opt);
      });
    }
    if (accountSelect) {
      const current = preferredAccount || accountSelect.value || qs('#modelAccountSelect')?.value || '';
      const accounts = Array.from(new Set(ledger.map(line => line.account).filter(Boolean))).sort();
      accountSelect.replaceChildren();
      accounts.forEach(account => {
        const opt = document.createElement('option');
        opt.value = account;
        opt.textContent = account;
        accountSelect.appendChild(opt);
      });
      if (current && accounts.includes(current)) accountSelect.value = current;
      else if (accounts.length) accountSelect.value = accounts[0];
    }
  }

  function selectedAccountingType() {
    return qs('#modelVoucherTypeFilter')?.value || '';
  }

  function selectedAccountingAccount() {
    return qs('#modelVoucherAccountFilter')?.value || qs('#modelAccountSelect')?.value || '';
  }

  function renderVoucherTable(vouchers) {
    const type = selectedAccountingType();
    const account = selectedAccountingAccount();
    const rows = [];
    vouchers.forEach(voucher => {
      if (type && voucher.type !== type) return;
      const lines = voucher.lines || [];
      if (account && !lines.some(line => line.account === account)) return;
      rows.push({
        Comprobante: voucher.voucher_id,
        Mes: voucher.month,
        Tipo: voucher.type,
        Origen: voucher.source,
        Descripcion: voucher.description,
        Debe: voucher.debit_total,
        Haber: voucher.credit_total,
      });
    });
    renderSimpleTable('#modelVoucherPreview', ['Comprobante', 'Mes', 'Tipo', 'Origen', 'Descripcion', 'Debe', 'Haber'], rows, 'Sin comprobantes para el filtro.');
  }

  function renderLedgerTable(ledger) {
    const account = selectedAccountingAccount();
    const rows = ledger
      .filter(line => !account || line.account === account)
      .map(line => ({
        Mes: line.month,
        Comprobante: line.voucher_id,
        Descripcion: line.description,
        Debe: line.debit,
        Haber: line.credit,
        Saldo: line.running_balance,
      }));
    renderSimpleTable('#modelLedgerPreview', ['Mes', 'Comprobante', 'Descripcion', 'Debe', 'Haber', 'Saldo'], rows, 'Sin movimientos para la cuenta.');
  }

  function renderTracePanel(accounting) {
    const wrap = qs('#modelTracePreview');
    if (!wrap) return;
    const account = selectedAccountingAccount();
    const block = lastModelPreviewData ? getModelBlock(lastModelPreviewData) : null;
    const months = block?.meta?.months || lastModelPreviewData?.summary?.months || [];
    const month = months[months.length - 1] || '';
    const trace = accounting?.trace?.[`${account}|${month}`];
    wrap.replaceChildren();
    wrap.classList.remove('empty-state');
    if (!trace) {
      wrap.classList.add('empty-state');
      wrap.textContent = 'Sin trazabilidad para la cuenta seleccionada.';
      return;
    }
    const summary = document.createElement('div');
    summary.className = 'trace-summary';
    summary.textContent = `${account} ${month}: saldo inicial ${formatMoney(trace.opening_balance)}, debe ${formatMoney(trace.debits)}, haber ${formatMoney(trace.credits)}, saldo final ${formatMoney(trace.closing_balance)}.`;
    wrap.appendChild(summary);
    renderSimpleTableElement(
      wrap,
      ['Comprobante', 'Descripcion', 'Debe', 'Haber', 'Saldo'],
      (trace.entries || []).map(line => ({
        Comprobante: line.voucher_id,
        Descripcion: line.description,
        Debe: line.debit,
        Haber: line.credit,
        Saldo: line.running_balance,
      }))
    );
  }

  function renderSimpleTable(containerSel, columns, rows, emptyText) {
    const wrap = qs(containerSel);
    if (!wrap) return;
    wrap.replaceChildren();
    wrap.classList.remove('empty-state');
    if (!rows.length) {
      wrap.classList.add('empty-state');
      wrap.textContent = emptyText;
      return;
    }
    renderSimpleTableElement(wrap, columns, rows);
  }

  function renderSimpleTableElement(wrap, columns, rows) {
    const table = document.createElement('table');
    table.className = 'statement-table statement-ledger';
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    columns.forEach(col => {
      const th = document.createElement('th');
      th.textContent = col;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    const tbody = document.createElement('tbody');
    rows.forEach(row => {
      const tr = document.createElement('tr');
      columns.forEach((col, idx) => {
        const td = document.createElement('td');
        const value = row[col];
        td.textContent = typeof value === 'number' ? formatAccountingMoney(value, { zeroAsDash: true }) : (value ?? '');
        if (typeof value === 'number' && value < 0) td.classList.add('negative');
        if (idx === 0) td.classList.add('label-cell');
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    wrap.appendChild(table);
  }

  async function fetchModelPreview(loadingText) {
    setModelMessage(loadingText);
    const payload = buildModelPayload();
    const resp = await fetch('/api/model/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'El modelo tiene validaciones pendientes');
    lastModelPayload = payload;
    lastModelPreviewData = data;
    return data;
  }

  function setGenerateEnabled(enabled) {
    const btn = qs('#btnModeloGenerar');
    if (btn) btn.disabled = !enabled;
  }

  function showModelOutcome(data, fallbackMessage, enableGenerate, showCashWarning = true) {
    const block = getModelBlock(data);
    renderModelSummary(block.summary || data.summary || {});
    qs('#modelPreviewCard')?.classList.remove('hidden');
    setGenerateEnabled(enableGenerate);

    const negativeCash = block.summary?.negative_cash_months || [];
    if (showCashWarning && negativeCash.length) {
      const months = negativeCash.map(x => `${x.month}: ${formatMoney(x.cash)}`).join('; ');
      setModelMessage(`Advertencia de caja: el efectivo queda negativo. Seleccione Efectivo y Equivalentes de Efectivo para revisar el detalle (${months}).`, 'warning');
      return;
    }
    setModelMessage(fallbackMessage, 'info');
  }

  async function onModelErPreview() {
    try {
      const data = await fetchModelPreview('Calculando Estado de Resultados...');
      renderModelData(data, { includeEsf: false });
      showModelOutcome(data, 'ER calculado. Puede continuar con saldos iniciales y generar el ESF.', false, false);
    } catch (e) {
      setGenerateEnabled(false);
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function onModelEsfPreview() {
    try {
      const data = await fetchModelPreview('Calculando Estado de Situacion Financiera...');
      renderModelData(data, { includeEsf: true });
      showModelOutcome(data, 'Modelo validado: ER, ESF, caja y balance cuadran.', true);
    } catch (e) {
      setGenerateEnabled(false);
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function onModelPreview() {
    try {
      const data = await fetchModelPreview('Calculando modelo completo...');
      renderModelData(data, { includeEsf: true });
      showModelOutcome(data, 'Modelo validado: ER, ESF, caja y balance cuadran.', true);
    } catch (e) {
      setGenerateEnabled(false);
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function onModelGenerate() {
    try {
      const payload = lastModelPayload || buildModelPayload();
      setModelMessage('Generando documento...');
      const resp = await fetch('/api/model/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        let msg = 'Error generando documento';
        try {
          const data = await resp.json();
          msg = data.error || msg;
        } catch {}
        throw new Error(msg);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `certificacion_modelo_${(payload.period.seed || 'app').slice(0, 24)}.docx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setModelMessage('Documento generado.');
    } catch (e) {
      setModelMessage(String(e.message || e), 'error');
    }
  }

  async function executeChatWorkflow(workflow) {
    const action = workflow?.action || workflow?.workflow_action || '';
    markPendingChatCommandApplied();
    if (action === 'save_draft') {
      appendChatMessage('Guardando borrador...', 'app');
      await saveCurrentDraft();
      appendChatMessage('Borrador guardado.', 'app');
      return;
    }
    if (action === 'save_final') {
      appendChatMessage('Guardando version final...', 'app');
      await saveCurrentFinal();
      appendChatMessage('Version final guardada.', 'app');
      return;
    }
    if (action === 'generate_document') {
      appendChatMessage('Generando documento...', 'app');
      await onModelGenerate();
      appendChatMessage('Documento generado.', 'app');
      return;
    }
    appendChatMessage('No reconozco el flujo solicitado.', 'error');
  }

  function applyChatUiActions(actions) {
    (actions || []).forEach(action => {
      if (!action || !action.type) return;
      if (action.type === 'select_account') {
        selectAccountEverywhere(action.account || '');
      } else if (action.type === 'open_saved_models') {
        showSavedModelsPanel();
        if (action.filter) setFieldValue('savedModelsFilter', action.filter);
        renderSavedModels();
      } else if (action.type === 'scroll_to') {
        scrollToChatTarget(action.target || '');
      } else if (action.type === 'select_voucher') {
        highlightVoucher(action.voucher_id || '');
      }
    });
  }

  function selectAccountEverywhere(account) {
    if (!account) return;
    const accountSelect = qs('#modelAccountSelect');
    if (accountSelect && Array.from(accountSelect.options).some(opt => opt.value === account)) {
      accountSelect.value = account;
      renderSelectedAccountMovement();
    }
    const voucherAccount = qs('#modelVoucherAccountFilter');
    if (voucherAccount && Array.from(voucherAccount.options).some(opt => opt.value === account)) {
      voucherAccount.value = account;
      if (lastModelPreviewData) renderAccounting(lastModelPreviewData, account);
    }
  }

  function highlightVoucher(voucherId) {
    if (!voucherId) return;
    selectedChatVoucherId = voucherId;
    const typeSelect = qs('#modelVoucherTypeFilter');
    if (typeSelect) typeSelect.value = '';
    if (lastModelPreviewData) renderAccounting(lastModelPreviewData);
    qsa('#modelVoucherPreview tr').forEach(row => {
      const firstCell = row.querySelector('td');
      row.classList.toggle('row-highlight', !!firstCell && firstCell.textContent === voucherId);
    });
  }

  function scrollToChatTarget(target) {
    const targets = {
      accounting: '#accountingWorkbench',
      ledger: '#accountingWorkbench',
      vouchers: '#accountingWorkbench',
      client_documents: '.doc-extract-panel',
      saved_models: '#savedModelsPanel',
      chat: '#modelChatCard',
    };
    const el = qs(targets[target] || target);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function onModelChatSend() {
    const input = qs('#modelChatInput');
    const message = input ? input.value.trim() : '';
    if (!message) return;
    appendChatMessage(message, 'user');
    if (pendingChatData && isChatApplyCommand(message)) {
      await onModelChatApply();
      if (input) input.value = '';
      return;
    }
    if (pendingChatData && isChatDiscardCommand(message)) {
      onModelChatDiscard();
      if (input) input.value = '';
      return;
    }
    await requestModelChatProposal(message);
    if (input) input.value = '';
  }

  async function requestModelChatProposal(message) {
    clearPendingChatProposal();
    try {
      appendChatMessage('Estoy revisando el modelo...', 'app');
      const useAgent = !!activePeriodoId;
      const uiContext = buildModelChatUiContext();
      const endpoint = useAgent ? '/api/agent/command' : '/api/model/chat/command';
      const body = useAgent
        ? { periodo_id: activePeriodoId, message, ui_context: uiContext, current_payload: buildModelPayload(), is_dirty: !!editorDirty }
        : { payload: buildModelPayload(), message, scope: buildModelChatScope(), ui_context: uiContext };
      const resp = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      data.agent_mode = useAgent;
      const bubbles = qsa('#modelChatMessages .chat-bubble');
      const lastBubble = bubbles[bubbles.length - 1];
      if (lastBubble && lastBubble.textContent === 'Estoy revisando el modelo...') lastBubble.remove();
      const assistantText = data.assistant_message || data.error || 'No pude completar la instruccion.';
      if (data.ui_actions) applyChatUiActions(data.ui_actions);
      if (['answer', 'ui_action', 'navigation'].includes(data.response_type)) {
        appendChatMessage(assistantText, 'app');
        renderAgentToolData(data.data);
        setModelMessage(assistantText, 'info');
        return;
      }
      if (data.response_type === 'clarification' || data.response_type === 'question') {
        appendChatMessage(assistantText, 'app');
        setModelMessage(assistantText, 'warning');
        return;
      }
      if (!resp.ok || !data.ok) {
        appendChatMessage(assistantText, 'error');
        setModelMessage(assistantText, 'error');
        return;
      }
      pendingChatPayload = data.adjusted_payload || null;
      pendingChatData = data;
      renderChatProposal(data);
      setModelMessage(useAgent
        ? 'Propuesta creada en SQLite. Revise el registro y confirme si desea aplicarla.'
        : 'Propuesta calculada. Revise el impacto y aplique el ajuste si esta conforme.', 'info');
    } catch (e) {
      appendChatMessage(String(e.message || e), 'error');
      setModelMessage(String(e.message || e), 'error');
    }
  }

  function normalizeChatCommand(text) {
    return String(text || '')
      .toLowerCase()
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .trim();
  }

  function isChatApplyCommand(text) {
    const value = normalizeChatCommand(text);
    return ['aplica', 'aplicalo', 'aplicar', 'dale', 'ok', 'confirmo', 'confirmar', 'si', 'proceda', 'procede'].includes(value)
      || value.startsWith('aplica ')
      || value.startsWith('confirmo ');
  }

  function isChatDiscardCommand(text) {
    const value = normalizeChatCommand(text);
    return ['descarta', 'descartar', 'cancelar', 'cancela', 'no', 'olvidalo'].includes(value);
  }

  async function onModelChatApply() {
    if (!pendingChatData) return;
    const activeBubble = pendingChatData.proposalElement;
    if (pendingChatData.agent_mode) {
      const proposalId = pendingChatData.proposal?.id;
      if (!proposalId) {
        appendChatMessage('La propuesta no tiene identificador para aplicar.', 'error');
        return;
      }
      try {
        const applied = await fetchJson(`/api/agent/proposals/${encodeURIComponent(proposalId)}/apply`, { method: 'POST' });
        markProposalCardStatus(activeBubble, 'applied', `Aplicada — ${applied.assistant_message || 'Listo, aplique la propuesta al periodo.'}`);
        pendingChatPayload = null;
        pendingChatData = null;
        if (activePeriodoId) {
          const data = await fetchJson(`/api/periodos/${encodeURIComponent(activePeriodoId)}`);
          activePeriodoDetail = data;
          if (data.periodo?.payload) {
            runWithoutEditorDirty(() => applyModelPayload(data.periodo.payload, { draftId: null }));
          }
          updateEditorHeader();
          markEditorDirty(false);
          await onModelPreview();
        }
      } catch (e) {
        appendChatMessage(String(e.message || e), 'error');
        setModelMessage(String(e.message || e), 'error');
      }
      return;
    }
    if (pendingChatData.response_type === 'workflow') {
      await executeChatWorkflow(pendingChatData.workflow || pendingChatData.proposal || {});
      clearPendingChatProposal();
      return;
    }
    if (!pendingChatPayload) return;
    markPendingChatCommandApplied(pendingChatPayload);
    const payloadToApply = pendingChatPayload;
    const proposalKind = pendingChatData?.proposal?.kind || '';
    const events = pendingChatData?.new_events || pendingChatData?.proposal?.events || [];
    const journalEntries = pendingChatData?.new_journal_entries || [];
    const removedEvents = pendingChatData?.removed_events || [];
    const removedJournalEntries = pendingChatData?.removed_journal_entries || [];
    applyModelPayload(payloadToApply, { draftId: currentDraftId });
    markEditorDirty(true);
    if (proposalKind === 'journal_entry' || proposalKind === 'journal_entry_proposal' || proposalKind === 'voucher_reversal') {
      appendChatMessage(`${journalEntries.length || 1} comprobante(s) aplicado(s) al modelo.`, 'app');
    } else if (proposalKind === 'compound_events') {
      appendChatMessage(`Comprobante compuesto aplicado al modelo (${events.length} evento(s) tecnicos).`, 'app');
    } else if (proposalKind === 'assumption_change' || proposalKind === 'assumption_change_proposal') {
      appendChatMessage('Supuesto aplicado al modelo.', 'app');
    } else if (events.length) {
      appendChatMessage(`${events.length} evento(s) aplicado(s) al listado de eventos.`, 'app');
    } else if (removedEvents.length || removedJournalEntries.length) {
      appendChatMessage(`${removedEvents.length + removedJournalEntries.length} registro(s) removido(s) del modelo.`, 'app');
    } else {
      appendChatMessage('No habia ajuste que aplicar.', 'app');
    }
    setGenerateEnabled(false);
    await onModelPreview();
  }

  function markPendingChatCommandApplied(targetPayload = null) {
    const audit = pendingChatData?.audit || {};
    if (!audit.command_id) return;
    const appliedAt = new Date().toISOString();
    const target = targetPayload || buildModelPayload();
    const chat = { ...(target.chat || {}) };
    const commands = Array.isArray(chat.commands) ? chat.commands.map(item => ({ ...item })) : [];
    let found = false;
    commands.forEach(command => {
      if (command.command_id === audit.command_id) {
        command.status = 'applied';
        command.applied_at = appliedAt;
        found = true;
      }
    });
    if (!found) {
      commands.push({
        command_id: audit.command_id,
        message: audit.message || '',
        intent: pendingChatData?.intent || '',
        source: audit.source || 'chat_financiero',
        created_at: audit.created_at || appliedAt,
        applied_at: appliedAt,
        status: 'applied',
      });
    }
    chat.commands = commands;
    target.chat = chat;
    modelChatCommands = commands.map(item => ({ ...item }));
  }

  function onModelChatDiscard() {
    if (!pendingChatData) return;
    const activeBubble = pendingChatData.proposalElement;
    if (pendingChatData?.agent_mode && pendingChatData?.proposal?.id) {
      fetchJson(`/api/agent/proposals/${encodeURIComponent(pendingChatData.proposal.id)}/discard`, { method: 'POST' })
        .catch(() => {});
    }
    markProposalCardStatus(activeBubble, 'discarded', 'Propuesta descartada.');
    pendingChatPayload = null;
    pendingChatData = null;
  }

  async function onUndoLastChatAdjustment() {
    const hasChatEvents = parseModelEvents().some(ev => ev.source === 'chat_financiero' && ev.instruction_id);
    if (!hasChatEvents) {
      setModelMessage('No hay ajustes aplicados por chat para deshacer.', 'warning');
      appendChatMessage('No hay ajustes aplicados por chat para deshacer.', 'error');
      return;
    }
    appendChatMessage('deshacer ultimo ajuste', 'user');
    await requestModelChatProposal('deshacer último ajuste');
  }

  function activateMode(target) {
    if (!target) return;
    qsa('.mode-tab').forEach(b => b.classList.toggle('active', b.getAttribute('data-mode-target') === target));
    qsa('.mode-panel').forEach(panel => panel.classList.toggle('hidden', panel.id !== target));
    if (target === 'clientesMode') refreshClientes();
  }

  qsa('.mode-tab').forEach((btn) => {
    btn.addEventListener('click', () => activateMode(btn.getAttribute('data-mode-target')));
  });

  qsa('#modelMode input, #modelMode select, #modelMode textarea').forEach((el) => {
    if (!el.id || !el.id.startsWith('m_')) return;
    el.addEventListener('input', () => {
      clearPendingChatProposal();
      if (el.id === 'm_costo_pct' || el.id === 'm_var_costo') modelMonthlyOverrides = [];
      if (el.id === 'm_eventos') renderAdjustmentHistory();
      setGenerateEnabled(false);
    });
    el.addEventListener('change', () => {
      clearPendingChatProposal();
      if (el.id === 'm_eventos') renderAdjustmentHistory();
      setGenerateEnabled(false);
    });
  });

  qs('#btnModeloER')?.addEventListener('click', onModelErPreview);
  qs('#btnModeloESF')?.addEventListener('click', onModelEsfPreview);
  qs('#btnModeloPreview')?.addEventListener('click', onModelPreview);
  qs('#btnModeloGenerar')?.addEventListener('click', onModelGenerate);
  qs('#btnSaveDraft')?.addEventListener('click', saveCurrentDraft);
  qs('#btnOpenSavedModels')?.addEventListener('click', showSavedModelsPanel);
  qs('#btnSaveFinal')?.addEventListener('click', saveCurrentFinal);
  qs('#btnRefreshSavedModels')?.addEventListener('click', refreshSavedModels);
  qs('#savedModelsFilter')?.addEventListener('input', renderSavedModels);
  qs('#btnRefreshClientes')?.addEventListener('click', refreshClientes);
  qs('#btnNewCliente')?.addEventListener('click', () => openClienteForm({ mode: 'new' }));
  qs('#btnCloseClienteForm')?.addEventListener('click', () => qs('#clienteFormPanel')?.classList.add('hidden'));
  qs('#btnSaveCliente')?.addEventListener('click', saveCliente);
  qs('#btnDeleteCliente')?.addEventListener('click', deleteCliente);
  qs('#btnClienteExtractDocs')?.addEventListener('click', extractClienteDocs);
  qs('#btnUseClienteInModel')?.addEventListener('click', () => useClienteInModel(currentClienteDetail));
  qs('#btnAddTemplateLine')?.addEventListener('click', () => addTemplateRow('', 0));
  qs('#btnResetTemplate')?.addEventListener('click', resetClienteTemplateToGiro);
  qs('#btnSaveTemplate')?.addEventListener('click', saveClienteTemplate);
  qs('#btnNewPeriodo')?.addEventListener('click', openPeriodoForm);
  qs('#btnClosePeriodoForm')?.addEventListener('click', closePeriodoForm);
  qs('#btnSavePeriodo')?.addEventListener('click', savePeriodo);
  qs('#periodo_rollforward')?.addEventListener('change', refreshRollforwardPreview);
  qs('#periodo_mes_inicial')?.addEventListener('change', refreshRollforwardPreview);
  qs('#clientesGiroFilter')?.addEventListener('change', loadClientes);
  qs('#c_giro_negocio_id')?.addEventListener('change', () => {
    if (!currentClienteId) resetClienteTemplateToGiro();
  });
  qs('#clientesSearch')?.addEventListener('input', () => {
    clearTimeout(clientesSearchTimer);
    clientesSearchTimer = setTimeout(loadClientes, 300);
  });
  qs('#btnExtractClientDocs')?.addEventListener('click', onExtractClientDocs);
  qs('#modelAccountSelect')?.addEventListener('change', renderSelectedAccountMovement);
  qs('#modelVoucherTypeFilter')?.addEventListener('change', () => {
    if (lastModelPreviewData) renderAccounting(lastModelPreviewData);
  });
  qs('#modelVoucherAccountFilter')?.addEventListener('change', () => {
    if (lastModelPreviewData) renderAccounting(lastModelPreviewData);
  });
  qsa('.model-block-select').forEach(select => {
    select.addEventListener('change', () => {
      selectedModelBlockId = select.value;
      if (lastModelPreviewData) renderModelData(lastModelPreviewData, { includeEsf: lastModelRenderedEsf });
    });
  });
  qs('#modelChatScopeSelect')?.addEventListener('change', clearPendingChatProposal);
  qs('#btnModelChatSend')?.addEventListener('click', onModelChatSend);

  // ====================== Editor avanzado: event listeners ======================
  qs('#editableSelector')?.addEventListener('change', (ev) => selectEditablePeriodo(ev.target.value));
  qs('#btnRefreshEditablePeriodos')?.addEventListener('click', loadEditablePeriodos);
  qs('#btnSavePeriodoChanges')?.addEventListener('click', saveActivePeriodoChanges);
  qs('#btnDuplicateActivePeriodo')?.addEventListener('click', duplicateActivePeriodo);
  // Marcar dirty cuando el usuario cambia cualquier input del editor (solo si hay periodo activo borrador)
  qsa('#modelMode input, #modelMode select, #modelMode textarea').forEach(el => {
    if (['editableSelector', 'modelChatInput', 'modelChatScopeSelect', 'modelAccountSelect',
         'modelBlockSelectSummary', 'modelVoucherTypeFilter', 'modelVoucherAccountFilter'].includes(el.id)) return;
    el.addEventListener('input', () => markEditorDirty(true));
    el.addEventListener('change', () => markEditorDirty(true));
  });
  // Cargar periodos editables al inicio
  loadEditablePeriodos();
  qs('#btnUndoLastChatAdjustment')?.addEventListener('click', onUndoLastChatAdjustment);
  qsa('.chat-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const input = qs('#modelChatInput');
      if (!input) return;
      input.value = chip.getAttribute('data-chat-message') || '';
      input.focus();
    });
  });
  qs('#modelChatInput')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      onModelChatSend();
    }
  });
  renderAdjustmentHistory();
})();
