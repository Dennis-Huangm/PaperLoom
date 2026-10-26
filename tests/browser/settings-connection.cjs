// Run serve_settings_fixture.py first: this fixture never calls a real provider.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:8774/settings');
    await page.locator('[name=llm_model]').fill('fixture-model');
    await page.locator('#credential-llm_api_key').fill('fixture-key');
    await page.locator('#credential-llm_base_url').fill('https://example.test/v1');
    await page.locator('#test-model-connection').click();
    await page.waitForFunction(() => document.querySelector('#connection-test-result').textContent.includes('连接成功'));
    await page.locator('[name=llm_model]').fill('changed-model');
    assert.match(await page.locator('#connection-test-result').textContent(), /重新测试/);
    await page.locator('#model-connection-form button[type=submit]').first().click();
    await page.waitForURL('**/settings?saved=credentials');
    assert.equal(await page.locator('[name=llm_model]').inputValue(), 'changed-model');
    assert.equal(await page.locator('#credential-llm_api_key').inputValue(), '');
    await page.screenshot({path: process.env.SETTINGS_SCREENSHOT || 'settings-preview.png', fullPage: false});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('Settings desktop/mobile and test/edit/save flow passed.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
