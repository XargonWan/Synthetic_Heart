// debug-window.mjs — Extracted Debug window logic
export function createDebugWindow() {
    try {
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
                <div id="synth-debug-title" style="font-weight:600;">Antimation Pause</div>
                <div id="synth-debug-header-tools" style="display:flex;gap:6px;align-items:center;"></div>
            </div>
            <div id="synth-debug-body" style="padding:12px;display:flex;flex-direction:column;gap:12px;overflow:auto;height:calc(100% - 52px);">

                <div style="display:flex;gap:8px;align-items:center;justify-content:flex-start;">
                    <div id="synth-debug-controls" style="display:flex;gap:8px;align-items:center;">
                        <button id="synth-debug-pause" class="pill secondary" type="button" title="⏸️" aria-label="Pause">⏸️</button>
                        <button id="synth-debug-resync" class="pill secondary" type="button" title="Sync">🛜</button>
                        <button id="synth-debug-reset" class="pill" type="button" title="Reset">🔁</button>
                        <button id="synth-debug-minimize" class="pill secondary" type="button" title="Minimize">➖</button>
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
                title: 'Antimation Pause',
                mount: panel,
                width: 420,
                height: 520,
                x: 24,
                y: 'bottom',
                dockLabel: 'Restore Debug',
                dockClass: 'chat-toggle-btn',
                className: 'synth-winbox no-full'
            });
            try {
                if (window.SynthWindowManager && typeof window.SynthWindowManager.attachHeaderTools === 'function') {
                    const pauseBtn = panel.querySelector('#synth-debug-pause');
                    const resyncBtn = panel.querySelector('#synth-debug-resync');
                    const resetBtn = panel.querySelector('#synth-debug-reset');
                    // Add a native-like minimize control and a native pause control to the WinBox header
                    window.SynthWindowManager.attachHeaderTools('debug', winbox, [
                        {
                            label: '–',
                            title: 'Minimize',
                            className: 'synth-wb-tool-minimize',
                            onClick: () => { try { window.SynthWindowManager.minimize('debug'); } catch (e) { /* ignore */ } }
                        },
                        {
                            label: '⏸️',
                            title: '⏸️',
                            className: 'synth-wb-tool-pause',
                            onClick: () => { try { if (pauseBtn) pauseBtn.click(); } catch (e) { /* ignore */ } }
                        }
                    ]);
                }
            } catch (e) { /* ignore */ }
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
                const chatToggleBtn = document.getElementById('chat-toggle');
                if (chatToggleBtn && chatToggleBtn.parentElement !== dock) {
                    dock.appendChild(chatToggleBtn);
                    try { chatToggleBtn.style.position = 'static'; chatToggleBtn.style.right = ''; chatToggleBtn.style.bottom = ''; } catch (e) { /* ignore */ }
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

        let __dbgFeelingsSig = '';
        let __dbgFeelingsRows = new Map();

        const renderFeelings = () => {
            try {
                const feelingsHost = win.querySelector('#synth-debug-feelings');
                if (!feelingsHost) return;
                // Minimal placeholder: show an empty state until real data is available
                feelingsHost.innerHTML = '';
                const empty = document.createElement('div');
                empty.style.fontSize = '12px';
                empty.style.color = 'var(--text-soft)';
                empty.textContent = '—';
                feelingsHost.appendChild(empty);
            } catch (e) { /* ignore */ }
        };

        const renderFaceList = () => {
            try {
                const faceList = win.querySelector('#synth-debug-face-list');
                if (!faceList) return;
                faceList.innerHTML = '';
                const empty = document.createElement('div');
                empty.style.fontSize = '12px';
                empty.style.color = 'var(--text-soft)';
                empty.textContent = '—';
                faceList.appendChild(empty);
            } catch (e) { /* ignore */ }
        };

        // Initial render
        try { renderFeelings && renderFeelings(); } catch (e) { /* ignore */ }
        try { renderFaceList && renderFaceList(); } catch (e) { /* ignore */ }
        try { if (pauseBtn) pauseBtn.textContent = isPaused() ? '▶️' : '⏸️'; } catch (e) { /* ignore */ }

        try {
            if (!window.__synth_debug_on_vrm_loaded) {
                window.__synth_debug_on_vrm_loaded = () => {
                    try { renderFaceList && renderFaceList(); } catch (e) { /* ignore */ }
                    try { __dbgFeelingsSig = ''; renderFeelings && renderFeelings(); } catch (e) { /* ignore */ }
                    try { autofillLoopInputs && autofillLoopInputs(); } catch (e) { /* ignore */ }
                };
                window.addEventListener('vrmLoaded', window.__synth_debug_on_vrm_loaded);
            }
        } catch (e) { /* ignore */ }

        return win || winbox || null;
    } catch (err) {
        console.warn('[debug-window] createDebugWindow failed:', err);
        return null;
    }
}

// Register on global for backward compatibility
try { window.DebugWindow = window.DebugWindow || {}; window.DebugWindow.createDebugWindow = createDebugWindow; } catch (e) { /* ignore */ }

export default { createDebugWindow };
