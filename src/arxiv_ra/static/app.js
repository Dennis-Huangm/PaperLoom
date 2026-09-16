const toast = (message) => {
  const node = document.querySelector('#toast');
  if (!node) return;
  node.textContent = message;
  node.classList.add('show');
  window.setTimeout(() => node.classList.remove('show'), 3600);
};

const jobIcon = (status) => {
  if (status === 'succeeded') return 'fas fa-check';
  if (status === 'failed') return 'fas fa-times';
  return 'fas fa-circle-notch';
};

const renderJob = (job) => {
  const list = document.querySelector('#job-list');
  if (!list) return;
  list.querySelector('.queue-empty')?.remove();
  let node = list.querySelector(`[data-job-id="${job.id}"]`);
  if (!node) {
    node = document.createElement('article');
    node.dataset.jobId = job.id;
    list.prepend(node);
  }
  node.dataset.jobStatus = job.status;
  node.className = `job job-${job.status}`;
  node.replaceChildren();

  const indicator = document.createElement('span');
  indicator.className = 'job-indicator';
  const icon = document.createElement('i');
  icon.className = jobIcon(job.status);
  icon.setAttribute('aria-hidden', 'true');
  indicator.append(icon);

  const copy = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = job.label;
  const detail = document.createElement('p');
  detail.textContent = job.detail;
  copy.append(title, detail);

  const time = document.createElement('time');
  time.textContent = job.updated_at.slice(11, 19);
  node.append(indicator, copy, time);
  if (job.result_url) {
    const link = document.createElement('a');
    link.href = job.result_url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    link.textContent = '打开结果';
    node.append(link);
  }
};

const pollJob = async (jobId) => {
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (!response.ok) return;
    const job = await response.json();
    renderJob(job);
    if (job.status === 'queued' || job.status === 'running') {
      window.setTimeout(() => pollJob(jobId), 3000);
    } else if (job.status === 'succeeded') {
      toast(`${job.label}已完成`);
    } else {
      toast(`${job.label}失败：${job.detail}`);
    }
  } catch (_) {
    window.setTimeout(() => pollJob(jobId), 5000);
  }
};

document.querySelectorAll('.job-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const original = button?.innerHTML;
    if (button) {
      button.disabled = true;
      button.textContent = '已加入队列…';
    }
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '无法创建任务');
      renderJob(payload);
      toast('任务已加入后台队列，可继续浏览其他页面');
      pollJob(payload.id);
    } catch (error) {
      toast(error.message);
    } finally {
      if (button) {
        button.disabled = false;
        button.innerHTML = original;
      }
    }
  });
});

const zoteroLocationDialog = document.querySelector('#zotero-location-dialog');
const zoteroLocationSelect = document.querySelector('#zotero-location-select');
let pendingZoteroForm = null;

const submitZoteroForm = async (form, collectionKey) => {
  const button = form.querySelector('button[type="submit"]');
  const original = button?.innerHTML;
  if (button) {
    button.disabled = true;
    button.textContent = '正在保存…';
  }
  try {
    const formData = new FormData(form);
    formData.set('collection_key', collectionKey);
    const response = await fetch(form.action, {method: 'POST', body: formData});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Zotero 保存失败');
    toast(payload.message || '已保存到 Zotero');
  } catch (error) {
    toast(error.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = original;
    }
  }
};

document.querySelectorAll('.zotero-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const response = await fetch('/api/zotero/collections');
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '无法读取 Zotero 分类');
      zoteroLocationSelect.replaceChildren();
      const defaultOption = document.createElement('option');
      defaultOption.value = '';
      defaultOption.textContent = `默认分类：${payload.default_name || 'arXiv Research Assistant'}`;
      zoteroLocationSelect.append(defaultOption);
      const rootOption = document.createElement('option');
      rootOption.value = '__root__';
      rootOption.textContent = '我的文库（不新增分类）';
      zoteroLocationSelect.append(rootOption);
      (payload.collections || []).forEach((collection) => {
        const option = document.createElement('option');
        option.value = collection.key;
        option.textContent = collection.label;
        zoteroLocationSelect.append(option);
      });
      pendingZoteroForm = form;
      zoteroLocationDialog.showModal();
      zoteroLocationSelect.focus();
    } catch (error) {
      toast(error.message);
    }
  });
});

document.querySelector('#zotero-location-cancel')?.addEventListener('click', () => {
  pendingZoteroForm = null;
  zoteroLocationDialog.close();
});

document.querySelector('#zotero-location-confirm')?.addEventListener('click', () => {
  const form = pendingZoteroForm;
  const collectionKey = zoteroLocationSelect.value;
  pendingZoteroForm = null;
  zoteroLocationDialog.close();
  if (form) submitZoteroForm(form, collectionKey);
});

const zoteroConnect = document.querySelector('#zotero-connect');
zoteroConnect?.addEventListener('click', async () => {
  const original = zoteroConnect.innerHTML;
  zoteroConnect.disabled = true;
  zoteroConnect.textContent = '等待 Zotero 确认…';
  try {
    const response = await fetch(zoteroConnect.dataset.action, {method: 'POST'});
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

document.querySelectorAll('[data-job-id]').forEach((node) => {
  if (['queued', 'running'].includes(node.dataset.jobStatus)) pollJob(node.dataset.jobId);
});

const navToggle = document.querySelector('.nav-toggle');
navToggle?.addEventListener('click', () => {
  const nav = document.querySelector('.primary-nav');
  const open = nav?.classList.toggle('open') || false;
  navToggle.setAttribute('aria-expanded', String(open));
});

document.querySelectorAll('.library-add-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const original = button?.innerHTML;
    if (button) {
      button.disabled = true;
      button.textContent = '正在加入…';
    }
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '无法加入文献库');
      if (button) {
        button.innerHTML = '<i class="fas fa-thumbs-up" aria-hidden="true"></i>已加入文献库（相关）';
        button.classList.add('active');
        button.disabled = true;
      }
      toast(payload.message);
    } catch (error) {
      if (button) {
        button.disabled = false;
        button.innerHTML = original;
      }
      toast(error.message);
    }
  });
});

const historyDataNode = document.querySelector('#recommendation-history-data');
if (historyDataNode) {
  const history = JSON.parse(historyDataNode.textContent || '[]');
  const dialog = document.querySelector('#recommendation-calendar-dialog');
  const trigger = document.querySelector('#recommendation-calendar-trigger');
  const grid = document.querySelector('#recommendation-calendar-grid');
  const monthTitle = document.querySelector('#calendar-month-title');
  const previousMonth = document.querySelector('#calendar-previous-month');
  const nextMonth = document.querySelector('#calendar-next-month');
  const summary = document.querySelector('#calendar-result-summary');
  const selectedDate = dialog?.dataset.selectedDate || history[0]?.date || '';
  const available = new Map(history.map((item) => [item.date, Number(item.count) || 0]));
  const pad = (value) => String(value).padStart(2, '0');
  const monthKey = (date) => `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}`;
  const parseMonth = (value) => {
    const [year, month] = value.slice(0, 7).split('-').map(Number);
    return new Date(Date.UTC(year, month - 1, 1));
  };
  let visibleMonth = parseMonth(selectedDate || history[0]?.date || new Date().toISOString().slice(0, 10));
  const minimumMonth = history.length ? history[history.length - 1].date.slice(0, 7) : '';
  const maximumMonth = history.length ? history[0].date.slice(0, 7) : '';

  const renderCalendar = () => {
    if (!grid || !monthTitle) return;
    const year = visibleMonth.getUTCFullYear();
    const month = visibleMonth.getUTCMonth();
    monthTitle.textContent = `${year} 年 ${month + 1} 月`;
    grid.replaceChildren();
    const mondayOffset = (new Date(Date.UTC(year, month, 1)).getUTCDay() + 6) % 7;
    const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    for (let index = 0; index < mondayOffset; index += 1) {
      const blank = document.createElement('span');
      blank.className = 'calendar-day-empty';
      blank.setAttribute('aria-hidden', 'true');
      grid.append(blank);
    }
    let resultDays = 0;
    let resultPapers = 0;
    for (let day = 1; day <= daysInMonth; day += 1) {
      const date = `${year}-${pad(month + 1)}-${pad(day)}`;
      const count = available.get(date) || 0;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'calendar-day';
      button.disabled = count === 0;
      button.setAttribute('role', 'gridcell');
      button.setAttribute('aria-label', count ? `${date}，${count} 篇推荐` : `${date}，没有推荐结果`);
      if (date === selectedDate) {
        button.classList.add('selected');
        button.setAttribute('aria-current', 'date');
      }
      const number = document.createElement('span');
      number.textContent = String(day);
      button.append(number);
      if (count) {
        resultDays += 1;
        resultPapers += count;
        button.classList.add('has-results');
        const badge = document.createElement('small');
        badge.textContent = String(count);
        button.append(badge);
        button.addEventListener('click', () => window.location.assign(`/?date=${date}`));
      }
      grid.append(button);
    }
    if (summary) summary.textContent = resultDays
      ? `本月 ${resultDays} 天有结果，共 ${resultPapers} 篇`
      : '本月没有推荐结果';
    if (previousMonth) {
      const candidate = new Date(Date.UTC(year, month - 1, 1));
      previousMonth.disabled = Boolean(minimumMonth) && monthKey(candidate) < minimumMonth;
    }
    if (nextMonth) {
      const candidate = new Date(Date.UTC(year, month + 1, 1));
      nextMonth.disabled = Boolean(maximumMonth) && monthKey(candidate) > maximumMonth;
    }
  };

  trigger?.addEventListener('click', () => {
    renderCalendar();
    dialog?.showModal();
  });
  document.querySelector('#recommendation-calendar-close')?.addEventListener('click', () => dialog?.close());
  previousMonth?.addEventListener('click', () => {
    visibleMonth = new Date(Date.UTC(visibleMonth.getUTCFullYear(), visibleMonth.getUTCMonth() - 1, 1));
    renderCalendar();
  });
  nextMonth?.addEventListener('click', () => {
    visibleMonth = new Date(Date.UTC(visibleMonth.getUTCFullYear(), visibleMonth.getUTCMonth() + 1, 1));
    renderCalendar();
  });
  document.querySelector('#calendar-latest-date')?.addEventListener('click', () => {
    if (history[0]?.date) window.location.assign(`/?date=${history[0].date}`);
  });
  dialog?.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
}

const dataNode = document.querySelector('#recommendations-data');
if (dataNode) {
  const papers = JSON.parse(dataNode.textContent);
  const rows = [...document.querySelectorAll('.paper-row')];
  let activeFilter = 'all';
  let selectedPaperIndex = 0;

  const humanVenueStatus = (status) => {
    if (status === 'verified_metadata') return ['外部元数据已核实', 'verified'];
    if (status === 'declared_in_arxiv') return ['作者在 arXiv 声明', 'declared'];
    return ['待核实', ''];
  };

  const setText = (selector, value) => {
    const node = document.querySelector(selector);
    if (node) node.textContent = value ?? '';
  };

  const setFeedbackUI = (feedback) => {
    const verdict = feedback?.verdict || '';
    document.querySelectorAll('[data-feedback]').forEach((button) => {
      button.classList.toggle('active', button.dataset.feedback === verdict);
    });
  };

  const setLibraryUI = (saved) => {
    const button = document.querySelector('#inspector-library-button');
    const label = document.querySelector('#inspector-library-label');
    if (button) {
      button.dataset.saved = String(Boolean(saved));
      button.classList.toggle('active', Boolean(saved));
    }
    if (label) label.textContent = saved ? '已加入文献库（相关）' : '加入文献库';
  };

  const selectPaper = (index) => {
    const item = papers[index];
    if (!item) return;
    selectedPaperIndex = index;
    rows.forEach((row, rowIndex) => {
      const selected = rowIndex === index;
      row.classList.toggle('selected', selected);
      row.setAttribute('aria-pressed', String(selected));
      const icon = row.querySelector('.row-select i');
      if (icon) icon.className = selected ? 'fas fa-check-square' : 'far fa-square';
    });
    const paper = item.paper;
    const verified = item.verified || {};
    setText('#inspector-position', `${index + 1} / ${papers.length}`);
    setText('#inspector-title', paper.title);
    setText('#inspector-reason', paper.recommendation_detail || paper.recommendation_reason || '依据研究主题、关键词与发布时间完成自动筛选。');
    setText('#inspector-source', paper.source_label || '历史记录，来源待核实');
    setText('#inspector-abstract', paper.abstract_zh || paper.abstract);
    setText('#inspector-venue', verified.venue || '出版信息待核实');
    const [statusText, statusClass] = humanVenueStatus(verified.venue_status);
    const statusNode = document.querySelector('#inspector-venue-status');
    if (statusNode) {
      statusNode.textContent = statusText;
      statusNode.className = `status-text ${statusClass}`;
    }
    setText('#inspector-id', paper.arxiv_id);
    setText('#inspector-published', (paper.published || '').slice(0, 10) || '待核实');
    setText('#inspector-updated', (paper.updated || '').slice(0, 10) || '待核实');
    setText('#inspector-category', paper.primary_category);
    setText('#inspector-score', Number(paper.final_score).toFixed(1));
    const arxiv = document.querySelector('#inspector-arxiv');
    const pdf = document.querySelector('#inspector-pdf');
    const reportId = document.querySelector('#inspector-report-id');
    const reportLabel = document.querySelector('#inspector-report-label');
    const reportOpen = document.querySelector('#inspector-report-open');
    const zoteroId = document.querySelector('#inspector-zotero-id');
    if (arxiv) arxiv.href = paper.abs_url;
    if (pdf) pdf.href = paper.pdf_url;
    if (reportId) reportId.value = paper.arxiv_id + (paper.version ? `v${paper.version}` : '');
    if (reportLabel) reportLabel.textContent = item.has_report ? '重新生成报告' : '生成完整报告';
    if (reportOpen) {
      reportOpen.hidden = !item.report_url;
      reportOpen.href = item.report_url || '#';
    }
    if (zoteroId) zoteroId.value = paper.arxiv_id;
    setFeedbackUI(item.feedback);
    setLibraryUI(item.in_library);
    const tags = document.querySelector('#inspector-tags');
    if (tags) {
      tags.replaceChildren();
      [...new Set([paper.primary_category, ...(paper.categories || [])])].forEach((tag) => {
        const node = document.createElement('span');
        node.textContent = tag;
        tags.append(node);
      });
    }
  };

  rows.forEach((row) => {
    row.addEventListener('click', () => selectPaper(Number(row.dataset.paperIndex)));
  });

  document.querySelector('#inspector-library-button')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const item = papers[selectedPaperIndex];
    if (!item || button.disabled) return;
    button.disabled = true;
    try {
      const formData = new FormData();
      formData.set('arxiv_id', item.paper.arxiv_id);
      const response = await fetch('/api/library/toggle', {method: 'POST', body: formData});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '无法更新文献库');
      item.in_library = payload.saved;
      item.feedback = payload.feedback || null;
      setLibraryUI(payload.saved);
      setFeedbackUI(item.feedback);
      toast(payload.message);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.querySelectorAll('[data-feedback]').forEach((button) => {
    button.addEventListener('click', async () => {
      const item = papers[selectedPaperIndex];
      if (!item) return;
      const original = button.innerHTML;
      button.disabled = true;
      button.textContent = '记录中…';
      try {
        const formData = new FormData();
        formData.set('arxiv_id', item.paper.arxiv_id);
        formData.set('verdict', button.dataset.feedback);
        const response = await fetch('/api/feedback', {method: 'POST', body: formData});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || '反馈保存失败');
        item.feedback = payload.feedback;
        item.in_library = Boolean(payload.in_library);
        setFeedbackUI(payload.feedback);
        setLibraryUI(item.in_library);
        toast(payload.message);
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.innerHTML = original;
      }
    });
  });

  setFeedbackUI(papers[0]?.feedback);

  const applyFilters = () => {
    const query = (document.querySelector('#paper-search')?.value || '').trim().toLowerCase();
    const category = document.querySelector('#category-filter')?.value || 'all';
    let visible = 0;
    rows.forEach((row) => {
      const index = Number(row.dataset.paperIndex);
      const item = papers[index];
      const haystack = `${item.paper.title} ${item.paper.arxiv_id} ${(item.paper.authors || []).map((a) => a.name).join(' ')}`.toLowerCase();
      const filterMatches = activeFilter === 'all'
        || (activeFilter === 'high' && Number(row.dataset.score) >= 9)
        || (activeFilter === 'verified' && row.dataset.verified === 'true');
      const show = filterMatches && (category === 'all' || row.dataset.category === category) && (!query || haystack.includes(query));
      row.hidden = !show;
      if (show) visible += 1;
    });
    const empty = document.querySelector('#paper-list-empty');
    if (empty) empty.hidden = visible > 0;
  };

  document.querySelectorAll('[data-paper-filter]').forEach((button) => {
    button.addEventListener('click', () => {
      activeFilter = button.dataset.paperFilter;
      document.querySelectorAll('[data-paper-filter]').forEach((item) => item.classList.toggle('active', item === button));
      applyFilters();
    });
  });
  document.querySelector('#paper-search')?.addEventListener('input', applyFilters);
  document.querySelector('#paper-search-button')?.addEventListener('click', applyFilters);
  document.querySelector('#category-filter')?.addEventListener('change', applyFilters);
  document.querySelector('#clear-paper-filters')?.addEventListener('click', () => {
    activeFilter = 'all';
    document.querySelectorAll('[data-paper-filter]').forEach((item) => item.classList.toggle('active', item.dataset.paperFilter === 'all'));
    const search = document.querySelector('#paper-search');
    const category = document.querySelector('#category-filter');
    if (search) search.value = '';
    if (category) category.value = 'all';
    applyFilters();
  });
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      document.querySelector('#paper-search')?.focus();
    }
  });
}

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
