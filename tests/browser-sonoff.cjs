/* Mocked browser check: use Playwright and its Chromium, no account or device. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname,'..');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1100,height:950}});
    const errors = [], requests = [];
    page.on('pageerror',e=>errors.push(e.message));
    const module = {id:'111111111111',name:'SONOFF',room:'Kancl',driver:'sonoff_s60tpf',
      device_id:'test-sonoff',enabled:true,ip:'',version:'oauth2',power_dp:1,
      capabilities:{name:'SONOFF S60TPF · eWeLink cloud',kind:'plug',energy:true,controls:['power']},
      state:{online:true,power:false,cloud_fetched_at:new Date().toISOString(),checked_at:new Date().toISOString(),
             energy_notice:'Jednotky měření nejsou ověřené.'}};
    await page.route('**/*',async route => {
      const request=route.request(),url=new URL(request.url());
      if(url.pathname.startsWith('/api/iot')) {
        const body=request.postData()?request.postDataJSON():null;
        requests.push({method:request.method(),path:url.pathname,body});
        if(url.pathname.endsWith('/info')) return route.fulfill({json:{dashboard_url:'http://hanzhub.test:4001'}});
        if(url.pathname.endsWith('/candidates')) return route.fulfill({json:{candidates:[]}});
        if(url.pathname.endsWith('/events')) return route.fulfill({json:{events:[]}});
        if(url.pathname.endsWith('/command')) {
          module.state.power=body.value;
          return route.fulfill({json:{ok:true,state:module.state}});
        }
        if(url.pathname.endsWith('/state')) return route.fulfill({json:{state:module.state}});
        if(request.method()==='POST'||request.method()==='PUT') return route.fulfill({json:{ok:true,module}});
        return route.fulfill({json:{devices:[module]}});
      }
      const file=url.pathname==='/'?'index.html':url.pathname.slice(1);
      if(['index.html','iot.js','iot.css','hanzlogo.svg','favicon.svg','version.json'].includes(file)) {
        const contentType=file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':
          file.endsWith('.json')?'application/json':file.endsWith('.svg')?'image/svg+xml':'text/html';
        return route.fulfill({contentType,body:fs.readFileSync(path.join(root,'web',file))});
      }
      return route.fulfill({status:404,body:''});
    });
    await page.goto('http://hanzhub.test/');
    await page.locator('#switchState').waitFor();
    assert.equal(await page.locator('#switchState').textContent(),'OFF');
    assert.equal(await page.locator('#powerValue').textContent(),'—');
    assert.match(await page.locator('#energyNotice').textContent(),/nejsou ověřené/);
    await page.locator('#powerButton').click();
    await page.waitForFunction(()=>document.getElementById('switchState').textContent==='ON');
    assert.deepEqual(requests.find(r=>r.path.endsWith('/command')).body,{control:'power',value:true});
    await page.locator('#addBtn').click();
    await page.locator('[name=driver]').selectOption('sonoff_s60tpf');
    assert.equal(await page.locator('#sonoffFields').isVisible(),true);
    assert.equal(await page.locator('#ipLabel').isVisible(),false);
    assert.equal(await page.locator('#manualFields').isVisible(),false);
    assert.equal(await page.locator('#tapoFields').isVisible(),false);
    await page.locator('[name=name]').fill('New SONOFF');
    await page.locator('[name=sonoff_device_id]').fill('test-sonoff');
    await page.locator('#saveModuleBtn').click();
    await page.waitForFunction(()=>!document.getElementById('moduleDialog').open);
    assert.deepEqual(requests.find(r=>r.method==='POST'&&r.path==='/api/iot/devices').body,
      {name:'New SONOFF',room:'',driver:'sonoff_s60tpf',device_id:'test-sonoff',enabled:true});
    await page.locator('[data-action=edit]').click();
    assert.equal(await page.locator('[name=sonoff_device_id]').inputValue(),'test-sonoff');
    assert.equal(await page.locator('[name=driver]').isDisabled(),true);
    assert.deepEqual(errors,[]);
    console.log('SONOFF browser: OFF/ON, unavailable meters, add/edit form and payload passed.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
