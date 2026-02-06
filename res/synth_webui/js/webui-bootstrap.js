(function () {
    if (typeof window === 'undefined') return;

    // Configure VRM loader resource path when available.
    try {
        if (typeof loader !== 'undefined') {
            loader.setResourcePath('/skins/temp/');
            console.log('[synth_webui] VRM loader configured with resource path: /skins/temp/');
        }
    } catch (e) { /* ignore */ }

    // Defensive stub for animationHandler until vrm-viewer.mjs is ready.
    try {
        window.__synth_pending_preloads = window.__synth_pending_preloads || {};
        if (!window.animationHandler) {
            window.animationHandler = {
                applyExpressionsForFrame: function () { /* no-op until module loads */ },
                _flushFaceNow: function () { /* no-op until module loads */ },
                preloadAnimation: function (name, descriptor) { window.__synth_pending_preloads[name] = descriptor || null; }
            };
        }
    } catch (e) { /* ignore */ }
})();
