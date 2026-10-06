const renderSummaryMath = (root) => {
  root.querySelectorAll('.math-inline,.math-block').forEach((node) => {
    if (node.querySelector('.katex') || !window.katex) return;
    try {
      window.katex.render(node.textContent, node, {
        displayMode: node.dataset.display === 'true', throwOnError: false,
        strict: 'ignore', trust: false,
      });
    } catch (_) { node.classList.add('math-error'); }
  });
};
document.querySelectorAll('.paper-summary').forEach(renderSummaryMath);

// Carry the direction shown by this page through every asynchronous operation.
const scopedFetch = (url, options = {}) => {
  const headers = new Headers(options.headers || {});
  headers.set('X-Paperloom-Profile', document.body.dataset.profileId || '');
  return fetch(url, {...options, headers});
};

const toast = (message) => {
  const node = document.querySelector('#toast');
  if (!node) return;
  node.textContent = message;
  node.classList.add('show');
  window.setTimeout(() => node.classList.remove('show'), 3600);
};

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

const jobIcon = (status) => {
  if (status === 'succeeded') return 'fas fa-check';
  if (status === 'succeeded_with_warnings' || status === 'interrupted') return 'fas fa-exclamation-triangle';
  if (status === 'failed' || status === 'cancelled') return 'fas fa-times';
  return 'fas fa-circle-notch';
};

const activeJobStatuses = ['queued', 'running', 'cancelling'];
const jobPollTimers = new Map();
const jobWarningCounts = new Map();

const renderJob = (job) => {
  const list = document.querySelector('#job-list');
  if (!list) return;
  const trackingJobs = list.closest('details#tracking-jobs');
  if (trackingJobs && activeJobStatuses.includes(job.status)) trackingJobs.open = true;
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
  detail.textContent = job.progress == null ? job.detail : `${job.progress}% · ${job.detail}`;
  copy.append(title, detail);
  if (job.report_progress && Object.keys(job.report_progress).length) {
    const steps = document.createElement('p');
    const state = job.report_progress;
    steps.textContent = `全文进度：解析${state.parsed ? '已保存' : '未完成'} · 分片 ${state.chunks_done}/${state.chunks_total || '待确定'} · 整合${state.report_ready ? '已保存' : '未完成'}；恢复时校验兼容性`;
    copy.append(steps);
  }
  if (job.submitted_detail) {
    const submitted = document.createElement('p');
    submitted.textContent = `提交内容：${job.submitted_detail}${job.retry_of ? ` · 恢复自 ${job.retry_of}` : ''}`;
    copy.append(submitted);
  }
  if (job.warnings?.length) {
    const warnings = document.createElement('ul');
    warnings.className = 'job-warnings';
    job.warnings.forEach((warning) => {
      const item = document.createElement('li');
      const component = document.createElement('strong');
      component.textContent = `${warning.component}：`;
      item.append(component, document.createTextNode(warning.message));
      warnings.append(item);
    });
    copy.append(warnings);
  }

  const time = document.createElement('time');
  time.textContent = job.updated_at.slice(0, 16).replace('T', ' ');
  node.append(indicator, copy, time);
  const actions = document.createElement('span');
  actions.className = 'job-actions';
  if (['interrupted', 'failed', 'cancelled'].includes(job.status) || (job.status === 'succeeded_with_warnings' && job.report_progress?.resumable)) {
    const note = document.createElement('p');
    note.className = 'job-recovery-note';
    note.textContent = job.recovery_note || '';
    copy.append(note);
    if (job.retry_job_id) {
      const label = document.createElement('span');
      label.textContent = '已创建恢复任务';
      actions.append(label);
    } else if (job.recoverable && job.profile_id === (document.body.dataset.profileId || '')) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'job-cancel';
      retry.dataset.retryJob = job.id;
      retry.textContent = job.kind === 'report' ? '继续全文分析' : '重新执行';
      actions.append(retry);
    } else if (job.recoverable) {
      const label = document.createElement('span');
      label.textContent = `切换到 ${job.profile_id} 后恢复`;
      actions.append(label);
    }
  }
  if (job.result_url) {
    const link = document.createElement('a');
    link.href = job.result_url;
    link.textContent = '打开结果';
    actions.append(link);
  }
  if (job.status === 'queued' || job.status === 'running') {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'job-cancel';
    cancel.dataset.cancelJob = job.id;
    cancel.textContent = '取消任务';
    actions.append(cancel);
  } else if (job.status === 'cancelling') {
    const cancelling = document.createElement('span');
    cancelling.className = 'job-cancelling-label';
    cancelling.textContent = '取消中';
    actions.append(cancelling);
  }
  node.append(actions);
};

const pollJob = async (jobId) => {
  window.clearTimeout(jobPollTimers.get(jobId));
  try {
    const response = await scopedFetch(`/api/jobs/${jobId}`);
    if (!response.ok) return;
    const job = await response.json();
    const previousWarningCount = jobWarningCounts.get(jobId) || 0;
    if (activeJobStatuses.includes(job.status) && job.warnings.length > previousWarningCount) {
      const warning = job.warnings.at(-1);
      toast(`${warning.component}异常：${warning.message}`);
    }
    jobWarningCounts.set(jobId, job.warnings.length);
    renderJob(job);
    if (activeJobStatuses.includes(job.status)) {
      jobPollTimers.set(jobId, window.setTimeout(() => pollJob(jobId), 1000));
    } else if (job.status === 'succeeded') {
      jobPollTimers.delete(jobId);
      toast(`${job.label}已完成`);
    } else if (job.status === 'succeeded_with_warnings') {
      jobPollTimers.delete(jobId);
      toast(`${job.label}已完成，但有 ${job.warnings.length} 个组件异常`);
    } else if (job.status === 'cancelled') {
      jobPollTimers.delete(jobId);
      toast(`${job.label}已取消`);
    } else {
      jobPollTimers.delete(jobId);
      toast(`${job.label}失败：${job.detail}`);
    }
  } catch (_) {
    jobPollTimers.set(jobId, window.setTimeout(() => pollJob(jobId), 3000));
  }
};

document.querySelector('#job-list')?.addEventListener('click', async (event) => {
  const retry = event.target.closest('[data-retry-job]');
  if (retry) {
    retry.disabled = true;
    try {
      const response = await scopedFetch(`/api/jobs/${retry.dataset.retryJob}/retry`, {method: 'POST'});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '无法重新执行任务');
      const previousId = retry.dataset.retryJob;
      window.clearTimeout(jobPollTimers.get(previousId));
      jobPollTimers.delete(previousId);
      jobWarningCounts.delete(previousId);
      retry.closest('[data-job-id]')?.remove();
      renderJob(payload);
      if (activeJobStatuses.includes(payload.status)) pollJob(payload.id);
      toast('已打开恢复任务');
    } catch (error) {
      retry.disabled = false;
      toast(error.message);
    }
    return;
  }
  const button = event.target.closest('[data-cancel-job]');
  if (!button) return;
  button.disabled = true;
  button.textContent = '正在取消…';
  try {
    const response = await scopedFetch(`/api/jobs/${button.dataset.cancelJob}/cancel`, {method: 'POST'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || '无法取消任务');
    renderJob(payload);
    toast(payload.status === 'cancelled' ? '任务已取消' : '已发送取消请求');
    if (payload.status === 'cancelling') pollJob(payload.id);
  } catch (error) {
    button.disabled = false;
    button.textContent = '取消任务';
    toast(error.message);
  }
});

document.querySelector('#jobs-max-parallel')?.addEventListener('change', async (event) => {
  const select = event.currentTarget;
  const status = document.querySelector('#jobs-parallelism-status');
  const previous = select.dataset.savedValue;
  const body = new FormData();
  body.set('max_parallel', select.value);
  select.disabled = true;
  status.textContent = '正在保存…';
  try {
    const response = await scopedFetch('/api/jobs/parallelism', {method: 'POST', body});
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '无法保存并行任务数');
    select.value = String(payload.max_parallel);
    select.dataset.savedValue = select.value;
    status.textContent = '已保存，立即生效';
    toast(`最多并行 ${payload.max_parallel} 个任务；正在运行的任务会继续完成`);
  } catch (error) {
    select.value = previous;
    status.textContent = '保存失败，请重试';
    toast(error.message);
  } finally {
    select.disabled = false;
  }
});

document.querySelector('#clear-job-history')?.addEventListener('click', async (event) => {
  if (!window.confirm('清除所有已结束的后台任务记录？生成的报告和推荐结果会保留，运行中的任务不会受影响。')) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await scopedFetch('/api/jobs/completed', {method: 'DELETE'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || '无法清除任务记录');
    if (payload.deleted) {
      window.location.assign('/generate');
    } else {
      toast('没有可清除的已结束任务');
      button.disabled = false;
    }
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
});

document.querySelector('.recommendations-clear-form')?.addEventListener('submit', (event) => {
  const day = event.currentTarget.dataset.date;
  if (!window.confirm(`清除当前研究方向 ${day} 的推荐记录？此操作不可撤销。当日论文的历史去重记录会一并撤销，允许重新推荐；其他日期仍有推荐记录的论文继续去重。收藏、阅读报告与阅读进度会保留。已导出的 Obsidian/Zotero 内容不会删除。`)) {
    event.preventDefault();
  }
});

document.querySelector('.report-table')?.addEventListener('submit', (event) => {
  if (!event.target.matches('.report-delete-form')) return;
  if (!window.confirm('删除这篇论文的所有本地报告及对应 PDF、方法图？文献库收藏会保留。')) {
    event.preventDefault();
  }
});

const reportBulkForm = document.querySelector('#report-bulk-delete-form');
if (reportBulkForm) {
  const choices = [...document.querySelectorAll('.report-selection')];
  const selectAll = document.querySelector('#report-select-all');
  const clear = document.querySelector('#report-selection-clear');
  const submit = reportBulkForm.querySelector('button[type="submit"]');
  const count = document.querySelector('#report-selection-count');
  let deleting = false;
  const updateSelection = () => {
    const selected = choices.filter((choice) => choice.checked).length;
    count.textContent = `已选择 ${selected} 篇`;
    selectAll.checked = selected === choices.length;
    selectAll.indeterminate = selected > 0 && selected < choices.length;
    clear.disabled = deleting || selected === 0;
    submit.disabled = deleting || selected === 0;
    choices.forEach((choice) => choice.closest('.report-row').classList.toggle('is-selected', choice.checked));
  };
  choices.forEach((choice) => choice.addEventListener('change', updateSelection));
  selectAll.addEventListener('change', () => {
    choices.forEach((choice) => { choice.checked = selectAll.checked; });
    updateSelection();
  });
  clear.addEventListener('click', () => {
    choices.forEach((choice) => { choice.checked = false; });
    updateSelection();
  });
  reportBulkForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const selected = choices.filter((choice) => choice.checked).length;
    if (deleting || !selected) return;
    if (!window.confirm(`删除所选 ${selected} 篇论文的所有本地报告及对应 PDF、方法图？此操作不可撤销，文献库收藏会保留。`)) return;
    const body = new FormData(reportBulkForm);
    deleting = true;
    choices.forEach((choice) => { choice.disabled = true; });
    selectAll.disabled = true;
    submit.textContent = '正在删除…';
    updateSelection();
    try {
      const response = await scopedFetch(reportBulkForm.action, {method: 'POST', body});
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(typeof payload.detail === 'string' ? payload.detail : '无法批量删除报告');
      }
      window.location.assign(response.url);
    } catch (error) {
      toast(error.message);
    } finally {
      deleting = false;
      choices.forEach((choice) => { choice.disabled = false; });
      selectAll.disabled = false;
      submit.innerHTML = '<i class="far fa-trash-alt" aria-hidden="true"></i>删除所选报告';
      updateSelection();
    }
  });
  updateSelection();
}

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
      const response = await scopedFetch(form.action, {method: 'POST', body: new FormData(form)});
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
      if (form.id === 'comparison-form') validateComparison();
    }
  });
});

const profileForm = document.querySelector('.profile-form');
if (profileForm) {
  const button = profileForm.querySelector('button[type="submit"]');
  const status = profileForm.querySelector('.profile-submit-status');
  const icon = status.querySelector('i');
  const message = status.querySelector('.profile-submit-message');
  const detail = status.querySelector('.profile-submit-detail');
  const elapsed = status.querySelector('.profile-submit-elapsed');
  const originalButton = button.innerHTML;

  profileForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (button.disabled) return;

    const body = new URLSearchParams(new FormData(profileForm));
    const fields = [...profileForm.elements].filter((field) => field !== button);
    const hasReferences = profileForm.elements.reference_ids.value.trim() !== '';
    const started = Date.now();
    const updateElapsed = () => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      elapsed.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
      if (seconds >= 30) detail.textContent = '仍在等待服务器响应，表单内容已保留';
    };

    status.hidden = false;
    status.classList.remove('is-error');
    icon.className = 'fas fa-circle-notch fa-spin';
    message.textContent = '正在生成草稿';
    detail.textContent = hasReferences ? '正在处理参考论文并生成检索方案' : '正在整理主题线索并生成检索方案';
    updateElapsed();
    status.scrollIntoView({block: 'nearest'});
    const timer = window.setInterval(updateElapsed, 1000);
    button.disabled = true;
    button.innerHTML = '<i class="fas fa-circle-notch fa-spin" aria-hidden="true"></i>生成中';
    fields.forEach((field) => { field.disabled = true; });

    try {
      const response = await scopedFetch(profileForm.action, {method: 'POST', body});
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(typeof payload.detail === 'string' ? payload.detail : '服务器处理失败，请稍后重试');
      }
      const destination = new URL(response.url);
      if (!response.redirected || destination.origin !== window.location.origin || !destination.pathname.startsWith('/profiles/drafts/')) {
        throw new Error('草稿已提交，但未收到草稿页面，请查看待检查的草稿');
      }
      icon.className = 'fas fa-check';
      message.textContent = '草稿已生成';
      detail.textContent = '正在打开草稿';
      window.location.assign(destination.href);
    } catch (error) {
      status.classList.add('is-error');
      icon.className = 'fas fa-exclamation-circle';
      message.textContent = '生成失败';
      detail.textContent = error instanceof TypeError ? '连接中断，请检查待检查的草稿后重试' : error.message;
    } finally {
      window.clearInterval(timer);
      fields.forEach((field) => { field.disabled = false; });
      button.disabled = false;
      button.innerHTML = originalButton;
    }
  });
}

const batchForm = document.querySelector('#version-batch-form');
const batchDialog = document.querySelector('#batch-preview-dialog');
let pendingVersionBatch = null;
if (batchForm && batchDialog) {
  const selectors = Array.from(batchForm.querySelectorAll('input[name="arxiv_ids"]:not(:disabled)'));
  const limit = Number(batchForm.dataset.limit);
  const selectAll = document.querySelector('#batch-select-all');
  const previewButton = batchForm.querySelector('button[type="submit"]');
  const startButton = document.querySelector('#batch-preview-start');
  const previewLabel = previewButton.textContent;
  const updateSelection = () => {
    const count = selectors.filter(input => input.checked).length;
    document.querySelector('#batch-selection-summary').textContent =
      `已选 ${count} 篇 · 预览逐篇确认更新目标${count > limit ? `（每批最多 ${limit} 篇）` : ''}`;
    previewButton.disabled = count === 0 || count > limit;
    selectAll.checked = count > 0 && selectors.slice(0, limit).every(input => input.checked);
    selectAll.indeterminate = count > 0 && !selectAll.checked;
  };
  selectAll.addEventListener('change', () => {
    selectors.forEach((input, index) => { input.checked = selectAll.checked && index < limit; });
    updateSelection();
  });
  batchForm.addEventListener('change', updateSelection);
  batchForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    previewButton.disabled = true;
    previewButton.textContent = '正在准备预览…';
    try {
      const response = await scopedFetch(batchForm.action, {method: 'POST', body: new FormData(batchForm)});
      const plan = await response.json();
      if (!response.ok) throw new Error(plan.detail || '无法创建同步预览');
      pendingVersionBatch = plan.id;
      document.querySelector('#batch-preview-summary').textContent =
        `${plan.profile_name} · ${plan.summary.total} 篇 · 下载/校验 ${plan.summary.downloads} 份 PDF（本地缺新版 ${plan.summary.missing_pdf} 篇） · 至多生成 ${plan.summary.reports} 份报告`;
      const scope = [];
      if (plan.options.report) scope.push(`报告模型：${plan.model}`);
      if (plan.options.zotero) scope.push(`Zotero 分类：${plan.zotero_collection}`);
      if (plan.options.obsidian) scope.push(`Obsidian：${plan.obsidian_vault}；至多 ${plan.summary.note_summaries} 篇笔记摘要可能使用模型 ${plan.model}`);
      document.querySelector('#batch-preview-scope').textContent = scope.join('；') || '仅同步元数据、PDF 和已有收藏，不生成报告或导出到外部工具。';
      const list = document.querySelector('#batch-preview-items');
      list.replaceChildren();
      plan.items.forEach(item => {
        const row = document.createElement('li');
        const targets = [item.options?.report ? '生成新版报告' : '', item.options?.zotero ? '更新 Zotero 原文附件' : ''].filter(Boolean);
        row.textContent = `${item.title} · arXiv:${item.arxiv_id} · 目标 v${item.target_version} · ${targets.join('、') || '元数据与 PDF'}`;
        list.append(row);
      });
      document.querySelector('#batch-preview-error').textContent = '';
      batchDialog.showModal();
    } catch (error) {
      toast(error.message);
    } finally {
      previewButton.textContent = previewLabel;
      updateSelection();
    }
  });
  document.querySelector('#batch-preview-cancel').addEventListener('click', () => batchDialog.close());
  startButton.addEventListener('click', async () => {
    if (!pendingVersionBatch) return;
    startButton.disabled = true;
    try {
      const response = await scopedFetch(`/api/jobs/version-batch/${pendingVersionBatch}`, {method: 'POST'});
      const job = await response.json();
      if (!response.ok) throw new Error(job.detail || '无法启动批次');
      renderJob(job);
      pollJob(job.id);
      batchDialog.close();
      pendingVersionBatch = null;
      toast('批量同步已加入队列；可在后台任务中取消，完成后刷新查看逐篇结果');
    } catch (error) {
      document.querySelector('#batch-preview-error').textContent = error.message;
    } finally {
      startButton.disabled = false;
    }
  });
  updateSelection();
}

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
    const response = await scopedFetch(form.action, {method: 'POST', body: formData});
    const payload = await response.json();
    if (payload.receipt_url) {
      let receiptLink = form.querySelector('.collection-receipt');
      if (!receiptLink) { receiptLink = document.createElement('a'); receiptLink.className = 'collection-receipt'; form.append(receiptLink); }
      receiptLink.href = payload.receipt_url;
      receiptLink.textContent = '查看收录回执';
    }
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
      const response = await scopedFetch('/api/zotero/collections');
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

document.querySelectorAll('[data-job-id]').forEach((node) => {
  if (activeJobStatuses.includes(node.dataset.jobStatus)) pollJob(node.dataset.jobId);
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
      const response = await scopedFetch(form.action, {method: 'POST', body: new FormData(form)});
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
      button.classList.toggle('active', button.dataset.feedback === verdict && button.dataset.scope === feedback?.scope);
    });
    const undo = document.querySelector('#inspector-undo-feedback');
    if (undo) undo.hidden = !feedback;
  };

  const showRanking = (paper) => {
    const container = document.querySelector('#inspector-ranking');
    if (!container) return;
    container.replaceChildren();
    const info = paper.ranking_explanation;
    const line = (value) => { const p = document.createElement('p'); p.textContent = value; container.append(p); };
    if (!info?.components) { line('历史推荐未记录分数构成；下一次推荐会保存当时的匹配依据。'); return; }
    line(`命中关键词：${(info.positive_keywords || []).join('、') || '无'}；负向关键词：${(info.negative_keywords || []).join('、') || '无'}`);
    line(`类别：${(info.categories || []).join('、') || '无'}；概念组：${(info.concept_groups || []).map((g, i) => `${i + 1}：${g.join('、') || '未命中'}`).join(' / ') || '未设置'}`);
    line(Object.entries(info.components).map(([key, value]) => `${key} ${Number(value).toFixed(2)}`).join(' · '));
    line(`模型分：${info.llm_score ?? '未评分'}；反馈分：${info.feedback_score ?? 0}；综合分：${Number(info.final_score).toFixed(2)}`);
    line(`计算方式：${info.formula}。分数用于排序，不是相关性概率。`);
    if (info.feedback_matches?.length) line(`偏好命中：${info.feedback_matches.map(m => `${m.term} (${m.weight > 0 ? '+' : ''}${m.weight})`).join('、')}；累加后 × 0.28，限 −3 至 +3。`);
  };

  const showRecentInterest = (paper) => {
    const container = document.querySelector('#inspector-interest');
    if (!container) return;
    container.replaceChildren();
    const line = (value) => { const p = document.createElement('p'); p.textContent = value; container.append(p); };
    const interest = paper.ranking_explanation?.recent_interest;
    if (!interest) { line('历史推荐未记录收藏引导依据。'); return; }
    const statuses = {ready: '已使用近期收藏关注点', disabled: '本次未启用收藏引导', no_samples: '当前方向暂无收藏，使用基础检索', no_focus: '未归纳出方向内的具体关注点，使用基础检索', model_unavailable: '模型未配置，使用基础检索和本地偏好排序', failed: '关注点归纳失败，已回退到基础检索和本地偏好排序'};
    line(statuses[interest.status] || '使用基础检索');
    if (interest.focus) line(`关注点：${interest.focus}`);
    if (interest.query_terms?.length) line(`补充检索词：${interest.query_terms.join('、')}`);
    if (interest.cached) line('本次复用了相同方向与收藏样本的归纳结果。');
    for (const sample of interest.samples || []) line(`参考收藏：${sample.title || sample.arxiv_id} · ${sample.arxiv_id}`);
    if (paper.discovery_routes?.length) line(`发现路径：${paper.discovery_routes.map(route => route === 'recent' ? '近期收藏定向搜索' : '基础搜索').join('、')}`);
    line('以上记录属于本次推荐；之后新增或移除收藏不会改写历史依据。');
  };

  const setLibraryUI = (saved) => {
    const button = document.querySelector('#inspector-library-button');
    const label = document.querySelector('#inspector-library-label');
    if (button) {
      button.dataset.saved = String(Boolean(saved));
      button.classList.toggle('active', Boolean(saved));
    }
    if (label) label.textContent = saved ? '已加入文献库' : '加入文献库';
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
    showRanking(paper);
    showRecentInterest(paper);
    document.querySelector('#feedback-terms').value = (item.feedback?.scope === 'topic' ? item.feedback.terms : []).join(', ');
    const verified = item.verified || {};
    setText('#inspector-position', `${index + 1} / ${papers.length}`);
    setText('#inspector-title', paper.title);
    setText('#inspector-reason', paper.recommendation_detail || paper.recommendation_reason || '依据研究主题、关键词与发布时间完成自动筛选。');
    setText('#inspector-source', paper.source_label || '历史记录，来源待核实');
    setText('#inspector-abstract', paper.abstract_zh || paper.abstract);
    const summary = document.querySelector(`#paper-summary-${index}`);
    if (summary) {
      for (const kind of ['reason', 'abstract']) {
        const target = document.querySelector(`#inspector-${kind}`);
        const source = summary.content.querySelector(`[data-summary="${kind}"]`);
        if (target && source) {
          target.replaceChildren(...source.cloneNode(true).childNodes);
          renderSummaryMath(target);
        }
      }
    }
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
    const reportForm = document.querySelector('#inspector-report-form');
    const reportOpen = document.querySelector('#inspector-report-open');
    const selectedId = paper.arxiv_id + (paper.version ? `v${paper.version}` : '');
    const readingLink = document.querySelector('#inspector-reading');
    if (readingLink) {
      if (item.has_report) {
        const query = new URLSearchParams({arxiv_id:selectedId, origin:'recommendation'});
        const sourceDate = document.querySelector('#inspector-report-form [name=source_date]')?.value;
        if(sourceDate)query.set('source_date',sourceDate);
        readingLink.href = '/reading?' + query.toString();
        readingLink.removeAttribute('aria-disabled');readingLink.removeAttribute('title');
      } else {
        readingLink.removeAttribute('href');readingLink.setAttribute('aria-disabled','true');
        readingLink.title='生成报告后可阅读对话';
      }
    }
    if (arxiv) arxiv.href = paper.abs_url;
    if (pdf) pdf.href = paper.pdf_url;
    if (reportId) reportId.value = selectedId;
    if (reportForm) reportForm.hidden = Boolean(item.has_report);
    if (reportOpen) {
      reportOpen.hidden = !item.report_url;
      reportOpen.href = item.report_url || '#';
    }
    const collectionLink = document.querySelector('#inspector-collection');
    if (collectionLink) {
      const query = new URLSearchParams(new URL(collectionLink.href).search);
      query.set('arxiv_id', selectedId);
      collectionLink.href = '/collection?' + query.toString();
      const links = document.querySelector('#inspector-collection-links');
      links?.replaceChildren();
      scopedFetch('/api/collection?' + new URLSearchParams({arxiv_id: selectedId})).then((response) => response.json()).then((state) => {
        if (!links || new URL(collectionLink.href).searchParams.get('arxiv_id') !== selectedId) return;
        for (const [target, url] of Object.entries(state.links || {})) {
          if (target === 'obsidian' && !item.has_report) continue;
          const link = document.createElement('a');
          link.className = 'button secondary';
          link.href = url;
          const icon = document.createElement('i');
          icon.className = target === 'obsidian' ? 'fas fa-gem' : 'fas fa-book';
          icon.setAttribute('aria-hidden', 'true');
          link.append(icon, document.createTextNode(`打开 ${target === 'obsidian' ? 'Obsidian' : 'Zotero'}`));
          links.append(link);
        }
      }).catch(() => {});
    }
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
      formData.set('arxiv_id', item.paper.arxiv_id + (item.paper.version ? `v${item.paper.version}` : ''));
      formData.set('origin', 'recommendation');
      formData.set('source_date', document.querySelector('#inspector-report-form [name="source_date"]').value);
      const response = await scopedFetch('/api/library/toggle', {method: 'POST', body: formData});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '无法更新文献库');
      item.in_library = payload.saved;
      item.feedback = payload.feedback || null;
      if (papers[selectedPaperIndex] === item) {
        setLibraryUI(payload.saved);
        setFeedbackUI(item.feedback);
      }
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
        formData.set('arxiv_id', item.paper.arxiv_id + (item.paper.version ? `v${item.paper.version}` : ''));
      formData.set('origin', 'recommendation');
      formData.set('source_date', document.querySelector('#inspector-report-form [name="source_date"]').value);
        formData.set('verdict', button.dataset.feedback);
        formData.set('scope', button.dataset.scope);
        formData.set('terms', document.querySelector('#feedback-terms').value);
        const response = await scopedFetch('/api/feedback', {method: 'POST', body: formData});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || '反馈保存失败');
        item.feedback = payload.feedback;
        item.in_library = Boolean(payload.in_library);
        if (papers[selectedPaperIndex] === item) {
          setFeedbackUI(payload.feedback);
          setLibraryUI(item.in_library);
        }
        toast(payload.message);
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.innerHTML = original;
      }
    });
  });

  document.querySelector('#inspector-undo-feedback')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const item = papers[selectedPaperIndex];
    if (!item?.feedback || button.disabled) return;
    button.disabled = true;
    try {
      const data = new FormData();
      data.set('arxiv_id', item.paper.arxiv_id);
      data.set('expected_updated_at', item.feedback.updated_at || '');
      const response = await scopedFetch('/api/feedback/undo', {method: 'POST', body: data});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '撤销失败');
      item.feedback = null;
      item.in_library = payload.in_library;
      if (papers[selectedPaperIndex] === item) { setFeedbackUI(null); setLibraryUI(item.in_library); }
      toast(payload.message);
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  });
  selectPaper(0);

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
      document.querySelectorAll('[data-paper-filter]').forEach((item) => {
        item.classList.toggle('active', item === button);
        item.setAttribute('aria-pressed', String(item === button));
      });
      applyFilters();
    });
  });
  document.querySelector('#paper-search')?.addEventListener('input', applyFilters);
  document.querySelector('#paper-search-button')?.addEventListener('click', applyFilters);
  document.querySelector('#category-filter')?.addEventListener('change', applyFilters);
  document.querySelector('#clear-paper-filters')?.addEventListener('click', () => {
    activeFilter = 'all';
    document.querySelectorAll('[data-paper-filter]').forEach((item) => {
      const selected = item.dataset.paperFilter === 'all';
      item.classList.toggle('active', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
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

document.querySelectorAll('.feedback-undo-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (form.id === 'comparison-form' && !validateComparison()) return;
    const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await scopedFetch(form.action, {method: 'POST', body: new FormData(form)});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '撤销失败');
      const row = form.closest('.feedback-rule');
      row.replaceChildren(document.createTextNode(payload.message));
      toast(payload.message);
    } catch (error) { toast(error.message); button.disabled = false; }
  });
});

const comparisonForm = document.querySelector('#comparison-form');
const validateComparison = () => {
  if (!comparisonForm) return true;
  const selected = [...comparisonForm.querySelectorAll('input[name="papers"]:checked')];
  const distinct = new Set(selected.map(node => node.dataset.paperId)).size === selected.length;
  const valid = selected.length >= 2 && selected.length <= 5 && distinct;
  document.querySelector('#comparison-count').textContent = `已选 ${selected.length} 篇` + (!distinct ? '，同一论文只能选一个版本' : !valid ? '，请选择 2–5 篇' : '，版本将在提交时固定');
  comparisonForm.querySelector('button[type="submit"]').disabled = !valid;
  return valid;
};
if (comparisonForm) {
  comparisonForm.addEventListener('change', validateComparison);
  document.querySelector('#comparison-search').addEventListener('input', (event) => {
    const query = event.target.value.trim().toLowerCase();
    comparisonForm.querySelectorAll('.comparison-choice').forEach(node => { node.hidden = !node.dataset.search.includes(query); });
  });
  validateComparison();
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

// Explicit saves keep notes intact when a request fails or another editor wins.
document.querySelectorAll('.reading-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector('.reading-save-status');
    button.disabled = true;
    status.textContent = '正在保存…';
    try {
      const response = await scopedFetch(form.action, {method: 'POST', body: new FormData(form)});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '保存失败，输入内容已保留');
      form.elements.expected_updated_at.value = payload.reading.updated_at;
      form.elements.read_version.value = payload.reading.read_version || '';
      form.closest('.reading-editor').querySelector('.reading-summary').textContent =
        payload.label + (payload.reading.read_version ? ` v${payload.reading.read_version}` : '');
      status.textContent = payload.message;
    } catch (error) {
      status.textContent = error.message;
    } finally { button.disabled = false; }
  });
});

const draftEditor = document.querySelector('#draft-editor');
if (draftEditor) {
  const savedFields = new URLSearchParams(new FormData(draftEditor)).toString();
  const isDraftDirty = () => new URLSearchParams(new FormData(draftEditor)).toString() !== savedFields;
  const dirtyStatus = document.querySelector('#draft-dirty-status');
  const savedLabel = dirtyStatus.textContent;
  const taskStatus = document.querySelector('#draft-task-status');
  const previewForms = [...document.querySelectorAll('.draft-preview-form')];
  let previewBusy = false;
  const formatDraftTimes = () => document.querySelectorAll('.draft-result-time').forEach((node) => {
    const date = new Date(node.dateTime);
    if (!Number.isNaN(date.getTime())) node.textContent = date.toLocaleString('zh-CN', {hour12: false});
  });
  formatDraftTimes();
  draftEditor.addEventListener('input', () => {
    dirtyStatus.textContent = isDraftDirty() ? '有未保存的修改，请先保存再试搜或启用' : savedLabel;
  });
  document.querySelectorAll('[data-draft-action]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (isDraftDirty()) {
        event.preventDefault();
        event.stopImmediatePropagation();
        dirtyStatus.textContent = '请先保存修改；此操作会使用已保存的草稿';
        dirtyStatus.scrollIntoView({block: 'center', behavior: 'smooth'});
        toast('请先保存草稿修改');
      }
    });
  });
  const setDraftBusy = (busy) => {
    previewBusy = busy;
    previewForms.forEach((form) => {
      const button = form.querySelector('button');
      if (busy) button.dataset.wasDisabled = String(button.disabled);
      button.disabled = busy || button.dataset.wasDisabled === 'true';
    });
  };
  const watchDraftJob = async (jobId) => {
    try {
      const response = await scopedFetch(`/api/jobs/${jobId}`);
      const job = await response.json();
      if (!response.ok) throw new Error(job.detail || '无法读取试搜进度');
      if (activeJobStatuses.includes(job.status)) {
        taskStatus.textContent = job.status === 'queued' ? '已排队，正在等待执行…' : '正在检索和筛选论文，完成后自动显示结果…';
        window.setTimeout(() => watchDraftJob(jobId), 1500);
        return;
      }
      if (!['succeeded', 'succeeded_with_warnings'].includes(job.status)) throw new Error(job.detail || '任务未完成，请重试');
      const page = await scopedFetch(window.location.pathname, {cache: 'no-store'});
      if (!page.ok) throw new Error('任务已完成，请刷新查看结果');
      const html = new DOMParser().parseFromString(await page.text(), 'text/html');
      const results = html.querySelector('#draft-results');
      if (!results) throw new Error('任务已完成，请刷新查看结果');
      document.querySelector('#draft-results').replaceWith(results);
      formatDraftTimes();
      taskStatus.textContent = '本次检查已结束，结果已更新。';
      if (!isDraftDirty()) results.scrollIntoView({block: 'start', behavior: 'smooth'});
    } catch (error) {
      taskStatus.textContent = error.message;
    }
    setDraftBusy(false);
  };
  previewForms.forEach((form) => form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (previewBusy) return;
    const body = new FormData(form);
    setDraftBusy(true);
    taskStatus.textContent = '正在提交检查…';
    try {
      const response = await scopedFetch(form.action, {method: 'POST', body});
      const job = await response.json();
      if (!response.ok) throw new Error(job.detail || '提交失败');
      watchDraftJob(job.id);
    } catch (error) {
      taskStatus.textContent = error.message;
      setDraftBusy(false);
    }
  }));
  document.querySelector('[data-draft-action][action$="/regenerate"]')?.addEventListener('submit', (event) => {
    const button = event.currentTarget.querySelector('button');
    button.disabled = true;
    button.textContent = '正在读取参考论文并生成建议…';
  });
}

document.querySelector('.draft-clear-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  if (!window.confirm(`永久清空已删除草稿（当前 ${form.dataset.count} 份）？清空后无法恢复。已保存方向、未删除草稿、阅读记录和报告会保留。`)) return;
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = '正在清空…';
  try {
    const response = await scopedFetch(form.action, {method: 'POST', body: new FormData(form)});
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail || '清空失败，请重试');
    }
    window.location.hash = 'deleted-drafts';
    window.location.reload();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
    button.innerHTML = original;
  }
});

const draftDeleteDialog = document.querySelector('#draft-delete-dialog');
if (draftDeleteDialog) {
  let draftToDelete = null;
  let deleteRequestActive = false;
  const confirmDelete = draftDeleteDialog.querySelector('#draft-delete-confirm');
  document.querySelectorAll('.draft-delete-form').forEach((form) => form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (deleteRequestActive) return;
    draftToDelete = form;
    draftDeleteDialog.querySelector('#draft-delete-name').textContent = form.dataset.draftName;
    draftDeleteDialog.showModal();
    draftDeleteDialog.querySelector('#draft-delete-cancel').focus();
  }));
  draftDeleteDialog.querySelector('#draft-delete-cancel').addEventListener('click', () => draftDeleteDialog.close());
  draftDeleteDialog.addEventListener('close', () => { if (!deleteRequestActive) draftToDelete = null; });
  confirmDelete.addEventListener('click', async () => {
    if (!draftToDelete || deleteRequestActive) return;
    deleteRequestActive = true;
    confirmDelete.disabled = true;
    confirmDelete.textContent = '正在删除…';
    try {
      const response = await scopedFetch(draftToDelete.action.replace('/profiles/drafts/', '/api/profile-drafts/'), {method: 'POST', body: new FormData(draftToDelete)});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '删除失败，请重试');
      window.location.hash = 'deleted-drafts';
      window.location.reload();
    } catch (error) {
      toast(error.message);
      deleteRequestActive = false;
      confirmDelete.disabled = false;
      confirmDelete.textContent = '删除草稿';
      draftDeleteDialog.close();
    }
  });
}
if (window.location.hash === '#deleted-drafts') {
  const deletedDrafts = document.querySelector('#deleted-drafts');
  if (deletedDrafts) deletedDrafts.open = true;
}

// Durable collection actions use the same profile boundary as other mutations.
document.querySelectorAll('.collection-submit').forEach((form) => {
  const choices = [...form.querySelectorAll('input[type="checkbox"]:not(:disabled)')];
  const button = form.querySelector('button[type="submit"]');
  const update = () => {
    const selected = choices.filter((choice) => choice.checked);
    choices.forEach((choice) => choice.closest('.collection-target')?.classList.toggle('selected', choice.checked));
    const names = selected.map((choice) => choice.name === 'zotero' ? 'Zotero' : 'Obsidian');
    const hint = form.querySelector('[data-collection-selection]');
    if (hint) hint.textContent = names.length ? '已选择 ' + names.join(' 与 ') : '请选择收录目标';
    const label = form.querySelector('[data-collection-submit-label]');
    if (label) label.textContent = names.length === 1 ? '收录到 ' + names[0] : '收录所选目标';
    if (button) button.disabled = !names.length;
  };
  choices.forEach((choice) => choice.addEventListener('change', update));
  update();
});
document.querySelectorAll('.collection-action').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.submitter || form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const response = await scopedFetch(form.action, {method: 'POST', body: new FormData(form)});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '收录操作失败');
      window.location.reload();
    } catch (error) {
      toast(error.message);
      if (button) button.disabled = false;
      form.removeAttribute('aria-busy');
    }
  });
});
