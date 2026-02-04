// skins.js — skins tab helper (stub)
(function(){
    'use strict';
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
            console.debug('[skins] init');
        } catch (e) { console.error('[skins] init failed', e); }
    }
    window.SynthWebUI = window.SynthWebUI || {};
    window.SynthWebUI.initSkinsTab = initSkinsTab;
    document.addEventListener('DOMContentLoaded', () => {
        // Optionally call when skins tab loads
    });
})();
