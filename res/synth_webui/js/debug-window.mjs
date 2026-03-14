// debug-window.mjs — Extracted Debug window logic
export function createDebugWindow() {
    try {
        console.log('[debug-window] module loaded');
        // compute API base once for the entire module
        const _apiBase = (window.__getApiBase && window.__getApiBase()) || '';
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
                    <div style="font-size:12px;color:var(--text-soft);margin-right:6px;">Animation</div>
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
                        <div style="display:flex;flex-direction:column;gap:4px;flex:1 1 120px;min-width:100px;">
                            <label style="font-size:11px;color:var(--text-soft);">Start frame</label>
                            <input id="synth-debug-loop-start" type="number" placeholder="start" style="padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);" />
                        </div>
                        <div style="display:flex;flex-direction:column;gap:4px;flex:1 1 120px;min-width:100px;">
                            <label style="font-size:11px;color:var(--text-soft);">End frame</label>
                            <input id="synth-debug-loop-end" type="number" placeholder="end" style="padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);" />
                        </div>
                        <div style="display:flex;flex-direction:column;gap:4px;flex:0 0 86px;min-width:86px;">
                            <label style="font-size:11px;color:var(--text-soft);">FPS</label>
                            <input id="synth-debug-loop-fps" type="number" placeholder="fps" style="padding:6px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid var(--border);color:var(--text);" />
                        </div>
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
                    <div id="synth-debug-feelings-warning" style="display:none;margin-bottom:8px;padding:6px 10px;background:rgba(255,165,0,0.1);border:1px solid rgba(255,165,0,0.3);border-radius:8px;font-size:11px;color:#ffa500;line-height:1.3;">
                        ⚠️ VRM 0.x detected. Feeling overrides support is not yet stable for this model version.
                    </div>
                    <div id="synth-debug-feelings-list" style="max-height:240px;overflow:auto;display:flex;flex-direction:column;gap:8px;"></div>
                    <div style="display:flex;gap:8px;margin-top:8px;">
                        <button id="synth-debug-feelings-reset-upstream" class="pill" type="button" style="flex:1;">Reset Upstream</button>
                        <button id="synth-debug-feelings-reset-zero" class="pill secondary" type="button" style="flex:1;">Reset All 0</button>
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
            try {
                if (!window.SynthWindowManager || typeof window.SynthWindowManager.create !== 'function') return null;
                const panel = buildDebugPanel();
                win = panel;
                const createNow = () => {
                    try {
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
                        // Show WinBox by default when debug is enabled so it is visible to the user
                        try { if (winbox && typeof winbox.show === 'function') winbox.show(); } catch (e) {}
                    } catch (e) {
                        console.warn('[debug-window] WinBox creation failed', e);
                        winbox = null;
                    }
                };

                if (typeof window.WinBox === 'undefined' && typeof window.SynthWindowManager.ensureWinBoxAssets === 'function') {
                    // Load WinBox assets and create when ready
                    window.SynthWindowManager.ensureWinBoxAssets().then((ok) => { if (ok) createNow(); }).catch(() => {});
                    return null;
                }
                if (typeof window.WinBox === 'undefined') return null;
                createNow();
                try { renderHeaderToolsIntoWinBox(); } catch (e) { /* ignore */ }
                return winbox;
            } catch (e) { return null; }
        };

        if (!tryCreateWinBox()) {
            // WinBox not available synchronously — create a visible DOM fallback so the Debug panel is usable.
            try {
                win = buildDebugPanel();
                // Default desktop styling
                win.style.position = 'fixed';
                win.style.right = '18px';
                win.style.bottom = '86px';
                win.style.width = '420px';
                win.style.maxWidth = 'calc(100% - 40px)';
                win.style.minWidth = '320px';
                win.style.height = '520px';
                win.style.maxHeight = 'calc(100vh - 140px)';
                win.style.minHeight = '240px';
                win.style.zIndex = 999999;
                win.style.background = 'var(--surface)';
                win.style.color = 'var(--text)';
                win.style.border = '1px solid var(--border)';
                win.style.borderRadius = '12px';
                win.style.overflow = 'hidden';
                win.style.resize = 'both';
                document.body.appendChild(win);

                // Legacy fallback remains a normal fixed-size panel (no mobile fullscreen override to avoid custom mobile window behavior).

                // Initialize interactions that expect a legacy DOM window (minimize/drag/resize)
                try { createResizeHandlesForElement(win); } catch (e) { /* ignore */ }
                (function makeDraggable(el) {
                    const header = el.querySelector('#synth-debug-title-bar');
                    if (!header) return;
                    let dragging = false;
                    let startX = 0, startY = 0;
                    let offsetX = 0, offsetY = 0;
                    header.addEventListener('pointerdown', (ev) => {
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

            } catch (e) {
                console.warn('[debug-window] WinBox not available and fallback creation failed', e);
                try { if (window.SynthWindowManager && typeof window.SynthWindowManager.ensureWinBoxAssets === 'function') window.SynthWindowManager.ensureWinBoxAssets().then((ok) => { if (ok) tryCreateWinBox(); }); } catch (ex) {}
            }
        }

        if (!winbox) {
            // Legacy DOM-managed debug window removed — WinBox is the canonical window manager.
            // If WinBox becomes available later, the panel will be created automatically via ensureWinBoxAssets.
            console.warn('[debug-window] running without WinBox; legacy DOM window support removed.');
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

        async function resyncFromBackend(force = false) {
            try {
                if (!window.animationHandler) return;
                if (isPaused() && !force) return;
                try {
                    const st = window.__synth_last_rich_animation_state || null;
                    if (st && typeof window.animationHandler.applyAnimationState === 'function') {
                        window.animationHandler.applyAnimationState(st);
                    }
                } catch (e) { /* ignore */ }
                try {
                    const resp = await fetch(_apiBase + '/api/animation_state');
                    if (resp && resp.ok) {
                        const summary = await resp.json();
                        if (summary && summary.state) {
                            // Skip startAction if the animation_id hasn't changed.
                            // This prevents restarting an already-running animation on
                            // every polling cycle (every 2 s when debug-window is open).
                            const serverId = summary.animation_id || null;
                            if (serverId && window.__synth_current_animation_id && serverId === window.__synth_current_animation_id) {
                                return;
                            }
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



        // Attach header tools both for WinBox-managed header and legacy DOM header area
        const renderHeaderToolsIntoWinBox = () => {
            try {
                if (!winbox) return;
                const winEl = winbox.window || winbox.dom || winbox.g || null;
                if (!winEl) return;
                const drag = winEl.querySelector('.wb-drag');
                if (!drag) return;
                let toolsEl = drag.querySelector('.synth-wb-tools[data-tools-id="debug"]');
                if (!toolsEl) {
                    toolsEl = document.createElement('div');
                    toolsEl.className = 'synth-wb-tools';
                    toolsEl.dataset.toolsId = 'debug';
                    drag.appendChild(toolsEl);
                }
                toolsEl.innerHTML = '';
                const addBtn = (label, title, clickFn, cls) => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'synth-wb-tool-btn' + (cls ? (' ' + cls) : '');
                    btn.textContent = label;
                    if (title) { btn.title = title; btn.setAttribute('aria-label', title); }
                    btn.addEventListener('pointerdown', (ev) => { try { ev.stopPropagation(); } catch (e) {} });
                    btn.addEventListener('click', (ev) => { try { ev.stopPropagation(); } catch (e) {} try { if (typeof clickFn === 'function') clickFn(); } catch (e) {} });
                    toolsEl.appendChild(btn);
                };
                // No header tools for Debug (buttons removed)
                // Rendered header area intentionally kept empty to avoid duplicate controls.
            } catch (e) { /* ignore */ }
        };

        const renderHeaderToolsIntoDOM = () => {
            try {
                const headerTools = (win && win.querySelector) ? win.querySelector('#synth-debug-header-tools') : null;
                if (!headerTools) return;
                headerTools.innerHTML = '';
                const addBtn = (label, title, clickFn, cls) => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'synth-wb-tool-btn' + (cls ? (' ' + cls) : '');
                    btn.textContent = label;
                    if (title) { btn.title = title; btn.setAttribute('aria-label', title); }
                    btn.addEventListener('click', (ev) => { try { ev.stopPropagation(); } catch (e) {} try { if (typeof clickFn === 'function') clickFn(); } catch (e) {} });
                    headerTools.appendChild(btn);
                };
                // No header tools for Debug (buttons removed)
            } catch (e) { /* ignore */ }
        };

        // Try to render header tools now and again after WinBox becomes available or DOM ready
        try { renderHeaderToolsIntoDOM(); } catch (e) {}
        try { renderHeaderToolsIntoWinBox(); } catch (e) {}
        setTimeout(() => { try { renderHeaderToolsIntoWinBox(); } catch (e) {} try { renderHeaderToolsIntoDOM(); } catch (e) {} }, 300);
        window.addEventListener('synth-winbox-ready', () => { try { renderHeaderToolsIntoWinBox(); } catch (e) {} });

        // Ensure that if advanced debug UI failed to appear we fall back to the inline debug overlay
        setTimeout(() => {
            try {
                const adv = document.getElementById('synth-advanced-debug');
                const debugRoot = document.getElementById('synth-debug');
                const advVisible = adv && (adv.offsetParent !== null || getComputedStyle(adv).display !== 'none');
                if (!advVisible && debugRoot) {
                    try { debugRoot.style.display = 'block'; } catch (e) {}
                    try { debugRoot.setAttribute('data-debug-enabled', '1'); } catch (e) {}
                    try { if (typeof updateDebug === 'function') updateDebug('Advanced debug unavailable — falling back to inline overlay'); } catch (e) {}
                    console.warn('[debug-window] Advanced debug UI not visible, falling back to inline overlay');
                }
            } catch (e) { /* ignore */ }
        }, 800);

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


            if (!paused) {
                await resyncFromBackend();
            }
        };

        

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
                try { if (loadedSpan) loadedSpan.textContent = 'Loading...'; } catch (e) {}
                try { console.debug('[debug-window] refreshFilesForType called', { actionType, hasHandler: !!window.animationHandler, hasWindowAnimationMappings: !!window.animationMappings, hasVRMAnimationMappings: !!window.VRMAnimationMappings }); } catch (e) {}

                // Prefer using the live handler if available
                let files = null;
                let source = null;
                try {
                    if (window.animationHandler && typeof window.animationHandler.getAnimationsForType === 'function') {
                        files = await window.animationHandler.getAnimationsForType(actionType);
                        source = 'handler';
                    }
                } catch (e) { /* ignore */ }

                // If handler unavailable or returned nothing, check global mappings (VRMAnimationMappings preferred)
                try {
                    if ((!files || !Array.isArray(files) || files.length === 0) && window.VRMAnimationMappings && typeof window.VRMAnimationMappings === 'object') {
                        // Determine skin: prefer activeSkinName, else first key in mappings, else null
                        const skinFromActive = (window.activeSkinName && String(window.activeSkinName).split('/').pop().replace('.vrm','')) ? String(window.activeSkinName).split('/').pop().replace('.vrm','') : null;
                        const skin = skinFromActive || Object.keys(window.VRMAnimationMappings || {})[0] || 'Rei';
                        try { files = window.VRMAnimationMappings[skin] && Array.isArray(window.VRMAnimationMappings[skin][actionType]) ? window.VRMAnimationMappings[skin][actionType] : null; } catch (e) { files = null; }
                        if (Array.isArray(files) && files.length) source = `mapping:${skin}`;
                    }
                } catch (e) { /* ignore */ }

                // Final fallback: call server API directly
                try {
                    if ((!files || !Array.isArray(files) || files.length === 0)) {
                        // Derive skin for API call
                        const skinFromActive = (window.activeSkinName && String(window.activeSkinName).split('/').pop().replace('.vrm','')) ? String(window.activeSkinName).split('/').pop().replace('.vrm','') : null;
                        const skin = skinFromActive || (window.VRMAnimationMappings && Object.keys(window.VRMAnimationMappings || {})[0]) || 'Rei';
                        try {
                            const resp = await fetch(_apiBase + `/api/animations/${encodeURIComponent(skin)}/${encodeURIComponent(actionType)}`);
                            if (resp && resp.ok) {
                                const j = await resp.json();
                                if (j && Array.isArray(j.animations)) {
                                    files = j.animations;
                                    source = `api:${skin}`;
                                }
                            } else {
                                try { console.debug('[debug-window] /api/animations returned non-ok status', resp && resp.status); } catch (e) {}
                            }
                        } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }

                try { console.debug('[debug-window] refreshFilesForType result', { actionType, source: source, files: Array.isArray(files) ? files.slice(0,20) : files, count: Array.isArray(files) ? files.length : 0 }); } catch (e) {}
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
                // Auto-select first file and autofill frame inputs so the UI is ready to Start
                try { if (selFile && selFile.options && selFile.options.length) selFile.selectedIndex = 0; } catch (e) { /* ignore */ }
                try { await autofillLoopInputs(); } catch (e) { /* ignore */ }
            } catch (err) {
                console.warn('[synth_webui] Failed to refresh debug file list:', err);
                try { if (loadedSpan) loadedSpan.textContent = '0'; } catch (e) {}
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
                    console.debug('[debug-window] loop Start clicked', { hasHandler: !!window.animationHandler, selType: selType && selType.value, selFile: selFile && selFile.value, start: startInput && startInput.value, end: endInput && endInput.value, fps: fpsInput && fpsInput.value });

                    // Compute the target args early so they can be queued if the handler is not ready
                    const aType = selType ? selType.value : 'think';
                    let aFile = (selFile && selFile.value) ? selFile.value : null;
                    // Defensive: if no value but options exist, pick the first non-empty option
                    try {
                        if (!aFile && selFile && selFile.options && selFile.options.length) {
                            for (let i=0;i<selFile.options.length;i++) {
                                const v = String(selFile.options[i].value || '').trim();
                                if (v) { selFile.selectedIndex = i; aFile = v; break; }
                            }
                        }
                    } catch (e) { /* ignore */ }
                    let s = parseInt((startInput && startInput.value) ? startInput.value : '0', 10);
                    const fps = parseFloat((fpsInput && fpsInput.value) ? fpsInput.value : '30');
                    const eRaw = (endInput && String(endInput.value).trim() !== '') ? parseInt(endInput.value, 10) : NaN;
                    let e = Number.isFinite(eRaw) ? eRaw : NaN;

                    // Always compute end from the animation max frame when starting the debug loop
                    let descriptor = null;
                    let useFps = 30;
                    try {
                        let computedMax = 0;
                        descriptor = (window.animationHandler && window.animationHandler.loadedDescriptors) ? (window.animationHandler.loadedDescriptors[aFile] || null) : null;
                        const desiredFps = (descriptor && Number.isFinite(Number(descriptor.fps))) ? Number(descriptor.fps) : Number(fps || 30);
                        useFps = (Number.isFinite(desiredFps) && desiredFps > 0) ? desiredFps : 30;
                        try {
                            if (window.animationHandler && window.animationHandler.loadedAnimations) {
                                const norm = (typeof window.animationHandler._normalizeAnimationKey === 'function') ? window.animationHandler._normalizeAnimationKey(aFile) : aFile;
                                const clip = window.animationHandler.loadedAnimations[norm] || window.animationHandler.loadedAnimations[aFile] || null;
                                if (clip) computedMax = computeMaxFramesFromClip(clip, useFps);
                            }
                        } catch (e2) { /* ignore */ }
                        if (!computedMax) computedMax = computeMaxFramesFromDescriptor(descriptor);
                        if (computedMax >= 0) {
                            e = computedMax;
                            try { if (endInput) endInput.value = String(e); } catch (e) { /* ignore */ }
                        }
                    } catch (e3) { /* ignore */ }

                    // Default start to 0 when invalid
                    if (!Number.isFinite(s) || s < 0) s = 0;

                    // Basic validation before queuing
                    if (!aFile) {
                        alert('Please select an animation file first');
                        try { console.debug('[debug-window] No animation file selected; selFile.options=', selFile && selFile.options ? Array.from(selFile.options).map(o=>o.value) : null); } catch (e) {}
                        return;
                    }

                    // Ensure UI inputs are populated with a sensible fallback immediately so the user sees values
                    try {
                        if (startInput && String(startInput.value || '').trim() === '') {
                            startInput.value = '0';
                            s = 0;
                        }
                        if (endInput && String(endInput.value || '').trim() === '') {
                            const fallback = Math.max(1, (Number.isFinite(s) ? (s + 1) : 1));
                            endInput.value = String(fallback);
                            e = fallback;
                            console.debug('[debug-window] Pre-filled endInput with fallback', { aFile, s, e: fallback });
                        }
                    } catch (e) { /* ignore */ }

                    if (!window.animationHandler) {
                        // If end not available or <= start, pick a fallback end = start+1 (or 1) so we can queue a preview
                        if (!Number.isFinite(e) || e <= s) {
                            const fallback = Math.max(1, (Number.isFinite(s) ? (s + 1) : 1));
                            console.debug('[debug-window] Using fallback end frame (queued)', { aFile, s, fallback });
                            e = fallback;
                            try { if (endInput) endInput.value = String(e); } catch (e) { /* ignore */ }
                        }
                        if (!Number.isFinite(s) || !Number.isFinite(e)) return alert('Please enter numeric frame values');
                        try {
                            // Preview semantics: visually indicate Preview (handler will run it when ready). Keep the queued action so it will execute later, but label it 'Preview' to reflect intent.
                            window.__synth_pending_actions = window.__synth_pending_actions || [];
                            window.__synth_pending_actions.push({ type: 'startTemporaryLoop', args: [aType, aFile, s, e, Number.isFinite(fps) ? fps : 30] });
                            try { console.debug('[debug-window] preview queued startTemporaryLoop action', { aType, aFile, s, e, fps }); } catch (e) {}
                            // Provide visual feedback: disable start button and mark as Preview
                            try { if (loopStartBtn) { loopStartBtn.disabled = true; loopStartBtn.textContent = 'Preview'; loopStartBtn.title = 'Preview (will start when animation handler is ready)'; } } catch (e) {}
                        } catch (e) { /* ignore */ }
                        return console.warn('[synth_webui] animationHandler not ready — preview queued');
                    }

                    try {
                        // Try to ensure the requested clip is actually loadable first - call loadAnimation if available
                        let clipOk = false;
                        let loadedClip = null;
                        try {
                            if (typeof window.animationHandler.loadAnimation === 'function') {
                                try {
                                    const loaded = await window.animationHandler.loadAnimation(aType, aFile);
                                    console.debug('[debug-window] loadAnimation pre-check result', { aFile, loaded: !!loaded, duration: loaded && loaded.duration });
                                    clipOk = !!loaded;
                                    loadedClip = loaded || null;
                                } catch (e) {
                                    console.debug('[debug-window] loadAnimation threw during pre-check', e);
                                    clipOk = false;
                                }
                            }
                        } catch (e) { /* ignore */ }

                        // If not loadable by filename, attempt a full-path fallback using detected skin
                        if (!clipOk) {
                            try {
                                const skin = (window.activeSkinName && String(window.activeSkinName).split('/').pop().replace('.vrm','')) ? String(window.activeSkinName).split('/').pop().replace('.vrm','') : 'Rei';
                                const candidatePath = `/skins/${skin}/animations/${aType}/${encodeURIComponent(aFile)}`;
                                try {
                                    const loaded2 = await window.animationHandler.loadAnimation(aType, candidatePath);
                                    console.debug('[debug-window] loadAnimation fallback result', { candidatePath, loaded: !!loaded2 });
                                    clipOk = !!loaded2;
                                    loadedClip = loaded2 || loadedClip;
                                    if (clipOk) {
                                        // If successful, replace the aFile with the full path for the start call
                                        aFile = candidatePath;
                                    }
                                } catch (e) {
                                    console.debug('[debug-window] loadAnimation fallback threw', e);
                                }
                            } catch (e) { /* ignore */ }
                        }

                        if (!clipOk) {
                            alert('Failed to load animation clip. Check console for details.');
                            console.warn('[debug-window] Aborting startTemporaryLoop - clip not found', { aType, aFile });
                            return;
                        }

                        // Recompute end frame from the loaded clip (authoritative) before starting
                        try {
                            let computedMax = 0;
                            const effectiveFps = (Number.isFinite(useFps) && useFps > 0) ? useFps : 30;
                            if (loadedClip) {
                                computedMax = computeMaxFramesFromClip(loadedClip, effectiveFps);
                            } else if (window.animationHandler && window.animationHandler.loadedAnimations) {
                                const norm = (typeof window.animationHandler._normalizeAnimationKey === 'function') ? window.animationHandler._normalizeAnimationKey(aFile) : aFile;
                                const clip = window.animationHandler.loadedAnimations[norm] || window.animationHandler.loadedAnimations[aFile] || null;
                                if (clip) computedMax = computeMaxFramesFromClip(clip, effectiveFps);
                            }
                            if (!computedMax) computedMax = computeMaxFramesFromDescriptor(descriptor);
                            if (computedMax >= 0) {
                                e = computedMax;
                                try { if (endInput) endInput.value = String(e); } catch (e) { /* ignore */ }
                            }
                        } catch (e) { /* ignore */ }

                        // If computation failed or computed end <= start, use a fallback end frame
                        if (!Number.isFinite(e) || e <= s) {
                            const fallback = Math.max(1, (Number.isFinite(s) ? (s + 1) : 1));
                            console.debug('[debug-window] Using fallback end frame (post-load)', { aFile, s, fallback });
                            e = fallback;
                            try { if (endInput) endInput.value = String(e); } catch (e) { /* ignore */ }
                        }
                        if (!Number.isFinite(s) || !Number.isFinite(e)) return alert('Please enter numeric frame values');

                        await window.animationHandler.startTemporaryLoop(aType, aFile, s, e, Number.isFinite(fps) ? fps : 30);
                        try { console.debug('[debug-window] startTemporaryLoop called successfully'); } catch (e) {}
                    } catch (ex) {
                        console.warn('[synth_webui] start temp loop error (from handler):', ex);
                    }
                } catch (err) { console.warn('[synth_webui] start temp loop error:', err); }
            });
        }
        if (loopClearBtn) {
            loopClearBtn.addEventListener('click', () => {
                try {
                    if (!window.animationHandler) {
                        // Queue clear so it runs when the handler becomes available
                        window.__synth_pending_actions = window.__synth_pending_actions || [];
                        window.__synth_pending_actions.push({ type: 'clearTemporaryOverride', args: [] });
                        try { console.debug('[debug-window] queued clearTemporaryOverride (handler not ready)'); } catch (e) {}
                        return;
                    }
                    window.animationHandler.clearTemporaryOverride();
                    resyncFromBackend(true);
                } catch (err) { console.warn('[synth_webui] clear temp loop failed:', err); }
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
                // Normalize file key for lookups (handler may normalize names internally)
                const normFile = (typeof window.animationHandler !== 'undefined' && typeof window.animationHandler._normalizeAnimationKey === 'function') ? window.animationHandler._normalizeAnimationKey(file) : file;
                const descriptor = (window.animationHandler && window.animationHandler.loadedDescriptors) ? (window.animationHandler.loadedDescriptors[normFile] || window.animationHandler.loadedDescriptors[file] || null) : null;
                const desiredFps = (descriptor && Number.isFinite(Number(descriptor.fps))) ? Number(descriptor.fps) : Number((fpsInput && fpsInput.value) ? fpsInput.value : 30);
                const fps = (Number.isFinite(desiredFps) && desiredFps > 0) ? desiredFps : 30;
                try { if (fpsInput) fpsInput.value = String(fps); } catch (e) { /* ignore */ }
                let maxFrameIndex = 0;
                try {
                    if (window.animationHandler && window.animationHandler.loadedAnimations) {
                        // Prefer normalized key, else try raw
                        const clip = window.animationHandler.loadedAnimations[normFile] || window.animationHandler.loadedAnimations[file] || null;
                        if (clip) maxFrameIndex = computeMaxFramesFromClip(clip, fps);
                    }
                } catch (e) { /* ignore */ }

                // If not found in loadedAnimations, try to dynamically load the clip (if handler supports it)
                if (!maxFrameIndex) {
                    try {
                        if (window.animationHandler && typeof window.animationHandler.loadAnimation === 'function') {
                            try {
                                const aType = selType && selType.value ? selType.value : 'think';
                                // Try short name first
                                let loaded = await window.animationHandler.loadAnimation(aType, file).catch(() => null);
                                if (loaded) {
                                    maxFrameIndex = computeMaxFramesFromClip(loaded, fps);
                                } else {
                                    // Fallback to candidate path using skin
                                    const skin = (window.activeSkinName && String(window.activeSkinName).split('/').pop().replace('.vrm','')) ? String(window.activeSkinName).split('/').pop().replace('.vrm','') : 'Rei';
                                    const candidatePath = `/skins/${skin}/animations/${aType}/${encodeURIComponent(file)}`;
                                    loaded = await window.animationHandler.loadAnimation(aType, candidatePath).catch(() => null);
                                    if (loaded) {
                                        maxFrameIndex = computeMaxFramesFromClip(loaded, fps);
                                        try { selFile.value = candidatePath; } catch (e) { /* ignore */ }
                                    }
                                }
                            } catch (e) { /* ignore */ }
                        }
                    } catch (e) { /* ignore */ }
                }

                // If still not found, attempt a full-path lookup from loadedAnimations
                if (!maxFrameIndex) {
                    try {
                        const skin = (window.activeSkinName && String(window.activeSkinName).split('/').pop().replace('.vrm','')) ? String(window.activeSkinName).split('/').pop().replace('.vrm','') : 'Rei';
                        const candidatePath = `/skins/${skin}/animations/${encodeURIComponent(selType && selType.value ? selType.value : 'think')}/${encodeURIComponent(file)}`;
                        const normCand = (typeof window.animationHandler !== 'undefined' && typeof window.animationHandler._normalizeAnimationKey === 'function') ? window.animationHandler._normalizeAnimationKey(candidatePath) : candidatePath;
                        const clip2 = window.animationHandler && window.animationHandler.loadedAnimations ? (window.animationHandler.loadedAnimations[normCand] || window.animationHandler.loadedAnimations[candidatePath] || null) : null;
                        if (clip2) {
                            maxFrameIndex = computeMaxFramesFromClip(clip2, fps);
                            try { selFile.value = candidatePath; } catch (e) { /* ignore */ }
                        }
                    } catch (e) { /* ignore */ }
                }

                if (!maxFrameIndex) maxFrameIndex = computeMaxFramesFromDescriptor(descriptor);

                // Debug log to help diagnose autofill failures
                try { console.debug('[debug-window] autofillLoopInputs', { file, normFile, fps, maxFrameIndex, descriptor: !!descriptor }); } catch (e) {}

                startInput.value = '0';
                endInput.value = String(maxFrameIndex || 0);
            } catch (e) { /* ignore */ }
        };

        // Feelings UI
        const extractEmotionValues = () => {
            try {
                if (window.animationHandler && window.animationHandler._lastFeelings) return window.animationHandler._lastFeelings;
                return window.__synth_debug_cached_emotions || {};
            } catch (e) { return {}; }
        };

        const getFeelingKeys = () => {
            try {
                const personaEmotionKeys = (window.__synth_persona_emotions_list && Array.isArray(window.__synth_persona_emotions_list)) ? window.__synth_persona_emotions_list : [];
                const canonical = [
                    'valence', 'arousal', 'stress', 'calm',
                    'happy', 'sad', 'angry', 'surprised', 'relaxed', 'neutral',
                    'scared', 'fear', 'disgust', 'curiosity', 'gratitude', 'empathy', 'trust'
                ];
                return Array.from(new Set([...personaEmotionKeys, ...canonical]))
                    .map(String)
                    .filter(k => k && !/^\d+$/.test(k))
                    .sort();
            } catch (e) {
                return [];
            }
        };

        let feelingRows = [];
        const renderFeelingsList = () => {
            try {
                const feelingsList = win.querySelector('#synth-debug-feelings-list');
                const feelingsWarning = win.querySelector('#synth-debug-feelings-warning');
                if (!feelingsList) return;

                // Handle VRM 0.x warning
                try {
                    const caps = window.__synth_vrm_capabilities || null;
                    const isVrm0 = caps && (caps.metaVersion === '0' || caps.version === '0.0' || !caps.hasExpressionManager);
                    if (feelingsWarning) {
                        feelingsWarning.style.display = isVrm0 ? 'block' : 'none';
                    }
                } catch (e) { /* ignore */ }

                const keys = getFeelingKeys();
                const overrides = (window.animationHandler && typeof window.animationHandler.getDebugEmotionOverrides === 'function') ? window.animationHandler.getDebugEmotionOverrides() : {};

                feelingsList.innerHTML = '';
                feelingRows = [];

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
                    
                    // Current value from overrides OR from animationHandler's base state
                    let startVal = 0;
                    if (overrides[k] !== undefined) {
                        startVal = overrides[k];
                    } else if (window.animationHandler && window.animationHandler._lastFeelings && window.animationHandler._lastFeelings[k] !== undefined) {
                        const raw = window.animationHandler._lastFeelings[k];
                        startVal = (raw > 1) ? raw / 10.0 : raw;
                    }

                    slider.value = String(clamp01(startVal));

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
                            if (window.animationHandler && window.animationHandler.setDebugEmotionOverride) {
                                window.animationHandler.setDebugEmotionOverride(k, vv);
                                console.debug('[debug-window] applied emotion override', k, vv);
                            } else {
                                window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                                window.__synth_pending_debug_actions.push({ type: 'setDebugEmotionOverride', key: k, value: vv });
                            }
                        } catch (e) { console.warn('[debug-window] apply feeling failed', e); }
                    };

                    slider.addEventListener('input', () => apply(slider.value));
                    num.addEventListener('change', () => apply(num.value));

                    row.appendChild(label);
                    row.appendChild(slider);
                    row.appendChild(num);
                    row.appendChild(cur);
                    feelingsList.appendChild(row);

                    feelingRows.push({ key: k, curEl: cur, sliderEl: slider, numEl: num });
                });
            } catch (e) { console.warn('[debug-window] renderFeelingsList failed', e); }
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

        // Bind feelings controls
        try {
            const feelingsResetUpstream = win.querySelector('#synth-debug-feelings-reset-upstream');
            const feelingsResetZero = win.querySelector('#synth-debug-feelings-reset-zero');
            
            if (feelingsResetUpstream) {
                feelingsResetUpstream.addEventListener('click', () => {
                    try {
                        if (window.animationHandler && window.animationHandler.clearDebugEmotionOverrides) {
                            window.animationHandler.clearDebugEmotionOverrides();
                        } else {
                            window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                            window.__synth_pending_debug_actions.push({ type: 'clearDebugEmotionOverrides' });
                        }
                        console.debug('[debug-window] feelings reset to upstream');
                    } catch (e) { /* ignore */ }
                    try { renderFeelingsList(); } catch (e) {}
                });
            }
            
            if (feelingsResetZero) {
                feelingsResetZero.addEventListener('click', () => {
                    try {
                        const keys = getFeelingKeys();
                        keys.forEach(k => {
                            if (window.animationHandler && window.animationHandler.setDebugEmotionOverride) {
                                window.animationHandler.setDebugEmotionOverride(k, 0);
                            } else {
                                window.__synth_pending_debug_actions = window.__synth_pending_debug_actions || [];
                                window.__synth_pending_debug_actions.push({ type: 'setDebugEmotionOverride', key: k, value: 0 });
                            }
                        });
                        console.debug('[debug-window] feelings reset to zero locally');
                    } catch (e) { /* ignore */ }
                    try { renderFeelingsList(); } catch (e) {}
                });
            }
        } catch (e) { /* ignore */ }

        // Bind face controls (feelings UI removed)
        try {
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
                        }
                        console.debug('[debug-window] queued clearDebugFaceOverrides');
                    } catch (e) { /* ignore */ }
                    try { renderFaceList(); } catch (e) {}
                    try { renderFeelingsList(); } catch (e) {}
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

                    // refresh face current values
                    if (window.animationHandler && faceRows && faceRows.length) {
                        faceRows.forEach((r) => {
                            try {
                                const vv = window.animationHandler._getFaceValue ? window.animationHandler._getFaceValue(r.key) : 0;
                                if (r.curEl) r.curEl.textContent = Number.isFinite(vv) ? vv.toFixed(2) : '—';
                            } catch (e) { /* ignore */ }
                        });
                    }

                    // refresh feelings current values
                    if (window.animationHandler && feelingRows && feelingRows.length) {
                        feelingRows.forEach((r) => {
                            try {
                                let vv = 0;
                                if (window.animationHandler._lastFeelings && window.animationHandler._lastFeelings[r.key] !== undefined) {
                                    const raw = window.animationHandler._lastFeelings[r.key];
                                    vv = (raw > 1) ? raw / 10.0 : raw;
                                }
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
            renderFaceList();
            renderFeelingsList();
            try {
                if (pauseBtn) {
                    pauseBtn.textContent = isPaused() ? '▶️' : '⏸️';
                    pauseBtn.title = isPaused() ? '▶️' : '⏸️';
                    pauseBtn.setAttribute('aria-label', isPaused() ? 'Play' : 'Pause');
                }
            } catch (e) { /* ignore */ }

            // Try an initial resync from backend to populate feelings/animation state
            try { resyncFromBackend().catch(e => {/*ignore*/}); } catch (e) { /* ignore */ }

                // If persona emotion keys were not injected, try loading persona JSON via animationHandler as a fallback
                try {
                    if (!window.__synth_persona_emotions_list) {
                        const skin = window.activeSkinName ? (window.activeSkinName.split('/').pop() || '').replace('.vrm','') : 'Rei';
                        // Try animationHandler loader if available
                        try {
                            if (window.animationHandler && typeof window.animationHandler._loadPersonaForSkin === 'function') {
                                window.animationHandler._loadPersonaForSkin(skin).then((persona) => {
                                    try {
                                        if (persona && persona.emotions && typeof persona.emotions === 'object') {
                                            window.__synth_persona_emotions_list = Object.keys(persona.emotions);
                                            // Crucial: populate the shared presets used by AnimationHandler for resolution
                                            if (!window.__synth_emotion_face_presets) {
                                                window.__synth_emotion_face_presets = persona.emotions;
                                            }
                                            try { renderFeelingsList(); } catch (e) {}
                                        }
                                    } catch (e) { /* ignore */ }
                                }).catch(() => {});
                            }
                        } catch (e) { /* ignore */ }

                        // HTTP fallback: directly fetch persona.json if still missing
                        try {
                            if (!window.__synth_persona_emotions_list) {
                                (async () => {
                                    try {
                                        const url = `/skins/${encodeURIComponent(skin)}/persona.json`;
                                        const r = await fetch(url);
                                        if (r && r.ok) {
                                            const j = await r.json();
                                            if (j && j.emotions && typeof j.emotions === 'object') {
                                                window.__synth_persona_emotions_list = Object.keys(j.emotions);
                                                if (!window.__synth_emotion_face_presets) {
                                                    window.__synth_emotion_face_presets = j.emotions;
                                                }
                                                try { renderFeelingsList(); } catch (e) {}
                                            }
                                        }
                                    } catch (e) { /* ignore */ }
                                })();
                            }
                        } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }
            try {
                if (!window.__synth_debug_on_vrm_loaded) {
                    window.__synth_debug_on_vrm_loaded = () => {
                        try { renderFaceList(); } catch (e) { /* ignore */ }
                        try { renderFeelingsList(); } catch (e) { /* ignore */ }
                        try { autofillLoopInputs(); } catch (e) { /* ignore */ }
                        // Try to refresh loop files now that VRM and animations are likely available
                        try {
                            const t = (selType && selType.value) ? selType.value : null;
                            if (t) {
                                try { console.debug('[debug-window] vrmLoaded -> refreshing loop files for type=', t); } catch (e) {}
                                refreshFilesForType(t).catch(e => {/*ignore*/});
                            }
                        } catch (e) { /* ignore */ }
                    };
                    window.addEventListener('vrmLoaded', window.__synth_debug_on_vrm_loaded);
                }

                // Unblock queued Start button when the real handler becomes available
                if (!window.__synth_animation_handler_ready_listener) {
                    window.__synth_animation_handler_ready_listener = () => {
                        try {
                            if (loopStartBtn) {
                                loopStartBtn.disabled = false;
                                try { loopStartBtn.textContent = isPaused() ? 'Resume' : 'Start'; } catch (e) { loopStartBtn.textContent = 'Start'; }
                                try { loopStartBtn.title = 'Start temporary loop'; } catch (e) {}
                            }
                            try { console.debug('[debug-window] animation handler ready - Start button re-enabled'); } catch (e) {}
                        } catch (e) { /* ignore */ }
                    };
                    window.addEventListener('synth_animation_handler_ready', window.__synth_animation_handler_ready_listener);
                }

                // Defensive polling: if handler becomes available later, re-enable Start and try to apply queued startTemporaryLoop actions
                try {
                    if (!window.__synth_debug_handler_poll_set) {
                        window.__synth_debug_handler_poll_set = true;
                        const poll = setInterval(async () => {
                            try {
                                if (!loopStartBtn) return;
                                if (window.animationHandler) {
                                    if (loopStartBtn.disabled) {
                                        loopStartBtn.disabled = false;
                                        try { loopStartBtn.textContent = isPaused() ? 'Resume' : 'Start'; } catch (e) { loopStartBtn.textContent = 'Start'; }
                                        try { loopStartBtn.title = 'Start temporary loop'; } catch (e) {}
                                    }
                                    // Refresh file list and autofill inputs now that handler is ready
                                    try {
                                        try { if (selType && typeof refreshFilesForType === 'function') await refreshFilesForType(selType.value); } catch (e) { /* ignore */ }
                                        try { await autofillLoopInputs(); } catch (e) { /* ignore */ }
                                    } catch (e) { /* ignore */ }

                                    // Attempt to process queued startTemporaryLoop actions from here (best-effort; vrm-viewer also flushes them)
                                    try {
                                        const pa = window.__synth_pending_actions || [];
                                        if (Array.isArray(pa) && pa.length) {
                                            const remaining = [];
                                            for (const act of pa) {
                                                try {
                                                    if (act && act.type === 'startTemporaryLoop' && typeof window.animationHandler.startTemporaryLoop === 'function') {
                                                        try { console.debug('[debug-window] applying queued startTemporaryLoop', act.args); } catch (e) {}
                                                        await window.animationHandler.startTemporaryLoop(...(act.args || []));
                                                    } else {
                                                        remaining.push(act);
                                                    }
                                                } catch (e) {
                                                    remaining.push(act);
                                                }
                                            }
                                            try { window.__synth_pending_actions = remaining; } catch (e) {}
                                        }
                                    } catch (e) { /* ignore */ }
                                    // Once handler is present and we've attempted processing, stop polling
                                    clearInterval(poll);
                                }
                            } catch (e) { /* ignore */ }
                        }, 500);
                    }
                } catch (e) { /* ignore */ }
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

                                        // Apply emotion override actions
                                        else if (act.type === 'setDebugEmotionOverride') {
                                            if (window.animationHandler && typeof window.animationHandler.setDebugEmotionOverride === 'function') {
                                                try { window.animationHandler.setDebugEmotionOverride(act.key, act.value); applied = true; } catch (e) { applied = false; }
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

                                        else if (act.type === 'clearDebugEmotionOverrides') {
                                            if (window.animationHandler && typeof window.animationHandler.clearDebugEmotionOverrides === 'function') {
                                                try { window.animationHandler.clearDebugEmotionOverrides(); applied = true; } catch (e) { applied = false; }
                                            }
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
                                            const r = await fetch(_apiBase + '/api/emotion_state');
                                            if (r && r.ok) {
                                                const j = await r.json();
                                                if (j && j.emotions && typeof j.emotions === 'object') {
                                                    window.__synth_debug_cached_emotions = j.emotions;
                                                    try { console.debug('[debug-window] fetched /api/emotion_state keys=', Object.keys(j.emotions).slice(0,20)); } catch (e) {}
                                                    // trigger immediate re-render
                                                    try { renderFeelingsList(); } catch (e) {}
                                                }
                                            }
                                        } catch (e) { /* ignore */ }
                                    })();
                                }
                            } catch (e) { /* ignore */ }

                            // If the loop-file selector is empty (no files loaded), attempt a retry when animations become available
                            try {
                                try { if (!window.__synth_debug_last_files_refresh_ts) window.__synth_debug_last_files_refresh_ts = 0; } catch (e) {}
                                const nowTs = Date.now();
                                const loadedCount = (loadedSpan && loadedSpan.textContent) ? Number(loadedSpan.textContent) : (selFile && selFile.options ? selFile.options.length - 0 : 0);
                                const hasNoFiles = (!loadedCount || loadedCount === 0) || (selFile && selFile.options && selFile.options.length === 1 && selFile.options[0] && String(selFile.options[0].value || '') === '');
                                if (hasNoFiles && (nowTs - (window.__synth_debug_last_files_refresh_ts || 0) > 3000)) {
                                    try {
                                        window.__synth_debug_last_files_refresh_ts = nowTs;
                                        const t = (selType && selType.value) ? selType.value : null;
                                        if (t) {
                                            try { console.debug('[debug-window] retrying refreshFilesForType because no files present for type=', t); } catch (e) {}
                                            (async () => { try { await refreshFilesForType(t); try { await autofillLoopInputs(); } catch (e) {} } catch (e) { /* ignore */ } })();
                                        }
                                    } catch (e) { /* ignore */ }
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
