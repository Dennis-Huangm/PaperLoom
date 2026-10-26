if('scrollRestoration' in history){history.scrollRestoration='manual';}if(!location.hash){requestAnimationFrame(function(){window.scrollTo(0,0);});}
// Exported older reports load this shared script without the enhancement module.
if(!window.PaperLoomMath){const script=document.createElement('script');script.src=new URL('math.js',document.currentScript.src);document.head.append(script);}
// Load through the shared reader so previously exported reports benefit too.
(() => {
  const images = [...document.querySelectorAll('.report-article img')];
  if (!images.length) return;
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  const url = new URL('report-images.css', document.currentScript.src);
  url.search = new URL(document.currentScript.src).search;
  stylesheet.href = url;
  document.head.append(stylesheet);
  const occurrences = new Map();
  const refreshers = [];
  images.forEach((img, index) => {
    const source = img.getAttribute('src');
    const occurrence = occurrences.get(source) || 0;
    occurrences.set(source, occurrence + 1);
    const key = `paperloom.reportImage.v1:${location.pathname}:${source}:${occurrence}`;
    let preferred = null;
    try {
      const saved = Number(localStorage.getItem(key));
      if (Number.isFinite(saved) && saved >= 1 && saved <= 100) preferred = saved;
    } catch { /* Storage is optional, including for file:// reports. */ }
    const frame = document.createElement('span');
    frame.className = 'report-image';
    const anchor = img.closest('a');
    const target = anchor || img;
    target.before(frame);
    frame.append(target);
    const controls = document.createElement('span');
    controls.className = 'report-image-controls';
    controls.setAttribute('role', 'group');
    controls.setAttribute('aria-label', `图片 ${index + 1} 大小`);
    const makeButton = (text, label) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = text;
      button.setAttribute('aria-label', label);
      return button;
    };
    const minus = makeButton('−', '缩小图片');
    const plus = makeButton('＋', '放大图片');
    const reset = makeButton('恢复默认', '恢复图片默认大小');
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = '1';
    slider.max = '100';
    slider.step = '1';
    slider.setAttribute('aria-label', '图片宽度占可用区域的百分比');
    const value = document.createElement('span');
    value.className = 'report-image-size';
    controls.append(minus, slider, plus, value, reset);
    controls.hidden = true;
    frame.append(controls);
    let displayed = 100;
    function refresh() {
      const width = frame.clientWidth;
      if (!width || !img.naturalWidth || !img.naturalHeight) return;
      const topbar = document.querySelector('.report-topbar')?.offsetHeight || 0;
      const availableHeight = Math.max(80, window.innerHeight - topbar - 120);
      const defaultWidth = Math.min(width, img.naturalWidth, availableHeight * img.naturalWidth / img.naturalHeight);
      const desiredWidth = preferred === null ? defaultWidth : width * preferred / 100;
      img.style.setProperty('--report-image-width', `${desiredWidth}px`);
      displayed = Math.max(1, Math.min(100, Math.round(desiredWidth / width * 100)));
      slider.value = String(displayed);
      value.textContent = `${displayed}%`;
      slider.setAttribute('aria-valuetext', `${displayed}% 正文可用宽度${preferred === null ? '，自动适应' : ''}`);
      minus.disabled = displayed <= 1;
      plus.disabled = displayed >= 100;
      controls.hidden = false;
    }
    function choose(percent) {
      preferred = percent === null ? null : Math.max(1, Math.min(100, percent));
      try {
        if (preferred === null) localStorage.removeItem(key);
        else localStorage.setItem(key, String(preferred));
      } catch { /* Reading and resizing remain available without storage. */ }
      refresh();
    }
    slider.addEventListener('input', () => choose(Number(slider.value)));
    minus.addEventListener('click', () => choose(displayed - 5));
    plus.addEventListener('click', () => choose(displayed + 5));
    reset.addEventListener('click', () => choose(null));
    img.addEventListener('load', refresh);
    new ResizeObserver(refresh).observe(frame);
    refreshers.push(refresh);
    refresh();
  });
  window.addEventListener('resize', () => refreshers.forEach(refresh => refresh()));
})();
(() => {
  const sidebar = document.querySelector('.report-sidebar');
  const shell = document.querySelector('.report-shell');
  const toggle = document.querySelector('.toc-toggle');
  const edgeToggle = document.querySelector('#report-toc-toggle');
  const closeButton = document.querySelector('#report-toc-close');
  const backdrop = document.querySelector('#report-toc-backdrop');
  if (!sidebar || !toggle || !shell) return;
  // Existing file:// reports still use the earlier markup and shared script.
  if (!edgeToggle || !closeButton) {
    function setLegacyOpen(value) {
      sidebar.classList.toggle('open', value);
      toggle.setAttribute('aria-expanded', String(value));
    }
    toggle.addEventListener('click', () => {
      const value = !sidebar.classList.contains('open');
      if (value) document.dispatchEvent(new Event('report-toc-opening'));
      setLegacyOpen(value);
    });
    sidebar.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setLegacyOpen(false)));
    document.addEventListener('report-toc-close', () => setLegacyOpen(false));
    document.addEventListener('keydown', event => { if (event.key === 'Escape') setLegacyOpen(false); });
    return;
  }
  const desktop = matchMedia('(min-width:761px)');
  let preferredOpen = true;
  try { preferredOpen = localStorage.getItem('paperloom.reportToc.open') !== 'false'; } catch { /* optional */ }
  let open = false;
  function setOpen(value, remember = true) {
    open = value;
    if (remember && desktop.matches) {
      preferredOpen = value;
      try { localStorage.setItem('paperloom.reportToc.open', String(value)); } catch { /* optional */ }
    }
    sidebar.hidden = !open;
    sidebar.classList.toggle('open', open && !desktop.matches);
    const drawer = open && !desktop.matches;
    document.body.classList.toggle('toc-drawer-open', drawer);
    if (backdrop) backdrop.hidden = !drawer;
    document.querySelector('.report-article').inert = drawer || document.body.classList.contains('papers-drawer-open');
    if (drawer) {
      sidebar.setAttribute('role', 'dialog');
      sidebar.setAttribute('aria-modal', 'true');
    } else {
      sidebar.removeAttribute('role');
      sidebar.removeAttribute('aria-modal');
    }
    shell.classList.toggle('toc-collapsed', !open && desktop.matches);
    edgeToggle.hidden = open || !desktop.matches;
    for (const button of [toggle, edgeToggle]) {
      button.setAttribute('aria-expanded', String(open));
      button.setAttribute('aria-controls', sidebar.id);
      button.setAttribute('aria-label', open ? '收起目录' : '展开目录');
    }
  }
  function show() {
    document.dispatchEvent(new Event('report-toc-opening'));
    setOpen(true);
    if (!desktop.matches) closeButton.focus();
  }
  function close() {
    setOpen(false);
    (desktop.matches ? edgeToggle : toggle).focus();
  }
  toggle.addEventListener('click', () => open ? close() : show());
  edgeToggle.addEventListener('click', show);
  closeButton.addEventListener('click', close);
  backdrop?.addEventListener('click', close);
  sidebar.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
    if (!desktop.matches) setOpen(false);
  }));
  document.addEventListener('report-toc-close', () => setOpen(false, false));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && open && !desktop.matches) close();
    if (event.key === 'Tab' && open && !desktop.matches) {
      const controls = [...sidebar.querySelectorAll('button,a[href]')];
      const first = controls[0], last = controls[controls.length - 1];
      if (!sidebar.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  desktop.addEventListener('change', () => setOpen(desktop.matches && preferredOpen, false));
  setOpen(desktop.matches && preferredOpen, false);
})();

// The action row can wrap when the viewport or reading dock changes width.
// Share its actual height with sticky navigation and anchor scrolling.
const reportTopbar = document.querySelector('.report-topbar');
if (reportTopbar) {
  const updateTopbarHeight = () => {
    const height = reportTopbar.offsetHeight;
    if (height) document.documentElement.style.setProperty('--report-topbar-height', `${height}px`);
  };
  updateTopbarHeight();
  new ResizeObserver(updateTopbarHeight).observe(reportTopbar);
}

document.querySelector('#report-print')?.addEventListener('click', () => window.print());
// Printed reports include the complete matrices, even if the reading view is
// collapsed. Restore the reader's disclosure choices after printing.
let printClosedTables = [];
window.addEventListener('beforeprint', () => {
  printClosedTables = [...document.querySelectorAll('details.report-table-details:not([open])')];
  printClosedTables.forEach(table => { table.open = true; });
});
window.addEventListener('afterprint', () => {
  printClosedTables.forEach(table => { table.open = false; });
  printClosedTables = [];
});
const button = document.querySelector('#report-library-add');
if (button && location.protocol === 'file:') button.remove();
if (button && location.protocol !== 'file:') {
  const message = document.querySelector('#report-action-message');
  const headers = {'X-Paperloom-Profile': button.dataset.profileId};
  const markSaved = () => {
    button.classList.add('saved'); button.disabled = true;
    button.querySelector('span').textContent = '已加入文献库（相关）';
  };
  fetch('/api/library/status?arxiv_id=' + encodeURIComponent(button.dataset.arxivId), {headers})
    .then(response => response.ok ? response.json() : {}).then(data => {if (data.saved) markSaved();}).catch(() => {});
  button.addEventListener('click', async () => {
    button.disabled = true;
    const data = new FormData();
    data.set('arxiv_id', button.dataset.arxivId);
    data.set('origin', 'report');
    data.set('report_id', button.dataset.reportId);
    try {
      const response = await fetch('/api/library/add', {method: 'POST', body: data, headers});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '加入失败');
      markSaved(); message.textContent = result.message;
    } catch (error) { button.disabled = false; message.textContent = error.message; }
  });
}
