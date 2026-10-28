(() => {
const {scopedFetch, toast} = window.PaperLoom;

document.querySelectorAll('.credential-visibility').forEach((button) => {
  const card = button.closest('.credential-card');
  const input = card.querySelector('.credential-input-control input:not([type="hidden"])');
  const stored = card.querySelector('.credential-value');
  const clear = card.querySelector('.clear-option input');
  const setVisible = (visible) => {
    input.value = visible ? stored.value : '';
    input.placeholder = stored.value || (input.dataset.configured === 'true' && input.dataset.edited !== 'true')
      ? '********' : '输入新值';
    input.type = visible ? 'text' : 'password';
    button.setAttribute('aria-pressed', String(visible));
    button.setAttribute('aria-label', `${visible ? '隐藏' : '显示'} ${button.dataset.label}`);
    button.innerHTML = `<i class="far fa-eye${visible ? '-slash' : ''}" aria-hidden="true"></i><span>${visible ? '隐藏' : '显示'}</span>`;
  };
  input.addEventListener('input', () => {
    input.dataset.edited = 'true';
    stored.value = input.value;
  });
  clear.addEventListener('change', () => {
    if (clear.checked) setVisible(false);
    input.disabled = clear.checked;
    button.disabled = clear.checked;
  });
  button.addEventListener('click', async () => {
    if (input.type === 'text') {
      setVisible(false);
      return;
    }
    button.disabled = true;
    try {
      if (!stored.value && input.dataset.configured === 'true'
          && input.dataset.loaded !== 'true' && input.dataset.edited !== 'true') {
        const body = new FormData();
        body.set('field', stored.name);
        const response = await scopedFetch('/settings/credentials/reveal', {method: 'POST', body});
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '无法读取当前配置');
        // Keep edits made while the request was in flight.
        if (input.dataset.edited !== 'true') stored.value = payload.value;
        input.dataset.loaded = 'true';
      }
      if (!clear.checked) setVisible(true);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = clear.checked;
    }
  });
});

const connectionForm = document.querySelector('#model-connection-form');
const connectionTest = document.querySelector('#test-model-connection');
const presetSwitcher = document.querySelector('#model-preset-switcher');
const presetName = document.querySelector('#model-preset-name');
let connectionEdited = false;
let presetPending = false;
connectionForm?.addEventListener('input', () => {
  connectionEdited = true;
  presetName.setCustomValidity('');
});

const updatePresetButtons = () => {
  if (!presetSwitcher) return;
  const selected = presetSwitcher.querySelector('select').selectedOptions[0];
  presetSwitcher.querySelectorAll('button').forEach((button) => {
    button.disabled = !selected.value || (!button.hasAttribute('formaction') && selected.dataset.available !== 'true');
  });
};
presetSwitcher?.addEventListener('change', updatePresetButtons);
updatePresetButtons();

// Keep drafts visible when a preset request fails validation or the service
// cannot be reached. Normal credential saves retain their existing form flow.
async function submitPreset(event) {
  const form = event.currentTarget;
  const button = event.submitter;
  const action = button?.getAttribute('formaction') || form.getAttribute('action');
  if (!action.startsWith('/settings/model-presets')) return;
  event.preventDefault();
  if (presetPending) return;
  if (form === connectionForm && !presetName.value.trim()) {
    presetName.setCustomValidity('请填写配置名称。');
    presetName.reportValidity();
    presetName.setCustomValidity('');
    return;
  }
  if (action.endsWith('/delete')) {
    const selected = presetSwitcher.querySelector('select').selectedOptions[0];
    if (!window.confirm(`删除已保存的配置“${selected.textContent}”？当前连接仍可使用。`)) return;
  }
  if (form === presetSwitcher && connectionEdited
      && !window.confirm('当前表单有未保存的修改，继续后将丢弃这些修改。是否继续？')) return;
  presetPending = true;
  button.disabled = true;
  try {
    const response = await scopedFetch(action, {method: 'POST', body: new FormData(form)});
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(typeof payload.detail === 'string' ? payload.detail : '无法保存模型配置，请稍后重试。');
    }
    const destination = response.url + '#credentials';
    if (window.location.href === destination) window.location.reload();
    else window.location.assign(destination);
  } catch (error) {
    toast(error.message);
    presetPending = false;
    button.disabled = false;
    updatePresetButtons();
  }
}
connectionForm?.addEventListener('submit', submitPreset);
presetSwitcher?.addEventListener('submit', submitPreset);
if (connectionForm && connectionTest) {
  const result = document.querySelector('#connection-test-result');
  const idleTestLabel = connectionTest.innerHTML;
  let generation = 0;
  connectionForm.addEventListener('input', () => {
    generation += 1;
    if (!result.hidden) result.textContent = '配置已修改，请重新测试连接。';
  });
  connectionTest.addEventListener('click', async () => {
    if (!connectionForm.reportValidity()) return;
    const version = generation;
    connectionTest.disabled = true;
    connectionTest.textContent = '正在测试…';
    result.hidden = false;
    result.className = 'notice';
    result.textContent = '正在请求模型，请稍候…';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await scopedFetch('/settings/connection-test', {
        method: 'POST', body: new FormData(connectionForm), signal: controller.signal,
      });
      const payload = await response.json();
      if (version !== generation) return;
      result.className = `notice ${payload.ok ? 'success' : ''}`;
      result.textContent = (payload.message || '测试失败，请稍后重试。')
        + (payload.ok ? ` 耗时 ${payload.elapsed_ms} ms。保存后生效。` : '');
    } catch (error) {
      if (version === generation) result.textContent = error.name === 'AbortError'
        ? '测试超时，请检查网络后重试。' : '无法完成测试，请检查本地服务是否正常运行。';
    } finally {
      clearTimeout(timeout);
      connectionTest.disabled = false;
      connectionTest.innerHTML = idleTestLabel;
    }
  });
}

const zoteroConnect = document.querySelector('#zotero-connect');
zoteroConnect?.addEventListener('click', async () => {
  const original = zoteroConnect.innerHTML;
  zoteroConnect.disabled = true;
  zoteroConnect.textContent = '等待 Zotero 确认…';
  try {
    const response = await scopedFetch(zoteroConnect.dataset.action, {method: 'POST'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Zotero 授权失败');
    toast(payload.message);
    window.setTimeout(() => window.location.reload(), 800);
  } catch (error) {
    toast(error.message);
    zoteroConnect.disabled = false;
    zoteroConnect.innerHTML = original;
  }
});

const settingsLinks = [...document.querySelectorAll('.settings-section-nav a')];
if (settingsLinks.length) {
  const nav = document.querySelector('.settings-section-nav');
  const activateLink = (activeLink) => {
    settingsLinks.forEach((link) => {
      link.classList.toggle('active', link === activeLink);
      if (link === activeLink) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    // Reveal the active section within the horizontal navigation without
    // changing the document's vertical scroll position.
    const item = activeLink.getBoundingClientRect();
    const frame = nav.getBoundingClientRect();
    if (item.left < frame.left || item.right > frame.right) {
      const delta = item.left < frame.left ? item.left - frame.left - 6 : item.right - frame.right + 6;
      const scale = frame.width / nav.offsetWidth;
      nav.scrollLeft += delta / scale;
    }
  };
  settingsLinks.forEach((link) => link.addEventListener('click', () => {
    activateLink(link);
  }));
  const sections = settingsLinks.map((link) => document.querySelector(link.getAttribute('href'))).filter(Boolean);
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    const activeLink = settingsLinks.find((link) => link.getAttribute('href') === `#${visible.target.id}`);
    if (activeLink) activateLink(activeLink);
  }, {rootMargin: '-15% 0px -70% 0px', threshold: [0, .2, .5]});
  sections.forEach((section) => observer.observe(section));
}

})();
