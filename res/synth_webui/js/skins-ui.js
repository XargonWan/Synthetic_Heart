// skins-ui.js — extracted Skins UI helpers (fetch/render/activate/clear)
(function(){
    'use strict';

    async function fetchSkins(){
        const grid = document.getElementById('skins-grid');
        if(!grid) return;
        try {
            const res = await fetch('/api/skins');
            if (!res.ok) throw new Error('Failed to load skins: HTTP ' + res.status);
            const data = await res.json();
            // The API returns an array of skins
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
            const preview = document.createElement('div'); preview.className='skin-preview';
            if(s.preview_url){
                const img = document.createElement('img'); img.src = s.preview_url; preview.appendChild(img);
            } else {
                preview.innerHTML = '<div class="meta">No preview</div>';
            }
            const title = document.createElement('div'); title.innerHTML = `<strong>${s.name}</strong>`;

            // Build metadata section with version, author, description
            let metaHtml = '';
            if(s.vrm_version) metaHtml += `<div class="skin-vrm-version"><small>VRM ${s.vrm_version}</small></div>`;
            if(s.version) metaHtml += `<div class="skin-version"><small>Skin v${s.version}</small></div>`;
            if(s.author) metaHtml += `<div class="skin-author"><small>by ${s.author}</small></div>`;
            if(s.description) metaHtml += `<div class="skin-description"><small>${s.description}</small></div>`;
            const meta = document.createElement('div'); meta.className='skin-meta'; meta.innerHTML = metaHtml;

            const actions = document.createElement('div'); actions.className='skin-actions';
            const act = document.createElement('button'); act.className='btn-primary'; act.textContent='Activate';
            act.disabled = !s.valid;
            act.addEventListener('click', ()=> activateSkin(s.folder || s.name));
            const info = document.createElement('button'); info.className='btn-ghost'; info.textContent = s.vrm_present? 'Has VRM':'No VRM'; info.disabled=true;
            actions.appendChild(act); actions.appendChild(info);
            card.appendChild(preview); card.appendChild(title); card.appendChild(meta); card.appendChild(actions);
            grid.appendChild(card);
        })
    }

    async function activateSkin(name){
        try{
            const res = await fetch(`/api/skins/${encodeURIComponent(name)}/activate`, {method:'POST'});
            if(!res.ok) throw new Error('Activate failed');
            await fetchSkins();
            if(window.refreshModels) await window.refreshModels();
        }catch(e){ alert('Error: '+e.message) }
    }

    async function clearUploaded(){
        try{
            const res = await fetch('/api/skins/uploaded/clear', {method:'POST'});
            if(!res.ok) throw new Error('Clear failed');
            await fetchSkins();
            if(window.refreshModels) await window.refreshModels();
            alert('Restored default Rei VRM');
        }catch(e){ alert('Error: '+e.message) }
    }

    function init(){
        try{
            const clearBtn = document.getElementById('clear-uploaded');
            if(clearBtn) clearBtn.addEventListener('click', clearUploaded);
            const navSkins = document.getElementById('nav-skins');
            if(navSkins) navSkins.addEventListener('click', function(){ fetchSkins(); });

            // Load skins on page initialization if fetchSkins isn't provided elsewhere
            try { if (typeof fetchSkins === 'function') fetchSkins(); } catch (e) {}
        } catch (e){ console.warn('[synth_webui] skins-ui init failed', e); }
    }

    // Export for other modules and for backward compatibility
    window.fetchSkins = window.fetchSkins || fetchSkins;
    window.renderSkins = window.renderSkins || renderSkins;
    window.activateSkin = window.activateSkin || activateSkin;
    window.clearUploadedSkins = window.clearUploadedSkins || clearUploaded;

    // Auto-init
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();