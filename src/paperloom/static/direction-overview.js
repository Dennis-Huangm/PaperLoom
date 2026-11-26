(() => {
  const panel = document.querySelector('[data-overview-panel]');
  if (!panel) return;
  const active = new Set(['queued', 'running', 'cancelling']);
  if (!active.has(panel.dataset.overviewStatus)) return;
  const labels = {
    queued: '排队中', running: '生成中', cancelling: '正在取消', ready: '可阅读',
    failed: '生成失败', cancelled: '已取消', interrupted: '已中断', none: '尚未生成',
  };
  const statusLabel = panel.querySelector('[data-overview-status-label]');
  const progress = panel.querySelector('[data-overview-progress]');
  const forms = [...document.querySelectorAll('form')];
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
        if (!forms.some(form => form.dataset.overviewDirty === 'true')) location.reload();
        else {
          panel.dataset.overviewStatus = data.status;
          statusLabel.textContent = labels[data.status] || '任务已结束';
          const background = panel.querySelector('[data-overview-background]');
          if (background) background.hidden = true;
          if (progress) {
            progress.textContent = data.error || (data.article ? '可在新标签页阅读，当前修改仍可保存。' : '保存修改后可刷新页面。');
          }
          if (data.article) {
            let link = panel.querySelector('[data-overview-read]');
            if (!link) {
              link = document.createElement('a');
              link.href = panel.querySelector('form').action;
              link.className = 'button primary';
              link.dataset.overviewRead = '';
              link.textContent = '阅读方向小综述';
              panel.querySelector('.draft-actions').prepend(link);
            }
            link.target = '_blank';
            link.rel = 'noopener';
          }
        }
        return;
      }
      panel.dataset.overviewStatus = data.status;
      if (statusLabel.textContent !== labels[data.status]) statusLabel.textContent = labels[data.status];
      if (progress && data.job && progress.textContent !== data.job.detail) progress.textContent = data.job.detail;
    } catch (_) {
      const message = '连接暂不可用，正在重试';
      if (statusLabel.textContent !== message) statusLabel.textContent = message;
    }
    setTimeout(poll, 2000);
  };
  forms.forEach(form => {
    form.addEventListener('input', () => { form.dataset.overviewDirty = 'true'; });
    form.addEventListener('change', () => { form.dataset.overviewDirty = 'true'; });
  });
  window.addEventListener('pagehide', () => { stopped = true; });
  setTimeout(poll, 1000);
})();
