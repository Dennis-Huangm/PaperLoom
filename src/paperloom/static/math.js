// Formula enhancement never gates navigation or the reading workspace.
(() => {
  if(window.PaperLoomMath)return;
  const selector='.math-inline,.math-block', pending=new Set();
  let scheduled=false;
  function schedule() {
    if(scheduled || !window.katex || !pending.size)return;
    scheduled=true;
    setTimeout(drain,0);
  }
  function render(root) {
    if(root.nodeType!==Node.ELEMENT_NODE && root.nodeType!==Node.DOCUMENT_NODE)return;
    const add=node=>{if(!node.querySelector('.katex,.katex-error'))pending.add(node);};
    if(root.matches?.(selector))add(root);
    root.querySelectorAll(selector).forEach(add);
    schedule();
  }
  function drain() {
    scheduled=false;
    const deadline=performance.now()+8;
    let count=0;
    for(const node of pending) {
      pending.delete(node);
      if(!node.isConnected || node.querySelector('.katex,.katex-error'))continue;
      try {
        window.katex.render(node.textContent,node,{
          displayMode:node.dataset.display==='true' || node.classList.contains('math-block'),
          throwOnError:false,strict:'ignore',trust:false,
        });
      } catch (_) {node.classList.add('math-error');}
      // Yield even when many individually inexpensive formulas are queued.
      if(++count>=12 || performance.now()>=deadline)break;
    }
    schedule();
  }
  window.PaperLoomMath={render};
  new MutationObserver(records=>{
    for(const record of records) {
      const target=record.target.nodeType===Node.ELEMENT_NODE ? record.target : record.target.parentElement;
      if(target?.closest('.katex'))continue;
      const formula=target?.closest(selector);
      if(formula && !formula.querySelector('.katex,.katex-error'))render(formula);
      for(const node of record.addedNodes)if(node.nodeType===Node.ELEMENT_NODE && !node.closest('.katex'))render(node);
    }
  }).observe(document.documentElement,{childList:true,subtree:true,characterData:true});
  document.querySelectorAll('script[src*="katex.min.js"]').forEach(script=>script.addEventListener('load',schedule));
  render(document);
})();
