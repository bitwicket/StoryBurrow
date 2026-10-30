(()=>{
const el=document.getElementById('sb-update');if(!el)return;
const game=encodeURIComponent(el.dataset.game),csrf=el.dataset.csrf;
const $=id=>document.getElementById(id),msg=t=>$('sb-update-message').textContent=t;
const chunk=16*1024*1024;let token=null,busy=false,finished=false;
try{$('sb-retain-build').checked=localStorage.getItem('storyburrow-retain-build')!=='false'}catch(_){}
$('sb-retain-build').onchange=()=>{try{localStorage.setItem('storyburrow-retain-build',String($('sb-retain-build').checked))}catch(_){}};
async function response(r){const o=await r.json();if(!r.ok||!o.ok)throw Error(o.error||`HTTP ${r.status}`);return o}
async function post(path,body){return response(await fetch(path,{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:JSON.stringify(body)}))}
function progress(received,total,label){$('sb-update-progress-wrap').hidden=false;const pct=total?Math.floor(received/total*100):100;$('sb-update-progress').value=pct;$('sb-update-progress-text').textContent=`${label}: ${pct}% · ${(received/1024**3).toFixed(2)} / ${(total/1024**3).toFixed(2)} GiB`}
function pickerDisabled(disabled){for(const id of ['sb-update-pick-zip','sb-update-pick-folder','sb-update-zip','sb-update-folder'])$(id).disabled=disabled}
async function upload(files,kind){if(busy||finished||!files.length)return;busy=true;pickerDisabled(true);$('sb-update-review').hidden=true;try{
const manifest=files.map(f=>({path:kind==='zip'?f.name:f.webkitRelativePath,size:f.size}));
msg('Preparing upload…');const s=await post('/api/import/uploads',{kind,files:manifest});token=s.upload_id;const total=files.reduce((n,f)=>n+f.size,0);let received=0;
for(let i=0;i<files.length;i++){let offset=0;while(offset<files[i].size){const blob=files[i].slice(offset,offset+chunk);let ok=false;for(let attempt=0;attempt<3&&!ok;attempt++){
try{const r=await response(await fetch(`/api/import/uploads/${token}/chunk/${i}`,{method:'PUT',credentials:'same-origin',headers:{'X-CSRF-Token':csrf,'X-Chunk-Offset':String(offset),'Content-Type':'application/octet-stream'},body:blob}));offset=r.offset;received=r.received_bytes;ok=true}
catch(err){const s=await response(await fetch(`/api/import/uploads/${token}`,{credentials:'same-origin'}));if(s.offsets[i]>offset){offset=s.offsets[i];received=s.received_bytes;ok=true}else if(attempt===2)throw err;}}
progress(received,total,'Uploading')}}
msg('Inspecting replacement…');const result=await post(`/api/import/uploads/${token}/complete`,{});const d=result.inspection.detected;
$('sb-update-version').value=d.installed_version?.value||'';
$('sb-update-inspection').textContent=`Source: ${result.inspection.source.name} · Detected title: ${d.title?.value||'Unknown'} · Launcher: ${d.launcher||'Unknown'}`;
$('sb-update-pick').hidden=true;$('sb-update-review').hidden=false;$('sb-update-progress-wrap').hidden=true;msg('Review the build and confirm the replacement.');
}catch(e){msg(`Upload failed: ${e.message}`);pickerDisabled(false)}finally{busy=false}}
$('sb-update-pick-zip').onclick=()=>$('sb-update-zip').click();$('sb-update-pick-folder').onclick=()=>$('sb-update-folder').click();
$('sb-update-zip').onchange=e=>upload([...e.target.files],'zip');$('sb-update-folder').onchange=e=>upload([...e.target.files],'folder');
$('sb-update-drop').onclick=()=>$('sb-update-zip').click();$('sb-update-drop').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('sb-update-zip').click()}};
$('sb-update-drop').ondragover=e=>{e.preventDefault();e.currentTarget.classList.add('over')};$('sb-update-drop').ondragleave=e=>e.currentTarget.classList.remove('over');
$('sb-update-drop').ondrop=e=>{e.preventDefault();e.currentTarget.classList.remove('over');const files=[...e.dataTransfer.files];if(files.length===1&&/\.zip$/i.test(files[0].name))upload(files,'zip');else msg('Use Choose Folder for extracted games to preserve relative paths.')};
$('sb-update-confirm').onclick=async()=>{if(busy||finished||!token)return;const version=$('sb-update-version').value.trim();if(!version){msg('Enter the incoming version.');return}
if(!confirm(`Update ${el.dataset.game} to ${version}? A verified save snapshot will be created first.`))return;
busy=true;$('sb-update-confirm').disabled=true;try{await post(`/api/games/${game}/update/${token}`,{version,retain_build:$('sb-retain-build').checked});msg('Creating save snapshot and updating game…');
while(true){await new Promise(resolve=>setTimeout(resolve,1500));const o=await response(await fetch(`/api/games/${game}/update/${token}`,{credentials:'same-origin'}));
if(o.status==='updated'){$('sb-update-review').hidden=true;$('sb-update-success').hidden=false;$('sb-update-summary').textContent=`${el.dataset.game} was updated to ${o.result.installed_version}.`;msg('Update completed successfully.');finished=true;setTimeout(()=>{window.location.assign(el.dataset.return)},3500);break}
if(o.status==='failed')throw Error(o.error||'Update failed')}}
catch(e){msg(`Update failed: ${e.message}`)}finally{busy=false;if(!finished)$('sb-update-confirm').disabled=false}};
})();
