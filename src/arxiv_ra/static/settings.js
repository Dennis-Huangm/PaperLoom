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
if (connectionForm && connectionTest) {
  const result = document.querySelector('#connection-test-result');
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
      connectionTest.textContent = '测试连接';
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
  settingsLinks.forEach((link) => link.addEventListener('click', () => {
    settingsLinks.forEach((item) => item.classList.toggle('active', item === link));
  }));
  const sections = settingsLinks.map((link) => document.querySelector(link.getAttribute('href'))).filter(Boolean);
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    settingsLinks.forEach((link) => link.classList.toggle('active', link.getAttribute('href') === `#${visible.target.id}`));
  }, {rootMargin: '-15% 0px -70% 0px', threshold: [0, .2, .5]});
  sections.forEach((section) => observer.observe(section));
}

})();
