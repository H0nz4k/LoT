'use strict';
(() => {
  const el = id => document.getElementById(id);
  const escape = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (v,d=3) => typeof v==='number' && Number.isFinite(v) ? v.toLocaleString('cs-CZ',{minimumFractionDigits:d,maximumFractionDigits:d}) : '—';
  const date = ts => new Date(ts*1000).toLocaleString('cs-CZ',{timeZone:'Europe/Prague'});
  const iso = d => d.toISOString().slice(0,10);
  const today = () => new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Prague',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const start = '2026-11-20';
  let report,metric='kwh',period='30',sort='day',descending=true,visible=false,sequence=0,busy=false;
  const button=document.createElement('button');
  button.type='button';button.id='heatingTab';button.textContent='Vytápění';button.className='btn';button.setAttribute('aria-expanded','false');button.setAttribute('aria-controls','heatingView');
  document.querySelector('.toolbar .heading-actions').prepend(button);
  function toggle(open) {
    visible=open;el('heatingView').hidden=!open;button.setAttribute('aria-expanded',String(open));
    el('controlView').hidden=open;el('manageView').hidden=true;
    if(open) {el('controlTab').setAttribute('aria-selected','false');el('manageTab').setAttribute('aria-selected','false');load();}
  }
  button.onclick=()=>{if(visible){toggle(false);el('controlTab').click();}else toggle(true);};
  window.addEventListener('iot-control-view',()=>{visible=false;el('heatingView').hidden=true;button.setAttribute('aria-expanded','false');});
  function range() {
    let end=today(),begin=end;
    const day=new Date(end+'T12:00:00Z');
    if(period==='yesterday'){day.setUTCDate(day.getUTCDate()-1);begin=end=iso(day);}
    else if(period==='month') begin=end.slice(0,7)+'-01';
    else if(period==='year') begin=end.slice(0,4)+'-01-01';
    else if(period==='custom'){begin=el('heatingFrom').value;end=el('heatingTo').value;}
    else if(!['today','yesterday'].includes(period)){day.setUTCDate(day.getUTCDate()-Number(period)+1);begin=iso(day);}
    // Show an honest empty period before the ledger begins (yesterday at first start).
    if(end<start) return null;
    return {from:begin<start?start:begin,to:end};
  }
  async function request(path,options={}) {
    const res=await fetch('/api/iot'+path,{cache:'no-store',signal:AbortSignal.timeout(20000),...options});
    const body=await res.json();if(!res.ok)throw new Error(body.error||'Chyba načtení monitoringu');return body;
  }
  async function load() {
    const token=++sequence;
    try {
      const r=range();
      if(!r){report=null;el('heatingChart').textContent='Období je před začátkem evidence 20. 11. 2026.';el('heatingRows').textContent='';el('heatingCsv').removeAttribute('href');return;}
      const data=await request('/heating?'+new URLSearchParams(r));
      if(token!==sequence)return;
      report=data;el('heatingError').hidden=true;
      el('heatingStatus').textContent=({active:'Aktivní · cloudová počítadla',unavailable:'Nedostupné měření',gap:'Výpadek komunikace',error:'Chyba'})[data.status]||'Chyba';
      el('heatingLast').textContent='Poslední úspěšný sběr: '+(data.last_success?date(data.last_success):'—')+' · SONOFF S60TPF · '+data.device_id+' · Náklady jsou orientační.';
      const o=data.overview,t=data.totals;
      const source = row => row.estimated?' · odhad':'';
      const tiles=[['Příkon z cloudu · stáří neznámé',data.current_power_w,'W',1],['Dnes'+source(o.today),o.today.kwh,'kWh'],['Dnešní náklady'+source(o.today),o.today.cost,'Kč',2],['Včera'+source(o.yesterday),o.yesterday.kwh,'kWh'],['Tento měsíc'+source(o.month),o.month.kwh,'kWh'],['Náklady za měsíc'+source(o.month),o.month.cost,'Kč',2],['Celkem od prvního sběru'+source(t),t.kwh,'kWh'],['Celkové náklady'+source(t),t.cost,'Kč',2],['Doba topení · dílčí odhad',t.run_h,'h',2]];
      el('heatingMetrics').innerHTML=tiles.map(([label,value,unit,digits])=>`<div><span>${label}</span><strong>${fmt(value,digits??3)}</strong> ${unit}</div>`).join('');
      el('heatingCsv').href='/api/iot/heating.csv?'+new URLSearchParams(r);
      el('heatingTariff').hidden=typeof app==='undefined'||!app.canManage;
      if(document.activeElement!==el('heatingRate'))el('heatingRate').value=String(data.tariffs.at(-1).czk_per_kwh).replace('.',',');
      el('heatingTariffs').textContent='Tarify: '+data.tariffs.map(t=>date(t.effective)+' · '+t.czk_per_kwh.replace('.',',')+' Kč/kWh').join('; ');
      el('heatingEvents').innerHTML=data.events.length?data.events.map(e=>`<li>${escape(date(e.at))} · ${escape(e.phase)} · ${escape(e.message)}</li>`).join(''):'<li>Bez zaznamenaných událostí v období.</li>';
      renderChart();renderRows();
    } catch(error){if(token===sequence){el('heatingStatus').textContent='Chyba';el('heatingError').hidden=false;el('heatingError').textContent=error.message;}}
  }
  function renderChart() {
    const rows=metric==='power_w'?report.power.map(r=>({label:date(r.at),value:r.power_w,partial:r.partial})):report.days.map(r=>({label:r.day,value:r[metric],partial:r.missing_seconds>180||r.estimated||r.allocation_estimated}));
    const valid=rows.filter(r=>r.value!==null);
    el('heatingChartLabel').textContent=metric==='power_w'?'Průměr dostupných vzorků; stáří údaje zařízení neznámé':'Denní součty · přerušovaně = neúplné období / odhad časového rozdělení';
    if(!valid.length){el('heatingChart').textContent='Měření pro tento graf není dostupné.';return;}
    const max=Math.max(...valid.map(r=>r.value),.001),width=Math.max(600,rows.length*14),step=width/Math.max(rows.length,1);
    const unit=metric==='cost'?'Kč':metric==='power_w'?'W':'kWh';
    el('heatingChart').innerHTML=`<svg viewBox="0 0 ${width} 200" role="img" aria-label="${escape(unit)} za vybrané období" style="min-width:${Math.min(width,1400)}px">${rows.map((r,i)=>r.value===null?`<text x="${i*step+step/2}" y="185" fill="#b7c1d2" text-anchor="middle">·</text>`:`<rect tabindex="0" role="img" aria-label="${escape(r.label+': '+fmt(r.value)+' '+unit+(r.partial?' · neúplné / odhad':''))}" x="${i*step+2}" y="${190-r.value/max*170}" width="${Math.max(2,step-4)}" height="${Math.max(2,r.value/max*170)}" ${r.partial?'fill-opacity=".5" stroke="#34d399" stroke-dasharray="3 2"':''}><title>${escape(r.label)}: ${fmt(r.value)} ${unit}${r.partial?' · neúplné / odhad':''}</title></rect>`).join('')}<text x="8" y="14" fill="#b7c1d2">${fmt(max)} ${unit}</text></svg>`;
  }
  function renderRows() {
    const rows=[...report.days].sort((a,b)=>{const av=a[sort],bv=b[sort];if(av===null)return 1;if(bv===null)return -1;return (typeof av==='string'?av.localeCompare(bv):av-bv)*(descending?-1:1);});
    el('heatingRows').innerHTML=rows.map(r=>`<tr><td>${escape(r.day.split('-').reverse().join('. '))}</td><td>${fmt(r.kwh)} kWh</td><td>${fmt(r.cost,2)} Kč</td><td>${fmt(r.run_h,2)} h</td><td>${r.kwh===null?'Nedostupné':r.estimated?'Odhad':'Měřeno'}${r.missing_seconds>180?' · neúplné':''}${r.allocation_estimated?' · rozdělení odhadem':''}${r.runtime_missing_seconds>0?' · provoz neúplný':''}</td></tr>`).join('');
  }
  document.querySelectorAll('[data-period]').forEach(b=>b.onclick=()=>{period=b.dataset.period;document.querySelectorAll('[data-period]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));el('heatingRange').hidden=period!=='custom';if(period!=='custom')load();});
  document.querySelectorAll('[data-metric]').forEach(b=>b.onclick=()=>{metric=b.dataset.metric;document.querySelectorAll('[data-metric]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));if(report)renderChart();});
  document.querySelectorAll('[data-sort]').forEach(b=>b.onclick=()=>{descending=sort===b.dataset.sort?!descending:false;sort=b.dataset.sort;if(report)renderRows();});
  el('heatingFrom').value=start;el('heatingTo').value=today();
  el('heatingRange').onsubmit=e=>{e.preventDefault();load();};
  el('heatingTariff').onsubmit=async e=>{e.preventDefault();if(busy)return;busy=true;const submit=e.submitter;submit.disabled=true;try{await request('/heating/tariff',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({czk_per_kwh:el('heatingRate').value})});await load();}catch(error){el('heatingError').hidden=false;el('heatingError').textContent=error.message;}finally{busy=false;submit.disabled=false;}};
  setInterval(()=>{if(visible&&!document.hidden)load();},60000);
})();
