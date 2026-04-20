// skins-upload.js — helpers for skins upload refresh and model refresh
(async function(){
    try {
        console.log('[skins-upload] script loaded');
        async function postUploadRefresh(){
            try{
                // refresh skins grid
                if (typeof fetchSkins === 'function') await fetchSkins();
            }catch(e){ console.warn('[synth_webui] postUploadRefresh: failed to refresh skins', e); }
            try{
                // refresh VRM models list (avatar manager)
                if(typeof window.refreshModels === 'function') await window.refreshModels();
            }catch(e){ console.warn('[synth_webui] postUploadRefresh: failed to refresh models', e); }
        }

        const skinUpload = document.getElementById('skin-vrm-upload');
        if(skinUpload){
            skinUpload.addEventListener('change', async (e)=>{
                const file = e.target.files && e.target.files[0];
                if (file) {
                    console.log('[synth_webui] skin upload selected', file.name);
                    const form = new FormData();
                    form.append('file', file, file.name);
                    try {
                        const res = await fetch('/api/vrm', { method: 'POST', body: form });
                        if (!res.ok) {
                            const txt = await res.text();
                            throw new Error(`HTTP ${res.status}: ${txt}`);
                        }
                        console.log('[synth_webui] VRM upload successful');
                    // user feedback
                    try { alert('VRM uploaded successfully'); } catch(_){}
                    } catch (err) {
                        console.error('[synth_webui] VRM upload failed', err);
                        alert('Failed to upload VRM: ' + err.message);
                    }
                }

                // reset input so the same file can be re‑selected if needed
                try{ e.target.value = ''; }catch(_){ }

                // Schedule a delayed refresh to allow the main upload handler to complete.
                setTimeout(()=>{ postUploadRefresh().catch(()=>{}); }, 1200);
            });
        }

        // Export for external callers if needed
        window.postUploadRefresh = window.postUploadRefresh || postUploadRefresh;
    } catch (e) {
        console.warn('[synth_webui] skins-upload helper failed to initialize', e);
    }
})();
