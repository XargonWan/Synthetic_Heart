// main.js — lightweight loader and helpers for the Synth WebUI
(function(){
    'use strict';

    // Minimal config accessor
    window.SynthConfig = window.__SYNTH_CONFIG || {};
    // Use a dedicated desktop root instead of iframe separation.
    window.SynthConfig.DESKTOP_IFRAME = false;
    window.SynthWebUISetStatus = window.SynthWebUISetStatus || function setSynthWebUIStatus(message, level) {
        try {
            const label = document.getElementById('status-label');
            if (label) {
                label.textContent = message || '';
            }
            window.__synth_status_last = { message, level, at: Date.now() };
        } catch (e) { /* ignore */ }
    };



    // Generic section loader. Fetches /templates/<section>.html and injects into the tab panel.
    async function loadSection(section) {
        try {
            const panel = document.querySelector(`.tab-panel[data-tab="${section}"]`);
            if (!panel) return null;
            // If panel already contains substantial content, skip
            if (panel.dataset.loaded === '1') return panel;
            if (panel.dataset.loading === '1') return panel;
            if (panel.children && panel.children.length > 0) {
                panel.dataset.loaded = '1';
                return panel;
            }

            panel.dataset.loading = '1';

            const resp = await fetch(`/templates/${section}.html`);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const text = await resp.text();
            const parser = new DOMParser();
            const doc = parser.parseFromString(text, 'text/html');

            // If the template is a tab-panel matching id, append children inside
            const children = Array.from(doc.body.children || []);
            if (children.length === 1 && children[0].id === panel.id) {
                Array.from(children[0].children).forEach(n => panel.appendChild(n));
            } else {
                children.forEach(n => panel.appendChild(n));
            }

            // Execute scripts in the inserted content (preserve order)
            const scripts = Array.from(panel.querySelectorAll('script'));
            for (const old of scripts) {
                const el = document.createElement('script');
                if (old.type) el.type = old.type;
                if (old.src) {
                    el.src = old.src;
                    el.async = false;
                    await new Promise((resolve) => {
                        el.onload = () => resolve();
                        el.onerror = () => resolve();
                        document.body.appendChild(el);
                    });
                } else {
                    el.textContent = old.textContent;
                    document.body.appendChild(el);
                }
                old.remove();
            }

            panel.dataset.loaded = '1';
            panel.dataset.loading = '0';
            if (panel.children.length === 0 && text.trim().length > 0) {
                panel.innerHTML = text;
            }
            // Call an optional section-specific initializer, e.g. initSkinsTab, initHistoryTab
            try {
                const initName = 'init' + section.charAt(0).toUpperCase() + section.slice(1) + 'Tab';
                if (window.SynthWebUI && typeof window.SynthWebUI[initName] === 'function') {
                    try { window.SynthWebUI[initName](); } catch (e) { console.debug('[synth_webui] section init failed', initName, e); }
                }
            } catch (e) { /* ignore */ }
            return panel;
        } catch (e) {
            console.error('[synth_webui] loadSection failed', e);
            const panel = document.querySelector(`.tab-panel[data-tab="${section}"]`);
            if (panel) panel.innerText = 'Failed to load section: ' + e;
            return null;
        }
    }

    // Expose public helpers
    window.SynthWebUI = window.SynthWebUI || {};
    window.SynthWebUI.loadSection = loadSection;

    document.addEventListener('DOMContentLoaded', () => {
        // DIAGNOSTICS: capture top-level clicks and report overlay/pointer state to console.
        try {
            setTimeout(() => {
                try {
                    console.log('[synth_webui diagnostics] navButtons:', document.querySelectorAll('.nav-btn').length);
                    const overlay = document.getElementById('synth-error-overlay');
                    console.log('[synth_webui diagnostics] synth-error-overlay display:', overlay ? window.getComputedStyle(overlay).display : 'not-present');
                    console.log('[synth_webui diagnostics] body pointer-events:', window.getComputedStyle(document.body).pointerEvents);
                    // Find any visible fixed element with high z-index
                    const candidates = Array.from(document.querySelectorAll('*')).filter(el => {
                        try {
                            const cs = window.getComputedStyle(el);
                            return cs && cs.position === 'fixed' && cs.display !== 'none' && parseInt(cs.zIndex || '0') >= 1000;
                        } catch (e) { return false; }
                    });
                    if (candidates.length) {
                        const top = candidates[0];
                        console.log('[synth_webui diagnostics] top fixed candidate:', top.tagName, 'id=', top.id || '(no-id)', 'class=', top.className, 'z=', window.getComputedStyle(top).zIndex);
                    } else {
                        console.log('[synth_webui diagnostics] no high-z fixed element found');
                    }
                } catch (e) {}
            }, 300);

            // Initialize Chat window module (separate file) and attach header tools
            try {
                import('./chat-window.mjs').then(async (mod) => {
                    try {
                        if (mod && typeof mod.createChatWindow === 'function') {
                            // Ensure the Home section (and #chat mount) is available before creating the window
                            try { if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') await window.SynthWebUI.loadSection('home'); } catch (e) { /* ignore */ }
                            // createChatWindow returns a Promise resolving to the WinBox instance (or null)
                            const winbox = await mod.createChatWindow().catch(() => null);
                        }
                    } catch (e) { /* ignore */ }
                }).catch((e) => { try { console.debug('[synth_webui] chat-window import failed', e); } catch (e) {} });
            } catch (e) { /* ignore */ }

            // Also log any click events so we know they reach the document
            document.addEventListener('click', (ev) => {
                try {
                    const t = ev.target || {};
                    console.log('[synth_webui diagnostics] click at', Date.now(), 'target=', t.tagName, 'id=', t.id, 'class=', t.className);
                } catch (e) {}
            }, true); // capture phase
        } catch (e) { /* ignore diagnostics failures */ }
    });
})();

// Appended legacy main script (migrated from template)

// Initialize globals from server-rendered config (kept minimal)
try {
  if (window.__SYNTH_CONFIG) {
    if (window.__SYNTH_CONFIG.RESPONSE_TIMEOUT !== undefined) window.RESPONSE_TIMEOUT = window.__SYNTH_CONFIG.RESPONSE_TIMEOUT;
    if (window.__SYNTH_CONFIG.FAILED_MESSAGE_TEXT !== undefined) window.FAILED_MESSAGE_TEXT = window.__SYNTH_CONFIG.FAILED_MESSAGE_TEXT;
    if (window.__SYNTH_CONFIG.WEB_DEBUG !== undefined) window.__synth_web_debug_enabled = window.__SYNTH_CONFIG.WEB_DEBUG;
  }
} catch (e) { console.warn('[synth_webui] config init failed', e); }

        // Configuration values from server
        window.__synthLipSyncAnalyser = null;
        window.__synthLipSyncData = null;
        window.__synthIsLipSyncing = false;
        window.__synthLipSyncAudio = null;
        
        const navButtons = document.querySelectorAll('.nav-btn');
        const tabPanels = document.querySelectorAll('.tab-panel');
        
        console.log('[synth_webui] navButtons found:', navButtons.length);
        console.log('[synth_webui] tabPanels found:', tabPanels.length);
        
        const statusLabel = document.getElementById('status-label');
        const messages = document.getElementById('messages');
    const input = document.getElementById('input');
    const form = document.getElementById('composer');
    const sendBtn = document.getElementById('send');
        const uptimeEl = document.getElementById('stats-uptime');
        const sessionsEl = document.getElementById('stats-sessions');
        const connectionEl = document.getElementById('connection');
        const notifyToggle = document.getElementById('notify-toggle');
        const notifyStatus = document.getElementById('notify-status');
        const logOutput = document.getElementById('log-output');
        const logAutoscroll = document.getElementById('logs-autoscroll');
        const logFilters = document.querySelectorAll('.log-filter');
        const logsRefreshBtn = document.getElementById('logs-refresh');
        const logSearchInput = document.getElementById('log-search');
        const chatPanel = document.getElementById('chat');
        const chatToggleBtn = document.getElementById('chat-toggle');
        const componentsLLMSummary = document.getElementById('components-llm-summary');
        const componentsLLMList = document.getElementById('components-llm-list');
        const componentsInterfacesList = document.getElementById('components-interfaces-list');
        const componentsPluginsList = document.getElementById('components-plugins-list');
        const configGeneralList = document.getElementById('config-general-list');
        const configAdvancedList = document.getElementById('config-advanced-list');
        const configDisclaimer = document.getElementById('config-env-disclaimer');
        const configAdvancedWarning = document.getElementById('config-advanced-warning');
        const configExpandAll = document.getElementById('config-expand-all');
        const configCollapseAll = document.getElementById('config-collapse-all');
        
        console.log('[synth_webui] DOM elements queried at script start:');
        console.log('[synth_webui] configGeneralList:', configGeneralList);
        console.log('[synth_webui] configAdvancedList:', configAdvancedList);
        const NOTIFY_KEY = 'synth-webui-notify';
        const NOTIFY_ASKED_KEY = 'synth-webui-notify-asked';
        const HISTORY_KEY = 'synth-webui-history';
        const TAB_KEY = 'synth-webui-active-tab';
        // Initialize global activeTab from localStorage or DOM so modules can
        // safely reference current tab. We expose both `window.activeTab` and
        // a bare `activeTab` var to maximize compatibility with legacy code.
        try {
            const _initial = (localStorage && localStorage.getItem && localStorage.getItem(TAB_KEY)) || (document.querySelector && document.querySelector('.nav-btn.active') && document.querySelector('.nav-btn.active').getAttribute('data-tab')) || 'home';
            window.activeTab = window.activeTab || _initial;
            // Create a global var for non-module scripts
            if (typeof activeTab === 'undefined') {
                // eslint-disable-next-line no-unused-vars
                var activeTab = window.activeTab;
            }
        } catch (e) { /* ignore */ }

        const CHAT_WINDOW_STATE_KEY = 'synth-webui-window-state';
        const CHAT_MESSAGES_KEY = 'synth-webui-chat-messages';
        const CHAT_RECT_KEY = 'synth-webui-chat-rect';
        const TYPING_INDICATOR_KEY = 'synth-webui-typing-indicator';
        const VRM_MODEL_KEY = 'synth-webui-vrm-model';
        const HISTORY_LIMIT = 200;
        const LOG_BUFFER_LIMIT = 2000;
        const IS_SECURE = window.isSecureContext || window.location.protocol === 'https:' || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
        let ws = null;
        // Expose sessionId as a true global var so other scripts can reference it
        var sessionId = window.sessionId = window.sessionId || null;
        // Legacy helper for modules that call getSessionId()
        window.getSessionId = window.getSessionId || function getSessionId() {
            try { return window.sessionId || sessionId || null; } catch (e) { return null; }
        };
        let notificationsEnabled = false;
        let audioContext = null;
        let historyBuffer = [];
        let logsSocket = null;
        let logsReconnectTimer = null;
        let pendingAnimationCommands = []; // Queue for animation commands received before handler is ready
        let pendingAnimationTimeout = null; // Timeout for processing pending animations
        let animationHandler = null; // Global animation handler, initialized when VRM loads
        // WEB_DEBUG local-only pause: when enabled, ignore remote animation/action_state updates.
        // Preloads are still accepted so cache stays warm.
        window.__synth_web_debug_enabled = window.__synth_web_debug_enabled || false;
        window.__synth_debug_pause_all = window.__synth_debug_pause_all || false;
        window.__synth_debug_last_remote = window.__synth_debug_last_remote || { animation: null, animation_state: null, action_state: null };
        window.__synth_debug_last_remote_at = window.__synth_debug_last_remote_at || { animation: 0, animation_state: 0, action_state: 0 };
        // Queue preload requests received before VRM/AnimationHandler is ready.
        window.__synth_pending_preloads = window.__synth_pending_preloads || {}; 

        // -----------------------------------------------------------------------------
        // Window manager (WinBox) helpers
        // -----------------------------------------------------------------------------
        const synthWindowManager = (() => {
            const windows = new Map();
            let winboxLoading = false;
            let winboxReadyPromise = null;

            function ensureWinBoxAssets() {
                try { console.debug('[SynthWindowManager] ensureWinBoxAssets called, WinBox present=', typeof window.WinBox !== 'undefined'); } catch (e) { /* ignore */ }
                if (typeof window.WinBox !== 'undefined') return Promise.resolve(true);
                if (winboxReadyPromise) return winboxReadyPromise;
                winboxReadyPromise = new Promise((resolve) => {
                    try {
                        const cssId = 'winbox-css';
                        if (!document.getElementById(cssId)) {
                            try { console.debug('[SynthWindowManager] injecting winbox CSS'); } catch (e) {}
                            const link = document.createElement('link');
                            link.id = cssId;
                            link.rel = 'stylesheet';
                            link.href = '/js/vendor/winbox.min.css';
                            document.head.appendChild(link);
                        }

                        const scriptId = 'winbox-js';
                        if (!document.getElementById(scriptId)) {
                            try { console.debug('[SynthWindowManager] injecting winbox JS'); } catch (e) {}
                            const script = document.createElement('script');
                            script.id = scriptId;
                            script.src = '/js/vendor/winbox.min.js';
                            script.onload = () => { try { console.debug('[SynthWindowManager] winbox loaded:', typeof window.WinBox !== 'undefined'); } catch (e) {} resolve(typeof window.WinBox !== 'undefined'); };
                            script.onerror = () => { try { console.debug('[SynthWindowManager] winbox failed to load'); } catch (e) {} resolve(false); };
                            document.body.appendChild(script);
                        } else {
                            try { console.debug('[SynthWindowManager] winbox script tag already present'); } catch (e) {}
                            resolve(typeof window.WinBox !== 'undefined');
                        }
                    } catch (e) {
                        resolve(false);
                    }
                });
                return winboxReadyPromise;
            }

            function ensureDock() {
                let dock = document.getElementById('synth-minimized-stack');
                if (!dock) {
                    dock = document.createElement('div');
                    dock.id = 'synth-minimized-stack';
                    dock.className = 'synth-minimized-stack';
                    dock.setAttribute('aria-label', 'Minimized windows');
                    document.body.appendChild(dock);
                }
                try {
                    dock.style.position = 'fixed';
                    dock.style.left = 'auto';
                    dock.style.right = '18px';
                    dock.style.bottom = '18px';
                    dock.style.top = 'auto';
                    dock.style.display = 'flex';
                    dock.style.flexDirection = 'column';
                    dock.style.gap = '8px';
                    dock.style.alignItems = 'flex-end';
                    dock.style.zIndex = '10650';
                    // Ensure an announcer for screen readers
                    let announcer = dock.querySelector('.synth-dock-announcer');
                    if (!announcer) {
                        announcer = document.createElement('div');
                        announcer.className = 'synth-dock-announcer';
                        announcer.setAttribute('aria-live', 'polite');
                        announcer.style.position = 'absolute';
                        announcer.style.left = '-9999px';
                        dock.appendChild(announcer);
                    }
                } catch (e) { /* ignore */ }
                return dock;
            }

            function getTopbarHeight() {
                try {
                    const topbar = document.querySelector('header.top-bar');
                    if (topbar) return Math.ceil(topbar.getBoundingClientRect().height || 0);
                } catch (e) { /* ignore */ }
                return 0;
            }

            function getViewportSize() {
                const w = Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0);
                const h = Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0);
                return { width: w, height: h };
            }

            function applyViewportInsets(entry) {
                // NOTE: previously we altered winbox inset properties (right/bottom) to
                // accommodate scrollbars. Writing negative inset values causes WinBox's
                // internal maximize/resize math to behave incorrectly. Avoid modifying
                // those properties; rely on resize events and explicit constraints
                // instead.
                if (!entry || !entry.winbox) return;
                try {
                    // Keep function for debugging hooks; don't mutate winbox insets here.
                } catch (e) { /* ignore */ }
            }

            function applyMaximizeConstraints(entry) {
                if (!entry || !entry.winbox) return;
                const isMax = !!(entry.winbox.max || entry.winbox.maximized);
                if (!isMax) return;
                try {
                    // Prefer using the dedicated desktop root size when available so
                    // maximized windows fill the exact desktop area (no right/bottom gaps).
                    const desktopRoot = document.getElementById('desktop-root');
                    if (desktopRoot && typeof desktopRoot.getBoundingClientRect === 'function') {
                        const r = desktopRoot.getBoundingClientRect();
                        const left = Math.round(r.left || 0);
                        const top = Math.round(r.top || getTopbarHeight() || 0);
                        const width = Math.max(120, Math.round(r.width || entry.winbox.width || 0));
                        const height = Math.max(120, Math.round(r.height || entry.winbox.height || 0));
                        try { entry.winbox.move(left, top); } catch (e) { /* ignore */ }
                        try { entry.winbox.resize(width, height); } catch (e) { /* ignore */ }
                        // Compensate if the DOM rect is still smaller than requested (rounding/border issues)
                        try {
                            const winEl = entry.winbox.window || entry.winbox.dom || entry.winbox.g || null;
                            if (winEl && winEl.getBoundingClientRect) {
                                const rect = winEl.getBoundingClientRect();
                                const dw = width - Math.round(rect.width || 0);
                                const dh = height - Math.round(rect.height || 0);
                                if ((dw > 0) || (dh > 0)) {
                                    try { entry.winbox.resize(width + (dw > 0 ? 1 : 0), height + (dh > 0 ? 1 : 0)); } catch (e) { /* ignore */ }
                                }
                            }
                        } catch (e) { /* ignore */ }
                        return;
                    }
                } catch (e) { /* ignore */ }

                // Fallback to viewport-based sizing if desktop root isn't available
                const top = getTopbarHeight();
                const viewport = getViewportSize();
                const width = viewport.width || entry.winbox.width || 0;
                const height = Math.max(120, (viewport.height || entry.winbox.height || 0) - top);
                try { entry.winbox.move(0, top); } catch (e) { /* ignore */ }
                try { entry.winbox.resize(width, height); } catch (e) { /* ignore */ }
                try {
                    const winEl = entry.winbox.window || entry.winbox.dom || entry.winbox.g || null;
                    if (winEl && winEl.getBoundingClientRect) {
                        const rect = winEl.getBoundingClientRect();
                        const dw = width - Math.round(rect.width || 0);
                        const dh = height - Math.round(rect.height || 0);
                        if ((dw > 0) || (dh > 0)) {
                            try { entry.winbox.resize(width + (dw > 0 ? 1 : 0), height + (dh > 0 ? 1 : 0)); } catch (e) { /* ignore */ }
                        }
                    }
                } catch (e) { /* ignore */ }
            }

            function captureNormalRect(entry) {
                if (!entry || !entry.winbox) return;
                if (entry.winbox.max || entry.winbox.min) return;
                try {
                    const winEl = entry.winbox.window || entry.winbox.dom || entry.winbox.g || null;
                    if (winEl && winEl.getBoundingClientRect) {
                        const rect = winEl.getBoundingClientRect();
                        entry.lastNormalRect = {
                            x: Math.round(rect.left),
                            y: Math.round(rect.top),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height)
                        };
                        return;
                    }
                } catch (e) { /* ignore */ }
                entry.lastNormalRect = {
                    x: Math.round(entry.winbox.x || 0),
                    y: Math.round(entry.winbox.y || 0),
                    width: Math.round(entry.winbox.width || 0),
                    height: Math.round(entry.winbox.height || 0)
                };
            }

            function clampToTopbar(entry) {
                if (!entry || !entry.winbox) return;
                const top = getTopbarHeight();
                if (!top) return;
                const winEl = entry.winbox.window || entry.winbox.dom || entry.winbox.g || null;
                if (!winEl) return;
                const rect = winEl.getBoundingClientRect();
                if (!rect || rect.top >= top) return;
                const x = Number.isFinite(entry.winbox.x) ? entry.winbox.x : rect.left;
                try { entry.winbox.move(x, top); } catch (e) { /* ignore */ }
            }

            /* Ensure a window is fully (or mostly) inside the current viewport.
             * If it is partially offscreen, move it inwards and reduce size when
             * necessary so the WinBox chrome remains accessible on all devices.
             */
            function clampEntryToViewport(entry) {
                if (!entry || !entry.winbox) return;
                try {
                    // don't clamp minimized windows or maximized ones (we handle maximize elsewhere)
                    if (entry.minimized) return;
                    const winEl = entry.winbox.window || entry.winbox.dom || entry.winbox.g || null;
                    if (!winEl || !winEl.getBoundingClientRect) return;
                    const rect = winEl.getBoundingClientRect();
                    const viewport = getViewportSize();
                    const topbar = getTopbarHeight() || 0;

                    // minimum usable dimensions
                    const minWidth = 260;
                    const minHeight = 120;

                    let newWidth = Math.round(rect.width || entry.winbox.width || minWidth);
                    let newHeight = Math.round(rect.height || entry.winbox.height || minHeight);
                    let newLeft = Math.round(rect.left || (entry.winbox.x || 0));
                    let newTop = Math.round(rect.top || (entry.winbox.y || 0));

                    // If window is wider/taller than viewport, shrink it (respecting min)
                    if (newWidth > viewport.width) newWidth = Math.max(minWidth, viewport.width - 40);
                    if (newHeight > (viewport.height - topbar)) newHeight = Math.max(minHeight, viewport.height - topbar - 40);

                    // Ensure left/top are inside viewport bounds (allow small padding)
                    newLeft = Math.min(Math.max(8, newLeft), Math.max(8, viewport.width - newWidth - 8));
                    newTop = Math.min(Math.max(topbar, newTop), Math.max(topbar, viewport.height - newHeight - 8));

                    // Apply changes only when needed to avoid janky behavior
                    const needsMove = (Math.abs((entry.winbox.x || 0) - newLeft) > 1) || (Math.abs((entry.winbox.y || 0) - newTop) > 1);
                    const needsResize = (Math.abs((entry.winbox.width || 0) - newWidth) > 1) || (Math.abs((entry.winbox.height || 0) - newHeight) > 1);
                    if (needsResize) {
                        try { entry.winbox.resize(newWidth, newHeight); } catch (e) { /* ignore */ }
                    }
                    if (needsMove) {
                        try { entry.winbox.move(newLeft, newTop); } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }
            }

            function ensureDockButton(entry) {
                if (entry.dockButton && entry.dockButton.isConnected) return entry.dockButton;
                const btn = entry.dockButton || document.createElement('button');
                btn.type = 'button';
                // Ensure consistent class for styling and a11y
                const baseClass = entry.dockClass || 'chat-toggle-btn';
                btn.className = `${baseClass} synth-dock-btn`;
                btn.textContent = entry.iconText || '🗔';
                btn.setAttribute('aria-label', entry.dockLabel || 'Restore window');
                btn.title = entry.dockLabel || 'Restore window';
                btn.setAttribute('role', 'button');
                btn.setAttribute('tabindex', '0');
                btn.setAttribute('aria-pressed', 'false');
                btn.style.display = 'none';
                // Click restores the window
                btn.addEventListener('click', () => restore(entry.id));
                // Keyboard: Enter / Space to activate
                btn.addEventListener('keydown', (ev) => {
                    try {
                        if (ev.key === 'Enter' || ev.key === ' ' || ev.code === 'Space') {
                            ev.preventDefault();
                            restore(entry.id);
                        }
                    } catch (e) { /* ignore */ }
                });
                entry.dockButton = btn;
                return btn;
            }

            function attachDragHandle(entry, handleEl) {
                if (!entry || !entry.winbox || !handleEl) return;
                if (handleEl.dataset && handleEl.dataset.synthDragBound === '1') return;
                const winbox = entry.winbox;
                let dragging = false;
                let startX = 0;
                let startY = 0;
                let winX = 0;
                let winY = 0;

                const onMove = (ev) => {
                    if (!dragging) return;
                    const dx = ev.clientX - startX;
                    const dy = ev.clientY - startY;
                    try {
                        // Clamp to viewport (respect topbar)
                        const winEl = winbox.window || winbox.dom || winbox.g || null;
                        let w = 320, h = 240;
                        if (winEl) {
                            const r = winEl.getBoundingClientRect();
                            w = r.width || w; h = r.height || h;
                        }
                        const topbar = getTopbarHeight() || 0;
                        const viewport = getViewportSize();
                        // Allow a small overhang beyond the viewport bottom to avoid the "invisible wall" effect.
                        // Overhang is configurable via `window.SynthConfig.WINDOW_DRAG_BOTTOM_OVERHANG` (numeric, px).
                        const overhang = (window.SynthConfig && Number.isFinite(Number(window.SynthConfig.WINDOW_DRAG_BOTTOM_OVERHANG))) ? Math.max(0, Math.abs(Number(window.SynthConfig.WINDOW_DRAG_BOTTOM_OVERHANG))) : 180;
                        const maxX = Math.max(0, viewport.width - w);
                        const maxY = Math.max(topbar, viewport.height - h + overhang);
                        const targetX = Math.min(maxX, Math.max(0, Math.round((winX || 0) + dx)));
                        const targetY = Math.min(maxY, Math.max(topbar, Math.round((winY || 0) + dy)));
                        winbox.move(targetX, targetY);
                    } catch (e) { /* ignore */ }
                };

                const onUp = () => {
                    if (!dragging) return;
                    dragging = false;
                    document.removeEventListener('pointermove', onMove);
                    document.removeEventListener('pointerup', onUp);
                    try { saveState(entry.id); } catch (e) { /* ignore */ }
                };

                handleEl.addEventListener('pointerdown', (ev) => {
                    try {
                        if (ev.button !== 0) return;
                        if (ev.target && ev.target.closest && ev.target.closest('.chat-controls')) return;
                    } catch (e) { /* ignore */ }
                    dragging = true;
                    startX = ev.clientX;
                    startY = ev.clientY;
                    winX = winbox.x || 0;
                    winY = winbox.y || 0;
                    document.addEventListener('pointermove', onMove);
                    document.addEventListener('pointerup', onUp);
                });

                if (handleEl.dataset) handleEl.dataset.synthDragBound = '1';
            }

            function minimize(id) {
                const entry = windows.get(id);
                if (!entry || !entry.winbox) return;
                // Prevent double-minimize behavior
                if (entry.minimized) return;
                entry.minimized = true;
                // Prefer the native WinBox minimize API so internal state and classes
                // are updated correctly, fallback to hide() when not available.
                try {
                    if (typeof entry.winbox.minimize === 'function') entry.winbox.minimize();
                    else entry.winbox.hide();
                } catch (e) { /* ignore */ }

                const dock = ensureDock();
                const btn = ensureDockButton(entry);
                btn.style.display = 'flex';
                dock.appendChild(btn);
                try { entry.winbox.blur(); } catch (e) { /* ignore */ }
                // Announce minimize for screen reader
                try {
                    const dock = ensureDock();
                    const announcer = dock && dock.querySelector && dock.querySelector('.synth-dock-announcer');
                    if (announcer) {
                        announcer.textContent = (entry.dockLabel || 'Window') + ' minimized';
                        setTimeout(() => { try { announcer.textContent = ''; } catch (e) {} }, 2000);
                    }
                } catch (e) { /* ignore */ }
                try { saveState(id); } catch (e) { /* ignore */ }
            }

            function restore(id) {
                const entry = windows.get(id);
                if (!entry || !entry.winbox) return;
                entry.minimized = false;
                try { entry.winbox.show(); } catch (e) { /* ignore */ }
                try { entry.winbox.restore(); } catch (e) { /* ignore */ }
                try { entry.winbox.focus(); } catch (e) { /* ignore */ }
                if (entry.dockButton) {
                    try {
                        const dock = ensureDock();
                        const announcer = dock && dock.querySelector && dock.querySelector('.synth-dock-announcer');
                        entry.dockButton.style.display = 'none';
                        if (entry.dockButton.parentElement) entry.dockButton.parentElement.removeChild(entry.dockButton);
                        if (announcer) {
                            announcer.textContent = (entry.dockLabel || 'Window') + ' restored';
                            setTimeout(() => { try { announcer.textContent = ''; } catch (e) {} }, 1500);
                        }
                    } catch (e) { /* ignore DOM removal errors */ }
                }
                try { saveState(id); } catch (e) { /* ignore */ }
            }

            function toggleMaximize(id) {
                const entry = windows.get(id);
                if (!entry || !entry.winbox) return;
                try {
                    // If currently maximized, restore to previous size; otherwise maximize and apply constraints
                    if (entry.winbox.max || entry.winbox.maximized) {
                        try { entry.winbox.restore(); } catch (e) { /* ignore */ }
                        // After restore, ensure we return to the captured normal rectangle
                        if (entry.lastNormalRect) {
                            try {
                                entry.winbox.move(entry.lastNormalRect.x, entry.lastNormalRect.y);
                                entry.winbox.resize(entry.lastNormalRect.width, entry.lastNormalRect.height);
                            } catch (e) { /* ignore */ }
                        }
                    } else {
                        try { captureNormalRect(entry); } catch (e) { /* ignore */ }
                        try { entry.winbox.maximize(); } catch (e) { /* ignore */ }
                        try { applyMaximizeConstraints(entry); } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }
                try { saveState(id); } catch (e) { /* ignore */ }
            }

            function saveState(id) {
                const entry = windows.get(id);
                if (!entry || !entry.winbox) return;
                try {
                    // Use id in keys so we can persist multiple windows (chat, debug, etc.)
                    const stateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${id}-${sessionId}` : `${CHAT_WINDOW_STATE_KEY}-${id}`;
                    let state = 'normal';
                    if (entry.minimized) state = 'minimized';
                    else if (entry.winbox.max) state = 'maximized';
                    try { localStorage.setItem(stateKey, state); } catch (e) { /* ignore */ }

                    const rectKey = sessionId ? `${CHAT_RECT_KEY}-${id}-${sessionId}` : `${CHAT_RECT_KEY}-${id}`;
                    const payload = {
                        left: Math.round(entry.winbox.x || 0),
                        top: Math.round(entry.winbox.y || 0),
                        width: Math.round(entry.winbox.width || 0),
                        height: Math.round(entry.winbox.height || 0)
                    };
                    try { localStorage.setItem(rectKey, JSON.stringify(payload)); } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            }

            function restoreState(id) {
                const entry = windows.get(id);
                if (!entry || !entry.winbox) return false;
                try {
                    const rectKey = sessionId ? `${CHAT_RECT_KEY}-${id}-${sessionId}` : `${CHAT_RECT_KEY}-${id}`;
                    const legacyRectMobileKey = sessionId ? `${CHAT_RECT_KEY}-${id}-${sessionId}-mobile` : `${CHAT_RECT_KEY}-${id}-mobile`;
                    const legacyRectDesktopKey = sessionId ? `${CHAT_RECT_KEY}-${id}-${sessionId}-desktop` : `${CHAT_RECT_KEY}-${id}-desktop`;
                    const rectRaw = localStorage.getItem(rectKey) || localStorage.getItem(legacyRectDesktopKey) || localStorage.getItem(legacyRectMobileKey) || localStorage.getItem(sessionId ? `${CHAT_RECT_KEY}-${id}-${sessionId}` : `${CHAT_RECT_KEY}-${id}`) || localStorage.getItem(CHAT_RECT_KEY);
                    if (rectRaw) {
                        const rect = JSON.parse(rectRaw);
                        const hasWidth = typeof rect.width === 'number' && rect.width >= 260;
                        const hasHeight = typeof rect.height === 'number' && rect.height >= 180;
                        if (hasWidth && hasHeight) {
                            entry.winbox.resize(rect.width, rect.height);
                        } else if (hasWidth) {
                            entry.winbox.resize(rect.width, entry.winbox.height);
                        } else if (hasHeight) {
                            entry.winbox.resize(entry.winbox.width, rect.height);
                        }
                        if (typeof rect.left === 'number' || typeof rect.top === 'number') {
                            const topbar = getTopbarHeight() || 0;
                            const left = typeof rect.left === 'number' ? rect.left : entry.winbox.x;
                            let top = typeof rect.top === 'number' ? rect.top : entry.winbox.y;
                            if (top < topbar) top = topbar;
                            entry.winbox.move(left, top);
                        }
                    } else {
                        // No saved rect: position at bottom-left by default
                        try {
                            const viewport = getViewportSize();
                            const topbar = getTopbarHeight() || 0;
                            const winEl = entry.winbox.window || entry.winbox.dom || entry.winbox.g || null;
                            let height = 0;
                            if (winEl && winEl.getBoundingClientRect) {
                                const r = winEl.getBoundingClientRect();
                                height = r.height || Math.round(viewport.height * 0.7);
                            } else if (typeof entry.winbox.height === 'number') {
                                height = entry.winbox.height;
                            } else {
                                height = Math.round(viewport.height * 0.7);
                            }
                            const top = Math.max(topbar, Math.round(viewport.height - height - 18));
                            try { entry.winbox.move(18, top); } catch (e) { /* ignore */ }
                        } catch (e) { /* ignore */ }
                    }

                    const stateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${id}-${sessionId}` : `${CHAT_WINDOW_STATE_KEY}-${id}`;
                    const legacyStateMobileKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${id}-${sessionId}-mobile` : `${CHAT_WINDOW_STATE_KEY}-${id}-mobile`;
                    const legacyStateDesktopKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${id}-${sessionId}-desktop` : `${CHAT_WINDOW_STATE_KEY}-${id}-desktop`;
                    const localState = localStorage.getItem(stateKey) || localStorage.getItem(legacyStateDesktopKey) || localStorage.getItem(legacyStateMobileKey) || localStorage.getItem(sessionId ? `${CHAT_WINDOW_STATE_KEY}-${id}-${sessionId}` : `${CHAT_WINDOW_STATE_KEY}-${id}`) || localStorage.getItem(CHAT_WINDOW_STATE_KEY);
                    if (localState === 'minimized') {
                        minimize(id);
                    } else if (localState === 'maximized') {
                        entry.minimized = false;
                        entry.winbox.show();
                        try { entry.winbox.maximize(); } catch (e) { /* ignore */ }
                        try { applyMaximizeConstraints(entry); } catch (e) { /* ignore */ }
                        try { clampEntryToViewport(entry); } catch (e) { /* ignore */ }
                    } else {
                        entry.minimized = false;
                        entry.winbox.show();
                        entry.winbox.restore();
                        try { clampEntryToViewport(entry); } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }
                return true;
            }

            function create(opts) {
                if (!opts || !opts.mount) return null;
                if (typeof window.WinBox === 'undefined') {
                    try { console.debug('[SynthWindowManager] WinBox not present, starting loader for', opts.id); } catch (e) {}
                    if (!winboxLoading) {
                        winboxLoading = true;
                        ensureWinBoxAssets().then((ok) => {
                            winboxLoading = false;
                            try { console.debug('[SynthWindowManager] WinBox loader resolved:', ok); } catch (e) {}
                            if (ok) {
                                try { if (opts.id === 'chat') ensureChatWindow(); } catch (e) { /* ignore */ }
                            }
                        });
                    }
                    return null;
                }
                if (windows.has(opts.id)) return windows.get(opts.id).winbox;

                const mountEl = opts.mount;
                mountEl.classList.add('synth-window-managed');
                const entry = {
                    id: opts.id,
                    winbox: null,
                    dockButton: opts.dockButton || null,
                    dockClass: opts.dockClass || null,
                    dockLabel: opts.dockLabel || null,
                    iconText: opts.iconText || null,
                    minimized: false,
                    lastNormalRect: null
                };
                const className = `${opts.className || 'synth-winbox no-full no-close'} modern`;
                const desktopRoot = document.getElementById('desktop-root');
                const topbarOffset = getTopbarHeight() || 0;
                const winbox = new WinBox({
                    id: opts.id,
                    title: opts.title || 'Window',
                    mount: mountEl,
                    root: desktopRoot || undefined,
                    x: opts.x !== undefined ? opts.x : 24,
                    y: opts.y !== undefined ? opts.y : 'bottom',
                    top: topbarOffset,
                    width: opts.width || 420,
                    height: opts.height || '70%',
                    overflow: opts.overflow,
                    class: className
                });
                try { console.debug('[SynthWindowManager] created winbox for', opts.id, 'instance=', winbox); } catch (e) { /* ignore */ }
                entry.winbox = winbox;
                // Ensure we cleanup windows map when the WinBox instance is closed so
                // it can be recreated correctly on subsequent opens (hot-reload / repeated opens).
                try {
                    winbox.onclose = function() {
                        try {
                            windows.delete(opts.id);
                        } catch (e) { /* ignore */ }
                        try {
                            // Attempt to unmount and clear any references
                            if (winbox && winbox.g && winbox.g.winbox) winbox.g.winbox = null;
                        } catch (e) { /* ignore */ }
                    };
                } catch (e) { /* ignore */ }

                // Override the native maximize button behavior to use our toggleMaximize
                try {
                    const winEl = winbox.window || winbox.dom || winbox.g || null;
                    if (winEl) {
                        const maxBtn = winEl.querySelector('.wb-max');
                        if (maxBtn) {
                            maxBtn.addEventListener('click', (e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                toggleMaximize(opts.id);
                                return false;
                            }, true); // Use capture phase
                        }
                    }
                } catch (e) { /* ignore */ }

                // When WinBox toggles classes (e.g., via programmatic calls),
                // ensure maximize constraints are applied immediately by observing
                // class attribute changes on the WinBox root element.
                try {
                    const winEl = winbox.window || winbox.dom || winbox.g || null;
                    if (winEl && typeof MutationObserver !== 'undefined') {
                        const mo = new MutationObserver((mutations) => {
                            try {
                                // Only apply maximize constraints if the window is actually maximized
                                // and the class change involves max-related classes
                                const isMax = !!(entry.winbox.max || entry.winbox.maximized);
                                if (!isMax) return;

                                let hasMaxClassChange = false;
                                for (const mutation of mutations) {
                                    if (mutation.type === 'attributes' && mutation.attributeName === 'class') {
                                        const oldClass = mutation.oldValue || '';
                                        const newClass = winEl.className || '';
                                        if ((oldClass.includes('max') || newClass.includes('max')) ||
                                            (oldClass.includes('maximized') || newClass.includes('maximized'))) {
                                            hasMaxClassChange = true;
                                            break;
                                        }
                                    }
                                }
                                if (hasMaxClassChange) {
                                    applyMaximizeConstraints(entry);
                                }
                            } catch (e) { /* ignore */ }
                        });
                        mo.observe(winEl, { attributes: true, attributeFilter: ['class'], attributeOldValue: true });
                        entry._maximizeObserver = mo;
                    }
                } catch (e) { /* ignore */ }
                windows.set(opts.id, entry);
                ensureDockButton(entry);
                try { applyViewportInsets(entry); } catch (e) { /* ignore */ }
                try { clampEntryToViewport(entry); } catch (e) { /* ignore */ }
                return winbox;
            }

            function attachHeaderTools(id, winbox, tools) {
                // Prevent external callers from injecting header tools into the Debug window —
                // the Debug window manages its own header tools internally in `debug-window.mjs`.
                if (id === 'debug') return null;
                if (!winbox || !tools || !Array.isArray(tools)) return null;
                const winEl = winbox.window || winbox.dom || winbox.g || null;
                if (!winEl) return null;
                const drag = winEl.querySelector('.wb-drag');
                if (!drag) return null;
                let toolsEl = winEl.querySelector(`.synth-wb-tools[data-tools-id="${id}"]`);
                if (!toolsEl) {
                    toolsEl = document.createElement('div');
                    toolsEl.className = 'synth-wb-tools';
                    toolsEl.dataset.toolsId = id;
                    drag.appendChild(toolsEl);
                }
                toolsEl.innerHTML = '';
                tools.forEach((tool) => {
                    if (!tool) return;
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = `synth-wb-tool-btn${tool.className ? ` ${tool.className}` : ''}`;
                    btn.textContent = tool.label || '';
                    if (tool.title) {
                        btn.title = tool.title;
                        btn.setAttribute('aria-label', tool.title);
                    }
                    btn.addEventListener('pointerdown', (ev) => { try { ev.stopPropagation(); } catch (e) {} });
                    btn.addEventListener('click', (ev) => {
                        try { ev.stopPropagation(); } catch (e) {}
                        try { if (typeof tool.onClick === 'function') tool.onClick(); } catch (e) { /* ignore */ }
                    });
                    toolsEl.appendChild(btn);
                });
                return toolsEl;
            }

            function has(id) {
                return windows.has(id);
            }

            function get(id) {
                return windows.has(id) ? windows.get(id).winbox : null;
            }

            function ensureChatWindow() {
                try {
                    if (window.SynthWindowManager && typeof window.SynthWindowManager.ensureChatWindow === 'function') {
                        return window.SynthWindowManager.ensureChatWindow();
                    }
                    // Fallback: lazy-create the chat window via module
                    try { import('./chat-window.mjs').then((mod) => { try { if (mod && typeof mod.createChatWindow === 'function') mod.createChatWindow(); } catch (e) {} }).catch(() => {}); } catch (e) {}
                    return null;
                } catch (e) { return null; }
            }

            try {
                window.addEventListener('resize', () => {
                    try { windows.forEach((entry) => { applyViewportInsets(entry); applyMaximizeConstraints(entry); clampEntryToViewport(entry); }); } catch (e) { /* ignore */ }
                });
            } catch (e) { /* ignore */ }

            return {
                create,
                has,
                get,
                minimize,
                restore,
                toggleMaximize,
                saveState,
                restoreState,
                ensureChatWindow,
                ensureDock,
                ensureWinBoxAssets,
                attachHeaderTools
            };
        })();

        window.SynthWindowManager = window.SynthWindowManager || synthWindowManager;

        // Backwards-compatible helper to allow top-level calls like attachHeaderTools('chat', winbox, ...)
        function attachHeaderTools(id, winbox, tools) {
            try {
                if (window.SynthWindowManager && typeof window.SynthWindowManager.attachHeaderTools === 'function') {
                    return window.SynthWindowManager.attachHeaderTools(id, winbox, tools);
                }
            } catch (e) { /* ignore */ }
            return null;
        }

        // Phase priorities mirrored from server-side ActionStateManager
        const PHASE_PRIORITIES = {
            'IDLE': 0,
            'WRITING': 3,
            'TALKING': 5,
            'CORRECTING': 7,
            'THINKING': 10
        };

        // Expose a lightweight config refresh helper so modules can request an
        // update of runtime UI-config without hard dependency ordering. The
        // function is intentionally minimal: engines or other modules can
        // override `window.refreshConfig` with a richer implementation when
        // available.
        async function refreshConfig() {
            try {
                const configGeneralListEl = document.getElementById('config-general-list');
                const configAdvancedListEl = document.getElementById('config-advanced-list');
                const configDisclaimerEl = document.getElementById('config-env-disclaimer');
                const configAdvancedWarningEl = document.getElementById('config-advanced-warning');
                if (!configGeneralListEl || !configAdvancedListEl) return;
                const response = await fetch('/api/config');
                if (!response.ok) throw new Error('HTTP ' + response.status);
                const payload = await response.json();
                const items = Array.isArray(payload.items) ? payload.items : [];

                if (payload.messages && configDisclaimerEl) {
                    if (payload.messages.env_override) configDisclaimerEl.textContent = payload.messages.env_override;
                }
                if (payload.messages && configAdvancedWarningEl) {
                    if (payload.messages.advanced_warning) configAdvancedWarningEl.textContent = payload.messages.advanced_warning;
                }

                const renderList = (list, container) => {
                    container.innerHTML = '';
                    if (!list.length) {
                        const empty = document.createElement('div');
                        empty.className = 'meta';
                        empty.textContent = 'No configuration entries found.';
                        container.appendChild(empty);
                        return;
                    }
                    list.forEach((item) => {
                        const row = document.createElement('div');
                        row.className = 'config-row';

                        const labelLine = document.createElement('div');
                        labelLine.className = 'config-label-line';
                        const label = document.createElement('span');
                        label.textContent = item.label || item.key || 'Unnamed';
                        labelLine.appendChild(label);
                        if (item.env_override) {
                            const override = document.createElement('span');
                            override.className = 'override-icon';
                            override.textContent = '⚠️';
                            labelLine.appendChild(override);
                        }
                        row.appendChild(labelLine);

                        if (item.description) {
                            const desc = document.createElement('div');
                            desc.className = 'config-description';
                            desc.textContent = item.description;
                            row.appendChild(desc);
                        }

                        const inputWrap = document.createElement('div');
                        inputWrap.className = 'config-input';

                        const value = item.value === null || item.value === undefined ? '' : item.value;
                        const isEditable = !!item.editable && !item.env_override;

                        let inputEl = null;
                        let extraEl = null;
                        if (item.ui_type === 'bool' || item.value_type === 'bool') {
                            const checkbox = document.createElement('input');
                            const key = item.key || item.label || `bool-${Math.random().toString(36).slice(2)}`;
                            checkbox.type = 'checkbox';
                            checkbox.id = `config-${key}`;
                            checkbox.checked = value === true || value === 1 || value === '1' || value === 'true';
                            checkbox.disabled = !isEditable;
                            inputEl = checkbox;
                            const toggleLabel = document.createElement('label');
                            toggleLabel.className = 'toggle-switch';
                            toggleLabel.setAttribute('for', checkbox.id);
                            const slider = document.createElement('span');
                            slider.className = 'toggle-slider';
                            toggleLabel.appendChild(slider);
                            extraEl = toggleLabel;
                        } else if (item.ui_type === 'select' && Array.isArray(item.options) && item.options.length) {
                            const select = document.createElement('select');
                            item.options.forEach((opt) => {
                                const option = document.createElement('option');
                                option.value = opt;
                                option.textContent = opt;
                                if (String(value) === String(opt)) option.selected = true;
                                select.appendChild(option);
                            });
                            select.disabled = !isEditable;
                            inputEl = select;
                        } else if (item.ui_type === 'file') {
                            // File upload control for exposed file variables
                            const fileWrap = document.createElement('div');
                            fileWrap.className = 'file-upload-wrap';

                            const current = document.createElement('div');
                            current.className = 'file-current';
                            if (value) {
                                try {
                                    // Show a download link pointing to the file endpoint
                                    const link = document.createElement('a');
                                    link.textContent = (typeof value === 'string') ? value.split('/').pop() : 'file';
                                    link.href = `/api/config/${encodeURIComponent(item.key)}/file`;
                                    link.target = '_blank';
                                    current.appendChild(link);
                                } catch (e) {
                                    current.textContent = String(value);
                                }
                            } else {
                                current.textContent = 'No file uploaded.';
                            }

                            const inputFile = document.createElement('input');
                            inputFile.type = 'file';
                            inputFile.disabled = !isEditable;

                            const uploadBtn = document.createElement('button');
                            uploadBtn.textContent = 'Upload';
                            uploadBtn.disabled = !isEditable;
                            uploadBtn.addEventListener('click', async () => {
                                const f = inputFile.files && inputFile.files[0];
                                if (!f) { alert('Select a file to upload'); return; }
                                try {
                                    const fd = new FormData();
                                    fd.append('file', f);
                                    uploadBtn.disabled = true;
                                    uploadBtn.textContent = 'Uploading...';
                                    const res = await fetch(`/api/config/${encodeURIComponent(item.key)}/upload`, { method: 'POST', body: fd });
                                    if (!res.ok) {
                                        const txt = await res.text();
                                        alert('Upload failed: ' + txt);
                                    } else {
                                        await refreshConfig();
                                    }
                                } catch (e) {
                                    console.error('[synth_webui] File upload failed', e);
                                    alert('File upload failed');
                                } finally {
                                    uploadBtn.disabled = false;
                                    uploadBtn.textContent = 'Upload';
                                }
                            });

                            fileWrap.appendChild(current);
                            fileWrap.appendChild(inputFile);
                            fileWrap.appendChild(uploadBtn);

                            inputEl = fileWrap;
                        } else if (item.ui_type === 'textarea' || item.value_type === 'json') {
                            const textarea = document.createElement('textarea');
                            textarea.rows = 3;
                            textarea.value = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
                            textarea.disabled = !isEditable;
                            inputEl = textarea;
                        } else {
                            const input = document.createElement('input');
                            input.type = item.ui_type === 'password' ? 'password' : (item.value_type === 'int' || item.value_type === 'float' || item.ui_type === 'number' ? 'number' : 'text');
                            input.value = typeof value === 'string' ? value : JSON.stringify(value);
                            input.disabled = !isEditable;
                            inputEl = input;
                        }

                        if (inputEl) inputWrap.appendChild(inputEl);
                        if (extraEl) inputWrap.appendChild(extraEl);

                        // Attach save handlers for editable inputs so pressing Enter or changing
                        // selects/checkboxes persists values via the /api/config endpoint
                        if (isEditable && inputEl) {
                            const saveValue = async (val) => {
                                try {
                                    // Provide immediate visual feedback by disabling control
                                    if (inputEl && typeof inputEl.disabled !== 'undefined') inputEl.disabled = true;
                                    const payload = { key: item.key, value: val };
                                    const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                                    if (!res.ok) {
                                        const txt = await res.text();
                                        try { window.showToast('Save failed: ' + txt, true); } catch (e) {}
                                    } else {
                                        try {
                                            const out = await res.json();
                                            window.showToast('Saved', false);
                                            if (out && out.requires_reload) {
                                                window.showToast(out.message || 'Component reload recommended', false);
                                            }
                                            // Refresh configuration so UI reflects authoritative values
                                            try { await refreshConfig(); } catch (e) { /* ignore */ }
                                        } catch (e) {
                                            window.showToast('Saved', false);
                                        }
                                    }
                                } catch (e) {
                                    try { window.showToast('Save failed', true); } catch (e) {}
                                } finally {
                                    try { if (inputEl && typeof inputEl.disabled !== 'undefined') inputEl.disabled = false; } catch (e) {}
                                }
                            };

                            // Checkbox
                            if (inputEl.tagName && inputEl.tagName.toLowerCase() === 'input' && inputEl.type === 'checkbox') {
                                inputEl.addEventListener('change', () => { saveValue(inputEl.checked); });
                            } else if (inputEl.tagName && inputEl.tagName.toLowerCase() === 'select') {
                                inputEl.addEventListener('change', () => { saveValue(inputEl.value); });
                            } else if (inputEl.tagName && inputEl.tagName.toLowerCase() === 'textarea') {
                                // Ctrl+Enter to submit JSON/textarea; blur to auto-save
                                let debounced = null;
                                inputEl.addEventListener('keydown', (ev) => {
                                    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
                                        ev.preventDefault();
                                        saveValue(inputEl.value);
                                    }
                                    // simple debounce to avoid excessive saves on blur
                                    if (debounced) clearTimeout(debounced);
                                });
                                inputEl.addEventListener('blur', () => { debounced = setTimeout(() => saveValue(inputEl.value), 150); });
                            } else {
                                // Default: single-line inputs — Enter to save, blur to save
                                inputEl.addEventListener('keydown', (ev) => {
                                    if (ev.key === 'Enter' && !ev.shiftKey && !ev.ctrlKey && !ev.metaKey) {
                                        ev.preventDefault();
                                        saveValue(inputEl.value);
                                    }
                                });
                                inputEl.addEventListener('blur', () => { saveValue(inputEl.value); });
                            }
                        }

                        row.appendChild(inputWrap);
                        container.appendChild(row);
                    });
                };

                const general = items.filter((item) => !item.advanced);
                const advanced = items.filter((item) => item.advanced);
                renderList(general, configGeneralListEl);
                renderList(advanced, configAdvancedListEl);
            } catch (e) {
                console.error('[synth_webui] Failed to load configuration', e);
                const configGeneralListEl = document.getElementById('config-general-list');
                const configAdvancedListEl = document.getElementById('config-advanced-list');
                if (configGeneralListEl) configGeneralListEl.innerHTML = '<div class="meta">Failed to load configuration.</div>';
                if (configAdvancedListEl) configAdvancedListEl.innerHTML = '<div class="meta">Failed to load configuration.</div>';
            }
        }
        window.refreshConfig = window.refreshConfig || refreshConfig;

        // -----------------------------------------------------------------------------
        // Core UI wiring (navigation + chat controls + WebSocket)
        // -----------------------------------------------------------------------------
        (function(){
            'use strict';

            async function loadComponentsSummary() {
                try {
                    const componentsLLMSummaryEl = document.getElementById('components-llm-summary');
                    const componentsLLMListEl = document.getElementById('components-llm-list');
                    const componentsInterfacesListEl = document.getElementById('components-interfaces-list');
                    const componentsPluginsListEl = document.getElementById('components-plugins-list');
                    if (!componentsLLMListEl || !componentsInterfacesListEl || !componentsPluginsListEl) return;
                    const res = await fetch('/api/components');
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    const data = await res.json();

                    if (componentsLLMSummaryEl && data.llm) {
                        componentsLLMSummaryEl.textContent = `Active engine: ${data.llm.active || '—'}`;
                    }

                    const llmSelect = document.getElementById('llm-engine-select');
                    const llmModelLabel = document.getElementById('llm-engine-model');
                    const llmLoginStateLabel = document.getElementById('llm-engine-login-state');
                    const llmLoginBtn = document.getElementById('llm-login-btn');
                    const devToggle = document.getElementById('dev-components-toggle');

                    if (llmSelect && data.llm && Array.isArray(data.llm.engines)) {
                        const engines = data.llm.engines.slice().sort((a, b) => {
                            const an = (a.display_name || a.name || '').toLowerCase();
                            const bn = (b.display_name || b.name || '').toLowerCase();
                            return an.localeCompare(bn);
                        });
                        llmSelect.innerHTML = '';
                        engines.forEach((engine) => {
                            const opt = document.createElement('option');
                            opt.value = engine.name;
                            opt.textContent = engine.display_name || engine.name || 'LLM';
                            if (engine.active) opt.selected = true;
                            llmSelect.appendChild(opt);
                        });

                        const resolveActive = () => engines.find(e => e.active) || engines.find(e => e.name === data.llm.active) || engines[0] || null;
                        const updateLlmInfo = (engine) => {
                            if (llmModelLabel) llmModelLabel.textContent = `model: ${engine ? (engine.display_name || engine.name || '—') : '—'}`;
                            const loginState = engine ? (engine.login_state || (engine.logged_in ? 'logged' : 'unlogged')) : '—';
                            if (llmLoginStateLabel) llmLoginStateLabel.textContent = `state: ${loginState}`;
                            if (llmLoginBtn) {
                                llmLoginBtn.disabled = !engine || !engine.loaded;
                                llmLoginBtn.textContent = engine && engine.logged_in ? 'Logged' : 'Login';
                            }
                        };
                        updateLlmInfo(resolveActive());

                        if (!llmSelect.dataset.bound) {
                            llmSelect.addEventListener('change', async () => {
                                const selected = llmSelect.value;
                                if (!selected) return;
                                try {
                                    const res = await fetch('/api/components/llm', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ name: selected })
                                    });
                                    if (!res.ok) throw new Error('HTTP ' + res.status);
                                    await loadComponentsSummary();
                                } catch (e) {
                                    console.error('[synth_webui] Failed to switch LLM', e);
                                    alert('Failed to switch LLM engine.');
                                }
                            });
                            llmSelect.dataset.bound = '1';
                        }

                        if (llmLoginBtn && !llmLoginBtn.dataset.bound) {
                            llmLoginBtn.addEventListener('click', async () => {
                                const selected = llmSelect.value;
                                if (!selected) return;
                                try {
                                    const res = await fetch('/api/components/llm/login', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ name: selected })
                                    });
                                    if (!res.ok) throw new Error('HTTP ' + res.status);
                                    await loadComponentsSummary();
                                } catch (e) {
                                    console.error('[synth_webui] Failed to start LLM login', e);
                                    alert('LLM login flow failed to start.');
                                }
                            });
                            llmLoginBtn.dataset.bound = '1';
                        }
                    }

                    if (devToggle) {
                        devToggle.checked = !!data.dev_components_enabled;
                        if (!devToggle.dataset.bound) {
                            devToggle.addEventListener('change', async () => {
                                try {
                                    const res = await fetch('/api/components/dev/toggle', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ enabled: devToggle.checked })
                                    });
                                    const resp = await res.json();
                                    if (!res.ok) throw new Error(resp.detail || 'Request failed');
                                    if (resp.message) {
                                        alert(resp.message);
                                    }
                                } catch (e) {
                                    console.error('[synth_webui] Failed to toggle dev components', e);
                                    alert('Failed to toggle dev components.');
                                    devToggle.checked = !devToggle.checked;
                                }
                            });
                            devToggle.dataset.bound = '1';
                        }
                    }

                    const renderDetailsList = (items, container) => {
                        container.innerHTML = '';
                        if (!items || !items.length) {
                            const empty = document.createElement('div');
                            empty.className = 'meta';
                            empty.textContent = 'No components found.';
                            container.appendChild(empty);
                            return;
                        }
                        items.forEach((item) => {
                            const statusRaw = (item.status || 'unknown');
                            const statusValue = String(statusRaw).toLowerCase();
                            const statusClass = (['ok', 'ready', 'success', 'running', 'active'].includes(statusValue))
                                ? 'success'
                                : (['fail', 'failed', 'error', 'broken', 'disabled'].includes(statusValue))
                                    ? 'failed'
                                    : (['loading', 'starting', 'pending'].includes(statusValue))
                                        ? 'loading'
                                        : 'unknown';
                            const details = document.createElement('details');
                            details.className = 'component-item';
                            details.open = true;
                            const summary = document.createElement('summary');
                            const summaryMain = document.createElement('span');
                            summaryMain.className = 'component-summary-main';
                            const name = document.createElement('span');
                            name.className = 'component-name';
                            name.textContent = item.display_name || item.name || 'Component';
                            summaryMain.appendChild(name);
                            summary.appendChild(summaryMain);

                            const summaryActions = document.createElement('span');
                            summaryActions.className = 'component-summary-actions';
                            const status = document.createElement('span');
                            status.className = 'component-status component-status-' + statusClass;
                            status.textContent = statusValue;
                            summaryActions.appendChild(status);
                            summary.appendChild(summaryActions);
                            details.appendChild(summary);

                            const desc = document.createElement('div');
                            desc.className = 'component-description';
                            desc.textContent = item.details || item.description || '';
                            details.appendChild(desc);

                            if (item.error) {
                                const err = document.createElement('div');
                                err.className = 'component-error';
                                err.textContent = item.error;
                                details.appendChild(err);
                            }

                            if (Array.isArray(item.actions) && item.actions.length) {
                                const actionsWrap = document.createElement('div');
                                actionsWrap.className = 'component-actions';
                                const heading = document.createElement('div');
                                heading.className = 'component-actions-heading';
                                heading.textContent = `Actions (${item.actions.length})`;
                                actionsWrap.appendChild(heading);
                                const list = document.createElement('ul');
                                list.className = 'component-action-list';
                                item.actions.forEach((action) => {
                                    const li = document.createElement('li');
                                    const title = document.createElement('div');
                                    title.className = 'component-action-title';
                                    title.textContent = action.type || action.name || 'Action';
                                    li.appendChild(title);
                                    if (action.description) {
                                        const d = document.createElement('div');
                                        d.className = 'component-action-description';
                                        d.textContent = action.description;
                                        li.appendChild(d);
                                    }
                                    list.appendChild(li);
                                });
                                actionsWrap.appendChild(list);
                                details.appendChild(actionsWrap);
                            }
                            container.appendChild(details);
                        });
                    };

                    renderDetailsList(data.llm && data.llm.engines ? data.llm.engines : [], componentsLLMListEl);
                    renderDetailsList(data.interfaces || [], componentsInterfacesListEl);
                    renderDetailsList(data.plugins || [], componentsPluginsListEl);
                } catch (e) {
                    console.error('[synth_webui] Failed to load components', e);
                    const componentsLLMListEl = document.getElementById('components-llm-list');
                    const componentsInterfacesListEl = document.getElementById('components-interfaces-list');
                    const componentsPluginsListEl = document.getElementById('components-plugins-list');
                    if (componentsLLMListEl) componentsLLMListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    if (componentsInterfacesListEl) componentsInterfacesListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    if (componentsPluginsListEl) componentsPluginsListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                }
            }

            function safeEscapeHtml(text) {
                try {
                    if (window.SynthUtils && typeof window.SynthUtils.escapeHtml === 'function') {
                        return window.SynthUtils.escapeHtml(text);
                    }
                } catch (e) { /* ignore */ }
                const div = document.createElement('div');
                div.textContent = text === undefined || text === null ? '' : String(text);
                return div.innerHTML;
            }

            function setActiveTab(tab) {
                if (!tab) return;
                const buttons = document.querySelectorAll('.nav-btn[data-tab]');
                const panels = document.querySelectorAll('.tab-panel[data-tab]');
                buttons.forEach(btn => {
                    const isActive = btn.getAttribute('data-tab') === tab;
                    btn.classList.toggle('active', isActive);
                    btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
                });
                panels.forEach(panel => {
                    const isActive = panel.getAttribute('data-tab') === tab;
                    panel.classList.toggle('active', isActive);
                    if (isActive) {
                        panel.removeAttribute('aria-hidden');
                    } else {
                        panel.setAttribute('aria-hidden', 'true');
                    }
                });
                if (tab === 'home' && typeof window.resizeVRMRenderer === 'function') {
                    setTimeout(() => {
                        try { window.resizeVRMRenderer(); } catch (e) { /* ignore */ }
                    }, 150);
                }
            }

            function setupNavigation() {
                const navButtons = document.querySelectorAll('.nav-btn[data-tab]');
                const nav = document.querySelector('nav.main-nav');

                function updateTopbarHeight() {
                    const header = document.querySelector('header.top-bar');
                    if (!header) return;
                    try {
                        const height = header.offsetHeight || header.getBoundingClientRect().height;
                        if (height && document.documentElement) {
                            document.documentElement.style.setProperty('--topbar-height', `${Math.round(height)}px`);
                        }
                    } catch (e) { /* ignore */ }
                }

                function measureNavRequiredWidth(nav) {
                    try {
                        if (!nav) return 0;
                        const prevWrap = nav.style.flexWrap;
                        const prevWidth = nav.style.width;
                        const prevMaxWidth = nav.style.maxWidth;
                        nav.style.flexWrap = 'nowrap';
                        nav.style.width = 'max-content';
                        nav.style.maxWidth = 'none';
                        const required = nav.getBoundingClientRect().width || nav.scrollWidth || 0;
                        nav.style.flexWrap = prevWrap;
                        nav.style.width = prevWidth;
                        nav.style.maxWidth = prevMaxWidth;
                        return required;
                    } catch (e) { /* ignore */ }
                    return 0;
                }

                function adjustTopbarLayout() {
                    try {
                        const header = document.querySelector('header.top-bar');
                        const brand = header ? header.querySelector('.brand') : null;
                        const brandText = brand ? brand.querySelector('.brand-text') : null;
                        const nav = header ? header.querySelector('nav.main-nav') : null;
                        if (!header || !brand || !nav) return;

                        const headerStyle = window.getComputedStyle ? window.getComputedStyle(header) : null;
                        const paddingLeft = headerStyle ? (Number.parseFloat(headerStyle.paddingLeft) || 0) : 0;
                        const paddingRight = headerStyle ? (Number.parseFloat(headerStyle.paddingRight) || 0) : 0;
                        const gapVal = headerStyle ? (headerStyle.columnGap || headerStyle.gap || '0') : '0';
                        const headerGap = Number.parseFloat(gapVal) || 0;

                        header.classList.remove('topbar--compact');
                        header.classList.remove('topbar--wrap');

                        const headerWidth = header.getBoundingClientRect().width || 0;
                        const brandWidth = brand.getBoundingClientRect().width || 0;
                        const navRequired = measureNavRequiredWidth(nav);
                        const available = Math.max(0, headerWidth - paddingLeft - paddingRight - brandWidth - headerGap);

                        const tolerance = 2;
                        let compact = navRequired > (available + tolerance);
                        let lines = 1;
                        try {
                            const buttons = Array.from(nav.querySelectorAll('.nav-btn'));
                            const tops = buttons.map((btn) => Math.round(btn.getBoundingClientRect().top));
                            const unique = Array.from(new Set(tops));
                            lines = unique.length || 1;
                        } catch (e) { /* ignore */ }
                        if (lines >= 3) {
                            compact = true;
                        }
                        if (compact && brandText) {
                            header.classList.add('topbar--compact');
                        }

                        const brandWidthCompact = brand.getBoundingClientRect().width || 0;
                        const availableCompact = Math.max(0, headerWidth - paddingLeft - paddingRight - brandWidthCompact - headerGap);
                        const wrap = navRequired > (availableCompact + tolerance);
                        if (wrap) {
                            header.classList.add('topbar--wrap');
                        }
                    } catch (e) { /* ignore */ }
                }

                // Desktop iframe helper
                function setupDesktopIframe(initialSection) {
                    try {
                        const iframe = document.getElementById('desktop-iframe');
                        if (!iframe) return null;
                        iframe.style.display = '';
                        // Mark document so CSS can hide redundant tab-panels while iframe is active
                        try { document.documentElement.classList.add('desktop-iframe-enabled'); } catch (e) { /* ignore */ }
                        const desired = '/iframe/' + (initialSection || 'home');
                        if (iframe.getAttribute('src') !== desired) iframe.setAttribute('src', desired);
                        return iframe;
                    } catch (e) { /* ignore */ return null; }
                }

                // Helper to toggle page-level scrolling for long document-style tabs
                function _adjustPageScroll(tab) {
                    try {
                        if (['settings', 'components', 'about'].includes(tab)) {
                            document.documentElement.style.overflow = 'auto';
                            document.body.style.overflow = 'auto';
                            try { const targetPanel = document.querySelector(`[data-tab="${tab}"]`); if (targetPanel) targetPanel.scrollIntoView({ behavior: 'auto', block: 'start' }); } catch (e) { /* ignore */ }
                        } else {
                            document.documentElement.style.overflow = 'hidden';
                            document.body.style.overflow = 'hidden';
                        }
                    } catch (e) { /* ignore */ }
                }

                navButtons.forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const tab = btn.getAttribute('data-tab');
                        if (!tab) return;
                        try {
                            setActiveTab(tab);
                            try { window.activeTab = tab; if (localStorage && localStorage.setItem) localStorage.setItem('synth-webui-active-tab', tab); } catch (e) { /* ignore */ }

                            // If the desktop is embedded in an iframe, post a message to request a section load
                            if (window.SynthConfig && window.SynthConfig.DESKTOP_IFRAME) {
                                try {
                                    const iframe = document.getElementById('desktop-iframe');
                                    if (iframe && iframe.contentWindow) {
                                        iframe.contentWindow.postMessage({ type: 'load', section: tab }, window.location.origin);
                                    }
                                } catch (e) { /* ignore */ }
                            } else {
                                if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                                    await window.SynthWebUI.loadSection(tab);
                                }
                            }

                            if (tab === 'history' && window.SynthWebUI && typeof window.SynthWebUI.initHistoryTab === 'function') {
                                try { window.SynthWebUI.initHistoryTab(); } catch (e) { /* ignore */ }
                            }

                            // Ensure page-level scrolling behavior matches the active tab
                            _adjustPageScroll(tab);
                        } catch (e) {
                            console.warn('[synth_webui] tab switch failed', e);
                        }
                    });
                });

                // Restore last active tab and load its section once.
                try {
                    const saved = (localStorage && localStorage.getItem && localStorage.getItem('synth-webui-active-tab')) || 'home';
                    setActiveTab(saved);
                    try { setupDesktopIframe(saved); } catch (e) { /* ignore */ }
                    // Ensure page scroll state matches the restored tab (fix for settings not scrollable)
                    try { _adjustPageScroll(saved); } catch (e) { /* ignore */ }
                } catch (e) {
                    setActiveTab('home');
                    try { setupDesktopIframe('home'); } catch (e) { /* ignore */ }
                }

                try {
                    if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                        window.SynthWebUI.loadSection('history').then(() => {
                            if (window.SynthWebUI && typeof window.SynthWebUI.initHistoryTab === 'function') {
                                window.SynthWebUI.initHistoryTab();
                            }
                        });
                    }
                } catch (e) { /* ignore */ }

                updateTopbarHeight();
                window.addEventListener('resize', updateTopbarHeight);
                try {
                    const header = document.querySelector('header.top-bar');
                    if (header && typeof ResizeObserver !== 'undefined') {
                        const ro = new ResizeObserver(() => { try { updateTopbarHeight(); adjustTopbarLayout(); } catch (e) { /* ignore */ } });
                        ro.observe(header);
                    }
                } catch (e) { /* ignore */ }
                try { adjustTopbarLayout(); } catch (e) { /* ignore */ }
            }

            function getSynthDisplayName() {
                try {
                    if (window.SynthConfig && window.SynthConfig.SYNTH_NAME) return window.SynthConfig.SYNTH_NAME;
                    if (window.SynthConfig && window.SynthConfig.BRAND_NAME) return window.SynthConfig.BRAND_NAME;
                    const headerName = document.querySelector('.brand-text h1');
                    if (headerName && headerName.textContent) return headerName.textContent.trim();
                } catch (e) { /* ignore */ }
                return 'SyntH';
            }

            function appendMessage(container, sender, text) {
                try { if (window.SynthChat && typeof window.SynthChat.appendMessage === 'function') return window.SynthChat.appendMessage(container, sender, text); } catch (e) { /* ignore */ }
            }

            function setupChatControls() {
                try { if (window.SynthChat && typeof window.SynthChat.initChatUI === 'function') return window.SynthChat.initChatUI(); } catch (e) { /* ignore */ }
            }

            function setupChatMessaging() {
                try { if (window.SynthChat && typeof window.SynthChat.initChatUI === 'function') return window.SynthChat.initChatUI(); } catch (e) { /* ignore */ }
            }

            function initHomeTab() {
                if (window.__synth_home_initialized) return;
                const messagesEl = document.getElementById('messages');
                const inputEl = document.getElementById('input');
                if (!messagesEl || !inputEl) return;
                try {
                    if (window.SynthWindowManager && typeof window.SynthWindowManager.ensureChatWindow === 'function') {
                        window.SynthWindowManager.ensureChatWindow();
                    }
                } catch (e) { /* ignore */ }
                // Delegate chat UI to the chat-window module
                try {
                    import('./chat-window.mjs').then((mod) => {
                        try { if (mod && typeof mod.createChatWindow === 'function') mod.createChatWindow(); } catch (e) { /* ignore */ }
                        try { if (mod && typeof mod.initChatUI === 'function') mod.initChatUI(); } catch (e) { /* ignore */ }
                    }).catch((e) => { console.debug('[synth_webui] chat-window import failed', e); });
                } catch (e) { /* ignore */ }
                window.__synth_home_initialized = true;
                try {
                    if (window.SynthChat && typeof window.SynthChat.restoreChatState === 'function') {
                        window.SynthChat.restoreChatState();
                    }
                } catch (e) { /* ignore */ }
            }

            function initSettingsTab() {
                if (!window.__synth_settings_initialized) {
                    const resetBtn = document.getElementById('reset-window-positions');
                    if (resetBtn) {
                        resetBtn.addEventListener('click', () => {
                            try {
                                const keys = [];
                                for (let i = 0; i < localStorage.length; i++) {
                                    const k = localStorage.key(i);
                                    if (!k) continue;
                                    if (k.startsWith('synth-webui-window-state') || k.startsWith('synth-webui-chat-rect')) {
                                        keys.push(k);
                                    }
                                }
                                keys.forEach(k => localStorage.removeItem(k));
                            } catch (e) { /* ignore */ }

                            // If WinBox is managing the chat, restore its state and reposition
                            try {
                                if (window.SynthWindowManager && typeof window.SynthWindowManager.ensureChatWindow === 'function') {
                                    try { window.SynthWindowManager.ensureChatWindow(); } catch (e) {}
                                    try { if (window.SynthWindowManager.has && typeof window.SynthWindowManager.has === 'function' && window.SynthWindowManager.has('chat')) { window.SynthWindowManager.restore('chat'); } } catch (e) {}
                                    try { if (typeof window.resetWindowPositions === 'function') window.resetWindowPositions(); } catch (e) {}
                                    return;
                                }
                            } catch (e) { /* ignore */ }

                            // Fallback for non-winbox chat: clear inline styles and classes
                            try {
                                const chat = document.getElementById('chat');
                                if (chat) {
                                    chat.classList.remove('minimized', 'maximized', 'expanded', 'hidden');
                                    chat.style.left = '';
                                    chat.style.top = '';
                                    chat.style.right = '';
                                    chat.style.bottom = '';
                                    chat.style.width = '';
                                    chat.style.height = '';
                                }
                            } catch (e) { /* ignore */ }
                        });
                    }
                    initNotifications();
                    window.__synth_settings_initialized = true;
                }
                refreshConfig();
            }

            function initComponentsTab() {
                loadComponentsSummary();
            }

            function initLogsTab() {
                if (window.__synth_logs_initialized) return;
                const logOutput = document.getElementById('log-output');
                if (!logOutput) return;
                const logAutoscroll = document.getElementById('logs-autoscroll');
                const logFilters = document.querySelectorAll('.log-filter');
                const logSearchInput = document.getElementById('log-search');
                const logsRefreshBtn = document.getElementById('logs-refresh');

                function detectLevel(text) {
                    const match = String(text || '').match(/\b(DEBUG|INFO|WARNING|ERROR)\b/i);
                    if (!match) return 'other';
                    return match[1].toLowerCase();
                }

                function isLevelEnabled(level) {
                    const checkbox = document.querySelector(`.log-filter[data-level="${level}"]`);
                    if (!checkbox) return true;
                    return checkbox.checked;
                }

                function scrollLogsToBottom() {
                    try {
                        if (logOutput) {
                            logOutput.scrollTop = logOutput.scrollHeight;
                        }
                    } catch (e) { /* ignore */ }
                }

                function applyFilters() {
                    const search = (logSearchInput && logSearchInput.value || '').trim().toLowerCase();
                    const children = Array.from(logOutput.querySelectorAll('.log-line'));
                    children.forEach((line) => {
                        const level = line.dataset.level || 'other';
                        const levelOk = isLevelEnabled(level);
                        const textOk = !search || (line.textContent || '').toLowerCase().includes(search);
                        line.style.display = (levelOk && textOk) ? '' : 'none';
                    });
                    const auto = logAutoscroll ? logAutoscroll.checked : true;
                    if (auto) {
                        scrollLogsToBottom();
                    }
                }

                function appendLogLine(text) {
                    const level = detectLevel(text);
                    const line = document.createElement('div');
                    line.className = `log-line level-${level}`;
                    line.dataset.level = level;
                    line.textContent = text;
                    logOutput.appendChild(line);
                    const auto = logAutoscroll ? logAutoscroll.checked : true;
                    if (auto) {
                        scrollLogsToBottom();
                    }
                    applyFilters();
                }

                function connectLogs() {
                    if (window.__synth_logs_socket && (window.__synth_logs_socket.readyState === WebSocket.OPEN || window.__synth_logs_socket.readyState === WebSocket.CONNECTING)) {
                        return;
                    }
                    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
                    const ws = new WebSocket(`${protocol}://${window.location.host}/logs`);
                    window.__synth_logs_socket = ws;

                    ws.onmessage = (event) => {
                        appendLogLine(event.data);
                    };
                    ws.onclose = () => {
                        setTimeout(connectLogs, 2000);
                    };
                }

                logFilters.forEach((checkbox) => {
                    checkbox.addEventListener('change', applyFilters);
                });
                if (logSearchInput) logSearchInput.addEventListener('input', applyFilters);
                if (logsRefreshBtn) {
                    logsRefreshBtn.addEventListener('click', () => {
                        try {
                            if (window.__synth_logs_socket) {
                                window.__synth_logs_socket.close();
                            }
                        } catch (e) { /* ignore */ }
                        connectLogs();
                    });
                }

                connectLogs();
                window.__synth_logs_initialized = true;
            }

            async function initAboutTab() {
                if (window.__synth_about_initialized) return;
                const uptimeEl = document.getElementById('stats-uptime');
                const sessionsEl = document.getElementById('stats-sessions');
                const componentsEl = document.getElementById('stats-components');
                const messagesEl = document.getElementById('stats-messages');
                const versionEl = document.getElementById('system-version');
                const pythonEl = document.getElementById('system-python');
                const platformEl = document.getElementById('system-platform');
                const databaseEl = document.getElementById('system-database');
                if (!uptimeEl && !sessionsEl && !versionEl) return;

                try {
                    const res = await fetch('/api/about');
                    if (res.ok) {
                        const data = await res.json();
                        if (uptimeEl && data.uptime !== undefined) uptimeEl.textContent = formatUptime(data.uptime);
                        if (sessionsEl && data.sessions !== undefined) sessionsEl.textContent = String(data.sessions);
                        if (componentsEl && data.components !== undefined) componentsEl.textContent = String(data.components);
                        if (messagesEl && data.messages_today !== undefined && data.messages_today !== null) messagesEl.textContent = String(data.messages_today);
                        if (versionEl && data.version) versionEl.textContent = data.version;
                        if (pythonEl && data.python) pythonEl.textContent = data.python;
                        if (platformEl && data.platform) platformEl.textContent = data.platform;
                        if (databaseEl && data.database) databaseEl.textContent = data.database;
                    }
                } catch (e) {
                    console.warn('[synth_webui] about load failed', e);
                }

                try {
                    if (componentsEl && (!componentsEl.textContent || componentsEl.textContent === '--')) {
                        const res = await fetch('/api/components');
                        if (res.ok) {
                            const payload = await res.json();
                            const total = (payload.llm && payload.llm.engines ? payload.llm.engines.length : 0)
                                + (payload.interfaces ? payload.interfaces.length : 0)
                                + (payload.plugins ? payload.plugins.length : 0);
                            componentsEl.textContent = String(total);
                        }
                    }
                } catch (e) { /* ignore */ }

                window.__synth_about_initialized = true;
            }

            function formatUptime(seconds) {
                const total = Number(seconds) || 0;
                const days = Math.floor(total / 86400);
                const hours = Math.floor((total % 86400) / 3600);
                const minutes = Math.floor((total % 3600) / 60);
                if (days > 0) return `${days}d ${hours}h ${minutes}m`;
                if (hours > 0) return `${hours}h ${minutes}m`;
                return `${minutes}m`;
            }

            function initNotifications() {
                const toggle = document.getElementById('notify-toggle');
                const statusEl = document.getElementById('notify-status');
                if (!toggle || !statusEl) return;

                let enabled = false;
                try {
                    enabled = (localStorage.getItem(NOTIFY_KEY) === '1');
                } catch (e) { /* ignore */ }

                const setStatus = (state, label) => {
                    notificationsEnabled = state;
                    toggle.checked = state;
                    statusEl.textContent = label;
                    try { if (state) window.showToast('Notifications enabled', false); else window.showToast('Notifications disabled', false); } catch (e) {}
                };

                if (typeof Notification !== 'undefined' && Notification.permission === 'granted' && enabled) {
                    setStatus(true, 'Enabled');
                } else {
                    setStatus(false, (typeof Notification !== 'undefined' && Notification.permission === 'denied') ? 'Blocked by browser' : 'Disabled');
                }

                toggle.addEventListener('change', async () => {
                    if (!toggle.checked) {
                        try { localStorage.setItem(NOTIFY_KEY, '0'); } catch (e) { /* ignore */ }
                        setStatus(false, 'Disabled');
                        return;
                    }
                    if (typeof Notification === 'undefined') {
                        setStatus(false, 'Unsupported');
                        try { window.showToast('Notifications not supported in this environment', true); } catch (e) {}
                        return;
                    }
                    try {
                        const permission = await Notification.requestPermission();
                        if (permission === 'granted') {
                            try { localStorage.setItem(NOTIFY_KEY, '1'); } catch (e) { /* ignore */ }
                            setStatus(true, 'Enabled');
                        } else {
                            setStatus(false, permission === 'denied' ? 'Blocked by browser' : 'Disabled');
                            try { window.showToast(permission === 'denied' ? 'Notifications blocked by browser' : 'Notifications not enabled', true); } catch (e) {}
                        }
                    } catch (e) {
                        setStatus(false, 'Disabled');
                        try { window.showToast('Notifications failed', true); } catch (e) {}
                    }
                });
            }

            function maybeNotify(text) {
                if (!notificationsEnabled) return;
                if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
                if (!document.hidden) return;
                try {
                    new Notification('SyntH', { body: text, silent: false });
                } catch (e) { /* ignore */ }
            }

            // Ensure legacy chat notifier API is available for chat module
            try { window.SynthChat = window.SynthChat || {}; window.SynthChat.maybeNotify = maybeNotify; } catch (e) { /* ignore */ }

            window.SynthWebUI = window.SynthWebUI || {};
            window.SynthWebUI.initHomeTab = initHomeTab;
            window.SynthWebUI.initSettingsTab = initSettingsTab;
            window.SynthWebUI.initComponentsTab = initComponentsTab;
            window.SynthWebUI.initLogsTab = initLogsTab;
            window.SynthWebUI.initAboutTab = initAboutTab;

            document.addEventListener('DOMContentLoaded', () => {
                setupNavigation();
                try {
                    if (window.SynthWindowManager && typeof window.SynthWindowManager.ensureWinBoxAssets === 'function') {
                        window.SynthWindowManager.ensureWinBoxAssets().then((ok) => console.debug('[synth_webui] ensureWinBoxAssets early result:', ok));
                    }
                } catch (e) { /* ignore */ }

                // Preload archive module so ArchiveWindow is available synchronously when needed
                try {
                    const s = document.createElement('script');
                    s.type = 'module';
                    s.src = '/js/archive-window.mjs';
                    document.head.appendChild(s);
                } catch (e) { /* ignore */ }
            });
        })();
