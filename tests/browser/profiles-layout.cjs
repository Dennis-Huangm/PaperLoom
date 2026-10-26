// Start python tests/browser/serve_profiles_fixture.py first; no external models are used.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const page = await browser.newPage();
  const base = process.env.PROFILES_PREVIEW_URL || 'http://127.0.0.1:8792';
  assert.equal((await page.request.post(base + '/fixture/reset')).ok(), true);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('work/profiles-qa', {recursive: true});
  for (const width of [1920, 1440, 1024, 768, 390]) {
    await page.setViewportSize({width, height: 1000});
    await page.goto(base + '/profiles');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `library overflow at ${width}`);
    assert.equal(await page.locator('.profile-card').count(), 2);
    assert.equal(await page.locator('.profile-card').first().getAttribute('class'), 'profile-card active');
    assert.equal(await page.locator('.profile-card-details[open]').count(), 0);
    await page.screenshot({path: `work/profiles-qa/library-${width}.png`, fullPage: true});
    await page.getByRole('button', {name: '创建检索草稿', exact: true}).click();
    const dialog = page.getByRole('dialog', {name: '创建检索草稿', exact: true});
    await dialog.waitFor();
    assert.equal(await dialog.locator('[name="name"]').evaluate(node => node === document.activeElement), true);
    assert.equal(await dialog.evaluate(node => node.scrollWidth > node.clientWidth), false, `dialog overflow at ${width}`);
    await page.screenshot({path: `work/profiles-qa/create-${width}.png`});
    await dialog.getByLabel('暂定主题名称', {exact: false}).fill('保留未提交的研究主题');
    await page.keyboard.press('Escape');
    await page.getByRole('button', {name: '创建第一个草稿', exact: true}).click();
    assert.equal(await dialog.locator('[name="name"]').inputValue(), '保留未提交的研究主题');
    await dialog.getByRole('button', {name: '关闭创建草稿'}).click();
  }
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(base + '/profiles');
  const active = page.locator('.profile-card.active');
  await active.locator('.profile-card-details summary').click();
  assert.equal(await active.locator('.profile-reference-list li').count(), 6);
  assert.equal(await active.locator('.profile-reference-list a').first().isVisible(), true);
  assert.equal(await active.locator('.profile-card-detail-content .profile-tags span').count(), 7);
  assert.match(await active.locator('.profile-overview-link').getAttribute('href'), /overview\?source=saved$/);
  await active.getByRole('link', {name: '编辑方向', exact: true}).click();
  await page.waitForURL('**/edit');
  await page.goto(base + '/profiles');
  const second = page.locator('.profile-card:not(.active)');
  await second.getByRole('button', {name: '切换到此方向'}).click();
  await page.waitForURL('**/profiles?switched=image-agents');
  assert.match(await page.locator('.profile-card.active h3').textContent(), /多模态智能体/);

  // Exercise failure feedback in the new dialog without calling a model.
  let releaseCreate;
  const pendingCreate = new Promise(resolve => {releaseCreate = resolve;});
  await page.route('**/profiles/create', async route => {
    await pendingCreate;
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: '测试：请补充主题线索'})});
  });
  await page.getByRole('button', {name: '创建检索草稿', exact: true}).click();
  const dialog = page.locator('#profile-create-dialog');
  await dialog.locator('[name="name"]').fill('World models');
  await dialog.locator('[name="keywords"]').fill('world model');
  await dialog.getByRole('button', {name: '生成草稿', exact: true}).click();
  assert.equal(await dialog.getByRole('button', {name: '关闭创建草稿'}).isDisabled(), true);
  await page.keyboard.press('Escape');
  assert.equal(await dialog.isVisible(), true);
  releaseCreate();
  await dialog.getByText('测试：请补充主题线索', {exact: true}).waitFor();
  assert.equal(await dialog.locator('[name="name"]').inputValue(), 'World models');
  assert.equal(await dialog.locator('[name="keywords"]').isEnabled(), true);
  await dialog.getByRole('button', {name: '关闭创建草稿'}).click();
  await page.unroute('**/profiles/create');

  assert.equal((await page.request.post(base + '/fixture/drafts')).ok(), true);
  await page.goto(base + '/profiles');
  assert.equal(await page.locator('.draft-list-card').count(), 2);
  assert.equal(await page.getByText('生成失败', {exact: true}).isVisible(), true);
  await page.screenshot({path: 'work/profiles-qa/populated-1440.png', fullPage: true});
  const draft = page.locator('.draft-list-card').filter({hasText: '具身智能中的世界模型'});
  await draft.getByRole('button', {name: '删除草稿', exact: true}).click();
  await page.getByRole('dialog', {name: '删除这份草稿？'}).getByRole('button', {name: '删除草稿', exact: true}).click();
  await page.waitForFunction(() => document.querySelectorAll('.draft-list-card').length === 1);
  await page.getByRole('link', {name: /已删除草稿/}).click();
  assert.equal(await page.locator('#deleted-drafts').getAttribute('open'), '');
  await page.getByRole('button', {name: '恢复草稿', exact: true}).click();
  await page.waitForURL('**/profiles/drafts/world-models');
  await page.goto(base + '/profiles');
  await page.waitForFunction(() => document.querySelectorAll('.draft-list-card').length === 2);
  await page.goto(base + '/profiles#profile-create');
  await page.locator('#profile-create-dialog').waitFor();
  assert.equal(await page.locator('#profile-create-dialog').isVisible(), true);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Profiles responsive library, dialog focus and retained input, details, switch, error feedback, draft delete and restore passed.');
})().catch(error => {console.error(error); process.exit(1);});
