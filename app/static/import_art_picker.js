(() => {
  const host = document.getElementById('sb-import-artwork');
  const modal = document.getElementById('sb-import');
  if (!host || !modal) return;
  const $ = id => document.getElementById(id);
  const stage = $('sb-import-art-grid');
  const feedback = $('sb-import-art-feedback');
  const checkbox = $('sb-import-art-explicit');
  const confirm = $('sb-import-art-confirm');
  let vn = null, images = [], index = 0, confirmed = null, sequence = 0;
  const note = message => { feedback.textContent = message; };

  async function read(response) {
    const data = await response.json().catch(() => ({error:'Unexpected response'}));
    if (!response.ok || !data.ok) throw Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  stage.classList.add('sb-art-carousel');
  stage.setAttribute('role', 'group');
  stage.setAttribute('aria-label', 'VNDB artwork preview');
  const prev = document.createElement('button');
  prev.type = 'button'; prev.className = 'sb-art-nav sb-art-prev';
  prev.setAttribute('aria-label', 'Previous artwork'); prev.textContent = '‹';
  const next = document.createElement('button');
  next.type = 'button'; next.className = 'sb-art-nav sb-art-next';
  next.setAttribute('aria-label', 'Next artwork'); next.textContent = '›';
  const image = document.createElement('img');
  image.className = 'sb-art-preview'; image.alt = '';
  image.referrerPolicy = 'no-referrer';
  const frame = document.createElement('div');
  frame.className = 'sb-art-preview-frame';
  frame.append(image);
  const caption = document.createElement('div');
  caption.className = 'sb-art-caption';
  caption.setAttribute('aria-live', 'polite');
  stage.replaceChildren(prev, frame, next, caption);

  function show() {
    const item = images[index];
    const available = Boolean(item);
    frame.hidden = !available;
    prev.hidden = next.hidden = images.length < 2;
    prev.disabled = next.disabled = images.length < 2;
    if (!available) {
      image.removeAttribute('src'); image.alt = ''; caption.textContent = '';
      confirm.disabled = true;
      return;
    }
    image.src = item.url || item.thumbnail;
    image.alt = item.label || `Artwork ${index + 1}`;
    caption.textContent = `${item.label || 'Artwork'} · ${index + 1} of ${images.length}`;
    confirm.disabled = false;
    confirm.textContent = confirmed === item.id ? 'Artwork confirmed' : 'Confirm artwork';
  }
  function move(direction) {
    if (images.length < 2) return;
    index = (index + direction + images.length) % images.length;
    confirmed = null;
    show();
    note('Confirm the displayed image to use it for this game.');
  }
  prev.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  stage.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault(); move(event.key === 'ArrowLeft' ? -1 : 1);
    }
  });

  function clear() {
    ++sequence; vn = null; images = []; index = 0; confirmed = null;
    confirm.disabled = true; confirm.textContent = 'Confirm artwork'; host.hidden = true;
    show(); note('');
  }
  async function load() {
    if (!vn) return;
    const turn = ++sequence, id = vn;
    images = []; index = 0; confirmed = null;
    show(); note('Loading VNDB images…');
    try {
      const data = await read(await fetch(`/api/import/artwork?vndb_id=${encodeURIComponent(id)}`, {credentials:'same-origin'}));
      if (turn !== sequence || id !== vn) return;
      checkbox.checked = Boolean(data.allow_explicit);
     images = Array.isArray(data.images)
        ? data.images.filter(item =>
            item &&
            typeof item.id === 'string' &&
            (typeof item.url === 'string' || typeof item.thumbnail === 'string')
          )
        : [];
      show();
      note(images.length ? 'Browse with the arrows, then confirm the displayed artwork. Or leave the default VNDB cover.' : 'No images available with the current content setting; the default cover will be used.');
    } catch (err) { if (turn === sequence) note(err.message); }
  }
  checkbox.addEventListener('change', async () => {
    const value = checkbox.checked; checkbox.disabled = true;
    try {
      await read(await fetch('/api/import/artwork/preferences', {
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':modal.dataset.csrf},
        body:JSON.stringify({allow_explicit:value})
      }));
      await load();
    } catch(err) { checkbox.checked = !value; note(err.message); }
    finally { checkbox.disabled = false; }
  });
  confirm.addEventListener('click', () => {
    const item = images[index]; if (!item) return;
    confirmed = item.id; show();
    note('Artwork confirmed. This image will be cached when installation completes.');
  });
  document.addEventListener('storyburrow:import-vndb', event => {
    clear();
    if (event.detail?.id) { vn = event.detail.id; host.hidden = false; load(); }
  });
  document.addEventListener('storyburrow:import-reset', clear);
  window.StoryBurrowImportArtwork = {selectedId: () => confirmed, isReady: () => Boolean(vn)};
})();
