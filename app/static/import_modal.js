(()=>{
  const root=document.getElementById('sb-import'); if(!root)return;
  const $=id=>document.getElementById(id), csrf=root.dataset.csrf;
  const CHUNK=16*1024*1024;
  let preview=null, active=null, busy=false, installing=false, selectedVN=null, cancelRequested=false;
  let urlDownloadActive = false;
  const feedback=t=>{$('sb-import-feedback').textContent=t};
  const showReview=show=>{$('sb-import-pick').hidden=show;$('sb-import-review').hidden=!show};
  async function response(r){const type=r.headers.get('content-type')||'';if(!type.includes('application/json'))throw Error(`HTTP ${r.status}: unexpected server response`);const obj=await r.json();if(!r.ok||!obj.ok)throw Error(obj.error||`HTTP ${r.status}`);return obj}
  async function api(path,body){return response(await fetch(path,{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:JSON.stringify(body)}))}
  async function discard(){if(!active)return;try{await response(await fetch(`/api/import/uploads/${active.id}`,{method:'DELETE',credentials:'same-origin',headers:{'X-CSRF-Token':csrf}}))}catch(e){console.warn(e)}active=null}
  function resetView(){
    preview=null;selectedVN=null;cancelRequested=false;
    document.dispatchEvent(new Event('storyburrow:import-reset'));
    showReview(false);feedback('');
    for(const id of ['sb-zip-input','sb-folder-input','sb-import-name','sb-import-version']){
      const el=$(id);if(el)el.value='';
    }
    for(const id of ['sb-vndb-results'])$(id)?.replaceChildren();
    for(const id of ['sb-vndb-selection','sb-import-source','sb-import-technical','sb-upload-progress-text']){
      if($(id))$(id).textContent='';
    }
    const progressEl=$('sb-upload-progress');if(progressEl){progressEl.value=0;progressEl.hidden=true;}
    if($('sb-install-game'))$('sb-install-game').disabled=true;
    $('sb-url-panel').hidden = true;
    $('sb-url-input').value = '';
    $('sb-url-warning').hidden = true;
    $('sb-url-warning').textContent = '';
    $('sb-url-start').disabled = false;
    $('sb-url-cancel').hidden = true;
    urlDownloadActive = false;
  }
  async function close() {
    if (installing) {
      feedback('Installation is running. Keep this window open.');
      return;
    }
    if (busy) {
      cancelRequested = true;
      feedback(urlDownloadActive
        ? 'Cancelling server download…'
        : 'Stopping after current upload chunk…');
      return;
    }
    await discard();
    root.hidden = true;
    resetView();
  }
  $('sb-import-close').onclick=close;
  $('sb-import-open')?.addEventListener('click',()=>{resetView();root.hidden=false;$('sb-import-close').focus()});
  root.addEventListener('click',e=>{if(e.target===root)close()});
  document.addEventListener('keydown',e=>{if(!root.hidden&&e.key==='Escape'){e.preventDefault();close()}});
  $('sb-pick-zip').onclick=()=>$('sb-zip-input').click();
  $('sb-pick-folder').onclick=()=>$('sb-folder-input').click();
  $('sb-zip-input').onchange=e=>upload([...e.target.files],'zip');
  $('sb-folder-input').onchange=e=>upload([...e.target.files],'folder');
  $('sb-import-drop').ondragover=e=>{e.preventDefault();e.currentTarget.classList.add('over')};
  $('sb-import-drop').ondragleave=e=>e.currentTarget.classList.remove('over');
  $('sb-import-drop').ondrop=e=>{e.preventDefault();e.currentTarget.classList.remove('over');const fs=[...e.dataTransfer.files];if(fs.length===1&&/\.zip$/i.test(fs[0].name))upload(fs,'zip');else feedback('For extracted folders, use Choose Folder to preserve relative paths.')};
  function progress(received,total,label){const percent=total?Math.min(100,Math.floor(received/total*100)):100;const el=$('sb-upload-progress');if(el){el.hidden=false;el.value=percent;el.max=100}$('sb-upload-progress-text').textContent=`${label}: ${percent}% · ${(received/1024**3).toFixed(2)} / ${(total/1024**3).toFixed(2)} GiB`}
  function enterReview(inspection) {
    preview = inspection;
    selectedVN = null;
    document.dispatchEvent(new Event('storyburrow:import-reset'));
    const d = preview.detected;
    $('sb-import-name').value = d.title.value || '';
    $('sb-import-version').value = d.installed_version.value || '';
    $('sb-import-source').textContent =
      `${preview.source.name} · ${preview.source.type}`;
    $('sb-import-technical').textContent =
      `Launcher: ${d.launcher} · ${preview.technical.file_count} entries`;
    $('sb-vndb-results').replaceChildren();
    $('sb-vndb-selection').textContent = '';
    showReview(true);
    if ($('sb-save-review')) {
      $('sb-save-review').disabled = false;
    }
    if ($('sb-install-game')) {
      $('sb-install-game').disabled = false;
    }
    feedback('Inspection complete. Review detected metadata.');
  }
  $('sb-pick-url').onclick = () => {
    const panel = $('sb-url-panel');
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      $('sb-url-input').focus();
    }
  };
  
  $('sb-url-cancel').onclick = () => {
    if (urlDownloadActive) {
      cancelRequested = true;
      feedback('Cancelling server download…');
    }
  };
  function megaWarning(url) {
    try {
      const host = new URL(url).hostname.toLowerCase();
      return ['mega.nz', 'www.mega.nz', 'mega.co.nz'].includes(host);
    } catch {
      return false;
    }
  }
  function updateURLWarning() {
    const url = $('sb-url-input').value.trim();
    const warning = $('sb-url-warning');
    if (megaWarning(url)) {
      warning.textContent =
        'MEGA enforces transfer quotas. Large downloads may pause or fail ' +
        'when your available quota is exhausted.';
      warning.hidden = false;
    } else {
      warning.textContent = '';
      warning.hidden = true;
    }
  }
  $('sb-url-input').addEventListener('input', updateURLWarning);
  function urlProgress(received, total) {
    const bar = $('sb-upload-progress');
    const label = $('sb-upload-progress-text');
    bar.hidden = false;
    if (total > 0) {
      bar.removeAttribute('aria-busy');
      bar.value = Math.min(100, received / total * 100);
      label.textContent =
        `Downloading: ${Math.floor(received / total * 100)}% · ` +
        `${(received / 1024**2).toFixed(1)} / ` +
        `${(total / 1024**2).toFixed(1)} MiB`;
    } else {
      bar.removeAttribute('value');
      bar.setAttribute('aria-busy', 'true');
      label.textContent =
        `Downloading: ${(received / 1024**2).toFixed(1)} MiB received`;
    }
  }
  async function importFromURL() {
    if (busy || installing) return;
    const url = $('sb-url-input').value.trim();
    if (!url) {
      feedback('Enter a download URL.');
      return;
    }
    busy = true;
    urlDownloadActive = true;
    cancelRequested = false;
    preview = null;
    selectedVN = null;
    const startButton = $('sb-url-start');
    const cancelButton = $('sb-url-cancel');
    startButton.disabled = true;
    cancelButton.hidden = false;
    try {
      await discard();
      feedback('Starting server download…');
      const created = await api('/api/import/url', {url});
      active = {
        id: created.upload_id,
        kind: 'zip',
        files: []
      };
      let cancelSent = false;
      while (true) {
        if (cancelRequested && !cancelSent) {
          await api(
            `/api/import/url/${active.id}/cancel`,
            {}
          );
          cancelSent = true;
        }
        const responseData = await response(
          await fetch(`/api/import/url/${active.id}`, {
            credentials: 'same-origin',
            cache: 'no-store'
          })
        );
        const status = responseData.status;
        urlProgress(
          responseData.received_bytes || 0,
          responseData.total_bytes || 0
        );
        if (status === 'ready') {
          if (cancelRequested) {
            throw Error('Download cancelled');
          }
          $('sb-url-panel').hidden = true;
          enterReview(responseData.inspection);
          return;
        }
        if (status === 'failed') {
          throw Error(
            responseData.error || 'Server download failed'
          );
        }
        if (status === 'cancelled') {
          throw Error('Download cancelled');
        }
        feedback(
          status === 'inspecting'
            ? 'Download complete. Inspecting Ren’Py game…'
            : cancelRequested
              ? 'Cancelling server download…'
              : 'Downloading directly to StoryBurrow…'
        );
        await new Promise(resolve => setTimeout(resolve, 800));
      }
    } catch (error) {
      feedback(error.message);
    } finally {
      busy = false;
      urlDownloadActive = false;
      startButton.disabled = false;
      cancelButton.hidden = true;
      if (cancelRequested) {
        await discard();
        resetView();
        root.hidden = true;
      }
    }
  }
  $('sb-url-start').onclick = importFromURL;
  async function upload(files,kind){
    if(busy||!files.length)return;
    busy=true;cancelRequested=false;selectedVN=null;preview=null;document.dispatchEvent(new Event('storyburrow:import-reset'));
    const manifest=files.map(f=>({path:kind==='zip'?f.name:f.webkitRelativePath,size:f.size}));
    const total=files.reduce((n,f)=>n+f.size,0);
    try{
      feedback('Creating upload session…');
      const created=await api('/api/import/uploads',{kind,files:manifest});
      active={id:created.upload_id,files,kind};
      let received=created.received_bytes;
      progress(received,total,'Uploading');
      for(let i=0;i<files.length;i++){
        const file=files[i];let offset=created.offsets[i];
        while(offset<file.size){
          if(cancelRequested)throw Error('Upload cancelled');
          const startOffset=offset;
          const blob=file.slice(offset,Math.min(offset+CHUNK,file.size));
          let completed=false;
          for(let attempt=0;attempt<3&&!completed;attempt++){
            try{
              const r=await fetch(`/api/import/uploads/${active.id}/chunk/${i}`,{method:'PUT',credentials:'same-origin',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/octet-stream','X-Chunk-Offset':String(offset)},body:blob});
              const result=await response(r);
              received=result.received_bytes;offset=result.offset;completed=true;
              progress(received,total,'Uploading');
            }catch(err){
              // A lost response may mean the server accepted the chunk. Ask it for offsets.
              const server=await response(await fetch(`/api/import/uploads/${active.id}`,{credentials:'same-origin'}));
              offset=server.offsets[i];received=server.received_bytes;
              progress(received,total,'Retrying');
              // If the server committed the chunk, continue from its reported offset.
              if(offset>startOffset){completed=true;break}
              if(attempt===2)throw err;
            }
          }
        }
      }
      if(cancelRequested)throw Error('Upload cancelled');
      feedback('Verifying and inspecting…');
      const result=await api(`/api/import/uploads/${active.id}/complete`,{});
      enterReview(result.inspection);
    }catch(e){feedback(e.message)}finally{
      busy=false;
      if(cancelRequested){await discard();resetView();root.hidden=true;}
    }
  }
  // The Install action saves the current review automatically.
  const review=$('sb-import-review');
  const installButton=document.createElement('button');
  installButton.id='sb-install-game';installButton.type='button';installButton.className='play';
  installButton.style.cssText='width:auto;margin-top:12px';
  installButton.textContent='Install game';installButton.disabled=true;
  if(review)review.append(installButton);
  installButton.onclick=async()=>{
    if(!preview||!active||busy||installing)return;
    if(!confirm('Install this game into StoryBurrow? Existing installations will not be overwritten.'))return;
    installing=true;installButton.disabled=true;
    try{
      // Always persist the currently visible title, version and VNDB selection.
      await api(`/api/import/uploads/${active.id}/review`,{
        title:$('sb-import-name').value,installed_version:$('sb-import-version').value,vndb:selectedVN,artwork_image_id:window.StoryBurrowImportArtwork?.selectedId()||null
      });
      await api(`/api/import/uploads/${active.id}/install`,{});
      feedback('Installing game. Extracting and validating files…');
      while(true){
        await new Promise(resolve=>setTimeout(resolve,1500));
        const result=await response(await fetch(`/api/import/uploads/${active.id}/installation`,{credentials:'same-origin',cache:'no-store'}));
        if(result.status==='installed'){feedback('Installed! Refreshing library…');window.location.reload();return}
        if(result.status==='failed')throw Error(result.error||'Installation failed');
      }
    }catch(err){feedback(err.message)}finally{installing=false;installButton.disabled=false}
  };
  $('sb-skip-vndb').onclick=()=>{document.dispatchEvent(new Event('storyburrow:import-reset'));selectedVN=null;$('sb-vndb-selection').textContent='Using local metadata only.';$('sb-vndb-results').replaceChildren()};
  $('sb-search-vndb').onclick=async()=>{
    feedback('Searching VNDB…');$('sb-vndb-results').replaceChildren();
    try{const {candidates}=await api('/api/import/vndb',{title:$('sb-import-name').value});feedback(candidates.length?`Found ${candidates.length} candidates.`:'No VNDB matches found.');
      for(const c of candidates){const b=document.createElement('button');b.type='button';b.className='sb-vndb-item';
        if(c.image?.thumbnail){const img=document.createElement('img');img.src=c.image.thumbnail;img.alt='';b.append(img)}
        const span=document.createElement('span');span.textContent=`${c.title||'Untitled'} (${c.id||'unknown'})${c.released?' · '+c.released:''}`;b.append(span);
        b.onclick=()=>{selectedVN=c;document.dispatchEvent(new CustomEvent('storyburrow:import-vndb',{detail:{id:c.id}}));$('sb-vndb-selection').textContent=`Selected: ${c.title} (${c.id}). Preview only.`};$('sb-vndb-results').append(b)}
    }catch(e){feedback(e.message)}
  };
})();
