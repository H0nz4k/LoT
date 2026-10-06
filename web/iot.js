'use strict';

const $ = id => document.getElementById(id);
const app = {modules: [], selected: null, busy: false, loading: false, epoch: 0,
  pendingTemp: null, tempTimer: null, editing: null, candidates: [], dialogModule: null,
  formBusy: false, controllerId: null, toastTimer: null};
try { app.selected = localStorage.getItem('hanzhub-iot-selected'); } catch (_) {}
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<svg aria-hidden="true"><use href="#icon-${name}"/></svg>`;
const current = () => app.modules.find(m => m.id === app.selected);
const timeLabel = value => value ? new Date(value).toLocaleTimeString('cs-CZ', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
const plural = (n, one, few, many) => n === 1 ? one : n >= 2 && n <= 4 ? few : many;
const measurement = (value, digits = 1) => typeof value === 'number' && Number.isFinite(value)
  ? value.toLocaleString('cs-CZ', {maximumFractionDigits:digits}) : '—';

async function api(path, options = {}) {
  const response = await fetch('/api/iot' + path, {cache:'no-store', signal:AbortSignal.timeout(20000), ...options});
  let data;
  try { data = await response.json(); } catch (_) { throw new Error('IoT služba nevrátila platnou odpověď.'); }
  if (!response.ok) {
    const error = new Error(data.error || 'Požadavek nelze dokončit.');
    error.code = data.code;
    throw error;
  }
  return data;
}
const jsonRequest = (method, body) => ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
function showError(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
function toast(message, error = false) {
  clearTimeout(app.toastTimer);
  $('toast').textContent = message; $('toast').classList.toggle('error', error); $('toast').hidden = false;
  app.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4000);
}
function statusLabel(module) {
  if (!module.enabled) return 'Pozastavený';
  if (!module.state) return 'Čeká na spojení';
  if (!module.state.online) return 'Nedostupný';
  if (module.state.fault_code) return 'Porucha';
  return 'Online';
}
function statusDot(module) {
  const cls = !module.enabled || !module.state ? '' : !module.state.online ? 'offline' : module.state.fault_code ? 'fault' : 'online';
  return `<span class="status-dot ${cls}"></span>`;
}
function summary() {
  const n = app.modules.length;
  const online = app.modules.filter(m => m.enabled && m.state?.online).length;
  const faults = app.modules.filter(m => m.enabled && m.state?.online && m.state.fault_code).length;
  $('summary').innerHTML = `<span><strong>${n}</strong> ${plural(n,'modul','moduly','modulů')}</span><span><span class="status-dot online"></span><strong>${online}</strong> online</span>${faults ? `<span><span class="status-dot fault"></span><strong>${faults}</strong> ${plural(faults,'porucha','poruchy','poruch')}</span>` : ''}`;
  $('moduleCount').textContent = n;
}
function renderList() {
  const query = $('searchInput').value.toLocaleLowerCase('cs-CZ');
  const modules = app.modules.filter(m => `${m.name} ${m.room}`.toLocaleLowerCase('cs-CZ').includes(query));
  $('moduleList').innerHTML = modules.map(m => {
    const heater = m.driver === 'bot_iph2', s = m.state;
    let reading = '—';
    if (s?.online) {
      reading = heater ? `${s.current_temp_c} °C <span class="subtle">/ ${s.target_temp_c} °C</span>` : s.power ? 'ON' : 'OFF';
      if (m.driver === 'tapo_p110m' && typeof s.power_w === 'number') reading += ` <span class="subtle">· ${measurement(s.power_w)} W</span>`;
    }
    return `<button class="module-card" data-select="${m.id}" aria-pressed="${m.id === app.selected}" ${app.busy ? 'disabled' : ''}>
      <div class="module-card-top"><span class="module-icon ${heater ? '' : 'switch'}">${icon(heater ? 'heat' : m.driver === 'tapo_p110m' ? 'plug' : 'bulb')}</span><div><div class="module-name">${esc(m.name)}</div><div class="module-room">${esc(m.room || 'Místnost není přiřazená')}</div></div></div>
      <div class="module-card-bottom"><span>${statusDot(m)}${statusLabel(m)}</span><span class="module-reading">${reading}</span></div></button>`;
  }).join('') || `<div class="empty small">${query ? 'Žádné zařízení neodpovídá hledání.' : 'Zatím nemáš přidané moduly.'}</div>`;
}
function renderManagement() {
  $('managementTable').innerHTML = app.modules.map(m => `<tr><td><div class="table-name">${esc(m.name)}</div><div class="table-type">${esc(m.capabilities.name)}</div></td><td data-label="Místnost">${esc(m.room || '—')}</td><td data-label="IP"><span class="table-ip">${esc(m.ip)}</span><div class="table-type">${m.driver === 'tapo_p110m' ? 'Tapo · lokálně' : `Tuya ${esc(m.version)}`}</div></td><td data-label="Stav">${statusDot(m)}${statusLabel(m)}</td><td><div class="table-actions"><button class="btn btn-sm" data-edit="${m.id}" ${app.busy ? 'disabled' : ''}>Upravit</button><button class="btn btn-sm btn-danger" data-delete="${m.id}" ${app.busy ? 'disabled' : ''}>Odebrat</button></div></td></tr>`).join('') || '<tr><td colspan="5"><div class="empty small">Přidej první modul nebo jej načti z TinyTuya.</div></td></tr>';
}
function controllerMarkup(module) {
  const heater = module.driver === 'bot_iph2';
  const tapo = module.driver === 'tapo_p110m';
  return `<div class="device-header"><div><h2 id="deviceName"></h2><p class="device-meta" id="deviceMeta"></p></div><div class="device-header-actions"><span id="powerBadge" class="power-badge"></span><button class="icon-btn" data-action="edit" title="Upravit modul" aria-label="Upravit modul">${icon('edit')}</button></div></div>
    <div id="deviceNotice" class="notice error device-notice" role="alert" hidden></div>
    ${heater ? `<div class="gauge"><svg viewBox="0 0 330 258" aria-hidden="true"><defs><linearGradient id="gaugeGradient" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#fbbf24"/><stop offset="1" stop-color="#f97316"/></linearGradient></defs><path class="gauge-track" d="M63 225 A134 134 0 1 1 267 225" pathLength="100"/><path id="gaugeProgress" class="gauge-progress" d="M63 225 A134 134 0 1 1 267 225" pathLength="100" stroke-dasharray="100" stroke-dashoffset="100"/></svg><div class="gauge-values"><div class="gauge-label">Požadovaná teplota</div><div class="target-temperature"><strong id="targetValue">—</strong><span>°C</span></div><div class="current-reading">${icon('temp')}Aktuálně <strong id="currentValue">—</strong></div></div><span class="gauge-min">0 °C</span><span class="gauge-max">37 °C</span></div>
    <div class="temp-controls"><button id="tempMinus" class="temp-step" data-action="minus" aria-label="Snížit cílovou teplotu">−</button><input id="targetRange" type="range" min="0" max="37" step="1" aria-label="Cílová teplota v °C"><button id="tempPlus" class="temp-step" data-action="plus" aria-label="Zvýšit cílovou teplotu">+</button></div><div id="tempFeedback" class="temperature-feedback" role="status"></div>` : tapo ? `<div id="switchDisplay" class="switch-display meter-display">${icon('plug')}<p>Aktuální příkon</p><div class="power-reading"><strong id="powerValue">—</strong><span>W</span></div><span id="switchState" class="plug-state">—</span></div>
    <div class="energy-readings"><div><span>Dnes</span><strong id="energyToday">—</strong><small>kWh</small></div><div><span>Tento měsíc</span><strong id="energyMonth">—</strong><small>kWh</small></div><div><span>Napětí</span><strong id="voltageValue">—</strong><small>V</small></div><div><span>Proud</span><strong id="currentAmps">—</strong><small>A</small></div></div><p id="energyNotice" class="meter-help"></p>` : `<div id="switchDisplay" class="switch-display">${icon('bulb')}<h3 id="switchState">—</h3><p>Stav spínače</p></div>`}
    <div class="device-controls ${heater ? '' : 'switch-controls'}"><button id="powerButton" class="control-button" data-action="power">${icon('power')}<strong id="powerAction">Zapnutí</strong><small id="powerText">—</small></button>${heater ? `<button id="lockButton" class="control-button" data-action="lock">${icon('lock')}<strong>Dětský zámek</strong><small id="lockText">—</small></button><button id="timerButton" class="control-button" data-action="timer">${icon('clock')}<strong>Časovač</strong><small id="timerText">—</small></button>` : ''}</div>
    <div class="device-footer"><span id="lastObserved">Čekám na stav…</span><button data-action="refresh">${icon('refresh')}Načíst stav</button></div>`;
}
function updateController() {
  const m = current();
  if (!m) {
    app.controllerId = null;
    $('controller').classList.remove('is-on', 'is-plug');
    $('controller').innerHTML = `<div class="empty"><span class="empty-icon">${icon('grid')}</span><strong>Začni prvním modulem</strong><p>Přidej zařízení a ovládej jej z HanzHubu.</p><button class="btn btn-primary" data-action="add">+ Přidat modul</button></div>`;
    return;
  }
  const templateId = `${m.id}:${m.driver}`;
  if (app.controllerId !== templateId) { $('controller').innerHTML = controllerMarkup(m); app.controllerId = templateId; }
  const s = m.state, online = !!(m.enabled && s?.online), ready = online && !app.busy;
  const target = online ? app.pendingTemp ?? s.target_temp_c : null;
  $('deviceName').textContent = m.name;
  $('deviceMeta').textContent = [m.room, m.capabilities.name].filter(Boolean).join(' · ');
  $('powerBadge').className = 'power-badge' + (online ? s.power ? ' on' : ' off' : '');
  $('powerBadge').textContent = online ? s.power ? 'Zapnuto' : 'Vypnuto' : statusLabel(m);
  $('controller').classList.toggle('is-on', online && s.power);
  $('controller').classList.toggle('is-plug', m.driver === 'tapo_p110m');
  const fault = online && s.faults?.length ? s.faults.join(' · ') : null;
  showError('deviceNotice', !m.enabled ? 'Modul je pozastavený. Povol jej ve správě.' : s && !online ? s.error || 'Modul je nedostupný. Zkontroluj napájení a připojení k Wi-Fi.' : fault);
  $('powerButton').disabled = !ready;
  $('powerButton').classList.toggle('active', online && s.power);
  $('powerAction').textContent = online ? s.power ? 'Vypnout' : 'Zapnout' : 'Zapnutí';
  $('powerText').textContent = online ? s.power ? 'ON' : 'OFF' : '—';
  if (m.driver === 'bot_iph2') {
    $('targetValue').textContent = target ?? '—';
    $('currentValue').textContent = online ? `${s.current_temp_c} °C` : '—';
    $('gaugeProgress').style.strokeDashoffset = target === null ? 100 : 100 - target / 37 * 100;
    $('targetRange').value = target ?? 0; $('targetRange').disabled = !ready;
    $('tempMinus').disabled = !ready || target <= 0; $('tempPlus').disabled = !ready || target >= 37;
    $('lockButton').disabled = !ready; $('timerButton').disabled = !ready;
    $('lockButton').classList.toggle('active', online && s.locked);
    $('lockText').textContent = online ? s.locked ? 'Zamčeno' : 'Odemčeno' : '—';
    $('timerText').textContent = online ? s.timer_minutes ? `Zbývá ${s.timer_minutes} min` : 'Bez odpočtu' : '—';
    $('timerButton').classList.toggle('active', online && s.timer_minutes > 0);
    $('tempFeedback').classList.toggle('pending', app.pendingTemp !== null);
    $('tempFeedback').textContent = app.pendingTemp !== null ? app.busy ? 'Ukládám teplotu…' : 'Za okamžik uložím nový cíl…' : app.busy ? 'Odesílám příkaz…' : online ? 'Cílová teplota · změny se uloží automaticky' : 'Teplota bude dostupná po připojení.';
  } else {
    $('switchDisplay').classList.toggle('on', online && s.power);
    $('switchDisplay').classList.toggle('off', online && !s.power);
    $('switchState').textContent = online ? s.power ? 'ON' : 'OFF' : '—';
    if (m.driver === 'tapo_p110m') {
      for (const [id, field, digits] of [['powerValue','power_w',1], ['energyToday','energy_today_kwh',3],
        ['energyMonth','energy_month_kwh',3], ['voltageValue','voltage_v',1], ['currentAmps','current_a',3]]) {
        $(id).textContent = online ? measurement(s[field], digits) : '—';
      }
      const hasEnergy = online && ['power_w','energy_today_kwh','energy_month_kwh'].some(field => typeof s[field] === 'number');
      $('energyNotice').textContent = online ? hasEnergy ? 'Údaje ze zásuvky · napětí a proud podle dostupnosti měření.' : 'Zásuvka nyní neposkytuje údaje o spotřebě.' : 'Měření bude dostupné po připojení.';
    }
  }
  $('lastObserved').textContent = s?.checked_at ? `Poslední načtení ${timeLabel(s.checked_at)}` : 'Čekám na stav…';
  $('controller').querySelectorAll('[data-action="edit"],[data-action="refresh"]').forEach(b => { b.disabled = app.busy; });
}
function render() { summary(); renderList(); renderManagement(); updateController(); }
function clearPending() { clearTimeout(app.tempTimer); app.tempTimer = null; app.pendingTemp = null; }
function selectModule(id) {
  if (app.busy) return;
  clearPending(); app.selected = id;
  try { localStorage.setItem('hanzhub-iot-selected', id); } catch (_) {}
  render(); loadEvents();
}
async function loadModules() {
  if (app.loading) return;
  app.loading = true;
  try {
    const data = await api('/devices');
    const old = new Map(app.modules.map(m => [m.id, m]));
    app.modules = data.devices.map(m => {
      const previous = old.get(m.id);
      return {...m, state:m.state || (previous?.updated_at === m.updated_at ? previous.state : null)};
    });
    if (!current()) app.selected = app.modules[0]?.id ?? null;
    showError('pageError', ''); render(); loadEvents();
  } catch (error) {
    showError('pageError', error.code === 'lan_only' ? error.message : 'IoT služba není dostupná. Zkontroluj, že běží na HanzHubu.');
    $('summary').textContent = 'Spojení se službou není dostupné';
  } finally { app.loading = false; }
}
async function refreshStates(fresh = false) {
  if (app.busy || app.pendingTemp !== null || app.loading || document.hidden) return;
  const epoch = app.epoch;
  await Promise.allSettled(app.modules.filter(m => m.enabled).map(async module => {
    try {
      const data = await api(`/devices/${module.id}/state${fresh ? '?fresh=1' : ''}`);
      if (epoch !== app.epoch) return;
      const live = app.modules.find(m => m.id === module.id && m.updated_at === module.updated_at);
      if (live) {
        live.state = data.state;
        if (data.state.online && data.state.ip) live.ip = data.state.ip;
        render();
      }
    } catch (error) {
      if (epoch !== app.epoch) return;
      const live = app.modules.find(m => m.id === module.id && m.updated_at === module.updated_at);
      if (live) { live.state = {online:false, error:error.message}; render(); }
    }
  }));
}
async function loadEvents() {
  const id = app.selected;
  if (!id) { $('activityList').innerHTML = '<div class="empty small">Zatím žádné příkazy.</div>'; return; }
  try {
    const data = await api('/events?id=' + id);
    if (app.selected !== id) return;
    const labels = {power:'Zapnutí', locked:'Dětský zámek', target_temp_c:'Cílová teplota', timer_minutes:'Časovač'};
    $('activityList').innerHTML = data.events.slice(0,5).map(e => {
      const value = e.control === 'target_temp_c' ? `${e.value} °C` : e.control === 'timer_minutes' ? `${e.value} min` : e.value ? 'ON' : 'OFF';
      return `<div class="activity-item"><div class="activity-description"><span class="activity-check ${e.ok ? '' : 'failed'}">${e.ok ? '✓' : '!'}</span><span>${esc(labels[e.control] || e.control)} · ${esc(value)}${e.ok ? '' : ' · chyba'}</span></div><time class="activity-time">${timeLabel(e.at)}</time></div>`;
    }).join('') || '<div class="empty small">Zatím žádné příkazy.</div>';
  } catch (_) { if (app.selected === id) $('activityList').innerHTML = '<div class="empty small">Historie není dostupná.</div>'; }
}
async function sendCommand(control, value, moduleId = app.selected) {
  if (app.busy || moduleId !== app.selected) return false;
  const module = current();
  if (!module) return false;
  app.busy = true; app.epoch++; render();
  try {
    const data = await api(`/devices/${moduleId}/command`, jsonRequest('POST', {control,value}));
    module.state = data.state;
    return true;
  } catch (error) {
    module.state = {online:false, error:error.message}; toast(error.message, true);
    return false;
  } finally {
    app.busy = false;
    if (control === 'target_temp_c') clearPending();
    render(); loadEvents();
  }
}
function queueTemperature(value) {
  const m = current();
  if (!m?.state?.online || app.busy || !Number.isInteger(value) || value < 0 || value > 37) return;
  app.pendingTemp = value; clearTimeout(app.tempTimer); updateController();
  const id = m.id;
  app.tempTimer = setTimeout(() => sendCommand('target_temp_c', value, id), 650);
}

function changeView(manage) {
  $('controlView').hidden = manage; $('manageView').hidden = !manage;
  $('controlTab').setAttribute('aria-selected', String(!manage));
  $('manageTab').setAttribute('aria-selected', String(manage));
}
function sourceMode() { return app.editing ? 'manual' : document.querySelector('[name="sourceMode"]:checked').value; }
function updateFormFields() {
  const form = $('moduleForm'), tapo = form.elements.driver.value === 'tapo_p110m';
  const manual = !tapo && sourceMode() === 'manual';
  $('sourceSection').hidden = !!app.editing || tapo;
  $('tuyaVersionLabel').hidden = tapo;
  $('macLabel').hidden = !manual;
  $('tapoFields').hidden = !tapo;
  $('manualFields').hidden = !manual; $('candidateLabel').hidden = manual;
  $('candidateHelp').hidden = manual;
  form.elements.device_id.required = manual;
  form.elements.local_key.required = manual && !app.editing;
  form.elements.tapo_username.required = tapo && !app.editing;
  form.elements.tapo_password.required = tapo && !app.editing;
  $('tapoCredentialsHelp').textContent = app.editing
    ? 'Nech e-mail i heslo prázdné pro zachování uloženého účtu. Pro změnu vyplň obě pole.'
    : 'Účet se uloží pouze na serveru HanzHubu. Pro běžné ovládání používáme místní síť.';
  $('powerDpLabel').hidden = form.elements.driver.value !== 'tuya_switch';
}
async function openModule(id = null) {
  if (app.busy || app.formBusy) return;
  clearPending(); updateController();
  const form = $('moduleForm'); form.reset(); form.elements.driver.disabled = false;
  app.editing = id; app.candidates = [];
  form.elements.driver.querySelector('option[value="tapo_p110m"]').disabled = false;
  showError('formError', '');
  $('moduleDialogTitle').textContent = id ? 'Upravit modul' : 'Přidat modul';
  $('saveModuleBtn').textContent = id ? 'Uložit změny' : 'Přidat modul';
  $('sourceSection').hidden = !!id;
  $('keyHelp').textContent = id ? 'Nech prázdné pro zachování uloženého klíče.' : 'Klíč se uloží pouze na serveru.';
  if (id) {
    const module = app.modules.find(m => m.id === id);
    if (!module) return;
    for (const field of ['name','room','driver','ip','version','device_id','mac','power_dp']) form.elements[field].value = module[field] ?? '';
    form.elements.version.value = module.driver === 'tapo_p110m' ? '3.4' : module.version;
    form.elements.driver.disabled = module.driver === 'tapo_p110m';
    form.elements.driver.querySelector('option[value="tapo_p110m"]').disabled = module.driver !== 'tapo_p110m';
    form.elements.enabled.checked = module.enabled;
  } else {
    $('candidateSelect').innerHTML = '<option value="">Načítám zařízení…</option>';
    $('candidateHelp').textContent = 'Klíč se načte ze souboru na Raspberry.';
  }
  updateFormFields(); $('moduleDialog').showModal();
  if (!id) {
    try {
      const data = await api('/candidates'); app.candidates = data.candidates;
      if (app.editing || !$('moduleDialog').open) return;
      const candidates = data.candidates.filter(c => !c.registered);
      $('candidateSelect').innerHTML = '<option value="">Vyber zařízení…</option>' + candidates.map(c => `<option value="${esc(c.source_id)}" ${c.has_key ? '' : 'disabled'}>${esc(c.name)}${c.ip ? ' · ' + esc(c.ip) : ''}${c.has_key ? '' : ' · chybí klíč'}</option>`).join('');
      if (!candidates.length) $('candidateHelp').textContent = 'Žádné nové zařízení. Můžeš jej přidat ručně nebo doplnit devices.json přes TinyTuya wizard.';
    } catch (error) {
      $('candidateSelect').innerHTML = '<option value="">Soubor není dostupný</option>';
      $('candidateHelp').textContent = error.message + ' Můžeš použít ruční přidání.';
    }
  }
}
function applyCandidate() {
  const c = app.candidates.find(c => c.source_id === $('candidateSelect').value);
  if (!c) return;
  const form = $('moduleForm');
  if (!form.elements.name.value.trim()) form.elements.name.value = c.name;
  form.elements.ip.value = c.ip;
  form.elements.driver.value = c.driver_hint;
  updateFormFields();
}
async function submitModule(event) {
  event.preventDefault(); if (app.formBusy) return;
  const form = event.target, data = Object.fromEntries(new FormData(form));
  const payload = {name:data.name, room:data.room, driver:form.elements.driver.value, ip:data.ip,
    enabled:form.elements.enabled.checked};
  if (payload.driver === 'tapo_p110m') {
    Object.assign(payload, {tapo_username:data.tapo_username, tapo_password:data.tapo_password});
  } else {
    Object.assign(payload, {version:data.version, power_dp:Number(data.power_dp)});
    if (sourceMode() === 'import') {
    payload.source_id = $('candidateSelect').value;
    if (!payload.source_id) { showError('formError','Vyber zařízení nebo zvol ruční přidání.'); return; }
    } else Object.assign(payload, {device_id:data.device_id, mac:data.mac || null, local_key:data.local_key});
  }
  app.formBusy = true; $('saveModuleBtn').disabled = true; showError('formError','');
  try {
    const id = app.editing;
    const result = await api(id ? '/devices/' + id : '/devices', jsonRequest(id ? 'PUT' : 'POST', payload));
    app.epoch++; clearPending(); app.selected = result.module.id;
    $('moduleDialog').close(); toast(id ? 'Změny modulu uloženy.' : 'Modul přidán.');
    await loadModules(); await refreshStates(true);
  } catch (error) { showError('formError', error.message); }
  finally { app.formBusy = false; $('saveModuleBtn').disabled = false; }
}
function openTimer() {
  const m = current(); if (!m?.state?.online || app.busy) return;
  clearPending(); updateController(); app.dialogModule = m.id;
  $('timerHours').value = Math.ceil(m.state.timer_minutes / 60);
  showError('timerError', ''); $('timerDialog').showModal();
}
async function submitTimer(event) {
  event.preventDefault(); if (app.busy) return;
  $('saveTimerBtn').disabled = true;
  const ok = await sendCommand('timer_minutes', Number($('timerHours').value) * 60, app.dialogModule);
  if (ok) { $('timerDialog').close(); toast('Časovač uložen.'); }
  else showError('timerError', 'Časovač se nepodařilo potvrdit. Obnov stav zařízení.');
  $('saveTimerBtn').disabled = false;
}
function openDelete(id) {
  if (app.busy || app.formBusy) return;
  clearPending(); updateController();
  const m = app.modules.find(m => m.id === id); if (!m) return;
  app.dialogModule = id; $('deleteText').textContent = `Odebrat „${m.name}“ ze seznamu HanzHubu?`;
  showError('deleteError',''); $('deleteDialog').showModal();
}
async function submitDelete(event) {
  event.preventDefault(); if (app.formBusy) return;
  app.formBusy = true; $('deleteConfirmBtn').disabled = true;
  try {
    await api('/devices/' + app.dialogModule, {method:'DELETE'}); app.epoch++;
    $('deleteDialog').close(); toast('Modul odebrán.'); await loadModules();
  } catch (error) { showError('deleteError', error.message); }
  finally { app.formBusy = false; $('deleteConfirmBtn').disabled = false; }
}
async function refreshAll() {
  if (app.busy || app.pendingTemp !== null) return;
  $('refreshBtn').disabled = true;
  try { await loadModules(); await refreshStates(true); }
  finally { $('refreshBtn').disabled = false; }
}
document.addEventListener('click', event => {
  const close = event.target.closest('[data-close]');
  if (close) { if (!app.formBusy && !app.busy) $(close.dataset.close).close(); return; }
  const selected = event.target.closest('[data-select]');
  if (selected) { selectModule(selected.dataset.select); return; }
  const edit = event.target.closest('[data-edit]');
  if (edit) { openModule(edit.dataset.edit); return; }
  const del = event.target.closest('[data-delete]');
  if (del) { openDelete(del.dataset.delete); return; }
  const action = event.target.closest('[data-action]'); if (!action || action.disabled) return;
  const m = current(), s = m?.state;
  switch (action.dataset.action) {
    case 'add': openModule(); break;
    case 'edit': openModule(m?.id); break;
    case 'refresh': refreshAll(); break;
    case 'minus': queueTemperature((app.pendingTemp ?? s?.target_temp_c) - 1); break;
    case 'plus': queueTemperature((app.pendingTemp ?? s?.target_temp_c) + 1); break;
    case 'power': clearPending(); sendCommand('power', !s.power); break;
    case 'lock': clearPending(); sendCommand('locked', !s.locked); break;
    case 'timer': openTimer(); break;
  }
});
$('controller').addEventListener('input', event => { if (event.target.id === 'targetRange') queueTemperature(Number(event.target.value)); });
$('searchInput').addEventListener('input', renderList);
$('refreshBtn').addEventListener('click', refreshAll);
$('addBtn').addEventListener('click', () => openModule());
$('controlTab').addEventListener('click', () => changeView(false));
$('manageTab').addEventListener('click', () => changeView(true));
document.querySelector('.view-tabs').addEventListener('keydown', event => {
  if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
    event.preventDefault(); const manage = event.key === 'End' || (event.key !== 'Home' && $('controlTab').getAttribute('aria-selected') === 'true');
    changeView(manage); $(manage ? 'manageTab' : 'controlTab').focus();
  }
});
$('candidateSelect').addEventListener('change', applyCandidate);
$('moduleForm').addEventListener('change', event => { if (['sourceMode','driver'].includes(event.target.name)) updateFormFields(); });
$('moduleForm').addEventListener('submit', submitModule);
$('timerForm').addEventListener('submit', submitTimer);
$('deleteForm').addEventListener('submit', submitDelete);
$('importBtn').addEventListener('click', async () => {
  if (app.busy || app.formBusy) return;
  $('importBtn').disabled = true;
  try {
    const result = await api('/import', jsonRequest('POST', {})); app.epoch++;
    toast(`Načteno: ${result.added} nových, ${result.updated} aktualizovaných modulů.`);
    await loadModules(); await refreshStates(true);
  } catch (error) { toast(error.message, true); }
  finally { $('importBtn').disabled = false; }
});
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('cancel', event => {
  if (app.formBusy || app.busy) event.preventDefault();
}));
$('moduleDialog').addEventListener('close', () => {
  for (const field of ['local_key','tapo_username','tapo_password']) $('moduleForm').elements[field].value = '';
});
$('timerHours').innerHTML = Array.from({length:25}, (_, h) => `<option value="${h}">${h} ${plural(h,'hodina','hodiny','hodin')}</option>`).join('');
async function init() {
  try {
    const result = await api('/info');
    document.querySelectorAll('[data-dashboard]').forEach(a => { a.href = result.dashboard_url; });
  } catch (_) {}
  try {
    const response = await fetch('version.json', {cache:'no-store'});
    const result = await response.json();
    $('versionEl').textContent = 'v' + result.version; $('versionFooter').textContent = 'v' + result.version;
  } catch (_) {}
  await loadModules(); await refreshStates();
  setInterval(refreshStates, 15000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStates(); });
init();
