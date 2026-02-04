// init.js — initialization helpers for the SyntH WebUI
// Registers service worker, error overlay, install prompt handling, and header sizing

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/service-worker.js').then((reg) => {
        console.debug('[synth_webui] ServiceWorker registered', reg.scope);
    }).catch((err) => console.debug('[synth_webui] ServiceWorker registration failed', err));
}

// Global error overlay and handlers (helpful in dev to surface blocking JS errors)
(function(){
    try {
        const overlay = document.createElement('pre');
        overlay.id = 'synth-error-overlay';
        overlay.style.cssText = 'display:none;position:fixed;left:0;right:0;top:0;max-height:50vh;overflow:auto;z-index:2147483647;background:rgba(0,0,0,0.85);color:#fff;padding:12px;font:12px/1.4 monospace;white-space:pre-wrap;';
        document.addEventListener('DOMContentLoaded', () => {
            try { document.body && document.body.appendChild(overlay); } catch (e) {}
        });

        function postConsole(level, message) {
            try {
                const payload = JSON.stringify({ level: String(level || 'info'), message: String(message || '') });
                if (navigator && typeof navigator.sendBeacon === 'function') {
                    const blob = new Blob([payload], { type: 'application/json' });
                    navigator.sendBeacon('/api/log-console', blob);
                    return;
                }
                fetch('/api/log-console', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: payload,
                    keepalive: true
                }).catch(() => {});
            } catch (e) { /* ignore */ }
        }

        function isTransientConnectivityError(text) {
            const t = String(text || '');
            return /WebSocket|network|ECONN|EHOSTUNREACH|ERR_NETWORK|Failed to fetch|Load failed|Connection lost/i.test(t);
        }

        function formatRejectionReason(reason) {
            try {
                if (reason instanceof Error) {
                    return reason.stack || (reason.name + ': ' + reason.message);
                }
                if (typeof reason === 'string') {
                    return reason;
                }
                try { return JSON.stringify(reason); } catch (e) { return String(reason); }
            } catch (e) {
                return String(reason);
            }
        }
        // Expose for legacy consumers/tests
        try {
            window.formatRejectionReason = formatRejectionReason;
            window.__synth_formatRejectionReason = window.__synth_formatRejectionReason || formatRejectionReason;
        } catch (e) { /* ignore */ }
    } catch (e) { /* ignore */ }
})();

// Ensure sessionId global exists early so other modules don't throw reference errors
window.sessionId = window.sessionId || null;

// Attach click handler to install button (when present)
document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('synth-install-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        if (window.__synthDeferredPrompt) {
            try {
                window.__synthDeferredPrompt.prompt();
                const choice = await window.__synthDeferredPrompt.userChoice;
                console.debug('[synth_webui] install prompt choice', choice);
                btn.style.display = 'none';
                window.__synthDeferredPrompt = null;
            } catch (e) {
                console.debug('[synth_webui] install prompt failed', e);
            }
        } else {
            console.debug('[synth_webui] no deferred prompt available');
        }
    });
});

// Compute header height and expose as CSS var so panels can size to viewport correctly
function refreshTopbarHeight() {
    try {
        const top = document.querySelector('header.top-bar');
        if (!top) return;
        const h = top.getBoundingClientRect().height;
        document.documentElement.style.setProperty('--topbar-height', Math.ceil(h) + 'px');
    } catch (e) { /* ignore */ }
}
window.addEventListener('resize', () => { refreshTopbarHeight(); });
document.addEventListener('DOMContentLoaded', () => { refreshTopbarHeight(); });
