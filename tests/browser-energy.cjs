/* Run with PLAYWRIGHT_MODULE and PYTHON pointing to installed test runtimes. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
(async()=>{
  const child=spawn(process.env.PYTHON||'python',['tests/energy_preview.py'],{cwd:root,stdio:['ignore','pipe','pipe'],windowsHide:true});
  let browser;
  try{
    const port=await new Promise((resolve,reject)=>{
      let text='';const timeout=setTimeout(()=>reject(Error('Preview startup timeout')),15000);
      child.stdout.on('data',chunk=>{text+=chunk;if(text.includes('\n')){clearTimeout(timeout);resolve(Number(text.trim()));}});
      child.once('exit',code=>{clearTimeout(timeout);reject(Error('Preview exited '+code));});
    });
    browser=await chromium.launch({headless:true});
    await fs.mkdir(path.join(root,'test-results'),{recursive:true});
    for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
      const page=await browser.newPage({viewport,acceptDownloads:true});const errors=[];
      let recoveryPosts=0;
      page.on('request',request=>{if(request.method()==='POST'&&request.url().endsWith('/api/iot/heating/recovery'))recoveryPosts++;});
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(`http://127.0.0.1:${port}`);
      await page.getByRole('button',{name:'Vytápění',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#heatingMetrics').children.length===9);
      assert.match(await page.locator('#heatingStatus').innerText(),/Aktivní/);
      assert.equal(await page.locator('#heatingChart rect').count(),1);
      await page.getByRole('button',{name:'Náklady · Kč',exact:true}).click();
      await page.getByRole('button',{name:'Příkon · W',exact:true}).click();
      assert.ok(await page.locator('#heatingChart rect').count()>0);
      await page.getByRole('button',{name:'Spotřeba · kWh',exact:true}).click();
      await page.getByRole('button',{name:'Vlastní období',exact:true}).click();
      assert.equal(await page.locator('#heatingRange').isVisible(),true);
      await page.getByRole('button',{name:'Zobrazit',exact:true}).click();
      await page.getByRole('button',{name:'Spotřeba ↕',exact:true}).click();
      const download=page.waitForEvent('download');await page.getByRole('link',{name:'Export CSV'}).click();
      const file=await download;assert.equal(file.suggestedFilename(),'infrapanel.csv');
      await page.locator('#heatingRate').fill('3,1');
      await page.getByRole('button',{name:'Uložit od nynějška',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#heatingTariffs').textContent.includes('3,1 Kč/kWh'));
      await page.waitForFunction(()=>!document.querySelector('#infraRecover').disabled);
      await page.locator('#infraRecover').click();
      await page.getByRole('button',{name:'Zrušit',exact:true}).click();
      assert.equal(recoveryPosts,0);
      await page.locator('#infraRecover').click();
      await page.keyboard.press('Escape');
      assert.equal(recoveryPosts,0);
      await page.locator('#infraRecover').click();
      const recoveryResponse=page.waitForResponse(response=>response.request().method()==='POST'&&response.url().endsWith('/api/iot/heating/recovery'));
      await page.getByRole('button',{name:'Schvaluji vypnutí na 30 sekund',exact:true}).click();
      const operation=(await (await recoveryResponse).json()).operation;
      await page.waitForFunction(id=>document.querySelector('#infraRecoveryStatus').dataset.operationId===id&&document.querySelector('#infraRecoveryStatus').textContent.includes('obnova dokončena'),operation.id);
      assert.equal(recoveryPosts,1);
      await page.locator('#infraRecover').click();
      await page.keyboard.press('Escape');
      assert.equal(recoveryPosts,1);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.deepEqual(errors,[]);
      await page.screenshot({path:path.join(root,'test-results',`heating-${viewport.width}.png`),fullPage:true});
      await page.getByRole('tab',{name:'Ovládání',exact:true}).click();
      assert.equal(await page.locator('#heatingView').isVisible(),false);
      assert.equal(await page.locator('#controlView').isVisible(),true);
      await page.getByRole('button',{name:'Vytápění',exact:true}).click();
      await page.getByRole('tab',{name:'Ovládání',exact:true}).focus();
      await page.keyboard.press('Home');
      assert.equal(await page.locator('#heatingView').isVisible(),false);
      await page.getByRole('button',{name:'Vytápění',exact:true}).click();
      assert.equal(await page.locator('#heatingView').isVisible(),true);
      await page.close();
    }
    console.log('PASS: desktop/mobile, metrics, graph modes, custom dates, sorting, CSV, tariff history, explicit recovery confirmation/cancellation and control navigation; synthetic fixture only');
  }finally{if(browser)await browser.close();child.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
