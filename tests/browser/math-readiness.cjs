const {chromium}=require('playwright');
const fs=require('node:fs'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const os=require('node:os'),path=require('node:path'),{pathToFileURL}=require('node:url');
const docs=JSON.parse(execFileSync(process.env.PYTHON || '.venv/Scripts/python.exe',['tests/browser/readiness_documents.py'],{encoding:'utf8'}));
(async()=>{const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});try {
  for(const kind of ['report','reading','task']) {
    const page=await browser.newPage({viewport:{width:1200,height:900}}),errors=[];let katex;
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('http://fixture/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path.endsWith('katex.min.js')){katex=route;return;}
      if(path.startsWith('/static/'))return route.fulfill({contentType:path.endsWith('.js')?'application/javascript':path.endsWith('.css')?'text/css':'application/octet-stream',body:fs.readFileSync('src/paperloom'+path)});
      if(path.startsWith('/api/'))return route.fulfill({json:path.endsWith('/settings')?{model:'fixture'}:path.endsWith('/reports')?{reports:[]}:path.endsWith('/sessions')&&route.request().method()==='GET'?[]:{id:'fixture',paper:{title:'Fixture',arxiv_id:'2609.29718',version:2},messages:[],references:[]}});
      return route.fulfill({contentType:'text/html',body:docs[kind]});
    });
    await page.goto('http://fixture/'+kind,{waitUntil:'commit'});
    // DOMContentLoaded must not wait for optional formula download.
    await page.waitForLoadState('domcontentloaded',{timeout:3000});
    await page.screenshot({path:path.join(os.tmpdir(),'paperloom-'+kind+'-ready.png')});
    if(kind==='report') {
      await page.locator('.reading-open').click();
      assert.equal(await page.locator('#report-reading').isVisible(),true);
      await page.getByRole('button',{name:'收起阅读助手',exact:true}).click();
      await page.locator('#report-papers-toggle').click();
      assert.equal(await page.locator('#report-papers').isVisible(),true);
    } else if(kind==='reading') {
      assert.equal(await page.locator('.reading-welcome').isVisible(),true);
      await page.setViewportSize({width:700,height:900});
      await page.locator('.nav-toggle').click();
      assert.equal(await page.locator('.nav-toggle').getAttribute('aria-expanded'),'true');
    } else {
      await page.evaluate(()=>window.print=()=>{document.body.dataset.printed='true';});
      await page.locator('.print-result').click();
      assert.equal(await page.locator('body').getAttribute('data-printed'),'true');
    }
    await katex.fulfill({contentType:'application/javascript',body:fs.readFileSync('src/paperloom/static/vendor/katex/katex.min.js')});
    if(kind!=='reading')await page.locator('.math-inline .katex').first().waitFor();
    if(kind==='report')await page.waitForFunction(()=>document.querySelectorAll('.math-block .katex').length===80);
    await page.evaluate(()=>{const node=document.createElement('span');node.className='math-inline';node.id='late-math';node.textContent='y^2';document.body.append(node);});
    await page.locator('#late-math .katex').waitFor();
    await page.evaluate(()=>{document.querySelector('#late-math').textContent=String.fromCharCode(92)+'frac{';});
    await page.locator('#late-math .katex-error').waitFor();
    await page.evaluate(()=>{document.querySelector('#late-math').textContent='z^3';});
    await page.locator('#late-math .katex').waitFor();
    assert.deepEqual(errors,[]);
    console.log('PASS '+kind+': controls before KaTeX; initial and dynamic formulas after load');
    await page.evaluate(()=>sessionStorage.clear());
    await page.route('**/katex.min.js',route=>route.abort());
    await page.reload({waitUntil:'domcontentloaded'});
    if(kind==='report') {
      await page.locator('.reading-open').click();
      assert.equal(await page.locator('#report-reading').isVisible(),true);
      assert.equal(await page.locator('.math-inline').first().textContent(),'x^2');
    }
    if(kind==='reading')assert.equal(await page.locator('.reading-welcome').isVisible(),true);
    console.log('PASS '+kind+': formula download failure does not block the page');
    await page.close();
  }
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'paperloom-offline-readiness-'));
  for(const kind of ['offline','offline_paper']) {
    const filename=path.join(folder,kind+'.html');fs.writeFileSync(filename,docs[kind]);
    const page=await browser.newPage();await page.goto(pathToFileURL(filename).href);
    assert.equal(await page.locator('.reading-open').count(),0,'offline reports must not retain a disabled reading action');
    assert.equal(await page.locator('a[href^="http://127.0.0.1:8000/reading?"]').count(),kind==='offline_paper'?1:0);
    await page.locator('.math-inline .katex').waitFor();await page.close();
  }
  console.log('PASS offline reports: valid continuation links, no dead reading button, formulas render');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
