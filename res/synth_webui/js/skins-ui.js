// skins-ui.js — Skins UI helpers (fetch/render/activate/clear + skin editor)
(function(){
    'use strict';

    const _apiBase = (window.__getApiBase && window.__getApiBase()) || '';

    // ----------------------------------------------------------------
    // Core skin list
    // ----------------------------------------------------------------

    async function fetchSkins(){
        const grid = document.getElementById('skins-grid');
        if(!grid) return;
        try {
            const res = await fetch(_apiBase + '/api/skins');
            if (!res.ok) throw new Error('Failed to load skins: HTTP ' + res.status);
            const data = await res.json();
            renderSkins(data || []);
        } catch (e) {
            console.warn('[synth_webui] fetchSkins failed', e);
            grid.innerHTML = `<div class="meta">${e.message || 'Failed to load skins'}</div>`;
        }
    }

    function renderSkins(skins){
        const grid = document.getElementById('skins-grid');
        if(!grid) return;
        if(!skins || skins.length===0){
            grid.innerHTML = '<div class="meta">No skins found</div>';
            return;
        }
        grid.innerHTML = '';
        skins.forEach(s => {
            const card = document.createElement('div'); card.className='skin-card';

            // Preview
            const preview = document.createElement('div'); preview.className='skin-preview';
            if(s.preview_url){
                const img = document.createElement('img'); img.src = s.preview_url; preview.appendChild(img);
            } else {
                preview.innerHTML = '<div class="meta">No preview</div>';
            }

            // Title
            const title = document.createElement('div'); title.innerHTML = `<strong>${escapeHtml(s.name)}</strong>`;

            // Metadata
            let metaHtml = '';
            if(s.vrm_version) metaHtml += `<div class="skin-vrm-version"><small>VRM ${escapeHtml(s.vrm_version)}</small></div>`;
            if(s.version) metaHtml += `<div class="skin-version"><small>Skin v${escapeHtml(s.version)}</small></div>`;
            if(s.author) metaHtml += `<div class="skin-author"><small>by ${escapeHtml(s.author)}</small></div>`;
            if(s.description) metaHtml += `<div class="skin-description"><small>${escapeHtml(s.description)}</small></div>`;
            const meta = document.createElement('div'); meta.className='skin-meta'; meta.innerHTML = metaHtml;

            // Actions row 1: Activate + status
            const actions = document.createElement('div'); actions.className='skin-actions';
            const act = document.createElement('button'); act.className='btn-primary'; act.textContent='Activate';
            act.disabled = !s.valid;
            act.addEventListener('click', ()=> activateSkin(s.folder || s.name));
            const info = document.createElement('button'); info.className='btn-ghost'; info.textContent = s.vrm_present? 'Has VRM':'No VRM'; info.disabled=true;
            actions.appendChild(act); actions.appendChild(info);

            // Actions row 2: Download, Upload VRM, Upload Preview, Delete
            const actions2 = document.createElement('div'); actions2.className='skin-actions skin-actions-editor';

            // Download
            const dlBtn = document.createElement('button'); dlBtn.className='btn-ghost'; dlBtn.textContent='Download';
            dlBtn.addEventListener('click', ()=> downloadSkin(s.folder || s.name));
            actions2.appendChild(dlBtn);

            // Upload VRM
            const vrmLabel = document.createElement('label'); vrmLabel.className='btn-ghost'; vrmLabel.textContent='+ VRM';
            vrmLabel.style.cursor='pointer';
            const vrmInput = document.createElement('input'); vrmInput.type='file'; vrmInput.accept='.vrm'; vrmInput.style.display='none';
            vrmInput.addEventListener('change', (e) => {
                if(e.target.files && e.target.files[0]) uploadSkinVrm(s.folder || s.name, e.target.files[0]);
            });
            vrmLabel.appendChild(vrmInput);
            actions2.appendChild(vrmLabel);

            // Upload Preview
            const prevLabel = document.createElement('label'); prevLabel.className='btn-ghost'; prevLabel.textContent='+ Preview';
            prevLabel.style.cursor='pointer';
            const prevInput = document.createElement('input'); prevInput.type='file'; prevInput.accept='.png,.jpg,.jpeg,.webp'; prevInput.style.display='none';
            prevInput.addEventListener('change', (e) => {
                if(e.target.files && e.target.files[0]) uploadSkinPreview(s.folder || s.name, e.target.files[0]);
            });
            prevLabel.appendChild(prevInput);
            actions2.appendChild(prevLabel);

            // Delete: not for the bundled skins (Rei, Zero); a skin that came from an
            // extension is uninstalled through the extension.
            const folder = s.folder || s.name;
            if(s.removable !== false && folder !== 'Rei' && folder !== 'Zero'){
                const delBtn = document.createElement('button'); delBtn.className='btn-ghost btn-danger';
                delBtn.textContent = s.extension_id ? 'Uninstall' : 'Delete';
                delBtn.addEventListener('click', ()=> s.extension_id ? uninstallSkin(s) : deleteSkin(folder));
                actions2.appendChild(delBtn);
            }
            if(s.bundled){
                const tag = document.createElement('div'); tag.className='skin-description';
                tag.innerHTML = '<small>Bundled with SyntH</small>'; meta.appendChild(tag);
            } else if(s.extension_id){
                const tag = document.createElement('div'); tag.className='skin-description';
                tag.innerHTML = '<small>Installed from ' + escapeHtml(s.extension_id) + '</small>'; meta.appendChild(tag);
            }

            card.appendChild(preview); card.appendChild(title); card.appendChild(meta);
            card.appendChild(actions); card.appendChild(actions2);
            grid.appendChild(card);
        });
    }

    function escapeHtml(str) {
        if (!str) return '';
        return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    }

    // ----------------------------------------------------------------
    // Skin actions
    // ----------------------------------------------------------------

    async function activateSkin(name){
        try{
            const res = await fetch(_apiBase + `/api/skins/${encodeURIComponent(name)}/activate`, {method:'POST'});
            if(!res.ok) throw new Error('Activate failed');
            await fetchSkins();
            if(window.refreshModels) await window.refreshModels();
        }catch(e){ alert('Error: '+e.message); }
    }

    async function clearUploaded(){
        try{
            const res = await fetch(_apiBase + '/api/skins/uploaded/clear', {method:'POST'});
            if(!res.ok) throw new Error('Clear failed');
            await fetchSkins();
            if(window.refreshModels) await window.refreshModels();
            alert('Restored default Rei VRM');
        }catch(e){ alert('Error: '+e.message); }
    }

    // ----------------------------------------------------------------
    // Skin editor functions
    // ----------------------------------------------------------------

    async function createSkin(data){
        try{
            const res = await fetch(_apiBase + '/api/skins', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            if(!res.ok){
                const err = await res.json().catch(()=>({}));
                throw new Error(err.detail || 'Failed to create skin');
            }
            await fetchSkins();
            return true;
        }catch(e){ alert('Error: '+e.message); return false; }
    }

    async function uploadSkinVrm(skinName, file){
        try{
            const fd = new FormData();
            fd.append('file', file);
            const res = await fetch(_apiBase + `/api/skins/${encodeURIComponent(skinName)}/vrm`, {method:'POST', body:fd});
            if(!res.ok){
                const err = await res.json().catch(()=>({}));
                throw new Error(err.detail || 'Upload failed');
            }
            await fetchSkins();
            if(window.refreshModels) await window.refreshModels();
        }catch(e){ alert('Error: '+e.message); }
    }

    async function uploadSkinPreview(skinName, file){
        try{
            const fd = new FormData();
            fd.append('file', file);
            const res = await fetch(_apiBase + `/api/skins/${encodeURIComponent(skinName)}/preview`, {method:'POST', body:fd});
            if(!res.ok){
                const err = await res.json().catch(()=>({}));
                throw new Error(err.detail || 'Upload failed');
            }
            await fetchSkins();
        }catch(e){ alert('Error: '+e.message); }
    }

    function downloadSkin(skinName){
        window.location.href = _apiBase + `/api/skins/${encodeURIComponent(skinName)}/download`;
    }

    async function uploadSkinZip(file){
        try{
            const fd = new FormData();
            fd.append('file', file);
            const res = await fetch(_apiBase + '/api/skins/upload', {method:'POST', body:fd});
            if(!res.ok){
                const err = await res.json().catch(()=>({}));
                throw new Error(err.detail || 'Upload failed');
            }
            await fetchSkins();
            if(window.refreshModels) await window.refreshModels();
        }catch(e){ alert('Error: '+e.message); }
    }

    async function deleteSkin(skinName){
        if(!confirm(`Delete skin "${skinName}"? This cannot be undone.`)) return;
        try{
            const res = await fetch(_apiBase + `/api/skins/${encodeURIComponent(skinName)}`, {method:'DELETE'});
            if(!res.ok){
                const err = await res.json().catch(()=>({}));
                throw new Error(err.detail || 'Delete failed');
            }
            await fetchSkins();
        }catch(e){ alert('Error: '+e.message); }
    }

    // ----------------------------------------------------------------
    // Skins from repositories (extensions of kind "skins")
    // ----------------------------------------------------------------

    const STORE = '/api/extensions/store';

    async function storeCall(path, body){
        const opts = body === undefined ? {method:'GET'}
            : {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)};
        const res = await fetch(_apiBase + STORE + path, opts);
        if(!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
    }

    function textEl(tag, text, cls){
        const n = document.createElement(tag); n.textContent = text || '';
        if(cls) n.className = cls;
        return n;
    }

    async function uninstallSkin(skin){
        if(!confirm(`Uninstall skin "${skin.name}"? Its files are removed.`)) return;
        try{
            const out = await storeCall('/remove', {id: skin.extension_id});
            if(!out.ok) throw new Error(out.error || 'Uninstall failed');
            await refreshSkins();
        }catch(e){ alert('Error: '+e.message); }
    }

    async function installSkin(id, button){
        button.disabled = true;
        try{
            const out = await storeCall('/install', {id});
            if(out.needs_confirmation){
                if(!confirm(out.error + '\n\nInstall anyway?')) { button.disabled = false; return; }
                const again = await storeCall('/install', {id, confirm_untrusted: true});
                if(!again.ok) throw new Error(again.error || 'Install failed');
            } else if(!out.ok) {
                throw new Error(out.error || 'Install failed');
            }
            await refreshSkins();
        }catch(e){ alert('Error: '+e.message); button.disabled = false; }
    }

    async function fetchSkinCatalog(){
        const host = document.getElementById('skins-catalog');
        if(!host) return;
        host.textContent = '';
        let data;
        try{
            data = await storeCall('');
        }catch(e){
            host.appendChild(textEl('div', 'The skin catalog could not be read (' + e.message + ').', 'meta'));
            return;
        }
        (data.problems || []).forEach(p => host.appendChild(textEl('div', p, 'component-error')));
        const offered = (data.extensions || []).filter(x => x.kind === 'skins' && !x.installed);
        const updates = (data.extensions || []).filter(x => x.kind === 'skins' && x.update);
        if(!offered.length && !updates.length){
            host.appendChild(textEl('div', data.repositories && data.repositories.length
                ? 'No more skins are offered by your repositories.'
                : 'No repositories yet: add one in Plugins > Repositories.', 'meta'));
            return;
        }
        const grid = document.createElement('div'); grid.className = 'skins-grid';
        updates.concat(offered).forEach(x => {
            const card = document.createElement('div'); card.className = 'skin-card';
            card.appendChild(textEl('strong', x.name || x.id));
            const meta = document.createElement('div'); meta.className = 'skin-meta';
            if(x.authors && x.authors.length) meta.appendChild(textEl('div', 'by ' + x.authors.join(', '), 'skin-author'));
            if(x.description) meta.appendChild(textEl('div', x.description, 'skin-description'));
            meta.appendChild(textEl('div', 'v' + x.available + (x.repo ? ' · ' + x.repo : ''), 'skin-version'));
            card.appendChild(meta);
            const actions = document.createElement('div'); actions.className = 'skin-actions';
            const btn = document.createElement('button'); btn.className = 'btn-primary';
            btn.textContent = x.installed ? 'Update to ' + x.available : 'Install';
            btn.addEventListener('click', () => x.installed ? updateSkin(x.id, btn) : installSkin(x.id, btn));
            actions.appendChild(btn); card.appendChild(actions);
            grid.appendChild(card);
        });
        host.appendChild(grid);
    }

    async function updateSkin(id, button){
        button.disabled = true;
        try{
            const out = await storeCall('/update', {id});
            if(!out.ok) throw new Error(out.error || 'Update failed');
            await refreshSkins();
        }catch(e){ alert('Error: '+e.message); button.disabled = false; }
    }

    async function refreshSkins(){
        await fetchSkins();
        await fetchSkinCatalog();
    }

    // ----------------------------------------------------------------
    // Init: wire up buttons
    // ----------------------------------------------------------------

    function init(){
        try{
            // Clear uploaded button
            const clearBtn = document.getElementById('clear-uploaded');
            if(clearBtn) clearBtn.addEventListener('click', clearUploaded);

            // Nav skins tab click
            const navSkins = document.querySelector('.nav-btn[data-tab="skins"]');
            if(navSkins) navSkins.addEventListener('click', function(){ fetchSkins(); fetchSkinCatalog(); });

            // set up upload input handler once element exists
            const skinUpload = document.getElementById('skin-vrm-upload');
            if(skinUpload){
                console.log('[skins-ui] attaching upload listener');
                skinUpload.addEventListener('change', async (e)=>{
                    console.log('[skins-ui] upload input change event');
                    const file = e.target.files && e.target.files[0];
                    if (file) {
                        const form = new FormData();
                        form.append('file', file, file.name);
                        try {
                            const res = await fetch('/api/vrm', { method: 'POST', body: form });
                            if (!res.ok) {
                                const txt = await res.text();
                                throw new Error(`HTTP ${res.status}: ${txt}`);
                            }
                            console.log('[skins-ui] VRM upload successful');
                            try{ alert('VRM uploaded successfully'); }catch(_){ }
                        } catch (err) {
                            console.error('[skins-ui] VRM upload failed', err);
                            alert('Failed to upload VRM: ' + err.message);
                        }
                    }
                    try{ e.target.value = ''; }catch(_){ }
                    setTimeout(()=>{ if(window.postUploadRefresh) window.postUploadRefresh().catch(()=>{}); }, 1200);
                });
            } else {
                console.log('[skins-ui] upload input not found during init');
            }

            // New Skin form toggle
            const newBtn = document.getElementById('btn-new-skin');
            const formEl = document.getElementById('skin-editor-form');
            if(newBtn && formEl){
                newBtn.addEventListener('click', ()=>{
                    formEl.style.display = formEl.style.display === 'none' ? 'block' : 'none';
                });
            }

            // Cancel button
            const cancelBtn = document.getElementById('btn-skin-cancel');
            if(cancelBtn && formEl){
                cancelBtn.addEventListener('click', ()=>{
                    formEl.style.display = 'none';
                });
            }

            // Create button
            const createBtn = document.getElementById('btn-skin-create');
            if(createBtn){
                createBtn.addEventListener('click', async ()=>{
                    const name = (document.getElementById('skin-form-name') || {}).value || '';
                    if(!name.trim()){ alert('Skin name is required'); return; }
                    const data = {
                        name: name.trim(),
                        author: (document.getElementById('skin-form-author') || {}).value || '',
                        version: (document.getElementById('skin-form-version') || {}).value || '1.0',
                        appearance: (document.getElementById('skin-form-appearance') || {}).value || ''
                    };
                    const ok = await createSkin(data);
                    if(ok && formEl){
                        formEl.style.display = 'none';
                        // Clear form
                        const nameEl = document.getElementById('skin-form-name'); if(nameEl) nameEl.value='';
                        const authorEl = document.getElementById('skin-form-author'); if(authorEl) authorEl.value='';
                        const versionEl = document.getElementById('skin-form-version'); if(versionEl) versionEl.value='1.0';
                        const appearanceEl = document.getElementById('skin-form-appearance'); if(appearanceEl) appearanceEl.value='';
                    }
                });
            }

            // Upload skin zip
            const zipInput = document.getElementById('skin-zip-upload');
            if(zipInput){
                zipInput.addEventListener('change', (e)=>{
                    if(e.target.files && e.target.files[0]){
                        uploadSkinZip(e.target.files[0]);
                        e.target.value = ''; // reset for re-upload
                    }
                });
            }

            // Initial load
            try { fetchSkins(); } catch(e) {}
        } catch (e){ console.warn('[synth_webui] skins-ui init failed', e); }
    }

    // Export for other modules
    window.fetchSkins = window.fetchSkins || fetchSkins;
    window.fetchSkinCatalog = window.fetchSkinCatalog || fetchSkinCatalog;
    window.renderSkins = window.renderSkins || renderSkins;
    window.activateSkin = window.activateSkin || activateSkin;
    window.clearUploadedSkins = window.clearUploadedSkins || clearUploaded;
    window.createSkin = window.createSkin || createSkin;
    window.deleteSkin = window.deleteSkin || deleteSkin;
    window.downloadSkin = window.downloadSkin || downloadSkin;

    // Auto-init
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
