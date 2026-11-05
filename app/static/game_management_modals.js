(() => {
  const modal = document.getElementById('sb-management-modal');
  if (!modal) return;
  const content = document.getElementById('sb-managed-content');
  const close = document.getElementById('sb-managed-close');
  const title = document.getElementById('sb-managed-modal-title');
  let priorFocus = null, active = null, isUpdateBusy = () => false, requestId = 0, pending = false;
  const pageOverflow = () => document.body.style.overflow = modal.hidden ? '' : 'hidden';
  const showError = (message) => { const p = document.createElement('p'); p.className='notice'; p.setAttribute('role','alert'); p.textContent=message; content.replaceChildren(p); };
  async function load(kind) {
    const request = ++requestId;
    const url = kind === 'update' ? content.dataset.updateUrl : content.dataset.buildsUrl;
    content.textContent = 'Loading…';
    try {
      const response = await fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const html = await response.text();
      if (modal.hidden || request !== requestId) return;
      // These templates are rendered by StoryBurrow itself; no remote HTML is used.
      content.innerHTML = html;
      isUpdateBusy = kind === 'update' && window.sbInitUpdateGame ? window.sbInitUpdateGame() : () => false;
    } catch (error) {
      if (request === requestId && !modal.hidden) showError(`Unable to load ${kind === 'update' ? 'update game' : 'previous builds'}: ${error.message}`);
    }
  }
  function open(kind) {
    if (pending) return;
    active = kind; priorFocus = document.activeElement;
    title.textContent = kind === 'update' ? 'Update game' : 'Previous builds';
    modal.hidden = false; pageOverflow(); close.focus(); load(kind);
  }
  function dismiss() {
    if (modal.hidden || pending) return;
    if (active === 'update' && isUpdateBusy() && !window.confirm('An update or upload may be in progress. Close this dialog?')) return;
    requestId++; modal.hidden = true; content.replaceChildren(); active = null;
    isUpdateBusy = () => false; pageOverflow(); priorFocus?.focus();
  }
  document.querySelectorAll('[data-sb-managed-open]').forEach(button => button.addEventListener('click', () => open(button.dataset.sbManagedOpen)));
  close.addEventListener('click', dismiss);
  modal.addEventListener('click', event => { if (event.target === modal) dismiss(); });
  document.addEventListener('keydown', event => {
    if (modal.hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
    if (event.key === 'Tab') {
      const focusable = [...modal.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),[tabindex="0"]')].filter(el => el.getClientRects().length && !el.closest('[hidden]'));
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length-1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  content.addEventListener('submit', async event => {
    const form = event.target.closest('form[data-sb-build-operation]');
    if (!form || active !== 'builds' || event.defaultPrevented) return;
    // Retain the original onsubmit confirm/prompt; canceled submit events do not reach this listener.
    event.preventDefault();
    if (pending) return;
    pending = true;
    const buttons = [...form.querySelectorAll('button')];
    buttons.forEach(b => b.disabled = true);
    try {
      const response = await fetch(form.action, { method: 'POST', body: new FormData(form), credentials: 'same-origin', redirect: 'follow' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (form.dataset.sbBuildOperation === 'rollback') {
        window.location.assign(content.dataset.editUrl); return;
      }
      await load('builds');
    } catch (error) {
      showError(`Previous build operation failed: ${error.message}`);
    } finally { pending = false; buttons.forEach(b => b.disabled = false); }
  });
})();
