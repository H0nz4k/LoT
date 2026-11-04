(() => {
  const status=document.getElementById('ewelinkStatus'),button=document.getElementById('ewelinkConnect'),list=document.getElementById('ewelinkDevices');
  const localURL='http://192.168.1.3:4011/';
  const remote=location.hostname==='iot.hanzhub.cz';
  if(location.hash==='#sonoff')document.getElementById('ewelinkBox').open=true;
  if(remote){
    const box=document.getElementById('ewelinkBox');
    const link=document.createElement('a');link.className='btn';link.href=localURL+'#sonoff';
    link.textContent='Otevřít IoT v domácí síti';link.style.display='inline-block';link.style.marginTop='12px';box.append(link);
    // Never carry OAuth codes, state or other public URL parameters into LAN.
    const query=new URLSearchParams(location.search);
    if(!query.has('code')&&!query.has('state')){
      fetch(localURL+'api/iot/lan-check',{mode:'cors',credentials:'omit',cache:'no-store',
        targetAddressSpace:'local',signal:AbortSignal.timeout(4000)})
        .then(r=>r.ok?r.json():null).then(d=>{
          if(d?.service==='hanzhub_iot'&&d.lan===true)location.replace(localURL+location.hash);
        }).catch(()=>{});
    }
  }
  async function call(path,options){const r=await fetch('/api/iot/ewelink/'+path,{cache:'no-store',...options});const d=await r.json();if(!r.ok)throw Error(d.error||'Připojení eWeLink se nepovedlo.');return d;}
  async function load(){
    try{
      const state=await call('status');button.hidden=state.connected||!state.configured;
      status.textContent=state.connected?'Účet eWeLink je připojený. Stav se obnovuje přes cloud přibližně každé dvě minuty.':state.configured?'Připoj účet, ve kterém máš zásuvku Sonoff.':'Aplikace eWeLink zatím není nastavená na serveru.';
      if(new URLSearchParams(location.search).get('ewelink')==='error'){status.textContent='Přihlášení eWeLink se nepovedlo. Zkus připojení znovu.';document.getElementById('ewelinkBox').open=true;}
      if(!state.connected)return;
      const [data,info,registered]=await Promise.all([call('devices'),fetch('/api/iot/info').then(r=>r.json()),fetch('/api/iot/devices').then(r=>r.json())]);list.replaceChildren();
      for(const device of data.devices){
        const row=document.createElement('div');row.style.margin='10px 0';const name=document.createElement('span');name.textContent=device.name+' · '+device.model+' · '+(device.online?'online':'offline')+' ';row.append(name);
        if((registered.devices||[]).some(m=>m.driver==='sonoff_ewelink'&&m.device_id===device.device_id)){const note=document.createElement('span');note.textContent='Přidáno';row.append(note);}
        else if(info.can_manage){const add=document.createElement('button');add.type='button';add.className='btn';add.textContent='Přidat do HanzHubu';add.onclick=async()=>{add.disabled=true;try{await call('import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:device.device_id})});location.reload();}catch(e){status.textContent=e.message;add.disabled=false;}};row.append(add);}
        else {const note=document.createElement('span');note.textContent='Pro přidání použij odkaz na domácí síť níže.';row.append(note);}
        list.append(row);
      }
      if(!data.devices.length)list.textContent='Účet nevrátil podporovanou Sonoff zásuvku s jedním spínačem.';
    }catch(e){status.textContent=e.message;}
  }
  button.onclick=async()=>{button.disabled=true;try{const d=await call('login');location.href=d.url;}catch(e){status.textContent=e.message;button.disabled=false;}};
  load();
})();
