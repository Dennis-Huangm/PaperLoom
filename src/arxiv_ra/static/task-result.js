(() => {
  document.querySelector('.print-result')?.addEventListener('click', () => window.print());
  const buttons = [...document.querySelectorAll('[data-scope]')];
  const papers = [...document.querySelectorAll('.result-paper')];
  const search = document.querySelector('input[type=search]');
  let scope = 'all';
  function filter() {
    const query = (search?.value || '').trim().toLocaleLowerCase();
    let count = 0;
    papers.forEach(paper => {
      paper.hidden = !(scope === 'all' || paper.dataset.sources.split(' ').includes(scope)) || !paper.dataset.search.toLocaleLowerCase().includes(query);
      if (!paper.hidden) count++;
    });
    const label = document.getElementById('result-count');
    if (label) label.textContent = `${count} / ${papers.length} 篇`;
    const empty = document.getElementById('result-empty');
    if (empty) empty.hidden = count !== 0;
  }
  buttons.forEach(button => button.addEventListener('click', () => {
    scope = button.dataset.scope;
    buttons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    filter();
  }));
  search?.addEventListener('input', filter);
  if (window.katex) document.querySelectorAll('.math-inline,.math-block').forEach(node => {
    try { window.katex.render(node.textContent, node, {displayMode: node.classList.contains('math-block'), throwOnError: false, trust: false}); } catch (_) { /* Keep original evidence readable. */ }
  });
})();
