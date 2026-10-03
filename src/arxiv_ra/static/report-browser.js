(() => {
  const panel = document.querySelector('#report-papers');
  if (!panel || !['http:', 'https:'].includes(location.protocol)) return;
  const shell = document.querySelector('.report-shell');
  const toggle = document.querySelector('#report-papers-toggle');
  const closeButton = document.querySelector('#report-papers-close');
  const backdrop = document.querySelector('#report-papers-backdrop');
  const search = document.querySelector('#report-papers-search');
  const list = document.querySelector('#report-papers-list');
  const status = document.querySelector('#report-papers-status');
  const retry = document.querySelector('#report-papers-retry');
  const clear = document.querySelector('#report-papers-clear');
  const resize = document.querySelector('#report-papers-resize');
  const wide = matchMedia('(min-width:1440px)');
  const toc = document.querySelector('.report-sidebar');
  const tocToggle = document.querySelector('.toc-toggle');
  // Storage may be disabled; navigation still works without persistence.
  function read(storage, key) { try { return window[storage].getItem(key); } catch { return null; } }
  function save(storage, key, value) { try { window[storage].setItem(key, value); } catch { /* optional */ } }
  let savedOpen = read('localStorage', 'paperloom.reportPapers.open') !== 'false';
  let width = Number(read('localStorage', 'paperloom.reportPapers.width')) || 320;
  let entries = [];
  let loaded = false;
  let requestNumber = 0;
  let open = false;
  const drawerWasOpen = read('sessionStorage', 'paperloom.reportPapers.drawerOpen') === 'true';
  search.value = read('sessionStorage', 'paperloom.reportPapers.search') || '';

  function setWidth(value) {
    width = Math.max(260, Math.min(440, value));
    shell.style.setProperty('--report-papers-width', width + 'px');
    resize.setAttribute('aria-valuenow', String(Math.round(width)));
  }
  function closeToc() {
    toc?.classList.remove('open');
    tocToggle?.setAttribute('aria-expanded', 'false');
  }
  function setOpen(value, remember = true) {
    open = value;
    if (remember) {
      savedOpen = value;
      save('localStorage', 'paperloom.reportPapers.open', String(value));
    }
    panel.hidden = !open;
    toggle.hidden = open;
    toggle.setAttribute('aria-expanded', String(open));
    shell.classList.toggle('papers-open', open && wide.matches);
    document.body.classList.toggle('papers-drawer-open', open && !wide.matches);
    backdrop.hidden = !open || wide.matches;
    if (!wide.matches) save('sessionStorage', 'paperloom.reportPapers.drawerOpen', String(open));
    document.querySelector('.report-article').inert = open && !wide.matches;
    if (toc) toc.inert = open && !wide.matches;
    if (open && !wide.matches) {
      closeToc();
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
      search.focus();
    } else {
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
    }
    if (open) load();
  }
  function close() { setOpen(false); toggle.focus(); }
  function render() {
    const query = search.value.trim().toLocaleLowerCase();
    const matches = entries.filter(entry =>
      `${entry.title} ${entry.arxiv_id}${entry.version ? 'v' + entry.version : ''} ${(entry.authors || []).join(' ')}`.toLocaleLowerCase().includes(query));
    list.replaceChildren();
    const fragment = document.createDocumentFragment();
    matches.forEach(entry => {
      const link = document.createElement('a');
      link.className = 'report-paper-link';
      link.href = entry.report_url;
      const current = panel.dataset.arxivId
        ? entry.arxiv_id === panel.dataset.arxivId.replace(/v\d+$/, '')
        : entry.report_id === panel.dataset.reportId;
      if (current) link.setAttribute('aria-current', 'page');
      const title = document.createElement('span');
      title.className = 'report-paper-title';
      title.textContent = entry.title;
      const meta = document.createElement('span');
      meta.className = 'report-paper-meta';
      meta.textContent = `arXiv: ${entry.arxiv_id}${entry.version ? 'v' + entry.version : ''} · ${entry.date}`;
      const directions = document.createElement('span');
      directions.className = 'report-paper-directions';
      directions.textContent = entry.directions.map(direction => direction.name).join('、');
      link.append(title, meta, directions);
      fragment.append(link);
    });
    list.append(fragment);
    status.textContent = matches.length ? `${matches.length} 篇论文` : query ? '没有找到匹配的论文' : '还没有本地报告';
    clear.hidden = !query || matches.length > 0;
  }
  async function load() {
    const currentRequest = ++requestNumber;
    retry.hidden = true;
    status.textContent = '正在加载本地报告…';
    panel.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('/api/reports', {cache: 'no-store'});
      if (!response.ok) throw new Error('catalog unavailable');
      const result = await response.json();
      if (currentRequest !== requestNumber) return;
      entries = result.reports;
      loaded = true;
      render();
    } catch {
      if (currentRequest !== requestNumber) return;
      status.textContent = '论文列表加载失败，请重试';
      retry.hidden = false;
    } finally {
      if (currentRequest === requestNumber) panel.removeAttribute('aria-busy');
    }
  }
  toggle.addEventListener('click', () => setOpen(true));
  closeButton.addEventListener('click', close);
  backdrop.addEventListener('click', close);
  retry.addEventListener('click', load);
  search.addEventListener('input', () => {
    save('sessionStorage', 'paperloom.reportPapers.search', search.value);
    if (loaded) render();
  });
  clear.addEventListener('click', () => { search.value = ''; search.dispatchEvent(new Event('input')); search.focus(); });
  tocToggle?.addEventListener('click', () => { if (open && !wide.matches) setOpen(false, false); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      if (open) close();
      closeToc();
    }
    if (event.key === 'Tab' && open && !wide.matches) {
      const controls = [...panel.querySelectorAll('button,input,a[href]')].filter(node => !node.hidden);
      const first = controls[0], last = controls[controls.length - 1];
      if (!panel.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  resize.addEventListener('pointerdown', event => {
    if (!wide.matches || event.button !== 0) return;
    event.preventDefault();
    resize.setPointerCapture(event.pointerId);
    document.body.classList.add('report-papers-resizing');
    const startX = event.clientX, startWidth = width;
    function move(event) { setWidth(startWidth + startX - event.clientX); }
    function finish() {
      document.body.classList.remove('report-papers-resizing');
      save('localStorage', 'paperloom.reportPapers.width', String(width));
      resize.removeEventListener('pointermove', move);
      resize.removeEventListener('lostpointercapture', finish);
    }
    resize.addEventListener('pointermove', move);
    resize.addEventListener('lostpointercapture', finish);
  });
  resize.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    setWidth(event.key === 'Home' ? 260 : event.key === 'End' ? 440 : width + (event.key === 'ArrowLeft' ? 10 : -10));
    save('localStorage', 'paperloom.reportPapers.width', String(width));
  });
  wide.addEventListener('change', () => setOpen(wide.matches && savedOpen, false));
  setWidth(width);
  setOpen(wide.matches ? savedOpen : drawerWasOpen, false);
})();
