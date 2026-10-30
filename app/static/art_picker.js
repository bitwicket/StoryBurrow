(() => {
  const picker = document.getElementById('sb-art-picker');
  if (!picker) return;
  const open = document.getElementById('sb-art-open');
  const grid = document.getElementById('sb-art-grid');
  const feedback = document.getElementById('sb-art-feedback');
  const save = document.getElementById('sb-art-save');
  const explicit = document.getElementById('sb-art-explicit');
  let selected = null;
  let pending = false;
  const write = (text) => { feedback.textContent = text; };
  const post = async (url, payload) => {
    const r = await fetch(url, { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-CSRF-Token': picker.dataset.csrf
    }, body: JSON.stringify(payload), credentials: 'same-origin' });
    const data = await r.json().catch(() => ({error: 'Unexpected server response'}));
    if (!r.ok || !data.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  };
  function closePicker() { if (!pending) { picker.hidden = true; selected = null; save.disabled = true; } }
  async function load() {
    selected = null; save.disabled = true; grid.replaceChildren(); write('Loading VNDB artwork…');
    try {
      const r = await fetch(picker.dataset.url, {credentials: 'same-origin'});
      const data = await r.json();
      if (!r.ok || !data.ok) throw new Error(data.error || 'Cannot load artwork');
      explicit.checked = data.allow_explicit;
      const images = data.images || [];
      images.forEach((item) => {
        const b = document.createElement('button'); b.type = 'button'; b.className = 'sb-art-tile';
        b.setAttribute('aria-pressed', 'false');
        const image = document.createElement('img'); image.loading = 'lazy'; image.alt = item.label;
        image.referrerPolicy = 'no-referrer'; image.src = item.thumbnail;
        const caption = document.createElement('span'); caption.textContent = item.label;
        b.append(image, caption);
        b.addEventListener('click', () => {
          grid.querySelectorAll('.sb-art-tile').forEach(tile => {tile.classList.remove('selected'); tile.setAttribute('aria-pressed','false');});
          b.classList.add('selected'); b.setAttribute('aria-pressed','true');
          selected = item.id; save.disabled = false;
          write(`${item.label} selected. Click Save selected artwork to confirm.`);
        });
        grid.appendChild(b);
      });
      write(images.length ? `${images.length} artwork choices available. Select one to preview.` : 'No artwork matches the current content setting.');
    } catch (e) { write(e.message); }
  }
  open?.addEventListener('click', () => {picker.hidden = false; picker.scrollIntoView({behavior:'smooth',block:'start'}); load();});
  document.getElementById('sb-art-close')?.addEventListener('click', closePicker);
  document.getElementById('sb-art-cancel')?.addEventListener('click', closePicker);
  explicit.addEventListener('change', async () => {
    explicit.disabled = true;
    try { await post(picker.dataset.preferenceUrl, {allow_explicit: explicit.checked}); await load(); }
    catch (e) {write(e.message); explicit.checked = !explicit.checked;}
    finally {explicit.disabled = false;}
  });
  save.addEventListener('click', async () => {
    if (!selected || pending) return;
    pending = true; save.disabled = true; write('Downloading and saving selected artwork…');
    try { await post(picker.dataset.url, {image_id: selected}); window.location.reload(); }
    catch (e) {write(e.message); save.disabled = false;}
    finally {pending = false;}
  });
})();
