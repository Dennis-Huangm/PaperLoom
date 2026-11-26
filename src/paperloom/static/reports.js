(() => {
  const workspace = document.querySelector('#report-library-workspace');
  if (!workspace) return;
  const revision = document.querySelector('#report-revision-dialog');
  if (revision) {
    const form = revision.querySelector('form');
    const fields = form.elements;
    const submit = form.querySelector('[type=submit]');
    const error = form.querySelector('.revision-error');
    const syncChoice = () => {
      const needsNumber = fields.stage.value === 'figure';
      fields.figure.closest('label').hidden = !needsNumber;
      fields.figure.disabled = !needsNumber;
      fields.figure.required = needsNumber;
      submit.disabled = !fields.stage.value || form.hasAttribute('aria-busy');
      error.hidden = true;
    };
    workspace.addEventListener('click', event => {
      const button = event.target.closest('.report-action-revise');
      if (!button) return;
      const row = button.closest('.report-row');
      form.reset();
      fields.arxiv_id.value = button.dataset.paperId;
      fields.report_id.value = row.dataset.reportId;
      revision.querySelector('#revision-paper').textContent = row.querySelector('h2').textContent.trim();
      syncChoice();
      revision.showModal();
    });
    form.addEventListener('change', syncChoice);
    revision.querySelectorAll('.revision-close, .revision-cancel').forEach(button =>
      button.addEventListener('click', () => { if (!form.hasAttribute('aria-busy')) revision.close(); }));
    revision.addEventListener('cancel', event => {
      if (form.hasAttribute('aria-busy')) event.preventDefault();
    });
    form.addEventListener('job-queued', () => revision.close());
    form.addEventListener('job-error', event => {
      error.textContent = event.detail;
      error.hidden = false;
    });
  }
  const filters = document.querySelector('.report-refinements');
  filters?.addEventListener('change', event => event.target.form.requestSubmit());
  const panel = document.querySelector('#report-directory');
  const dialog = document.querySelector('#report-directory-dialog');
  const toggle = document.querySelector('#report-directory-toggle');
  const close = document.querySelector('#report-directory-close');
  const list = panel.querySelector('.report-directory-list');
  const links = [...panel.querySelectorAll('.report-directory-link')];
  const rows = links.map(link => document.getElementById(link.hash.slice(1)));
  const header = document.querySelector('.app-header');
  const narrow = window.matchMedia('(max-width:1100px)');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion:reduce)');
  let collapsed = false;
  let current = -1;
  let frame = 0;

  const revealCurrent = () => {
    const link = links[current];
    if (!link || panel.hidden || (narrow.matches && !dialog.open)) return;
    // Scroll only the directory; scrollIntoView here would also move the page.
    const linkBox = link.getBoundingClientRect();
    const listBox = list.getBoundingClientRect();
    const scale = listBox.height / list.offsetHeight || 1;
    if (linkBox.top < listBox.top) list.scrollTop -= (listBox.top - linkBox.top) / scale;
    else if (linkBox.bottom > listBox.bottom) list.scrollTop += (linkBox.bottom - listBox.bottom) / scale;
  };

  const setCurrent = index => {
    if (index === current) return;
    if (current >= 0) {
      links[current].removeAttribute('aria-current');
      rows[current].classList.remove('is-current');
    }
    current = index;
    if (current >= 0) {
      links[current].setAttribute('aria-current', 'location');
      rows[current].classList.add('is-current');
      revealCurrent();
    }
  };

  const updateCurrent = () => {
    frame = 0;
    if (!narrow.matches && !collapsed) {
      const box = panel.getBoundingClientRect();
      const scale = box.width / panel.offsetWidth || 1;
      panel.style.setProperty('--reports-directory-viewport-top', `${Math.max(0, box.top / scale)}px`);
    }
    if (!rows.length) return;
    const threshold = header.getBoundingClientRect().bottom + 32;
    let index = 0;
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].getBoundingClientRect().top > threshold) break;
      index = i;
    }
    // The final card may be too short to reach the top of the viewport.
    if (window.scrollY > 0 && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
      index = rows.length - 1;
    }
    setCurrent(index);
  };
  const scheduleUpdate = () => {
    if (!frame) frame = window.requestAnimationFrame(updateCurrent);
  };

  const updateControls = () => {
    const expanded = narrow.matches ? dialog.open : !collapsed;
    toggle.hidden = !narrow.matches && expanded;
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-controls', narrow.matches ? dialog.id : panel.id);
    close.setAttribute('aria-label', narrow.matches ? '关闭论文目录' : '收起论文目录');
    document.body.classList.toggle('report-directory-modal-open', narrow.matches && dialog.open);
  };

  const layout = () => {
    const focusInPanel = panel.contains(document.activeElement);
    if (dialog.open) dialog.close();
    if (narrow.matches) {
      dialog.append(panel);
      panel.hidden = false;
    } else {
      workspace.prepend(panel);
      panel.hidden = collapsed;
    }
    workspace.classList.toggle('directory-collapsed', narrow.matches || collapsed);
    updateControls();
    if (focusInPanel && (narrow.matches || collapsed)) toggle.focus({preventScroll: true});
    revealCurrent();
    scheduleUpdate();
  };

  close.hidden = false;
  toggle.addEventListener('click', () => {
    if (narrow.matches) {
      dialog.showModal();
      updateControls();
      revealCurrent();
      (links[current] || close).focus({preventScroll: true});
    } else {
      collapsed = false;
      layout();
      close.focus({preventScroll: true});
    }
  });
  close.addEventListener('click', () => {
    if (narrow.matches) dialog.close();
    else {
      collapsed = true;
      layout();
      toggle.focus({preventScroll: true});
    }
  });
  dialog.addEventListener('close', updateControls);
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
  links.forEach(link => link.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (dialog.open) dialog.close();
    const index = links.indexOf(link);
    setCurrent(index);
    rows[index].focus({preventScroll: true});
    rows[index].scrollIntoView({block: 'start', behavior: reducedMotion.matches ? 'instant' : 'smooth'});
  }));

  const headerObserver = new ResizeObserver(() => {
    document.body.style.setProperty('--reports-sticky-top', `${header.offsetHeight + 16}px`);
    scheduleUpdate();
  });
  headerObserver.observe(header);
  narrow.addEventListener('change', layout);
  window.addEventListener('scroll', scheduleUpdate, {passive: true});
  window.addEventListener('resize', scheduleUpdate);

  let refreshing = false;
  let refreshPending = false;
  const sort = document.querySelector('#report-sort');
  const refreshOpenOrder = async () => {
    if (sort?.value !== 'opened_desc' || document.hidden) return;
    if (refreshing) {
      refreshPending = true;
      return;
    }
    refreshing = true;
    try {
      const response = await fetch(window.location.href, {cache: 'no-store'});
      if (!response.ok) return;
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!page.querySelector('#report-library-workspace') || document.hidden || sort.value !== 'opened_desc') return;
      const ids = [...page.querySelectorAll('.report-row')].map(row => row.dataset.reportId);
      if (ids.length === rows.length && ids.every((id, index) => id === rows[index].dataset.reportId)) return;
      const entries = new Map(rows.map((row, index) => [row.dataset.reportId, {row, link: links[index]}]));
      if (ids.length !== entries.size || ids.some(id => !entries.has(id))) {
        window.location.reload();
        return;
      }
      // Move existing nodes so bulk selections and their event handlers survive.
      setCurrent(-1);
      rows.splice(0, rows.length, ...ids.map(id => entries.get(id).row));
      links.splice(0, links.length, ...ids.map(id => entries.get(id).link));
      const table = rows[0].parentElement;
      rows.forEach((row, index) => {
        table.append(row);
        list.append(links[index]);
        links[index].querySelector('.report-directory-number').textContent = index + 1;
      });
      scheduleUpdate();
    } catch {
      // Keep the current usable list when the application is temporarily offline.
    } finally {
      refreshing = false;
      if (refreshPending) {
        refreshPending = false;
        refreshOpenOrder();
      }
    }
  };
  window.addEventListener('focus', refreshOpenOrder);
  document.addEventListener('visibilitychange', refreshOpenOrder);
  window.addEventListener('pageshow', event => {
    if (event.persisted) refreshOpenOrder();
  });
  layout();
})();
