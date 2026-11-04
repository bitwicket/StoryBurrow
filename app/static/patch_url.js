(() => {
  const root = document.querySelector('.sb-patch-url');
  if (!root) return;
  const id = root.dataset.gameId;
  const base = `/api/games/${encodeURIComponent(id)}/patches/url`;
  const csrf = root.dataset.csrf;
  const url = document.getElementById('sb-patch-url');
  const destination = document.getElementById('sb-patch-url-destination');
  const start = document.getElementById('sb-patch-url-start');
  const cancel = document.getElementById('sb-patch-url-cancel');
  const progress = document.getElementById('sb-patch-url-progress');
  const status = document.getElementById('sb-patch-url-status');
  let token = null, cancelled = false, running = false;
  async function api(path, method='GET', body=null) {
    const response = await fetch(path, {
      method, credentials:'same-origin', cache:'no-store',
      headers: body === null ? {} : {'X-CSRF-Token':csrf, 'Content-Type':'application/json'},
      ...(body === null ? {} : {body: JSON.stringify(body)})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  start.addEventListener('click', async () => {
    if (running) return;
    if (!url.value.trim()) { status.textContent='Enter a download URL.'; return; }
    running = true; cancelled = false; token = null;
    start.disabled = true; cancel.hidden = false;
    progress.hidden = false; progress.removeAttribute('value');
    status.textContent = 'Starting server download…';
    try {
      const created = await api(base, 'POST', {url: url.value.trim(), destination: destination.value.trim()});
      token = created.upload_id;
      while (true) {
        if (cancelled) {
          await api(`${base}/${token}/cancel`, 'POST', {});
          status.textContent='Cancelling server download…';
        }
        const result = await api(`${base}/${token}`);
        if (result.status === 'ready') {
          if (cancelled) { status.textContent = 'Download cancelled. The preview will expire automatically.'; return; }
          window.location.assign(`/games/${encodeURIComponent(id)}/patches/preview/${encodeURIComponent(result.token)}`);
          return;
        }
        if (result.status === 'failed' || result.status === 'cancelled') {
          throw new Error(result.error || 'Download cancelled');
        }
        const received = result.received_bytes || 0, total = result.total_bytes || 0;
        if (total) {
          progress.value = Math.min(100, received / total * 100);
          status.textContent = `${result.status === 'inspecting' ? 'Inspecting' : 'Downloading'}: ${Math.round(received / total * 100)}% (${(received / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} MiB)`;
        } else {
          progress.removeAttribute('value');
          status.textContent = `Downloading directly to server: ${(received / 1048576).toFixed(1)} MiB received`;
        }
        await new Promise(resolve => setTimeout(resolve, 800));
      }
    } catch (error) {
      status.textContent = error.message;
    } finally {
      running = false;
      start.disabled = false; cancel.hidden = true;
    }
  });
  cancel.addEventListener('click', () => { cancelled = true; cancel.disabled = true; });
})();
