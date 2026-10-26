(() => {
  const panel = document.querySelector('[data-overview-panel]');
  if (!panel) return;
  const active = new Set(['queued', 'running', 'cancelling']);
  if (!active.has(panel.dataset.overviewStatus)) return;
  const labels = {queued: '排队中', running: '生成中', cancelling: '正在取消'};
  let stopped = false;
  const poll = async () => {
    if (stopped) return;
    try {
      const response = await fetch(panel.dataset.overviewUrl, {cache: 'no-store'});
      if (!response.ok) throw new Error('无法取得综述进度');
      const data = await response.json();
      if (!active.has(data.status)) {
        stopped = true;
        // Reload only when there are no unsaved edits in the surrounding form.
        const forms = [...document.querySelectorAll('form')];
        if (!forms.some(form => form.dataset.overviewDirty === 'true')) location.reload();
        else {
          panel.querySelector('[data-overview-status-label]').textContent = data.article ? '已完成，请保存修改后刷新查看' : '任务已结束，请刷新查看';
          const link = document.createElement('a');
          link.href = `/profiles/${encodeURIComponent(panel.dataset.overviewUrl.split('/').pop().split('?')[0])}/overview${new URL(panel.dataset.overviewUrl, location.origin).search}`;
          link.textContent = '打开小综述';
          if (data.article) panel.append(link);
        }
        return;
      }
      panel.querySelector('[data-overview-status-label]').textContent = labels[data.status];
      const progress = panel.querySelector('[data-overview-progress]');
      if (progress && data.job) progress.textContent = data.job.detail;
    } catch (_) {
      panel.querySelector('[data-overview-status-label]').textContent = '连接暂不可用，正在重试';
    }
    setTimeout(poll, 2000);
  };
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('input', () => { form.dataset.overviewDirty = 'true'; });
    form.addEventListener('change', () => { form.dataset.overviewDirty = 'true'; });
  });
  window.addEventListener('pagehide', () => { stopped = true; });
  setTimeout(poll, 1000);
})();
