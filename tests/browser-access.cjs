/* Mock-only heater: this test never contacts Cloudflare, a HUB, or a real device. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..', 'web');

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:390,height:850}});
    await page.clock.install();
    const errors = [], commands = [], management = [];
    page.on('pageerror', error => errors.push(error.message));
    let expired = false, remote = true;
    const state = {online:true,power:false,current_temp_c:20,target_temp_c:26,locked:false,
                   timer_minutes:0,fault_code:0,faults:[],checked_at:new Date().toISOString()};
    const module = {id:'111111111111',name:'Testovací topení',room:'Pokoj',driver:'bot_iph2',
                    enabled:true,updated_at:'fixture',capabilities:{name:'Infrapanel',controls:['power','target_temp_c']}};
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), p = url.pathname;
      if (p.startsWith('/api/iot')) {
        if (expired) return route.fulfill({status:403,json:{code:'access_required',error:'Přihlášení vypršelo. Přihlas se znovu přes Cloudflare Access.'}});
        if (p.endsWith('/info')) return route.fulfill({json:{dashboard_url:'https://hub.example.com',access:remote?'remote':'lan',can_manage:!remote}});
        if (p.endsWith('/devices') && request.method()==='GET')
          return route.fulfill({json:{devices:[{...module,state}],access:remote?'remote':'lan',can_manage:!remote}});
        if (p.endsWith('/state')) return route.fulfill({json:{state}});
        if (p.endsWith('/events')) return route.fulfill({json:{events:[]}});
        if (p.endsWith('/command')) {
          const command = request.postDataJSON(); commands.push(command);
          state[command.control] = command.value;
          return route.fulfill({json:{ok:true,state}});
        }
        management.push({method:request.method(),path:p});
        return route.fulfill({status:403,json:{code:'management_lan_only',error:'Správa jen z LAN.'}});
      }
      const file = p==='/' ? 'index.html' : path.basename(p);
      const local = path.join(root,file);
      if (fs.existsSync(local) && fs.statSync(local).isFile()) {
        const type = file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':file.endsWith('.json')?'application/json':'text/html';
        return route.fulfill({contentType:type,body:fs.readFileSync(local)});
      }
      return route.abort();
    });
    await page.goto('https://iot.example.com/');
    await page.locator('#powerButton:enabled').waitFor();
    assert.equal(commands.length,0,'loading must not switch on the real/mock heater');
    assert.equal(await page.locator('#manageTab').isHidden(),true);
    assert.equal(await page.locator('#addBtn').isHidden(),true);
    assert.equal(await page.locator('[data-action="edit"]').isHidden(),true);
    assert.match(await page.locator('#accessMode').textContent(),/Cloudflare Access/);
    await page.evaluate(() => {changeView(true); return openModule();});
    assert.equal(await page.locator('#manageView').isHidden(),true);
    assert.deepEqual(management,[]);
    await page.locator('#powerButton').click();
    await page.locator('#powerBadge').filter({hasText:'Zapnuto'}).waitFor();
    assert.deepEqual(commands,[{control:'power',value:true}]);
    await page.locator('#tempPlus').click();
    await page.clock.fastForward(700);
    await page.locator('#targetValue').filter({hasText:'27'}).waitFor();
    await page.locator('#powerButton:enabled').waitFor();
    assert.deepEqual(commands[1],{control:'target_temp_c',value:27});
    expired=true;
    await page.locator('#powerButton').click();
    await page.locator('#deviceNotice').filter({hasText:/Přihlášení vypršelo/}).waitFor();
    assert.equal(await page.locator('#powerButton').isDisabled(),true);
    assert.equal(commands.length,2,'expired auth must not issue or retry a command');
    expired=false; remote=false;
    await page.reload();
    await page.locator('#manageTab:visible').waitFor();
    assert.equal(await page.locator('#addBtn').isVisible(),true,'LAN management stays available');
    assert.equal(commands.length,2,'reload must not replay commands');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.deepEqual(errors,[]);
    console.log('Remote heater browser checks passed: confirmed on/temperature, no implicit/replayed commands, expired login, remote management hidden/blocked, LAN management, mobile.');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
