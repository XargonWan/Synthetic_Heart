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
            window.__synth_pending_actions = window.__synth_pending_actions || [];
            window.animationHandler = {
                applyExpressionsForFrame: function () { /* no-op until module loads */ },
                _flushFaceNow: function () { /* no-op until module loads */ },
                preloadAnimation: function (name, descriptor) { window.__synth_pending_preloads[name] = descriptor || null; },
                // Queue a startAction so it will be executed once the real handler is available
                startAction: function (state, animation, playOnce, playSection, descriptor, frameRange, phaseAuthoritative) {
                    try {
                        window.__synth_pending_actions = window.__synth_pending_actions || [];
                        window.__synth_pending_actions.push({ type: 'startAction', args: [state, animation, playOnce, playSection, descriptor, frameRange, phaseAuthoritative] });
                        console.warn('[synth_webui] animationHandler not ready — queued startAction');
                    } catch (e) { /* ignore */ }
                },
                startTemporaryLoop: function (type, file, startFrame, endFrame, fps) {
                    try {
                        window.__synth_pending_actions = window.__synth_pending_actions || [];
                        window.__synth_pending_actions.push({ type: 'startTemporaryLoop', args: [type, file, startFrame, endFrame, fps] });
                        console.warn('[synth_webui] animationHandler not ready — queued startTemporaryLoop');
                    } catch (e) { /* ignore */ }
                },
                clearTemporaryOverride: function () {
                    try {
                        window.__synth_pending_actions = window.__synth_pending_actions || [];
                        window.__synth_pending_actions.push({ type: 'clearTemporaryOverride', args: [] });
                        console.warn('[synth_webui] animationHandler not ready — queued clearTemporaryOverride');
                    } catch (e) { /* ignore */ }
                }
            };
        }
    } catch (e) { /* ignore */ }
})();
