// debug-window.mjs — Extracted Debug window logic
export function createDebugWindow() {
    try {
        console.log('[debug-window] module loaded');
        // clamp01 helper
        const clamp01 = (x) => {
            const v = Number(x);
            if (!Number.isFinite(v)) return 0;
            return Math.max(0, Math.min(1, v));
        };

        const isPaused = () => !!window.__synth_debug_pause_all;

        const ensureFallbackDock = () => {
            let dock = document.getElementById('synth-minimized-stack');
            if (dock) return dock;
            dock = document.createElement('div');
            dock.id = 'synth-minimized-stack';
            dock.style.position = 'fixed';
            dock.style.right = 'auto';
            dock.style.left = '18px';
            dock.style.bottom = '18px';
            dock.style.display = 'flex';
            dock.style.flexDirection = 'column';
            dock.style.gap = '8px';
            dock.style.zIndex = 99999;
            dock.style.alignItems = 'flex-start';
            document.body.appendChild(dock);
            return dock;
        };

        const getDock = () => {
            if (window.SynthWindowManager && typeof window.SynthWindowManager.ensureDock === 'function') {
                return window.SynthWindowManager.ensureDock();
            }
            return ensureFallbackDock();
        };

        const buildDebugPanel = () => {
            const panel = document.createElement('div');
            panel.id = 'synth-advanced-debug';
            panel.className = 'synth-window-panel';
            panel.style.display = 'flex';
            panel.style.flexDirection = 'column';
            panel.style.width = '100%';
            panel.style.height = '100%';
            panel.innerHTML = `
            <div id="synth-debug-title-bar" style="display:flex;align-items:center;justify-content:space-between;gap:6px;padding:10px 12px;border-bottom:1px solid var(--border);cursor:move;user-select:none;">
                <div id="synth-debug-title" style="font-weight:600;">Debug</div>
                <div id="synth-debug-header-tools" style="display:flex;gap:6px;align-items:center;"></div>
            </div>
            <div id="synth-debug-body" style="padding:12px;display:flex;flex-direction:column;gap:12px;overflow:auto;height:calc(100% - 52px);">

                <div style="display:flex;gap:8px;align-items:center;justify-content:flex-start;">
                    <div id="synth-debug-controls" style="display:flex;gap:8px;align-items:center;">
                        <button id="synth-debug-pause" class="pill secondary" type="button" title="⏸️">⏸️</button>
                        <button id="synth-debug-resync" class="pill secondary" type="button" title="Sync">🛜</button>
                        <button id="synth-debug-reset" class="pill" type="button" title="Reset">🔁</button>
                    </div>
                </div>

                <div class="card" style="margin:0;">
                    <h2 style="margin:0 0 8px 0;">Status</h2>
                    <div style="font-size:12px;color:var(--text-soft);line-height:1.4;">
                        <div>Paused: <span id="synth-debug-status-paused">—</span></div>
                        <div>Current: <span id="synth-debug-status-current">—</span></div>
                        <div>Phase: <span id="synth-debug-status-phase">—</span></div>
                        <div>Frame: <span id="synth-debug-status-frame">—</span></div>
                        <div>Remote: <span id="synth-debug-status-remote">—</span></div>
                    </div>
                </div>

                <div class="card" style="margin:0;">
                    <h2 style="margin:0 0 8px 0;">Loop Override</h2>
                    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
                        <select id="synth-debug-loop-type" style="flex:1 1 140px;min-width:120px;padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);"></select>
                        <select id="synth-debug-loop-file" style="flex:2 1 220px;min-width:160px;padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);"></select>
                    </div>
                    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;">
                        <input id="synth-debug-loop-start" type="number" placeholder="start" style="flex:1 1 120px;min-width:100px;padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);" />
                        <input id="synth-debug-loop-end" type="number" placeholder="end" style="flex:1 1 120px;min-width:100px;padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);" />
                        <input id="synth-debug-loop-fps" type="number" placeholder="fps" style="flex:0 0 86px;min-width:86px;padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);" />
                    </div>
                    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;">
                        <button id="synth-debug-loop-start-btn" class="pill" type="button" style="flex:1;">Start</button>
                        <button id="synth-debug-loop-clear-btn" class="pill secondary" type="button" style="flex:1;">Clear</button>
                        <button id="synth-debug-loop-refresh" class="pill secondary" type="button">Refresh</button>
                    </div>
                    <div style="margin-top:8px;font-size:11px;color:var(--text-soft);">Loaded: <span id="synth-debug-loop-loaded">0</span></div>
                </div>

                <div class="card" style="margin:0;">
                    <h2 style="margin:0 0 8px 0;">Feelings</h2>
                    <div id="synth-debug-feelings" style="display:flex;flex-direction:column;gap:8px;"></div>
                    <div style="display:flex;gap:8px;margin-top:8px;">
                        <button id="synth-debug-feelings-clear" class="pill secondary" type="button">Clear Overrides</button>
                    </div>
                </div>

                <div class="card" style="margin:0;">
                    <h2 style="margin:0 0 8px 0;">Facial Morphs</h2>
                    <input id="synth-debug-face-filter" placeholder="filter…" style="width:100%;padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);" />
                    <div id="synth-debug-face-list" style="margin-top:8px;max-height:240px;overflow:auto;display:flex;flex-direction:column;gap:8px;"></div>
                    <div style="display:flex;gap:8px;margin-top:8px;">
                        <button id="synth-debug-face-clear" class="pill secondary" type="button">Clear Overrides</button>
                    </div>
                </div>
            </div>
        `;
            return panel;
        };

        let win = null;
        let winbox = null;
        const tryCreateWinBox = () => {
            if (!window.SynthWindowManager || typeof window.SynthWindowManager.create !== 'function') return null;
            if (typeof window.WinBox === 'undefined') return null;
            const panel = buildDebugPanel();
            win = panel;
                winbox = window.SynthWindowManager.create({
                id: 'debug',
                title: 'Debug',
                mount: panel,
                width: 420,
                height: 520,
                    x: 'right',
                y: 'bottom',
                dockLabel: 'Debug',
                dockClass: 'debug-toggle-btn',
                className: 'synth-winbox no-close'
            });
            // Intentionally do not attach header tools for Debug — pause control lives inside the debug panel only.

            return winbox;
        };

        if (!tryCreateWinBox()) {
            win = buildDebugPanel();
            win.style.position = 'fixed';
            win.style.right = '18px';
            win.style.bottom = '86px';
            win.style.width = '420px';
            win.style.maxWidth = 'calc(100% - 40px)';
            win.style.minWidth = '320px';
            win.style.height = '520px';
            win.style.maxHeight = 'calc(100vh - 140px)';
            win.style.minHeight = '240px';
            win.style.zIndex = 99998;
            win.style.background = 'var(--surface)';
            win.style.color = 'var(--text)';
            win.style.border = '1px solid var(--border)';
            win.style.borderRadius = '12px';
            win.style.overflow = 'hidden';
            win.style.resize = 'both';
            document.body.appendChild(win);

            const dock = getDock();
            try {
                const debugToggleBtn = document.getElementById('debug-toggle');
                if (debugToggleBtn && debugToggleBtn.parentElement !== dock) {
                    dock.appendChild(debugToggleBtn);
                    try { debugToggleBtn.style.position = 'static'; debugToggleBtn.style.right = ''; debugToggleBtn.style.bottom = ''; } catch (e) { /* ignore */ }
                }
            } catch (e) { /* ignore */ }
        }

        if (!winbox) {
            (function makeDraggable(el) {
                const header = el.querySelector('#synth-debug-title-bar');
                if (!header) return;
                let dragging = false;
                let startX = 0, startY = 0;
                let offsetX = 0, offsetY = 0;
                header.addEventListener('pointerdown', (ev) => {
                    try {
                        try { ev.stopPropagation(); } catch (e) {}
                        try { if (window.__synth_active_interaction) return; } catch (e) {}
                        const t = ev && ev.target ? ev.target : null;
                        if (t && typeof t.closest === 'function') {
                            if (t.closest('button, input, select, textarea, a')) return;
                        }
                        dragging = true;
                        const dragPointerId = (ev.pointerId !== undefined) ? ev.pointerId : 'mouse';
                        try { window.__synth_active_interaction = { type: 'debug_drag', id: dragPointerId }; } catch (e) {}
                        const r = el.getBoundingClientRect();
                        try {
                            el.style.left = r.left + 'px';
                            el.style.top = r.top + 'px';
                            el.style.right = 'auto';
                            el.style.bottom = 'auto';
                        } catch (e) { /* ignore */ }
                        startX = ev.clientX;
                        startY = ev.clientY;
                        offsetX = startX - r.left;
                        offsetY = startY - r.top;
                        try { header.setPointerCapture && header.setPointerCapture(ev.pointerId); } catch (e) {}
                    } catch (e) { /* ignore */ }
                });
                window.addEventListener('pointermove', (ev) => {
                    if (!dragging) return;
                    try {
                        el.style.left = (ev.clientX - offsetX) + 'px';
                        el.style.top = (ev.clientY - offsetY) + 'px';
                        el.style.right = 'auto';
                        el.style.bottom = 'auto';
                    } catch (e) { /* ignore */ }
                });
                window.addEventListener('pointerup', (ev) => { try { dragging = false; if (window.__synth_active_interaction && window.__synth_active_interaction.type === 'debug_drag') window.__synth_active_interaction = null; } catch (e) {} });
            })(win);

            try { createResizeHandlesForElement(win); } catch (e) { /* ignore */ }

            let debugDockBtn = null;
            const ensureDebugDockBtn = () => {
                if (debugDockBtn && debugDockBtn.isConnected) return debugDockBtn;
                debugDockBtn = document.createElement('button');
                debugDockBtn.type = 'button';
                debugDockBtn.className = 'chat-toggle-btn';
                debugDockBtn.textContent = '💻';
                debugDockBtn.setAttribute('aria-label', 'Restore debug');
                debugDockBtn.title = 'Restore Debug';
                try {
                    debugDockBtn.style.position = 'static';
                    debugDockBtn.style.top = '';
                    debugDockBtn.style.right = '';
                    debugDockBtn.style.bottom = '';
                    debugDockBtn.style.left = '';
                    debugDockBtn.style.zIndex = '';
                } catch (e) { /* ignore */ }
                debugDockBtn.addEventListener('click', () => {
                    try { win.style.display = ''; } catch (e) { /* ignore */ }
                    try { if (debugDockBtn && debugDockBtn.parentElement) debugDockBtn.parentElement.removeChild(debugDockBtn); } catch (e) { /* ignore */ }
                });
                return debugDockBtn;
            };

            const minimizeBtnLegacy = win.querySelector('#synth-debug-minimize');
            if (minimizeBtnLegacy) {
                minimizeBtnLegacy.addEventListener('click', () => {
                    try { win.style.display = 'none'; } catch (e) { /* ignore */ }
                    try { getDock().appendChild(ensureDebugDockBtn()); } catch (e) { /* ignore */ }
                });
            }
        }

        const minimizeBtn = win.querySelector('#synth-debug-minimize');
        if (minimizeBtn) {
            minimizeBtn.addEventListener('click', () => {
                if (winbox && window.SynthWindowManager && typeof window.SynthWindowManager.minimize === 'function') {
                    try { window.SynthWindowManager.minimize('debug'); } catch (e) { /* ignore */ }
                    return;
                }
            });
        }

        async function resyncFromBackend() {
            try {
                if (!window.animationHandler) return;
                if (isPaused()) return;
                try {
                    const st = window.__synth_last_rich_animation_state || null;
                    if (st && typeof window.animationHandler.applyAnimationState === 'function') {
                        window.animationHandler.applyAnimationState(st);
                    }
                } catch (e) { /* ignore */ }
                try {
                    const resp = await fetch('/api/animation_state');
                    if (resp && resp.ok) {
                        const summary = await resp.json();
                        if (summary && summary.state) {
                            const playOnce = !!(summary.descriptor && summary.descriptor.play_once);
                            await window.animationHandler.startAction(summary.state, summary.animation || null, playOnce, null, summary.descriptor || null);
                            return;
                        }
                    }
                } catch (e) { /* ignore */ }

                try {
                    const last = (window.__synth_debug_last_remote && window.__synth_debug_last_remote.animation) ? window.__synth_debug_last_remote.animation : null;
                    if (last && last.state) {
                        const playOnce = (last.descriptor && last.descriptor.play_once) || (last.loop === false);
                        if (last.animation_state && typeof window.animationHandler.applyAnimationState === 'function') {
                            window.animationHandler.applyAnimationState(last.animation_state);
                        }
                        await window.animationHandler.startAction(last.state, last.animation || null, !!playOnce, last.play_section || null, last.descriptor || null);
                    }
                } catch (e) { /* ignore */ }
            } catch (e) { /* ignore */ }
        }

        const pauseBtn = (win && win.querySelector) ? win.querySelector('#synth-debug-pause') : null;
        const resyncBtn = (win && win.querySelector) ? win.querySelector('#synth-debug-resync') : null;
        const resetBtn = (win && win.querySelector) ? win.querySelector('#synth-debug-reset') : null;

        const setPaused = async (paused) => {
            try {
                window.__synth_debug_pause_all = !!paused;
                window.__synth_web_debug_enabled = true;
            } catch (e) { /* ignore */ }
            try {
                if (window.animationHandler) {
                    if (paused) {
                        try { if (typeof window.animationHandler._stopBlinkLoop === 'function') window.animationHandler._stopBlinkLoop(); } catch (e) { /* ignore */ }
                        try { if (typeof window.animationHandler._stopEyeMovement === 'function') window.animationHandler._stopEyeMovement(); } catch (e) { /* ignore */ }
                    } else {
                        try { if (window.animationHandler._blinkAutoEnabled && typeof window.animationHandler._startBlinkLoop === 'function') window.animationHandler._startBlinkLoop(); } catch (e) { /* ignore */ }
                        try { if (window.animationHandler._eyeAutoEnabled && typeof window.animationHandler._startEyeMovement === 'function') window.animationHandler._startEyeMovement(); } catch (e) { /* ignore */ }
                    }
                }
            } catch (e) { /* ignore */ }

            try {
                if (pauseBtn) {
                    pauseBtn.textContent = paused ? '▶️' : '⏸️';
                    // Use emojis for title as requested; keep aria-label descriptive for accessibility
                    pauseBtn.title = paused ? '▶️' : '⏸️';
                    pauseBtn.setAttribute('aria-label', paused ? 'Play' : 'Pause');
                }
                if (winbox) {
                    const winEl = winbox.window || winbox.dom || winbox.g || null;
                    const toolBtn = winEl ? winEl.querySelector('.synth-wb-tool-pause') : null;
                    if (toolBtn) {
                        toolBtn.textContent = paused ? '▶️' : '⏸️';
                        toolBtn.title = paused ? '▶️' : '⏸️';
                        toolBtn.setAttribute('aria-label', paused ? 'Play' : 'Pause');
                    }
                }
            } catch (e) { /* ignore */ }
            if (!paused) {
                await resyncFromBackend();
            }
        };

        if (pauseBtn) {
            pauseBtn.addEventListener('click', async () => {
                try { await setPaused(!isPaused()); } catch (e) { /* ignore */ }
            });
        }
        if (resyncBtn) {
            resyncBtn.addEventListener('click', async () => { await resyncFromBackend(); });
        }
        if (resetBtn) {
            resetBtn.addEventListener('click', async () => {
                try { if (window.animationHandler) { window.animationHandler.clearDebugFaceOverrides?.(); window.animationHandler.clearDebugEmotionOverrides?.(); window.animationHandler.clearTemporaryOverride?.(); } } catch (e) { /* ignore */ }
                try { await resetLoopOverrideUI(); } catch (e) { /* ignore */ }
                await setPaused(false);
                await resyncFromBackend();
            });
        }

        // Loop override controls
        const types = ['idle','think','talk','write','touch'];
        const selType = win.querySelector('#synth-debug-loop-type');
        const selFile = win.querySelector('#synth-debug-loop-file');
        const startInput = win.querySelector('#synth-debug-loop-start');
        const endInput = win.querySelector('#synth-debug-loop-end');
        const fpsInput = win.querySelector('#synth-debug-loop-fps');
        const loadedSpan = win.querySelector('#synth-debug-loop-loaded');

        if (fpsInput) fpsInput.value = '30';
        if (selType) {
            types.forEach((t) => {
                const opt = document.createElement('option');
                opt.value = t;
                opt.textContent = t;
                selType.appendChild(opt);
            });
        }

        async function refreshFilesForType(actionType) {
            try {
                const files = window.animationHandler ? await window.animationHandler.getAnimationsForType(actionType) : (window.animationMappings ? (window.animationMappings[actionType] || []) : []);
                if (selFile) selFile.innerHTML = '';
                if (!files || files.length === 0) {
                    if (selFile) {
                        const o = document.createElement('option');
                        o.value = '';
                        o.textContent = '— no files —';
                        selFile.appendChild(o);
                    }
                    if (loadedSpan) loadedSpan.textContent = '0';
                    return;
                }
                files.forEach((f) => {
                    const o = document.createElement('option');
                    o.value = f;
                    o.textContent = f;
                    if (selFile) selFile.appendChild(o);
                });
                if (loadedSpan) loadedSpan.textContent = String(files.length);
            } catch (err) {
                console.warn('[synth_webui] Failed to refresh debug file list:', err);
            }
        }

        if (selFile) {
            selFile.addEventListener('change', async () => { await autofillLoopInputs(); });
        }

        // Populate initial file list and bind selType change to refresh files
        (async () => {
            try {
                const initialType = (selType && selType.value) ? selType.value : 'think';
                await refreshFilesForType(initialType);
                try { if (selFile) selFile.selectedIndex = 0; } catch (e) { /* ignore */ }
                await autofillLoopInputs();
            } catch (e) { /* ignore */ }
        })();

        if (selType) {
            selType.addEventListener('change', async () => {
                try { await refreshFilesForType(selType.value); if (selFile) selFile.selectedIndex = 0; } catch (e) { /* ignore */ }
                try { await autofillLoopInputs(); } catch (e) { /* ignore */ }
            });
            const refreshBtn = win.querySelector('#synth-debug-loop-refresh');
            if (refreshBtn && selType) refreshBtn.addEventListener('click', async () => {
                await refreshFilesForType(selType.value);
                try { if (selFile) selFile.selectedIndex = 0; } catch (e) { /* ignore */ }
                await autofillLoopInputs();
            });
        }

        // Loop start / clear buttons
        const loopStartBtn = win.querySelector('#synth-debug-loop-start-btn');
        const loopClearBtn = win.querySelector('#synth-debug-loop-clear-btn');
        if (loopStartBtn) {
            loopStartBtn.addEventListener('click', async () => {
                try {
                    if (!window.animationHandler) {
                        try {
                            window.__synth_pending_actions = window.__synth_pending_actions || [];
                            // best-effort: queue a no-op action so the request isn't lost
                            window.__synth_pending_actions.push({ type: 'clearTemporaryOverride', args: [] });
                        } catch (e) { /* ignore */ }
                        return console.warn('[synth_webui] animationHandler not ready — action queued');
                    }
                    const aType = selType ? selType.value : 'think';
                    const aFile = (selFile && selFile.value) ? selFile.value : null;
                    const s = parseInt((startInput && startInput.value) ? startInput.value : '0', 10);
                    const e = parseInt((endInput && endInput.value) ? endInput.value : '0', 10);
                    const fps = parseFloat((fpsInput && fpsInput.value) ? fpsInput.value : '30');
                    if (!aFile) return alert('Please select an animation file first');
                    if (!Number.isFinite(s) || !Number.isFinite(e)) return alert('Please enter numeric frame values');
                    if (e <= s) return alert('end must be > start');
                    await window.animationHandler.startTemporaryLoop(aType, aFile, s, e, Number.isFinite(fps) ? fps : 30);
                } catch (err) { console.warn('[synth_webui] start temp loop error:', err); }
            });
        }
        if (loopClearBtn) {
            loopClearBtn.addEventListener('click', () => {
                try { if (!window.animationHandler) return; window.animationHandler.clearTemporaryOverride(); } catch (err) { console.warn('[synth_webui] clear temp loop failed:', err); }
            });
        }

        const getDescriptorForFile = (file) => {
            try {
                if (!file) return null;
                try {
                    if (window.animationHandler && window.animationHandler.loadedDescriptors) {
                        const norm = (typeof window.animationHandler._normalizeAnimationKey === 'function') ? window.animationHandler._normalizeAnimationKey(file) : String(file);
                        return window.animationHandler.loadedDescriptors[norm] || window.animationHandler.loadedDescriptors[file] || null;
                    }
                } catch (e) { /* ignore */ }
                try {
                    if (window.__synth_preloaded_animations && window.__synth_preloaded_animations[file]) return window.__synth_preloaded_animations[file];
                    if (window.__synth_pending_preloads && window.__synth_pending_preloads[file]) return window.__synth_pending_preloads[file];
                } catch (e) { /* ignore */ }
                return null;
            } catch (e) {
                return null;
            }
        };

        // Minimal helper implementations (safe fallbacks) to avoid runtime errors
        const computeMaxFramesFromDescriptor = (descriptor) => {
            try {
                if (!descriptor || typeof descriptor !== 'object') return 0;
                const nums = [];
                const pushNum = (n) => { if (Number.isFinite(n)) nums.push(Number(n)); };
                pushNum(descriptor.max_frames || descriptor.maxFrames);
                try { pushNum(descriptor.intro && descriptor.intro.end_frame); } catch (e) { /* ignore */ }
                try { pushNum(descriptor.loop && descriptor.loop.end_frame); } catch (e) { /* ignore */ }
                try { pushNum(descriptor.outro && descriptor.outro.end_frame); } catch (e) { /* ignore */ }
                return nums.length ? Math.max(0, Math.round(Math.max(...nums))) : 0;
            } catch (e) { return 0; }
        };

        const computeMaxFramesFromClip = (clip, fps) => {
            try {
                const f = Number(fps || 30);
                if (!clip || !Number.isFinite(f) || f <= 0) return 0;
                const totalFrames = Math.max(1, Math.round(Number(clip.duration || 0) * f));
                return Math.max(0, totalFrames - 1);
            } catch (e) { return 0; }
        };

        const autofillLoopInputs = async () => {
            try {
                const selFile = win.querySelector('#synth-debug-loop-file');
                const startInput = win.querySelector('#synth-debug-loop-start');
                const endInput = win.querySelector('#synth-debug-loop-end');
                const fpsInput = win.querySelector('#synth-debug-loop-fps');
                if (!selFile || !startInput || !endInput) return;
                const file = selFile.value;
                if (!file) return;
                const descriptor = (window.animationHandler && window.animationHandler.loadedDescriptors) ? (window.animationHandler.loadedDescriptors[file] || null) : null;
                const desiredFps = (descriptor && Number.isFinite(Number(descriptor.fps))) ? Number(descriptor.fps) : Number((fpsInput && fpsInput.value) ? fpsInput.value : 30);
                const fps = (Number.isFinite(desiredFps) && desiredFps > 0) ? desiredFps : 30;
                try { if (fpsInput) fpsInput.value = String(fps); } catch (e) { /* ignore */ }
                let maxFrameIndex = 0;
                try {
                    if (window.animationHandler && window.animationHandler.loadedAnimations) {
                        const norm = (typeof window.animationHandler._normalizeAnimationKey === 'function') ? window.animationHandler._normalizeAnimationKey(file) : file;
                        const clip = window.animationHandler.loadedAnimations[norm] || window.animationHandler.loadedAnimations[file] || null;
                        if (clip) maxFrameIndex = computeMaxFramesFromClip(clip, fps);
                    }
                } catch (e) { /* ignore */ }
                if (!maxFrameIndex) maxFrameIndex = computeMaxFramesFromDescriptor(descriptor);
                startInput.value = '0';
                endInput.value = String(maxFrameIndex || 0);
            } catch (e) { /* ignore */ }
        };

        // Feelings UI helpers (full impl copied from synth_webui_index)
        const extractEmotionValues = () => {
            try {
                const out = {};
                const mergeObj = (obj) => {
                    if (!obj || typeof obj !== 'object') return;
                    const values = (obj.values && typeof obj.values === 'object') ? obj.values : obj;
                    if (!values || typeof values !== 'object') return;
                    if (Array.isArray(values)) {
                        values.forEach((it) => {
                            try {
                                const name = it && (it.type || it.name) ? String(it.type || it.name) : '';
                                if (!name) return;
                                if (/^\d+$/.test(String(name))) return;
                                const raw = Number(it.intensity !== undefined ? it.intensity : it.value);
                                if (!Number.isFinite(raw)) return;
                                const v01 = (raw > 1) ? (raw / 10.0) : raw;
                                const vv = Math.max(0, Math.min(1, v01));
                                out[String(name)] = Math.max(out[String(name)] || 0, vv);
                            } catch (e) { /* ignore */ }
                        });
                        return;
                    }
                    Object.keys(values).forEach((k) => {
                        if (!k) return;
                        if (/^\d+$/.test(String(k))) return;
                        const v = Number(values[k]);
                        if (!Number.isFinite(v)) return;
                        const v01 = (v > 1) ? (v / 10.0) : v;
                        const vv = Math.max(0, Math.min(1, v01));
                        out[String(k)] = Math.max(out[String(k)] || 0, vv);
                    });
                };

                // Merge engine-provided emotions and feelings if present
                mergeObj(window.animationHandler ? (window.animationHandler._lastEmotions || null) : null);
                mergeObj(window.animationHandler ? (window.animationHandler._lastFeelings || null) : null);

                // Fallback: if animation state contains expressions (array of {type/name,intensity}), merge them too
                try {
                    const st = window.animationHandler && window.animationHandler._lastAnimationState ? window.animationHandler._lastAnimationState : null;
                    if (st && Array.isArray(st.expressions) && st.expressions.length) {
                        mergeObj({ values: st.expressions });
                    }
                } catch (e) { /* ignore */ }

                // Final fallback: if we have a cached server-side emotion snapshot from /api/emotion_state, merge it
                try {
                    if ((!Object.keys(out).length) && window.__synth_debug_cached_emotions && typeof window.__synth_debug_cached_emotions === 'object') {
                        mergeObj(window.__synth_debug_cached_emotions);
                    }
                } catch (e) { /* ignore */ }

                try { console.debug('[debug-window] extractEmotionValues ->', out); } catch (e) {}
                return out;
            } catch (e) {
                return {};
            }
        };

        const ensureFeelingsRows = (keys) => {
            try {
                if (!win) return;
                const feelingsHost = win.querySelector('#synth-debug-feelings');
                if (!feelingsHost) return;
                const sig = keys.join('|');
                if (__dbgFeelingsSig === sig && __dbgFeelingsRows.size) return;

                __dbgFeelingsSig = sig;
                __dbgFeelingsRows = new Map();
                feelingsHost.innerHTML = '';

                if (keys.length === 0) {
                    const empty = document.createElement('div');
                    empty.style.fontSize = '12px';
                    empty.style.color = 'var(--text-soft)';
                    empty.textContent = '—';
                    feelingsHost.appendChild(empty);
                    return;
                }

                keys.forEach((name) => {
                    const row = document.createElement('div');
                    row.style.display = 'grid';
                    row.style.gridTemplateColumns = '1fr 140px 56px';
                    row.style.alignItems = 'center';
                    row.style.gap = '8px';

                    const label = document.createElement('div');
                    label.style.fontSize = '12px';
                    label.style.color = 'var(--text)';
                    label.textContent = name;

                    const slider = document.createElement('input');
                    slider.type = 'range';
                    slider.min = '0';
                    slider.max = '1';
                    slider.step = '0.01';
                    slider.value = '0';

                    const num = document.createElement('input');
                    num.type = 'number';
                    num.min = '0';
                    num.max = '1';
                    num.step = '0.01';
                    num.value = '0';
                    num.style.padding = '6px';
                    num.style.borderRadius = '8px';
                    num.style.background = 'rgba(255,255,255,0.02)';
                    num.style.border = '1px solid var(--border)';
                    num.style.color = 'var(--text)';

                    const apply = (v) => {
                        const vv = clamp01(v);
                        slider.value = String(vv);
                        num.value = String(vv);
                        try {
                            // track applied emotion keys so Clear can remove local changes
                            try { if (!window.__synth_debug_applied_emotions) window.__synth_debug_applied_emotions = new Set(); } catch (e) { window.__synth_debug_applied_emotions = {}; }
                            try { if (window.__synth_debug_applied_emotions instanceof Set) window.__synth_debug_applied_emotions.add(name); } catch (e) {}

                            const helper = window.__synth_applyDebugEmotion || null;
                            if (helper) {
                                const ok = helper(name, vv);
                                if (ok) console.debug('[debug-window] applied emotion via helper', name, vv);
                                else {
                                    window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                                    window.__synth_pending_debug_actions.push({ type: 'setDebugEmotionOverride', name, value: vv });
                                    console.debug('[debug-window] queued debug emotion action (helper failed)', name, vv);
                                }
                            } else if (window.animationHandler && window.animationHandler.setDebugEmotionOverride) {
                                try { window.animationHandler.setDebugEmotionOverride(name, vv); console.debug('[debug-window] applied emotion via animationHandler', name, vv); } catch (e) { /* ignore */ }
                            } else {
                                window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                                window.__synth_pending_debug_actions.push({ type: 'setDebugEmotionOverride', name, value: vv });
                                console.debug('[debug-window] queued debug emotion action (no helper)', name, vv);
                            }
                        } catch (e) { console.warn('[debug-window] setDebugEmotionOverride failed', e); }
                    };
                    row.appendChild(label);
                    row.appendChild(slider);
                    row.appendChild(num);
                    feelingsHost.appendChild(row);

                    __dbgFeelingsRows.set(name, { slider, num });
                });
            } catch (e) { /* ignore */ }
        };

        const renderFeelings = () => {
            try {
                const feelingsHost = win.querySelector('#synth-debug-feelings');
                if (!feelingsHost) return;
                const base = extractEmotionValues();
                const overrides = (window.animationHandler && typeof window.animationHandler.getDebugEmotionOverrides === 'function') ? window.animationHandler.getDebugEmotionOverrides() : {};
                const personaKeys = (window.__synth_persona_emotions_list && Array.isArray(window.__synth_persona_emotions_list)) ? window.__synth_persona_emotions_list : [];

                try { console.debug('[debug-window] renderFeelings baseKeys=', Object.keys(base).slice(0,20), 'overrides=', Object.keys(overrides).slice(0,20), 'personaKeys=', personaKeys && personaKeys.length ? personaKeys.slice(0,20) : personaKeys); } catch (e) {}

                const keysSet = new Set();
                const cached = (window.__synth_debug_cached_emotions && typeof window.__synth_debug_cached_emotions === 'object') ? window.__synth_debug_cached_emotions : null;
                if (personaKeys && Array.isArray(personaKeys) && personaKeys.length) {
                    personaKeys.forEach(k => keysSet.add(k));
                    Object.keys(base).forEach(k => { if (personaKeys.includes(k)) keysSet.add(k); });
                    Object.keys(overrides).forEach(k => { if (personaKeys.includes(k)) keysSet.add(k); });
                } else {
                    // No persona list: prefer base and overrides, but if both are empty, use cached server snapshot
                    Object.keys(base).forEach(k => keysSet.add(k));
                    Object.keys(overrides).forEach(k => keysSet.add(k));
                    if ((!Object.keys(base).length) && (!Object.keys(overrides).length) && cached) {
                        Object.keys(cached).forEach(k => keysSet.add(k));
                        // Merge cached into base for display values
                        try { Object.keys(cached).forEach(k => { if (!base[k]) base[k] = Number(cached[k]); }); } catch (e) { /* ignore */ }
                    }
                }
                const keys = Array.from(keysSet).filter(k => k && !/^\d+$/.test(String(k))).sort();
                ensureFeelingsRows(keys);
                if (!keys.length) {
                    try { console.debug('[debug-window] renderFeelings: no keys to show'); } catch (e) {}
                    return;
                }
                keys.forEach((name) => {
                    const row = __dbgFeelingsRows.get(name);
                    if (!row) return;
                    const override = (overrides && overrides[name] !== undefined) ? clamp01(overrides[name]) : null;
                    const current = (override !== null) ? override : clamp01(base[name] || 0);

                    const active = document.activeElement;
                    const isActive = (active === row.slider || active === row.num);
                    if (!isActive || override !== null) {
                        row.slider.value = String(current);
                        row.num.value = String(current);
                    }
                });
            } catch (e) { /* ignore */ }
        };

        // Facial morph UI (full impl)
        const getFaceKeys = () => {
            try {
                const caps = window.__synth_vrm_capabilities || null;
                const keys = (caps && Array.isArray(caps.expressionKeys)) ? caps.expressionKeys : [];
                const extra = [
                    'blink','blinkLeft','blinkRight','eye_blink_left','eye_blink_right',
                    'eyes_closed','eyesClosed',
                    'eyes_wide','mouth_open','mouth_frown','brow_down','brow_up',
                    'mouth_smile','eyes_smile','mouth_O',
                    'aa','ih','ou','ee','oh',
                    'eye_look_left','eye_look_right','eye_look_up','eye_look_down'
                ];
                const rawKeys = Array.from(new Set([...(keys || []), ...extra].map(String)));
                const compositeMetrics = new Set(['valence','arousal','stress','calm','relaxed','neutral']);
                const personaEmotionKeys = (window.__synth_persona_emotions_list && Array.isArray(window.__synth_persona_emotions_list)) ? window.__synth_persona_emotions_list : [];
                const personaEmotionKeysLower = personaEmotionKeys.map(k => String(k).toLowerCase());
                const compositeEmotions = new Set([
                    'sad','happy','angry','surprised','relaxed','neutral','scared','fear','disgust','joy','love','smile',
                    'sorrow','fun','joy','anger','fear','disgust','surprise'
                ]);
                return rawKeys
                    .filter((k) => {
                        if (!k) return false;
                        const s = String(k);
                        if (/^\d+$/.test(s)) return false;
                        const low = s.toLowerCase();
                        const norm = low.replace(/[\._\-\s]+/g, '');
                        if (compositeMetrics.has(low) || compositeMetrics.has(norm)) return false;
                        if (personaEmotionKeysLower.includes(low) || personaEmotionKeysLower.includes(norm)) return false;
                        if (compositeEmotions.has(low) || compositeEmotions.has(norm)) return false;
                        return true;
                    })
                    .sort();
            } catch (e) {
                return [];
            }
        };

        let faceRows = [];
        const renderFaceList = () => {
            try {
                const faceFilter = win.querySelector('#synth-debug-face-filter');
                const faceList = win.querySelector('#synth-debug-face-list');
                if (!faceList) return;
                const filter = (faceFilter && faceFilter.value) ? String(faceFilter.value).toLowerCase() : '';
                const keys = getFaceKeys().filter((k) => !filter || k.toLowerCase().includes(filter));
                const overrides = (window.animationHandler && typeof window.animationHandler.getDebugFaceOverrides === 'function') ? window.animationHandler.getDebugFaceOverrides() : {};

                faceList.innerHTML = '';
                faceRows = [];
                if (keys.length === 0) {
                    const empty = document.createElement('div');
                    empty.style.fontSize = '12px';
                    empty.style.color = 'var(--text-soft)';
                    empty.textContent = '—';
                    faceList.appendChild(empty);
                    return;
                }

                keys.forEach((k) => {
                    const row = document.createElement('div');
                    row.style.display = 'grid';
                    row.style.gridTemplateColumns = '1fr 140px 56px 48px';
                    row.style.alignItems = 'center';
                    row.style.gap = '8px';

                    const label = document.createElement('div');
                    label.style.fontSize = '12px';
                    label.style.color = 'var(--text)';
                    label.style.overflow = 'hidden';
                    label.style.textOverflow = 'ellipsis';
                    label.style.whiteSpace = 'nowrap';
                    label.title = k;
                    label.textContent = k;

                    const slider = document.createElement('input');
                    slider.type = 'range';
                    slider.min = '0';
                    slider.max = '1';
                    slider.step = '0.01';
                    slider.value = String(clamp01((overrides[k] !== undefined) ? overrides[k] : (window.animationHandler && window.animationHandler._getFaceValue ? window.animationHandler._getFaceValue(k) : 0)));

                    const num = document.createElement('input');
                    num.type = 'number';
                    num.min = '0';
                    num.max = '1';
                    num.step = '0.01';
                    num.value = slider.value;
                    num.style.padding = '6px';
                    num.style.borderRadius = '8px';
                    num.style.background = 'rgba(255,255,255,0.02)';
                    num.style.border = '1px solid var(--border)';
                    num.style.color = 'var(--text)';

                    const cur = document.createElement('div');
                    cur.style.fontSize = '11px';
                    cur.style.color = 'var(--text-soft)';
                    cur.textContent = '—';

                    const apply = (v) => {
                        const vv = clamp01(v);
                        slider.value = String(vv);
                        num.value = String(vv);
                        try {
                            const helper = window.__synth_applyDebugFace || null;
                            if (!window.__synth_debug_applied_keys) try { window.__synth_debug_applied_keys = new Set(); } catch (e) { window.__synth_debug_applied_keys = {}; }
                            if (helper) {
                                const ok = helper(k, vv);
                                if (ok) {
                                    try { if (window.__synth_debug_applied_keys instanceof Set) window.__synth_debug_applied_keys.add(k); } catch (e) {}
                                    console.debug('[debug-window] applied via helper', k, vv);
                                } else {
                                    window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                                    window.__synth_pending_debug_actions.push({ type: 'setDebugFaceOverride', key: k, value: vv });
                                    try { if (window.__synth_debug_applied_keys instanceof Set) window.__synth_debug_applied_keys.add(k); } catch (e) {}
                                    console.debug('[debug-window] queued debug face action (helper failed)', k, vv);
                                }
                            } else if (window.animationHandler && window.animationHandler.setDebugFaceOverride) {
                                try { window.animationHandler.setDebugFaceOverride(k, vv); try { if (window.__synth_debug_applied_keys instanceof Set) window.__synth_debug_applied_keys.add(k); } catch (e) {} console.debug('[debug-window] applied via animationHandler', k, vv); } catch (e) { /* ignore */ }
                            } else {
                                // Queue the debug action until the handler/helper is available
                                try {
                                    window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                                    window.__synth_pending_debug_actions.push({ type: 'setDebugFaceOverride', key: k, value: vv });
                                    try { if (window.__synth_debug_applied_keys instanceof Set) window.__synth_debug_applied_keys.add(k); } catch (e) {}
                                    console.debug('[debug-window] queued debug face action (no helper)', k, vv);
                                } catch (e) { /* ignore */ }
                            }
                        } catch (e) { console.warn('[debug-window] apply face failed', e); }
                    };
                    slider.addEventListener('input', () => apply(slider.value));
                    num.addEventListener('change', () => apply(num.value));

                    row.appendChild(label);
                    row.appendChild(slider);
                    row.appendChild(num);
                    row.appendChild(cur);
                    faceList.appendChild(row);

                    faceRows.push({ key: k, curEl: cur, sliderEl: slider, numEl: num });
                });
            } catch (e) { /* ignore */ }
        };

        // Bind feelings & face controls
        try {
            const feelingsClearBtn = win.querySelector('#synth-debug-feelings-clear');
            if (feelingsClearBtn) {
                feelingsClearBtn.addEventListener('click', () => {
                    try {
                        // Immediate local clear of keys applied by this UI
                        try {
                            const applied = (window.__synth_debug_applied_emotions && window.__synth_debug_applied_emotions instanceof Set) ? Array.from(window.__synth_debug_applied_emotions) : [];
                            if (applied.length) {
                                try { console.debug('[debug-window] clearing local applied emotions', applied); } catch (e) {}
                                applied.forEach(n => { try { window.__synth_applyDebugEmotion ? window.__synth_applyDebugEmotion(n, null) : null; } catch (e) {} });
                                try { if (window.__synth_debug_applied_emotions instanceof Set) window.__synth_debug_applied_emotions.clear(); } catch (e) {}
                            }
                        } catch (e) { /* ignore */ }

                        if (window.animationHandler && window.animationHandler.clearDebugEmotionOverrides) {
                            try { window.animationHandler.clearDebugEmotionOverrides(); console.debug('[debug-window] invoked animationHandler.clearDebugEmotionOverrides'); } catch (e) { /* ignore */ }
                        } else {
                            window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                            window.__synth_pending_debug_actions.push({ type: 'clearDebugEmotionOverrides' });
                            console.debug('[debug-window] queued clearDebugEmotionOverrides');
                        }
                    } catch (e) { /* ignore */ }
                    __dbgFeelingsSig = '';
                    try { renderFeelings(); } catch (e) {}
                });
            }

            const faceFilter = win.querySelector('#synth-debug-face-filter');
            const faceClearBtn = win.querySelector('#synth-debug-face-clear');
            if (faceFilter) faceFilter.addEventListener('input', () => renderFaceList());
            if (faceClearBtn) {
                faceClearBtn.addEventListener('click', () => {
                    try {
                        // Immediate local clear of keys applied by this UI
                        try {
                            const appliedKeys = (window.__synth_debug_applied_keys && window.__synth_debug_applied_keys instanceof Set) ? Array.from(window.__synth_debug_applied_keys) : [];
                            if (appliedKeys.length) {
                                try { console.debug('[debug-window] clearing local applied keys', appliedKeys); } catch (e) {}
                                appliedKeys.forEach(k => {
                                    try { window.__synth_applyDebugFace ? window.__synth_applyDebugFace(k, null) : null; } catch (e) {}
                                });
                                try { if (window.__synth_debug_applied_keys instanceof Set) window.__synth_debug_applied_keys.clear(); } catch (e) {}
                            }
                        } catch (e) { /* ignore */ }

                        if (window.animationHandler && window.animationHandler.clearDebugFaceOverrides) {
                            try { window.animationHandler.clearDebugFaceOverrides(); console.debug('[debug-window] invoked animationHandler.clearDebugFaceOverrides'); } catch (e) { /* ignore */ }
                        } else {
                            window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                            window.__synth_pending_debug_actions.push({ type: 'clearDebugFaceOverrides' });
                            console.debug('[debug-window] queued clearDebugFaceOverrides');
                        }
                    } catch (e) { /* ignore */ }
                    try { renderFaceList(); } catch (e) {}
                });
            }
        } catch (e) { /* ignore */ }

        // Live status updater
        try {
            const stPaused = win.querySelector('#synth-debug-status-paused');
            const stCurrent = win.querySelector('#synth-debug-status-current');
            const stPhase = win.querySelector('#synth-debug-status-phase');
            const stFrame = win.querySelector('#synth-debug-status-frame');
            const stRemote = win.querySelector('#synth-debug-status-remote');

            setInterval(() => {
                try {
                    if (stPaused) stPaused.textContent = isPaused() ? 'yes' : 'no';
                    if (!window.animationHandler || !window.animationHandler.currentAction) {
                        if (stCurrent) stCurrent.textContent = '—';
                        if (stPhase) stPhase.textContent = '—';
                        if (stFrame) stFrame.textContent = '—';
                    } else {
                        const act = window.animationHandler.currentAction;
                        const clip = act.getClip ? act.getClip() : null;
                        if (stCurrent) stCurrent.textContent = (window.animationHandler.currentActionName || clip?.name || 'unknown');
                        if (stPhase) stPhase.textContent = window.animationHandler.currentActionPhase || '—';
                        if (clip && clip._meta?.loopFrames && Number.isFinite(act.time)) {
                            const lm = clip._meta.loopFrames;
                            const fps = Number(lm.fps) || 30;
                            const span = Math.max(1, (Number(lm.endFrame) - Number(lm.startFrame)) + 1);
                            const localFrame = Math.floor(act.time * fps);
                            const currentFrame = Number(lm.startFrame) + ((localFrame % span) + span) % span;
                            if (stFrame) stFrame.textContent = `${currentFrame}f / ${lm.startFrame}-${lm.endFrame}`;
                        } else if (clip && Number.isFinite(act.time)) {
                            const fps = 30;
                            const totalFrames = Math.max(1, Math.round(Number(clip.duration || 0) * fps));
                            const maxIdx = Math.max(0, totalFrames - 1);
                            const currentFrame = Math.min(maxIdx, Math.max(0, Math.floor(act.time * fps)));
                            if (stFrame) stFrame.textContent = `${currentFrame}f / 0-${maxIdx}`;
                        } else {
                            if (stFrame) stFrame.textContent = '—';
                        }
                    }
                    try {
                        const s = window.__synth_current_animation_state || null;
                        if (stRemote) stRemote.textContent = (s && (s.state || s.animation)) ? `${s.state || '—'} · ${(s.animation || '—')}` : '—';
                    } catch (e) { if (stRemote) stRemote.textContent = '—'; }

                    // Try a lazy autofill of loop end if still empty/0 (after preload/handler becomes ready)
                    try {
                        const selFile = win.querySelector('#synth-debug-loop-file');
                        const endInput = win.querySelector('#synth-debug-loop-end');
                        if (selFile && endInput && (String(endInput.value || '') === '' || String(endInput.value || '') === '0') && selFile.value) {
                            autofillLoopInputs();
                        }
                    } catch (e) { /* ignore */ }

                    // refresh feelings + face current values
                    renderFeelings();
                    if (window.animationHandler && faceRows && faceRows.length) {
                        faceRows.forEach((r) => {
                            try {
                                const vv = window.animationHandler._getFaceValue ? window.animationHandler._getFaceValue(r.key) : 0;
                                if (r.curEl) r.curEl.textContent = Number.isFinite(vv) ? vv.toFixed(2) : '—';
                            } catch (e) { /* ignore */ }
                        });
                    }
                } catch (e) { /* ignore */ }
            }, 300);

            async function resetLoopOverrideUI() {
                try {
                    const selType = win.querySelector('#synth-debug-loop-type');
                    const selFile = win.querySelector('#synth-debug-loop-file');
                    const endInput = win.querySelector('#synth-debug-loop-end');
                    if (!selType || !selFile) return;
                    try { selType.value = 'think'; } catch (e) { /* ignore */ }
                    await refreshFilesForType('think');
                    try { if (selFile) selFile.selectedIndex = 0; } catch (e) { /* ignore */ }
                    await autofillLoopInputs();
                } catch (e) { /* ignore */ }
            }

            // Expose helper for external code (backwards-compatibility)
            try { window.resetLoopOverrideUI = resetLoopOverrideUI; } catch (e) { /* ignore */ }
            try { window.__synth_debug_resyncFromBackend = resyncFromBackend; } catch (e) { /* ignore */ }

            // Initial render
            renderFeelings();
            renderFaceList();
            try { if (pauseBtn) pauseBtn.textContent = isPaused() ? 'Resume' : 'Pause'; } catch (e) { /* ignore */ }

            // Try an initial resync from backend to populate feelings/animation state
            try { resyncFromBackend().catch(e => {/*ignore*/}); } catch (e) { /* ignore */ }

            // VRM capabilities arrive after load: refresh face keys when the avatar loads.
            try {
                if (!window.__synth_debug_on_vrm_loaded) {
                    window.__synth_debug_on_vrm_loaded = () => {
                        try { renderFaceList(); } catch (e) { /* ignore */ }
                        try { __dbgFeelingsSig = ''; renderFeelings(); } catch (e) { /* ignore */ }
                        try { autofillLoopInputs(); } catch (e) { /* ignore */ }
                    };
                    window.addEventListener('vrmLoaded', window.__synth_debug_on_vrm_loaded);
                }
            } catch (e) { /* ignore */ }

            // Periodic background: attempt to flush any queued debug actions when the
            // handler/helper becomes available and periodically resync feelings.
            try {
                if (!window.__synth_debug_interval_set) {
                    window.__synth_debug_interval_set = true;
                    setInterval(() => {
                        try {
                            const q = window.__synth_pending_debug_actions || [];
                            if (q && Array.isArray(q) && q.length) {
                                try { console.debug('[debug-window] attempting flush of', q.length, 'pending debug actions'); } catch (e) {}
                                // Diagnostic: log animationHandler presence and override keys
                                try { console.debug('[debug-window] flush check: animationHandler=', !!window.animationHandler, 'hasClear=', !!(window.animationHandler && window.animationHandler.clearDebugFaceOverrides), '_debugFaceOverridesKeys=', Object.keys(window.animationHandler && window.animationHandler._debugFaceOverrides ? window.animationHandler._debugFaceOverrides : {}).slice(0,20)); } catch (e) { /* ignore */ }

                                const helper = window.__synth_applyDebugFace || null;
                                const newQ = [];
                                q.forEach((act) => {
                                    try {
                                        let applied = false;
                                        if (!act || !act.type) return;

                                        // Apply face override actions
                                        if (act.type === 'setDebugFaceOverride') {
                                            if (helper) {
                                                try { applied = !!helper(act.key, act.value); } catch (e) { applied = false; }
                                            } else if (window.animationHandler && typeof window.animationHandler.setDebugFaceOverride === 'function') {
                                                try { window.animationHandler.setDebugFaceOverride(act.key, act.value); applied = true; } catch (e) { applied = false; }
                                            }
                                        }

                                        // Handle clear action
                                        else if (act.type === 'clearDebugFaceOverrides') {
                                            try {
                                                if (window.animationHandler && typeof window.animationHandler.clearDebugFaceOverrides === 'function') {
                                                    window.animationHandler.clearDebugFaceOverrides();
                                                    applied = true;
                                                    try { console.debug('[debug-window] applied clearDebugFaceOverrides via animationHandler'); } catch (e) {}
                                                } else if (window.__synth_applyDebugFace) {
                                                    // as fallback, iterate known keys and clear them
                                                    try {
                                                        const keys = Object.keys(window.animationHandler && window.animationHandler._debugFaceOverrides ? window.animationHandler._debugFaceOverrides : {});
                                                        keys.forEach(k => { try { window.__synth_applyDebugFace(k, null); } catch (e) {} });
                                                        applied = keys.length > 0;
                                                        if (applied) try { console.debug('[debug-window] applied clearDebugFaceOverrides via helper on keys', keys); } catch (e) {}
                                                    } catch (e) { /* ignore */ }
                                                }
                                            } catch (e) { /* ignore */ }
                                        }

                                        if (!applied) newQ.push(act);
                                    } catch (e) { newQ.push(act); }
                                });
                                try { window.__synth_pending_debug_actions = newQ; } catch (e) { /* ignore */ }
                            }

                            // If feelings/base keys are empty or persona keys are missing, try to fetch server-side emotion snapshot
                            try {
                                const base = extractEmotionValues();
                                const personaKeys = (window.__synth_persona_emotions_list && Array.isArray(window.__synth_persona_emotions_list)) ? window.__synth_persona_emotions_list : [];
                                const needFetch = (!base || !Object.keys(base).length) && (!personaKeys || !personaKeys.length);
                                if (needFetch) {
                                    (async () => {
                                        try {
                                            const r = await fetch('/api/emotion_state');
                                            if (r && r.ok) {
                                                const j = await r.json();
                                                if (j && j.emotions && typeof j.emotions === 'object') {
                                                    window.__synth_debug_cached_emotions = j.emotions;
                                                    try { console.debug('[debug-window] fetched /api/emotion_state keys=', Object.keys(j.emotions).slice(0,20)); } catch (e) {}
                                                    // trigger immediate re-render
                                                    try { renderFeelings(); } catch (e) {}
                                                }
                                            }
                                        } catch (e) { /* ignore */ }
                                    })();
                                }
                            } catch (e) { /* ignore */ }

                        } catch (e) { /* ignore */ }

                        try { resyncFromBackend().catch(e => {/*ignore*/}); } catch (e) { /* ignore */ }
                    }, 2000);
                }
            } catch (e) { /* ignore */ }

        } catch (err) {
            console.warn('[debug-window] init debug panel failed:', err);
        }

        return win || winbox || null;
    } catch (err) {
        console.warn('[debug-window] createDebugWindow failed:', err);
        return null;
    }
}

// Register on global for backward compatibility
try { window.DebugWindow = window.DebugWindow || {}; window.DebugWindow.createDebugWindow = createDebugWindow; } catch (e) { /* ignore */ }

export default { createDebugWindow };
