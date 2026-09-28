// Run against an isolated application with fixture maps: GRAPH_PREVIEW_URL=http://127.0.0.1:8768.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const base = process.env.GRAPH_PREVIEW_URL || 'http://127.0.0.1:8768';
  fs.mkdirSync('work/graph-qa', {recursive: true});
  await page.goto(base + '/citations');
  const links = await page.locator('.graph-library-card a').evaluateAll(elements => elements.map(e => e.href));
  assert(links.length >= 2, 'Fixture needs 50- and 150-node maps');
  await page.setViewportSize({width:1440,height:900});
  await page.screenshot({path:'work/graph-qa/library.png',fullPage:true});
  const measurements = [];
  for (const url of links) {
    for (const [width,height] of [[1440,900],[1366,768],[1024,768],[390,844]]) {
      await page.setViewportSize({width,height});
      await page.goto(url);
      await page.waitForFunction(() => document.body.dataset.layout === 'ready');
      const metrics = await page.evaluate(() => ({nodes: document.querySelectorAll('#nodes > g').length,
        layout: Number(document.body.dataset.layoutMs), interactive: Number(document.body.dataset.interactiveMs),
        overflow:document.documentElement.scrollWidth>innerWidth,canvas:document.querySelector('#graph').getBoundingClientRect().width}));
      assert(!metrics.overflow, `Horizontal overflow at ${width}`);
      assert(metrics.layout < 3500, `Slow layout: ${metrics.layout}`);
      if(width===1366) assert(metrics.canvas>=600);
      measurements.push({width,height,...metrics});
      await page.screenshot({path:`work/graph-qa/map-${metrics.nodes}-${width}.png`,fullPage:true});
    }
  }
  await page.setViewportSize({width:1440,height:900}); await page.goto(links[0]);
  await page.waitForFunction(() => document.body.dataset.layout === 'ready');
  const positions = await page.locator('#nodes > g').evaluateAll(els => els.map(e=>e.getAttribute('transform')));
  await page.getByRole('button',{name:'引用脉络',exact:true}).click();
  assert.deepEqual(await page.locator('#nodes > g').evaluateAll(els=>els.map(e=>e.getAttribute('transform'))),positions);
  await page.locator('.paper-card').nth(1).click();
  assert.equal(await page.locator('.paper-card[aria-pressed=true]').count(),1);
  await page.locator('.relation-button').first().click();
  assert.equal(await page.locator('#detail-eyebrow').textContent(),'关联依据');
  await page.screenshot({path:'work/graph-qa/relationship.png',fullPage:true});
  await page.getByRole('searchbox').fill('no matching title zzzzz');
  assert.equal(await page.locator('.paper-card').count(),1);
  await page.getByRole('button',{name:/筛选/}).click();
  await page.getByRole('button',{name:'清除全部筛选'}).click();
  await page.getByRole('button',{name:'查看结果'}).click();
  assert((await page.locator('.paper-card').count())>1);
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'查看论文列表'}).click();
  await page.locator('.paper-card').nth(1).click();
  assert.equal(await page.locator('#detail-panel').getAttribute('aria-modal'),'true');
  await page.screenshot({path:'work/graph-qa/mobile-detail.png',fullPage:true});
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#detail-panel').getAttribute('aria-modal'),null);
  assert.deepEqual(errors,[]);
  fs.writeFileSync('work/graph-qa/measurements.json', JSON.stringify({browser:browser.version(),measurements,errors},null,2));
  console.log(JSON.stringify({browser:browser.version(),measurements,errors}));
  await browser.close();
})().catch(e=>{console.error(e);process.exit(1);});
