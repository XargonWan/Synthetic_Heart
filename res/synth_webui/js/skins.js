// skins.js — skins tab helper (stub)
(function(){
    'use strict';
    console.log('[skins] script loaded');
    async function initSkinsTab() {
        try {
            if (typeof window.fetchSkins === 'function') {
                window.fetchSkins();
            }
            const clearBtn = document.getElementById('clear-uploaded');
            if (clearBtn && !clearBtn.dataset.bound) {
                clearBtn.addEventListener('click', () => {
                    if (typeof window.clearUploadedSkins === 'function') {
                        window.clearUploadedSkins();
                    }
                });
                clearBtn.dataset.bound = '1';
            }

            // attach VRM upload handler here so it runs when skins tab is active
            const skinUpload = document.getElementById('skin-vrm-upload');
            if (skinUpload && !skinUpload.dataset.bound) {
                console.debug('[skins] attaching VRM upload listener');
                skinUpload.addEventListener('change', async (e) => {
                    console.debug('[skins] upload input change event');
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
                            console.log('[skins] VRM upload successful');
                            try { alert('VRM uploaded successfully'); } catch (_) {}
                        } catch (err) {
                            console.error('[skins] VRM upload failed', err);
                            alert('Failed to upload VRM: ' + err.message);
                        }
                    }
                    try { e.target.value = ''; } catch (_){ }
                    setTimeout(()=>{ if(window.postUploadRefresh) window.postUploadRefresh().catch(()=>{}); }, 1200);
                });
                skinUpload.dataset.bound = '1';
            }

            console.debug('[skins] init');
        } catch (e) { console.error('[skins] init failed', e); }
    }
    window.SynthWebUI = window.SynthWebUI || {};
    window.SynthWebUI.initSkinsTab = initSkinsTab;
    document.addEventListener('DOMContentLoaded', () => {
        // Optionally call when skins tab loads
    });
})();
