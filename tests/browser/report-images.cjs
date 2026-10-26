// Start serve_report_fixture.py before running this browser regression.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage({viewport: {width: 2560, height: 1080}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.env.REPORT_PREVIEW_URL || 'http://127.0.0.1:8771';
    const url = base + '/artifacts/2026-10-03/reports/paper-0/report.html';
    await page.goto(url);
    const frames = page.locator('.report-image');
    const image = frames.first().locator('img');
    const slider = frames.first().locator('input');
    const metrics = () => image.evaluate(img => {
      const box = img.getBoundingClientRect(), frame = img.closest('.report-image').getBoundingClientRect();
      return {width: box.width, height: box.height, available: frame.width,
        centered: Math.abs(box.x + box.width / 2 - frame.x - frame.width / 2) < 1};
    });
    async function check(predicate) {
      await page.waitForFunction(predicate);
    }
    await page.waitForSelector('.report-image-controls:not([hidden])');
    assert.equal(await frames.count(), 3);
    assert((await metrics()).height <= 900, 'default fits viewport height');
    assert((await metrics()).centered, 'default centered');
    assert((await frames.nth(2).locator('img').boundingBox()).width <= 120, 'small image not enlarged');
    const secondWidth = (await frames.nth(1).locator('img').boundingBox()).width;
    const initialHeight = (await metrics()).height;
    const initialCaptionY = (await page.locator('.report-image + em').first().boundingBox()).y;
    await slider.fill('25');
    await check(() => Math.abs(document.querySelector('.report-image img').getBoundingClientRect().width / document.querySelector('.report-image').getBoundingClientRect().width - .25) < .002);
    assert((await metrics()).height < initialHeight);
    assert((await page.locator('.report-image + em').first().boundingBox()).y < initialCaptionY, 'caption moves up');
    assert.equal((await frames.nth(1).locator('img').boundingBox()).width, secondWidth, 'independent images');
    await page.reload();
    await page.waitForSelector('.report-image-controls:not([hidden])');
    assert.equal(await slider.inputValue(), '25', 'preference survives reload');
    await frames.first().getByRole('button', {name: '放大图片', exact: true}).click();
    assert.equal(await slider.inputValue(), '30');
    await slider.focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await slider.inputValue(), '31', 'keyboard adjustment');
    await page.locator('#report-toc-close').click();
    await check(() => Math.abs(document.querySelector('.report-image img').getBoundingClientRect().width / document.querySelector('.report-image').getBoundingClientRect().width - .31) < .002);
    assert((await metrics()).centered, 'centered after sidebar change');
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({width, height: 900});
      await check(() => document.querySelector('.report-image img').getBoundingClientRect().width <= document.querySelector('.report-image').getBoundingClientRect().width);
      assert((await metrics()).centered, `centered at ${width}`);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `no overflow at ${width}`);
    }
    await page.setViewportSize({width: 2560, height: 1080});
    await slider.fill('100');
    await check(() => Math.abs(document.querySelector('.report-image img').getBoundingClientRect().width - document.querySelector('.report-image').getBoundingClientRect().width) < 1);
    assert((await metrics()).height > 900, 'manual enlargement can exceed viewport height');
    await slider.fill('10');
    const screenWidth = (await metrics()).width;
    await page.emulateMedia({media: 'print'});
    assert.equal(await frames.first().locator('.report-image-controls').isVisible(), false);
    assert((await metrics()).width > screenWidth, 'print ignores manual shrinking');
    await page.emulateMedia({media: 'screen'});
    assert.equal(await slider.inputValue(), '10');
    await frames.first().getByRole('button', {name: '恢复图片默认大小'}).click();
    assert.equal(await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('paperloom.reportImage.v1:')).length), 0);
    await page.reload();
    await page.waitForSelector('.report-image-controls:not([hidden])');
    assert((await metrics()).height <= 900, 'reset survives reload');
    fs.mkdirSync('work/report-qa', {recursive: true});
    await image.scrollIntoViewIfNeeded();
    await page.screenshot({path: 'work/report-qa/images-desktop.png'});
    await page.setViewportSize({width: 390, height: 900});
    await page.screenshot({path: 'work/report-qa/images-mobile.png'});
    // Unavailable storage must not prevent resizing.
    await page.addInitScript(() => {
      Storage.prototype.getItem = () => { throw new Error('storage disabled'); };
      Storage.prototype.setItem = () => { throw new Error('storage disabled'); };
      Storage.prototype.removeItem = () => { throw new Error('storage disabled'); };
    });
    await page.reload();
    await page.waitForSelector('.report-image-controls:not([hidden])');
    await slider.fill('50');
    assert.equal(await slider.inputValue(), '50');
    assert.deepEqual(errors, []);
    console.log('Image defaults, centering, independent controls, keyboard, persistence, resize, reset, print and unavailable storage passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
