// skins-upload.js — helpers for skins upload refresh and model refresh
(async function(){
    try {
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
