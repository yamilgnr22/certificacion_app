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
      if (!resp.ok) throw new Error('Error generando el documento');
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
        const [month, account, amount, currency] = line.split(',').map(x => (x || '').trim());
        return { month, account, amount: Number(amount || 0), currency: currency || 'nio' };
      })
      .filter(ev => ev.month && ev.account && Number.isFinite(ev.amount));
  }

  function buildModelPayload() {
    return {
      client: {
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
        end_month: inputValue('m_mes_final'),
        months: numberValue('m_cantidad_meses', 6),
        exchange_rate: numberValue('m_tasa_cambio', 36.6243),
        seed: inputValue('m_semilla'),
      },
      income: {
        base_income_usd: numberValue('m_ingresos_base', 100000),
        income_variability_pct: numberValue('m_var_ingresos', 15),
        cost_pct: numberValue('m_costo_pct', 70),
        cost_variability_pct: numberValue('m_var_costo', 5),
        cash_sales_pct: numberValue('m_contado_pct', 85),
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
      },
    };
  }

  function formatMoney(value) {
    const n = Number(value || 0);
    return n.toLocaleString('es-NI', { maximumFractionDigits: 0 });
  }

  function renderModelSummary(summary) {
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

  function renderPreviewTable(containerSel, tableData) {
    const wrap = qs(containerSel);
    if (!wrap) return;
    wrap.replaceChildren();
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    (tableData.columns || []).forEach(col => {
      const th = document.createElement('th');
      th.textContent = col;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    const tbody = document.createElement('tbody');
    (tableData.rows || []).forEach(row => {
      const tr = document.createElement('tr');
      (tableData.columns || []).forEach(col => {
        const td = document.createElement('td');
        const value = row[col];
        td.textContent = typeof value === 'number' ? formatMoney(value) : (value ?? '');
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    wrap.appendChild(table);
  }

  async function onModelPreview() {
    try {
      setModelMessage('Calculando modelo...');
      const payload = buildModelPayload();
      const resp = await fetch('/api/model/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || 'El modelo tiene validaciones pendientes');
      lastModelPayload = payload;
      renderModelSummary(data.summary || {});
      renderPreviewTable('#modelErPreview', data.preview?.er || {});
      renderPreviewTable('#modelEsfPreview', data.preview?.esf_mensual || {});
      qs('#modelPreviewCard')?.classList.remove('hidden');
      const btn = qs('#btnModeloGenerar');
      if (btn) btn.disabled = false;
      setModelMessage('Modelo validado: ER, ESF y balance cuadran.', 'info');
    } catch (e) {
      const btn = qs('#btnModeloGenerar');
      if (btn) btn.disabled = true;
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

  qsa('.mode-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-mode-target');
      qsa('.mode-tab').forEach(b => b.classList.toggle('active', b === btn));
      qsa('.mode-panel').forEach(panel => panel.classList.toggle('hidden', panel.id !== target));
    });
  });

  qs('#btnModeloPreview')?.addEventListener('click', onModelPreview);
  qs('#btnModeloGenerar')?.addEventListener('click', onModelGenerate);
})();
