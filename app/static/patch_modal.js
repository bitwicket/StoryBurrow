(() => {
  const modal = document.getElementById('sb-patch-modal');
  if (!modal) return;
  const $ = id => document.getElementById(id);
  const open = $('sb-patch-open'), close = $('sb-patch-close');
  const pick = $('sb-patch-pick'), upload = $('sb-patch-upload');
  const urlPanel = modal.querySelector('.sb-patch-url');
  const file = $('sb-patch-file'), drop = $('sb-patch-drop');
  let previousFocus = null;
  let downloading = false;
  function show(section) {
    pick.hidden = section !== 'pick';
    upload.hidden = section !== 'file';
    urlPanel.hidden = section !== 'url';
  }
  function openModal() {
    previousFocus = document.activeElement;
    show('pick');
    modal.hidden = false;
    close.focus();
  }
  function closeModal() {
    if (downloading) return;
    modal.hidden = true;
    if (previousFocus) previousFocus.focus();
  }
  function chooseFile(selected) {
    if (!selected) return;
    // Never copy the patch through the browser for URL imports; this path is local files only.
    if (selected !== file.files?.[0]) {
      const transfer = new DataTransfer();
      transfer.items.add(selected);
      file.files = transfer.files;
    }
    $('sb-patch-file-name').textContent = `Selected: ${selected.name}`;
    show('file');
  }
  open.addEventListener('click', openModal);
  close.addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', e => {
    if (modal.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); closeModal(); }
    if (e.key === 'Tab') {
      const focusable = [...modal.querySelectorAll('button:not([disabled]), input:not([disabled]):not([type=hidden])')].filter(el => el.getClientRects().length);
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length-1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  $('sb-patch-pick-file').addEventListener('click', () => file.click());
  drop.addEventListener('click', () => file.click());
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file.click(); } });
  file.addEventListener('change', () => chooseFile(file.files?.[0]));
  ['dragenter', 'dragover'].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => chooseFile(e.dataTransfer.files?.[0]));
  $('sb-patch-pick-url').addEventListener('click', () => { show('url'); $('sb-patch-url').focus(); });
  $('sb-patch-file-back').addEventListener('click', () => show('pick'));
  $('sb-patch-url-back').addEventListener('click', () => { if (!downloading) show('pick'); });
  // patch_url.js owns the existing server-side URL download/polling behavior.
  // Prevent closing or switching panels while its transfer is active.
  const start = $('sb-patch-url-start');
  start.addEventListener('click', () => { if ($('sb-patch-url').value.trim()) downloading = true; });
  const observer = new MutationObserver(() => { downloading = start.disabled; });
  observer.observe(start, {attributes: true, attributeFilter: ['disabled']});
})();
