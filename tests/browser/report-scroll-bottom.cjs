// Start python tests/browser/serve_report_fixture.py before running this script.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage({viewport: {width: 1920, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.env.REPORT_PREVIEW_URL || 'http://127.0.0.1:8771';
    await page.goto(base + '/artifacts/2026-10-03/reports/paper-0/report.html');
    await page.waitForSelector('.report-paper-link');
    fs.mkdirSync('work/report-scroll-qa', {recursive: true});
    for (const width of [3840, 2560, 1920, 1440, 1100, 900, 390]) {
      await page.setViewportSize({width, height: 1000});
      // Let the toolbar ResizeObserver publish its height after reflow.
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      for (const position of ['middle', 'bottom']) {
        await page.evaluate(position => window.scrollTo({
          top: position === 'bottom' ? document.documentElement.scrollHeight : document.documentElement.scrollHeight / 2,
          behavior: 'instant',
        }), position);
        const geometry = await page.evaluate(() => {
          const box = selector => {
            const node = document.querySelector(selector);
            if (!node.getClientRects().length) return null;
            const {top, bottom, height} = node.getBoundingClientRect();
            return {top, bottom, height};
          };
          return {header: box('.report-topbar'), toc: box('.report-sidebar'), papers: box('#report-papers'), viewport: innerHeight};
        });
        const context = `${width}px at ${position}`;
        assert(Math.abs(geometry.header.top) <= 1, `toolbar stays at the viewport top: ${context}`);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, context);
        for (const name of ['toc', 'papers']) {
          if (!geometry[name]) continue;
          assert(Math.abs(geometry[name].top - geometry.header.bottom) <= 1, `${name} stays below the toolbar: ${context}`);
          assert(Math.abs(geometry[name].bottom - geometry.viewport) <= 1, `${name} reaches the viewport bottom: ${context}`);
        }
      }
      if ([1920, 390].includes(width)) await page.screenshot({path: `work/report-scroll-qa/bottom-${width}.png`});
      if (width < 1440) {
        await page.locator('#report-papers-toggle').click();
        const drawer = await page.locator('#report-papers').boundingBox();
        assert(Math.abs(drawer.y) <= 1, `report drawer starts at viewport top: ${width}`);
        assert(Math.abs(drawer.y + drawer.height - 1000) <= 1, `report drawer reaches viewport bottom: ${width}`);
        await page.keyboard.press('Escape');
      }
    }
    await page.locator('.toc-toggle').click();
    const toc = await page.locator('.report-sidebar').boundingBox();
    assert(Math.abs(toc.y) <= 1 && Math.abs(toc.height - 1000) <= 1, 'mobile TOC fills the viewport at the report end');
    await page.keyboard.press('Escape');
    assert.deepEqual(errors, []);
    console.log('PASS report middle/bottom scrolling, sticky toolbar/navigation, desktop scaling, mobile drawers and horizontal overflow at seven widths.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
