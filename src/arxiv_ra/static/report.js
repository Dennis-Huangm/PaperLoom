if('scrollRestoration' in history){history.scrollRestoration='manual';}if(!location.hash){requestAnimationFrame(function(){window.scrollTo(0,0);});}
document.querySelectorAll('.math-block,.math-inline').forEach(function(node){try{katex.render(node.textContent,node,{displayMode:node.dataset.display==='true',throwOnError:false,strict:'ignore',trust:false});}catch(error){node.classList.add('math-error');}});
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
    const height = reportTopbar.getBoundingClientRect().height;
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
