(() => {
const {scopedFetch, toast, jobs} = window.PaperLoom;

const list = document.querySelector('#job-list');
list?.addEventListener('paperloom:job-rendered', ({detail: {job, node}}) => {
  const conferenceTasks = list.closest('.conference-task-history');
  const active = ['queued', 'running', 'cancelling'].includes(job.status);
  if (conferenceTasks && active) conferenceTasks.open = true;
  const copy = node.querySelector('.job-copy');
  const title = node.querySelector('.job-title');
  const detail = node.querySelector('.job-detail');
  const warnings = node.querySelector('.job-warnings');
  const actions = node.querySelector('.job-actions');
    const status = document.createElement('span');
    status.className = `conference-status status-${job.status}`;
    status.textContent = {queued: '排队中', running: '检索中', cancelling: '取消中',
      succeeded: '已完成', succeeded_with_warnings: '部分来源未完成', failed: '失败',
      cancelled: '已取消', interrupted: '已中断'}[job.status] || job.status;
    title.append(' ', status);
  if (['failed', 'interrupted'].includes(job.status)) {
    const hint = document.createElement('p');
    hint.textContent = '检索未完成，可调整条件后重新提交，或删除这条记录。';
    const details = document.createElement('details');
    details.className = 'conference-error-details';
    const summary = document.createElement('summary');
    summary.textContent = '查看任务错误详情';
    details.append(summary, detail);
    copy.append(hint, details);
  }
  if (warnings) {
      const details = document.createElement('details');
      details.className = 'conference-error-details';
      const summary = document.createElement('summary');
      summary.textContent = `查看 ${job.warnings.length} 条来源提示`;
      details.append(summary, warnings);
      copy.append(details);
    }
  if (conferenceTasks && !active && job.profile_id === document.body.dataset.profileId) {
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'job-cancel job-delete';
    remove.dataset.deleteJob = job.id;
    remove.textContent = '删除记录';
    remove.setAttribute('aria-label', `删除任务记录：${job.label}`);
    actions.append(remove);
  }
});

const conferenceJobs = document.querySelector('#conference-jobs-data');
if (conferenceJobs) {
  jobs.loadHistory(JSON.parse(conferenceJobs.textContent || '[]'));
}

document.querySelectorAll('.conference-local-time').forEach((node) => {
  const date = new Date(node.dateTime);
  if (Number.isFinite(date.getTime())) node.textContent = date.toLocaleString('zh-CN', {hour12: false});
});

document.querySelectorAll('.conference-delete-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!window.confirm('删除这次检索的历史记录和结果？已收藏论文、阅读报告和来源缓存会保留。此操作不可撤销。')) return;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const response = await scopedFetch(form.action, {method: 'POST', body: new FormData(form)});
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(payload.detail || '无法删除检索记录');
      }
      window.location.assign(response.url);
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
  });
});

document.querySelectorAll('.conference-search-form').forEach((form) => jobs.bindForm(form));
})();
