if('scrollRestoration' in history){history.scrollRestoration='manual';}if(!location.hash){requestAnimationFrame(function(){window.scrollTo(0,0);});}
document.querySelectorAll('.math-block,.math-inline').forEach(function(node){try{katex.render(node.textContent,node,{displayMode:node.dataset.display==='true',throwOnError:false,strict:'ignore',trust:false});}catch(error){node.classList.add('math-error');}});
var toggle=document.querySelector('.toc-toggle'),sidebar=document.querySelector('.report-sidebar');if(toggle&&sidebar){toggle.addEventListener('click',function(){var open=sidebar.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open));});sidebar.querySelectorAll('a').forEach(function(link){link.addEventListener('click',function(){sidebar.classList.remove('open');toggle.setAttribute('aria-expanded','false');});});}

document.querySelector('#report-print')?.addEventListener('click', () => window.print());
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
