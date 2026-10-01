const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/arxiv_ra/static/app.js'), 'utf8');
const handler = source.slice(source.indexOf("document.querySelectorAll('.credential-visibility')"), source.indexOf('const jobIcon'));
const element = () => ({
  checked: false, disabled: false, value: '', dataset: {}, listeners: {},
  addEventListener(event, callback) { this.listeners[event] = callback; },
  setAttribute(name, value) { this[name] = value; },
});

async function check(secret) {
  const input = element(), stored = element(), button = element(), clear = element();
  Object.assign(input, {type: 'password', name: 'llm_api_key', placeholder: '********', dataset: {configured: 'true'}});
  stored.name = 'llm_api_key';
  button.dataset.label = 'LLM API Key';
  button.closest = () => ({querySelector: (selector) => selector === '.credential-value' ? stored : selector.includes('control') ? input : clear});
  let requests = 0;
  vm.runInNewContext(handler, {
    document: {querySelectorAll: () => [button]},
    FormData: class { set() {} },
    scopedFetch: async () => { requests++; return {ok: true, json: async () => ({value: secret})}; },
    toast: (message) => { throw new Error(message); },
  });
  await button.listeners.click();
  assert.equal(input.value, secret);
  await button.listeners.click();
  assert.equal(input.value, '', 'Hidden input must not render a mask matching the credential length');
  assert.equal(input.placeholder, '********');
  assert.equal(stored.value, secret, 'Hiding must preserve the value submitted on save');
  await button.listeners.click();
  assert.equal(input.value, secret);
  assert.equal(requests, 1);
  input.value = 'edited-fixture';
  input.listeners.input();
  await button.listeners.click();
  assert.equal(input.value, '');
  assert.equal(stored.value, 'edited-fixture');
  await button.listeners.click();
  assert.equal(input.value, 'edited-fixture');
  clear.checked = true;
  clear.listeners.change();
  assert.equal(input.value, '');
  assert(input.disabled && button.disabled);
  clear.checked = false;
  clear.listeners.change();
  await button.listeners.click();
  assert.equal(input.value, 'edited-fixture');
  input.value = '';
  input.listeners.input();
  await button.listeners.click();
  assert.equal(stored.value, '');
  await button.listeners.click();
  assert.equal(input.value, '');
  assert.equal(requests, 1, 'An intentionally emptied value must not be fetched again');
}

(async () => {
  await check('abc');
  await check('a-fixture-with-a-much-longer-length');
  console.log('Fixed mask, reveal, edit/save, clear and empty-value checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
