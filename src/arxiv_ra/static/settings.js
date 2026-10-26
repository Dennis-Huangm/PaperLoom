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
