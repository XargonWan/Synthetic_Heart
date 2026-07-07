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

            // If a parsed child wraps content with the same id as the panel,
            // extract its inner content to avoid nesting .tab-panel inside .tab-panel
            // (which would be hidden by CSS display:none).
            const children = Array.from(doc.body.children || []);
            const wrapper = children.find(c => c.id === panel.id);
            if (wrapper) {
                Array.from(wrapper.children).forEach(n => panel.appendChild(n));
                // Append sibling elements (e.g. <style> blocks) that sit outside the wrapper
                children.filter(c => c !== wrapper).forEach(n => panel.appendChild(n));
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
                import('./chat-window.mjs?v=20260323-expression-fix').then(async (mod) => {
                    try {
                        if (mod && typeof mod.createChatWindow === 'function') {
                            // Ensure the Home section (and #chat mount) is available before creating the window
                            try { if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') await window.SynthWebUI.loadSection('home'); } catch (e) { /* ignore */ }
                            // createChatWindow returns a Promise resolving to the WinBox instance (or null)
                            await mod.createChatWindow().catch(() => null);
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
    if (window.__SYNTH_CONFIG.MULTI_SESSION !== undefined) window.MULTI_SESSION = window.__SYNTH_CONFIG.MULTI_SESSION;
    // Vox (TTS) flags – used by chat-window.mjs for auto-play and replay
    if (window.__SYNTH_CONFIG.VOX_ENABLED !== undefined) window.VOX_ENABLED = window.__SYNTH_CONFIG.VOX_ENABLED;
    if (window.__SYNTH_CONFIG.VOX_AUDIO_CACHE_SIZE !== undefined) window.VOX_AUDIO_CACHE_SIZE = Number(window.__SYNTH_CONFIG.VOX_AUDIO_CACHE_SIZE) || 40;

    // Apply accent color from server-rendered runtime config (if provided)
    try {
      const accent = window.__SYNTH_CONFIG.WEBUI_ACCENT_COLOR;
      if (accent) {
        document.documentElement.style.setProperty('--accent', accent);
        // set a soft variant at ~16% alpha and compute readable contrast color
        const hexToRgb = (h) => { const c = h.replace('#',''); const bigint = parseInt(c.length===3?c.split('').map(x=>x+x).join(''):c,16); return [(bigint>>16)&255, (bigint>>8)&255, bigint&255]; };
        const [r,g,b] = hexToRgb(accent);
        document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b}, 0.16)`);
        document.documentElement.style.setProperty('--accent-r', String(r));
        document.documentElement.style.setProperty('--accent-g', String(g));
        document.documentElement.style.setProperty('--accent-b', String(b));
        try { document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(accent)); } catch (e) { document.documentElement.style.setProperty('--accent-contrast', '#07070c'); }
        try { document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(accent)); } catch (e) { document.documentElement.style.setProperty('--accent-dark', '#5b5b6b'); }
      }
    } catch (e) { /* ignore */ }
  }
} catch (e) { console.warn('[synth_webui] config init failed', e); }

// Helper: choose readable contrast color (white or dark accent text) using WCAG luminance/contrast
function _hexToRgb(h) { const c = String(h||'').replace('#',''); const hex = c.length===3 ? c.split('').map(x=>x+x).join('') : c; const bigint = parseInt(hex,16); return [(bigint>>16)&255, (bigint>>8)&255, bigint&255]; }
function _relativeLuminance(r,g,b) { const srgb = [r,g,b].map(v => { v = v/255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); }); return 0.2126*srgb[0] + 0.7152*srgb[1] + 0.0722*srgb[2]; }
function _contrastRatio(l1,l2) { const lighter = Math.max(l1,l2); const darker = Math.min(l1,l2); return (lighter + 0.05) / (darker + 0.05); }
function pickAccentContrastFromHex(hex) {
  try {
    const [r,g,b] = _hexToRgb(hex);
    const la = _relativeLuminance(r,g,b);
    const lWhite = _relativeLuminance(255,255,255);
    const lBlack = _relativeLuminance(7,7,12); // match --accent-contrast default
    const crWhite = _contrastRatio(la, lWhite);
    const crBlack = _contrastRatio(la, lBlack);
    return (crWhite >= crBlack) ? '#ffffff' : '#07070c';
    } catch (e) { return '#07070c'; }
}

// Darken a color (hex) using HSL lightness reduction to generate gradient end
function _rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h, s;
  const l = (max + min) / 2;
  if (max === min) {
    h = s = 0; // achromatic
  } else {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = (g - b) / d + (g < b ? 6 : 0); break;
      case g: h = (b - r) / d + 2; break;
      case b: h = (r - g) / d + 4; break;
    }
    h /= 6;
  }
  return [h * 360, s, l];
}
function _hslToRgb(h, s, l) {
  h /= 360;
  let r, g, b;
  if (s === 0) {
    r = g = b = l; // achromatic
  } else {
    const hue2rgb = (p, q, t) => {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1/6) return p + (q - p) * 6 * t;
      if (t < 1/2) return q;
      if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
      return p;
    };
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2rgb(p, q, h + 1/3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1/3);
  }
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}
function _rgbToHex(r, g, b) { return '#' + [r, g, b].map(x => x.toString(16).padStart(2, '0')).join(''); }
function darkenHex(hex, amount = 0.28) {
  try {
    const [r, g, b] = _hexToRgb(hex);
    const [h, s, l] = _rgbToHsl(r, g, b);
    const nl = Math.max(0, l - amount);
    const [nr, ng, nb] = _hslToRgb(h, s, nl);
    return _rgbToHex(nr, ng, nb);
  } catch (e) { return '#4b4b4b'; }
}
function pickAccentDarkFromHex(hex) { return darkenHex(hex, 0.28); }

        // Configuration values from server
        window.__synthLipSyncAnalyser = null;
        window.__synthLipSyncData = null;
        window.__synthIsLipSyncing = false;
        window.__synthLipSyncAudio = null;
        window.__synthLipSyncSource = null;
        window.__synthLipSyncCtx = null;
        
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
        const componentsCortexSummary = document.getElementById('components-cortex-summary');
        const componentsCortexList = document.getElementById('components-cortex-list');
        const componentsInterfacesList = document.getElementById('components-interfaces-list');
        const componentsPluginsList = document.getElementById('components-plugins-list');
        const componentsVoxList = document.getElementById('components-vox-list');
        const componentsAurisList = document.getElementById('components-auris-list');
        const componentsLiveList = document.getElementById('components-live-list');
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

            function setNormalRect(entry, rect) {
                if (!entry || !rect) return false;
                const x = Number.isFinite(rect.x) ? rect.x : rect.left;
                const y = Number.isFinite(rect.y) ? rect.y : rect.top;
                const width = Number.isFinite(rect.width) ? rect.width : null;
                const height = Number.isFinite(rect.height) ? rect.height : null;
                if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(width) || !Number.isFinite(height)) {
                    return false;
                }
                if (width < 120 || height < 120) return false;
                entry.lastNormalRect = {
                    x: Math.round(x),
                    y: Math.round(y),
                    width: Math.round(width),
                    height: Math.round(height)
                };
                return true;
            }

            function applyNormalRect(entry) {
                if (!entry || !entry.winbox) return false;
                if (!setNormalRect(entry, entry.lastNormalRect)) return false;
                const topbar = getTopbarHeight() || 0;
                const rect = entry.lastNormalRect;
                try { entry.winbox.resize(rect.width, rect.height); } catch (e) { /* ignore */ }
                try { entry.winbox.move(rect.x, Math.max(topbar, rect.y)); } catch (e) { /* ignore */ }
                try { clampEntryToViewport(entry); } catch (e) { /* ignore */ }
                try { captureNormalRect(entry); } catch (e) { /* ignore */ }
                return true;
            }

            function getWindowStorageKeys(id) {
                const stableStateKey = `${CHAT_WINDOW_STATE_KEY}-${id}`;
                const stableRectKey = `${CHAT_RECT_KEY}-${id}`;
                const sessionStateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${id}-${sessionId}` : null;
                const sessionRectKey = sessionId ? `${CHAT_RECT_KEY}-${id}-${sessionId}` : null;
                return {
                    stableStateKey,
                    stableRectKey,
                    sessionStateKey,
                    sessionRectKey,
                    stateLookupKeys: [
                        stableStateKey,
                        sessionStateKey,
                        sessionStateKey ? `${sessionStateKey}-desktop` : null,
                        sessionStateKey ? `${sessionStateKey}-mobile` : null,
                        `${CHAT_WINDOW_STATE_KEY}-${id}-desktop`,
                        `${CHAT_WINDOW_STATE_KEY}-${id}-mobile`,
                        CHAT_WINDOW_STATE_KEY,
                    ].filter(Boolean),
                    rectLookupKeys: [
                        stableRectKey,
                        sessionRectKey,
                        sessionRectKey ? `${sessionRectKey}-desktop` : null,
                        sessionRectKey ? `${sessionRectKey}-mobile` : null,
                        `${CHAT_RECT_KEY}-${id}-desktop`,
                        `${CHAT_RECT_KEY}-${id}-mobile`,
                        CHAT_RECT_KEY,
                    ].filter(Boolean),
                };
            }

            function getFirstStoredValue(keys) {
                if (!Array.isArray(keys)) return null;
                for (const key of keys) {
                    try {
                        const value = localStorage.getItem(key);
                        if (value) return value;
                    } catch (e) { /* ignore */ }
                }
                return null;
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
                    // Use native WinBox toggle if available, otherwise check state
                    if (entry.winbox.max) {
                        entry.winbox.restore();
                    } else {
                        captureNormalRect(entry);
                        entry.winbox.maximize();
                    }
                } catch (e) { /* ignore */ }
                try { saveState(id); } catch (e) { /* ignore */ }
            }

            function saveState(id) {
                const entry = windows.get(id);
                if (!entry || !entry.winbox) return;
                try {
                    const { stableStateKey, stableRectKey, sessionStateKey, sessionRectKey } = getWindowStorageKeys(id);
                    let state = 'normal';
                    if (entry.minimized) state = 'minimized';
                    else if (entry.winbox.max) state = 'maximized';
                    try { localStorage.setItem(stableStateKey, state); } catch (e) { /* ignore */ }
                    if (sessionStateKey) {
                        try { localStorage.setItem(sessionStateKey, state); } catch (e) { /* ignore */ }
                    }

                    const storedRect = (entry.winbox.max || entry.minimized) ? entry.lastNormalRect : null;
                    const payload = {
                        left: Math.round((storedRect && storedRect.x) || entry.winbox.x || 0),
                        top: Math.round((storedRect && storedRect.y) || entry.winbox.y || 0),
                        width: Math.round((storedRect && storedRect.width) || entry.winbox.width || 0),
                        height: Math.round((storedRect && storedRect.height) || entry.winbox.height || 0)
                    };
                    try { localStorage.setItem(stableRectKey, JSON.stringify(payload)); } catch (e) { /* ignore */ }
                    if (sessionRectKey) {
                        try { localStorage.setItem(sessionRectKey, JSON.stringify(payload)); } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }
            }

            function restoreState(id) {
                const entry = windows.get(id);
                if (!entry || !entry.winbox) return false;
                try {
                    const { stateLookupKeys, rectLookupKeys } = getWindowStorageKeys(id);
                    const rectRaw = getFirstStoredValue(rectLookupKeys);
                    if (rectRaw) {
                        const rect = JSON.parse(rectRaw);
                        setNormalRect(entry, rect);
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
                        try { captureNormalRect(entry); } catch (e) { /* ignore */ }
                    }

                    const localState = getFirstStoredValue(stateLookupKeys);
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
                    lastNormalRect: null,
                    maximizingInProgress: false
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
                    class: className,
                    onmaximize: function() {
                        try { applyMaximizeConstraints(entry); } catch (e) { /* ignore */ }
                        try { saveState(opts.id); } catch (e) { /* ignore */ }
                    },
                    onrestore: function() {
                        // Defer all post-restore work so WinBox fully completes its own restore
                        // sequence before we touch resize/move (avoids re-entrant freeze).
                        setTimeout(() => {
                            try {
                                if (entry.winbox && !entry.winbox.max && !entry.winbox.min) {
                                    clampEntryToViewport(entry);
                                    captureNormalRect(entry);
                                }
                            } catch (e) { /* ignore */ }
                            try { saveState(opts.id); } catch (e) { /* ignore */ }
                        }, 0);
                    },
                    onmove: function() {
                        if (!entry.maximizingInProgress && !this.max && !this.min) try { captureNormalRect(entry); } catch (e) { /* ignore */ }
                    },
                    onresize: function() {
                        if (!entry.maximizingInProgress && !this.max && !this.min) try { captureNormalRect(entry); } catch (e) { /* ignore */ }
                    }
                });
                try { console.debug('[SynthWindowManager] created winbox for', opts.id, 'instance=', winbox); } catch (e) { /* ignore */ }
                entry.winbox = winbox;
                try {
                    const nativeMaximize = typeof winbox.maximize === 'function' ? winbox.maximize.bind(winbox) : null;
                    if (nativeMaximize) {
                        winbox.maximize = function(...args) {
                            // Capture pre-maximize rect before WinBox resizes to fullscreen.
                            // Guard captureNormalRect in onresize/onmove while WinBox internal
                            // maximize is running (this.max is still false during resize/move
                            // calls inside maximize(), so without the flag captureNormalRect
                            // would snapshot the fullscreen dimensions and corrupt lastNormalRect).
                            try {
                                if (!this.max && !this.min) captureNormalRect(entry);
                            } catch (e) { /* ignore */ }
                            entry.maximizingInProgress = true;
                            try {
                                return nativeMaximize(...args);
                            } finally {
                                entry.maximizingInProgress = false;
                            }
                        };
                    }
                } catch (e) { /* ignore */ }
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
                try { captureNormalRect(entry); } catch (e) { /* ignore */ }
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
                    try { import('./chat-window.mjs?v=20260323-expression-fix').then((mod) => { try { if (mod && typeof mod.createChatWindow === 'function') mod.createChatWindow(); } catch (e) {} }).catch(() => {}); } catch (e) {}
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
                const response = await fetch((window.__getApiBase ? window.__getApiBase() : '') + '/api/config');
                if (!response.ok) throw new Error('HTTP ' + response.status);
                const payload = await response.json();
                const items = Array.isArray(payload.items) ? payload.items : [];
                const itemMap = {};
                items.forEach((it) => {
                    if (it && it.key) itemMap[it.key] = it;
                });
                const lockedAliasValues = (() => {
                    const base = ['SyntH', 'Synthetic Heart'];
                    const nameVal = itemMap.SYNTH_NAME && itemMap.SYNTH_NAME.value ? String(itemMap.SYNTH_NAME.value) : '';
                    const lower = new Set(base.map((v) => v.toLowerCase()));
                    if (nameVal && !lower.has(nameVal.toLowerCase())) base.push(nameVal);
                    return base;
                })();
                const lockedAliasLookup = new Set(lockedAliasValues.map((v) => String(v).toLowerCase()));

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

                        const persistValue = async (val, controls = []) => {
                            const targets = Array.isArray(controls) ? controls.filter(Boolean) : [];
                            try {
                                targets.forEach((el) => { try { if (typeof el.disabled !== 'undefined') el.disabled = true; } catch (e) {} });
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
                                        try { await refreshConfig(); } catch (e) { /* ignore */ }
                                    } catch (e) {
                                        window.showToast('Saved', false);
                                    }
                                }
                            } catch (e) {
                                try { window.showToast('Save failed', true); } catch (e) {}
                            } finally {
                                targets.forEach((el) => { try { if (typeof el.disabled !== 'undefined') el.disabled = !isEditable; } catch (e) {} });
                            }
                        };

                        let inputEl = null;
                        let extraEl = null;
                        let skipAutoSave = false;
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
                                    link.href = (window.__getApiBase ? window.__getApiBase() : '') + `/api/config/${encodeURIComponent(item.key)}/file`;
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

                        // --- Peer SyntH list: repeatable [Bot ID, Display Name] rows ---
                        } else if (item.ui_type === 'peer-list') {
                            const wrap = document.createElement('div');
                            wrap.className = 'repeatable-list';
                            skipAutoSave = true;

                            const parsePeers = () => {
                                if (Array.isArray(value)) return value.filter((row) => row && typeof row === 'object');
                                if (typeof value === 'string' && value.trim()) {
                                    try {
                                        const parsed = JSON.parse(value);
                                        if (Array.isArray(parsed)) return parsed.filter((row) => row && typeof row === 'object');
                                    } catch (e) {}
                                }
                                return [];
                            };

                            let peers = parsePeers();
                            const list = document.createElement('div');
                            list.className = 'repeatable-list-rows';

                            const emptyNote = document.createElement('div');
                            emptyNote.className = 'meta';
                            emptyNote.textContent = 'Add the bot ID + display name of each peer SyntH.';

                            const addBtn = document.createElement('button');
                            addBtn.type = 'button';
                            addBtn.className = 'btn-ghost';
                            addBtn.textContent = '+ Add peer SyntH';
                            addBtn.disabled = !isEditable;

                            const serializePeers = () => peers.filter((p) => p && p.id !== '' && p.id !== null && p.id !== undefined);

                            const renderRows = () => {
                                list.innerHTML = '';
                                if (!peers.length) {
                                    list.appendChild(emptyNote);
                                }
                                peers.forEach((peer, idx) => {
                                    const rowEl = document.createElement('div');
                                    rowEl.className = 'repeatable-row';

                                    const idInput = document.createElement('input');
                                    idInput.type = 'text';
                                    idInput.inputMode = 'numeric';
                                    idInput.autocomplete = 'off';
                                    idInput.placeholder = 'Bot ID (e.g. 8243553794)';
                                    idInput.value = peer && peer.id !== undefined && peer.id !== null ? String(peer.id) : '';
                                    idInput.disabled = !isEditable;

                                    const nameInput = document.createElement('input');
                                    nameInput.type = 'text';
                                    nameInput.autocomplete = 'off';
                                    nameInput.placeholder = 'Display name (e.g. Aria)';
                                    nameInput.value = peer && peer.name ? peer.name : '';
                                    nameInput.disabled = !isEditable;

                                    const removeBtn = document.createElement('button');
                                    removeBtn.type = 'button';
                                    removeBtn.className = 'btn-ghost';
                                    removeBtn.textContent = 'Remove';
                                    removeBtn.disabled = !isEditable;

                                    const commit = () => {
                                        const rawId = idInput.value.trim();
                                        const parsedId = rawId && /^-?\d+$/.test(rawId) ? parseInt(rawId, 10) : rawId;
                                        peers[idx] = { id: parsedId, name: nameInput.value.trim() };
                                        persistValue(serializePeers(), [idInput, nameInput, removeBtn, addBtn]);
                                    };

                                    idInput.addEventListener('blur', commit);
                                    nameInput.addEventListener('blur', commit);

                                    removeBtn.addEventListener('click', () => {
                                        peers.splice(idx, 1);
                                        renderRows();
                                        persistValue(serializePeers(), [idInput, nameInput, removeBtn, addBtn]);
                                    });

                                    rowEl.appendChild(idInput);
                                    rowEl.appendChild(nameInput);
                                    rowEl.appendChild(removeBtn);
                                    list.appendChild(rowEl);
                                });
                            };

                            addBtn.addEventListener('click', () => {
                                peers.push({ id: '', name: '' });
                                renderRows();
                                const lastInput = list.querySelector('.repeatable-row:last-child input');
                                try { if (lastInput) { lastInput.focus(); } } catch (e) {}
                                // Do NOT persist until the user fills in an ID (persist happens on blur)
                            });

                            renderRows();
                            wrap.appendChild(list);
                            wrap.appendChild(addBtn);
                            inputEl = wrap;

                        } else if (item.ui_type === 'textarea' || (item.value_type === 'json' && item.ui_type !== 'tags' && item.ui_type !== 'tag-combobox')) {
                            const textarea = document.createElement('textarea');
                            textarea.rows = 3;
                            textarea.value = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
                            textarea.disabled = !isEditable;
                            inputEl = textarea;

                        // --- Color picker: presets + custom color input ---
                        } else if (item.ui_type === 'color') {
                            const swatchWrap = document.createElement('div');
                            swatchWrap.style.display = 'flex';
                            swatchWrap.style.alignItems = 'center';
                            swatchWrap.style.gap = '0.5rem';

                            // Presets: prefer server-provided runtime presets
                            const presets = (window.__SYNTH_CONFIG && window.__SYNTH_CONFIG.WEBUI_ACCENT_PRESETS) || ['#6bfefe','#ff6bd6','#18c98c','#ffd166','#ff9ecb'];
                            const presetsEl = document.createElement('div');
                            presetsEl.style.display = 'flex';
                            presetsEl.style.gap = '0.4rem';

                            // --- Local preview/apply UX: don't persist on `input`.
                            // Provide preview (Apply / Cancel) so user can freely adjust the picker.
                            let previewValue = null;
                            const applyPreview = async (val) => {
                                previewValue = null;
                                await persist(val);
                            };

                            // Local persist helper (color-picker scoped) so handlers don't rely on per-input saveValue
                            const persist = async (val) => {
                                try {
                                    // disable controls while saving
                                    colorInput.disabled = true;
                                    Array.from(presetsEl.querySelectorAll('button')).forEach(b => b.disabled = true);
                                    applyBtn.disabled = true;
                                    cancelBtn.disabled = true;
                                    reset.disabled = true;
                                    const payload = { key: item.key, value: val };
                                    const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                                    if (!res.ok) {
                                        const txt = await res.text();
                                        try { window.showToast('Save failed: ' + txt, true); } catch (e) {}
                                    } else {
                                        try {
                                            const out = await res.json();
                                            try { window.showToast('Saved', false); } catch (e) {}
                                            if (out && out.requires_reload) {
                                                window.showToast(out.message || 'Component reload recommended', false);
                                            }
                                            try { await refreshConfig(); } catch (e) { /* ignore */ }
                                        } catch (e) {
                                            try { window.showToast('Saved', false); } catch (e) {}
                                        }
                                    }
                                } catch (e) {
                                    try { window.showToast('Save failed', true); } catch (e) {}
                                } finally {
                                    try { colorInput.disabled = !isEditable; Array.from(presetsEl.querySelectorAll('button')).forEach(b => b.disabled = !isEditable); applyBtn.disabled = !isEditable; cancelBtn.disabled = !isEditable; reset.disabled = !isEditable; } catch (e) {}
                                }
                            };

                            presets.forEach((c) => {
                                const b = document.createElement('button');
                                b.type = 'button';
                                b.title = c;
                                b.style.width = '34px';
                                b.style.height = '34px';
                                b.style.borderRadius = '50%';
                                b.style.border = '1px solid rgba(255,255,255,0.06)';
                                b.style.background = c;
                                if (String(value).toLowerCase() === String(c).toLowerCase()) b.style.boxShadow = '0 0 0 4px rgba(0,0,0,0.14) inset';
                                // clicking a preset previews the color (does not persist immediately)
                                b.addEventListener('click', (ev) => {
                                    ev.preventDefault();
                                    previewValue = c;
                                    try { document.documentElement.style.setProperty('--accent', c); const [r,g,b2] = _hexToRgb(c); document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b2}, 0.16)`); document.documentElement.style.setProperty('--accent-r', String(r)); document.documentElement.style.setProperty('--accent-g', String(g)); document.documentElement.style.setProperty('--accent-b', String(b2)); document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(c)); document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(c)); } catch (e) {}
                                });
                                presetsEl.appendChild(b);
                            });

                            const colorInput = document.createElement('input');
                            colorInput.type = 'color';
                            colorInput.value = typeof value === 'string' && value ? value : (item.default || '#6bfefe');
                            colorInput.disabled = !isEditable;

                            // Preview-only on input: apply to CSS variables but do not persist until Apply
                            colorInput.addEventListener('input', (ev) => {
                                const c = ev.target.value;
                                previewValue = c;
                                try {
                                    document.documentElement.style.setProperty('--accent', c);
                                    const [r,g,b2] = _hexToRgb(c);
                                    document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b2}, 0.16)`);
                                    document.documentElement.style.setProperty('--accent-r', String(r));
                                    document.documentElement.style.setProperty('--accent-g', String(g));
                                    document.documentElement.style.setProperty('--accent-b', String(b2));
                                    document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(c));
                                    document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(c));
                                } catch (e) {}
                            });

                            // Apply / Cancel controls for preview UX
                            const applyBtn = document.createElement('button');
                            applyBtn.type = 'button';
                            applyBtn.textContent = 'Apply';
                            applyBtn.className = 'apply';
                            applyBtn.disabled = !isEditable;
                            applyBtn.addEventListener('click', async () => {
                                const val = previewValue || colorInput.value;
                                // persist and refresh
                                await persist(val);
                            });

                            const cancelBtn = document.createElement('button');
                            cancelBtn.type = 'button';
                            cancelBtn.textContent = 'Cancel';
                            cancelBtn.className = 'cancel';
                            cancelBtn.disabled = !isEditable;
                            cancelBtn.addEventListener('click', () => {
                                // revert preview to authoritative value
                                const current = typeof value === 'string' && value ? value : (item.default || '#6bfefe');
                                previewValue = null;
                                colorInput.value = current;
                                try {
                                    document.documentElement.style.setProperty('--accent', current);
                                    const [r,g,b2] = _hexToRgb(current);
                                    document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b2}, 0.16)`);
                                    document.documentElement.style.setProperty('--accent-r', String(r));
                                    document.documentElement.style.setProperty('--accent-g', String(g));
                                    document.documentElement.style.setProperty('--accent-b', String(b2));
                                    document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(current));
                                    document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(current));
                                } catch (e) {}
                            });

                            const reset = document.createElement('button');
                            reset.type = 'button';
                            reset.textContent = 'Reset';
                            reset.disabled = !isEditable;
                            reset.addEventListener('click', async () => {
                                const def = item.default || '#6bfefe';
                                await persist(def);
                                colorInput.value = def;
                                try {
                                    document.documentElement.style.setProperty('--accent', def);
                                    const [r,g,b2] = _hexToRgb(def);
                                    document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b2}, 0.16)`);
                                    document.documentElement.style.setProperty('--accent-r', String(r));
                                    document.documentElement.style.setProperty('--accent-g', String(g));
                                    document.documentElement.style.setProperty('--accent-b', String(b2));
                                    document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(def));
                                    document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(def));
                                } catch (e) {}
                            });

                            swatchWrap.appendChild(presetsEl);
                            swatchWrap.appendChild(colorInput);
                            swatchWrap.appendChild(applyBtn);
                            swatchWrap.appendChild(cancelBtn);
                            swatchWrap.appendChild(reset);
                            inputEl = swatchWrap;

                        // --- Combobox: free-text input with searchable dropdown ---
                        } else if (item.ui_type === 'combobox') {
                            const opts = Array.isArray(item.options) ? item.options : [];
                            // Large option sets get a custom searchable dropdown
                            if (opts.length > 20) {
                                const wrap = document.createElement('div');
                                wrap.className = 'searchable-combo';
                                wrap.style.position = 'relative';
                                wrap.style.flex = '1';
                                skipAutoSave = true;

                                const input = document.createElement('input');
                                input.type = 'text';
                                input.value = typeof value === 'string' ? value : '';
                                input.disabled = !isEditable;
                                input.placeholder = `Search ${opts.length} options...`;
                                input.autocomplete = 'off';

                                const panel = document.createElement('div');
                                panel.className = 'combo-dropdown';
                                panel.style.cssText = 'display:none;position:absolute;top:100%;left:0;right:0;max-height:260px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;background:var(--bg-card,#1a1a2e);z-index:999;margin-top:2px;';

                                const MAX_VISIBLE = 80;
                                const renderList = (filter) => {
                                    panel.innerHTML = '';
                                    const q = (filter || '').toLowerCase();
                                    let count = 0;
                                    for (const opt of opts) {
                                        if (q && !opt.toLowerCase().includes(q)) continue;
                                        if (++count > MAX_VISIBLE) {
                                            const more = document.createElement('div');
                                            more.style.cssText = 'padding:0.35rem 0.6rem;color:var(--text-soft);font-size:0.8rem;';
                                            more.textContent = `${opts.length - MAX_VISIBLE}+ more — refine your search`;
                                            panel.appendChild(more);
                                            break;
                                        }
                                        const row = document.createElement('div');
                                        row.textContent = opt;
                                        row.style.cssText = 'padding:0.35rem 0.6rem;cursor:pointer;font-size:0.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
                                        row.addEventListener('mouseenter', () => { row.style.background = 'var(--accent-dim, rgba(107,254,254,0.12))'; });
                                        row.addEventListener('mouseleave', () => { row.style.background = ''; });
                                        row.addEventListener('mousedown', (ev) => {
                                            ev.preventDefault(); // prevent blur before click
                                            input.value = opt;
                                            panel.style.display = 'none';
                                            persistValue(opt, [input]);
                                        });
                                        panel.appendChild(row);
                                    }
                                    if (count === 0) {
                                        const empty = document.createElement('div');
                                        empty.style.cssText = 'padding:0.5rem 0.6rem;color:var(--text-soft);font-size:0.85rem;';
                                        empty.textContent = 'No matching models';
                                        panel.appendChild(empty);
                                    }
                                };

                                input.addEventListener('focus', () => {
                                    renderList(input.value);
                                    panel.style.display = '';
                                });
                                input.addEventListener('input', () => { renderList(input.value); });
                                input.addEventListener('blur', () => {
                                    // Delay to allow mousedown on options
                                    setTimeout(() => { panel.style.display = 'none'; }, 180);
                                });
                                input.addEventListener('keydown', (ev) => {
                                    if (ev.key === 'Enter') {
                                        ev.preventDefault();
                                        panel.style.display = 'none';
                                        persistValue(input.value, [input]);
                                    } else if (ev.key === 'Escape') {
                                        panel.style.display = 'none';
                                        input.blur();
                                    }
                                });

                                wrap.appendChild(input);
                                wrap.appendChild(panel);
                                inputEl = wrap;
                            } else {
                                // Small option sets: use native datalist
                                const input = document.createElement('input');
                                input.type = 'text';
                                input.autocomplete = 'off';
                                input.value = typeof value === 'string' ? value : '';
                                input.disabled = !isEditable;
                                if (opts.length) {
                                    const dlId = `datalist-${item.key}`;
                                    const dl = document.createElement('datalist');
                                    dl.id = dlId;
                                    opts.forEach((opt) => {
                                        const o = document.createElement('option');
                                        o.value = opt;
                                        dl.appendChild(o);
                                    });
                                    document.body.appendChild(dl);
                                    input.setAttribute('list', dlId);
                                }
                                inputEl = input;
                            }

                        // --- Action list: dropdown rows with add/remove ---
                        } else if (item.ui_type === 'action-list') {
                            const wrap = document.createElement('div');
                            wrap.className = 'repeatable-list';
                            skipAutoSave = true;

                            const options = Array.isArray(item.options) ? item.options.slice() : [];
                            const parseList = () => {
                                if (Array.isArray(value)) return value.slice();
                                if (typeof value === 'string' && value.trim()) {
                                    try { const parsed = JSON.parse(value); if (Array.isArray(parsed)) return parsed; } catch (e) {}
                                    return value.split(',').map((v) => v.trim()).filter(Boolean);
                                }
                                return [];
                            };

                            let actions = parseList();
                            const list = document.createElement('div');
                            list.className = 'repeatable-list-rows';

                            const emptyNote = document.createElement('div');
                            emptyNote.className = 'meta';
                            emptyNote.textContent = options.length ? 'Add actions to whitelist.' : 'No unsafe actions discovered yet.';

                            const addBtn = document.createElement('button');
                            addBtn.type = 'button';
                            addBtn.className = 'btn-ghost';
                            addBtn.textContent = '+ Add action';
                            addBtn.disabled = !isEditable || !options.length;

                            const renderRows = () => {
                                list.innerHTML = '';
                                if (!actions.length) {
                                    list.appendChild(emptyNote);
                                }
                                actions.forEach((action, idx) => {
                                    const rowEl = document.createElement('div');
                                    rowEl.className = 'repeatable-row repeatable-row--action';

                                    const select = document.createElement('select');
                                    if (!options.length) {
                                        const opt = document.createElement('option');
                                        opt.value = '';
                                        opt.textContent = 'No actions available';
                                        select.appendChild(opt);
                                        select.disabled = true;
                                    } else {
                                        options.forEach((opt) => {
                                            const option = document.createElement('option');
                                            option.value = opt;
                                            option.textContent = opt;
                                            if (String(action) === String(opt)) option.selected = true;
                                            select.appendChild(option);
                                        });
                                        select.disabled = !isEditable;
                                    }

                                    const removeBtn = document.createElement('button');
                                    removeBtn.type = 'button';
                                    removeBtn.className = 'btn-ghost';
                                    removeBtn.textContent = 'Remove';
                                    removeBtn.disabled = !isEditable;

                                    select.addEventListener('change', () => {
                                        actions[idx] = select.value;
                                        persistValue(actions.filter(Boolean), [select, removeBtn, addBtn]);
                                    });

                                    removeBtn.addEventListener('click', () => {
                                        actions.splice(idx, 1);
                                        renderRows();
                                        persistValue(actions.filter(Boolean), [select, removeBtn, addBtn]);
                                    });

                                    rowEl.appendChild(select);
                                    rowEl.appendChild(removeBtn);
                                    list.appendChild(rowEl);
                                });
                            };

                            addBtn.addEventListener('click', () => {
                                if (!options.length) return;
                                actions.push(options[0]);
                                renderRows();
                                persistValue(actions.filter(Boolean), [addBtn]);
                            });

                            renderRows();
                            wrap.appendChild(list);
                            wrap.appendChild(addBtn);
                            inputEl = wrap;

                        // --- Trainer IDs: interface + id rows with add/remove ---
                        } else if (item.ui_type === 'trainer-ids') {
                            const wrap = document.createElement('div');
                            wrap.className = 'repeatable-list';
                            skipAutoSave = true;

                            const interfaceOptions = Array.isArray(item.options) ? item.options.slice() : [];
                            const parseTrainerIds = () => {
                                if (Array.isArray(value)) return value;
                                if (typeof value !== 'string' || !value.trim()) return [];
                                return value.split(',').map((entry) => {
                                    const trimmed = entry.trim();
                                    if (!trimmed) return null;
                                    const parts = trimmed.split(':');
                                    if (parts.length < 2) return null;
                                    return { iface: parts[0].trim(), id: parts.slice(1).join(':').trim() };
                                }).filter(Boolean);
                            };

                            let entries = parseTrainerIds();
                            const list = document.createElement('div');
                            list.className = 'repeatable-list-rows';

                            const emptyNote = document.createElement('div');
                            emptyNote.className = 'meta';
                            emptyNote.textContent = 'Add interface + trainer id pairs.';

                            const addBtn = document.createElement('button');
                            addBtn.type = 'button';
                            addBtn.className = 'btn-ghost';
                            addBtn.textContent = '+ Add trainer';
                            addBtn.disabled = !isEditable || !interfaceOptions.length;

                            const serializeEntries = () => {
                                return entries
                                    .filter((e) => e && e.iface && e.id)
                                    .map((e) => `${e.iface}:${e.id}`)
                                    .join(',');
                            };

                            const renderRows = () => {
                                list.innerHTML = '';
                                if (!entries.length) {
                                    list.appendChild(emptyNote);
                                }
                                entries.forEach((entry, idx) => {
                                    const rowEl = document.createElement('div');
                                    rowEl.className = 'repeatable-row';

                                    const select = document.createElement('select');
                                    if (!interfaceOptions.length) {
                                        const opt = document.createElement('option');
                                        opt.value = '';
                                        opt.textContent = 'No interfaces available';
                                        select.appendChild(opt);
                                        select.disabled = true;
                                    } else {
                                        interfaceOptions.forEach((opt) => {
                                            const option = document.createElement('option');
                                            option.value = opt;
                                            option.textContent = opt;
                                            if (entry && String(entry.iface) === String(opt)) option.selected = true;
                                            select.appendChild(option);
                                        });
                                        select.disabled = !isEditable;
                                    }

                                    const idInput = document.createElement('input');
                                    idInput.type = 'text';
                                    idInput.autocomplete = 'off';
                                    idInput.placeholder = 'Trainer ID or username';
                                    idInput.value = entry && entry.id ? entry.id : '';
                                    idInput.disabled = !isEditable;

                                    const removeBtn = document.createElement('button');
                                    removeBtn.type = 'button';
                                    removeBtn.className = 'btn-ghost';
                                    removeBtn.textContent = 'Remove';
                                    removeBtn.disabled = !isEditable;

                                    select.addEventListener('change', () => {
                                        entries[idx] = { iface: select.value, id: idInput.value };
                                        persistValue(serializeEntries(), [select, idInput, removeBtn, addBtn]);
                                    });

                                    idInput.addEventListener('blur', () => {
                                        entries[idx] = { iface: select.value, id: idInput.value.trim() };
                                        persistValue(serializeEntries(), [select, idInput, removeBtn, addBtn]);
                                    });

                                    removeBtn.addEventListener('click', () => {
                                        entries.splice(idx, 1);
                                        renderRows();
                                        persistValue(serializeEntries(), [select, idInput, removeBtn, addBtn]);
                                    });

                                    rowEl.appendChild(select);
                                    rowEl.appendChild(idInput);
                                    rowEl.appendChild(removeBtn);
                                    list.appendChild(rowEl);
                                });
                            };

                            addBtn.addEventListener('click', () => {
                                const fallback = interfaceOptions[0] || '';
                                entries.push({ iface: fallback, id: '' });
                                renderRows();
                                // focus the new row's ID input so user can type immediately
                                const lastInput = list.querySelector('.repeatable-row:last-child input[type="text"]');
                                try { if (lastInput) { lastInput.focus(); lastInput.select(); } } catch (e) {}
                                // Do NOT persist until the user enters an ID (persist happens on blur)
                            });

                            renderRows();
                            wrap.appendChild(list);
                            wrap.appendChild(addBtn);
                            inputEl = wrap;

                        // --- Tags / tag-combobox: array editor with add/remove chips ---
                        } else if (item.ui_type === 'tags' || item.ui_type === 'tag-combobox') {
                            const wrap = document.createElement('div');
                            wrap.className = 'tag-list-input';
                            skipAutoSave = true;

                            // Normalize incoming value to an array
                            let tags = [];
                            try {
                                if (Array.isArray(value)) tags = value.slice();
                                else if (typeof value === 'string' && value.trim() !== '') {
                                    try { tags = JSON.parse(value); } catch (e) { tags = value.split(',').map(s => s.trim()).filter(Boolean); }
                                }
                            } catch (e) { tags = []; }

                            const chips = document.createElement('div');
                            chips.className = 'tag-chips';

                            const input = document.createElement('input');
                            input.type = 'text';
                            input.autocomplete = 'off';
                            input.className = 'tag-input-field';
                            input.placeholder = 'Add tag and press Enter';
                            input.disabled = !isEditable;

                            // Optionally provide suggestions for tag-combobox
                            if (item.ui_type === 'tag-combobox' && Array.isArray(item.options) && item.options.length) {
                                const dlId = `tags-datalist-${item.key}`;
                                const dl = document.createElement('datalist');
                                dl.id = dlId;
                                item.options.forEach((opt) => {
                                    const o = document.createElement('option');
                                    o.value = opt;
                                    dl.appendChild(o);
                                });
                                document.body.appendChild(dl);
                                input.setAttribute('list', dlId);
                            }

                            if (item.key === 'SYNTH_ALIASES') {
                                const existing = new Set(tags.map((t) => String(t).toLowerCase()));
                                lockedAliasValues.forEach((alias) => {
                                    if (!existing.has(String(alias).toLowerCase())) tags.push(alias);
                                });
                            }

                            const normalizeTagsForPersist = (list) => {
                                if (item.key !== 'SYNTH_ALIASES') return list;
                                const normalized = Array.isArray(list) ? list.slice() : [];
                                const existing = new Set(normalized.map((t) => String(t).toLowerCase()));
                                lockedAliasValues.forEach((alias) => {
                                    if (!existing.has(String(alias).toLowerCase())) normalized.push(alias);
                                });
                                return normalized;
                            };

                            const renderChips = () => {
                                chips.innerHTML = '';
                                tags.forEach((t, idx) => {
                                    const isLocked = item.key === 'SYNTH_ALIASES' && lockedAliasLookup.has(String(t).toLowerCase());
                                    const chip = document.createElement('span');
                                    chip.className = 'tag-chip';
                                    chip.textContent = String(t);
                                    if (isEditable && !isLocked) {
                                        const rem = document.createElement('button');
                                        rem.type = 'button';
                                        rem.className = 'tag-remove';
                                        rem.textContent = '×';
                                        rem.addEventListener('click', () => {
                                            tags.splice(idx, 1);
                                            renderChips();
                                            // Persist tags array
                                            persistValue(normalizeTagsForPersist(tags), [input]);
                                        });
                                        chip.appendChild(rem);
                                    }
                                    chips.appendChild(chip);
                                });
                            };

                            input.addEventListener('keydown', (ev) => {
                                if (ev.key === 'Enter') {
                                    ev.preventDefault();
                                    const v = input.value.trim();
                                    if (v) {
                                        if (!tags.includes(v)) tags.push(v);
                                        input.value = '';
                                        renderChips();
                                        persistValue(normalizeTagsForPersist(tags), [input]);
                                    }
                                }
                            });

                            input.addEventListener('blur', () => {
                                // Persist on blur in case user typed but didn't press Enter
                                const v = input.value.trim();
                                if (v) {
                                    if (!tags.includes(v)) tags.push(v);
                                    input.value = '';
                                }
                                renderChips();
                                persistValue(normalizeTagsForPersist(tags), [input]);
                            });

                            renderChips();
                            wrap.appendChild(chips);
                            wrap.appendChild(input);
                            inputEl = wrap;

                        } else {
                            const input = document.createElement('input');
                            input.type = item.ui_type === 'password' ? 'password' : (item.value_type === 'int' || item.value_type === 'float' || item.ui_type === 'number' ? 'number' : 'text');
                            input.autocomplete = item.ui_type === 'password' ? 'new-password' : 'off';
                            input.value = typeof value === 'string' ? value : JSON.stringify(value);
                            input.disabled = !isEditable;
                            inputEl = input;
                        }

                        if (inputEl) inputWrap.appendChild(inputEl);
                        if (extraEl) inputWrap.appendChild(extraEl);

                        // Attach save handlers for editable inputs so pressing Enter or changing
                        // selects/checkboxes persists values via the /api/config endpoint
                        if (isEditable && inputEl && !skipAutoSave) {
                            // Checkbox
                            if (inputEl.tagName && inputEl.tagName.toLowerCase() === 'input' && inputEl.type === 'checkbox') {
                                inputEl.addEventListener('change', () => { persistValue(inputEl.checked, [inputEl]); });
                            } else if (inputEl.tagName && inputEl.tagName.toLowerCase() === 'select') {
                                inputEl.addEventListener('change', () => { persistValue(inputEl.value, [inputEl]); });
                            } else if (inputEl.tagName && inputEl.tagName.toLowerCase() === 'textarea') {
                                // Ctrl+Enter to submit JSON/textarea; blur to auto-save
                                let debounced = null;
                                inputEl.addEventListener('keydown', (ev) => {
                                    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
                                        ev.preventDefault();
                                        persistValue(inputEl.value, [inputEl]);
                                    }
                                    // simple debounce to avoid excessive saves on blur
                                    if (debounced) clearTimeout(debounced);
                                });
                                inputEl.addEventListener('blur', () => { debounced = setTimeout(() => persistValue(inputEl.value, [inputEl]), 150); });
                            } else {
                                // Default: single-line inputs — Enter to save, blur to save
                                inputEl.addEventListener('keydown', (ev) => {
                                    if (ev.key === 'Enter' && !ev.shiftKey && !ev.ctrlKey && !ev.metaKey) {
                                        ev.preventDefault();
                                        persistValue(inputEl.value, [inputEl]);
                                    }
                                });
                                inputEl.addEventListener('blur', () => { persistValue(inputEl.value, [inputEl]); });
                            }
                        }

                        row.appendChild(inputWrap);
                        container.appendChild(row);
                    });
                };

                const groupByComponent = (list) => {
                    return list.reduce((acc, it) => {
                        const comp = it.component_label || it.component || 'Other';
                        acc[comp] = acc[comp] || [];
                        acc[comp].push(it);
                        return acc;
                    }, {});
                };

                const renderGrouped = (list, container) => {
                    container.innerHTML = '';
                    const groups = groupByComponent(list);
                    const keys = Object.keys(groups).sort();
                    if (!keys.length) {
                        const empty = document.createElement('div');
                        empty.className = 'meta';
                        empty.textContent = 'No configuration entries found.';
                        container.appendChild(empty);
                        return;
                    }
                    keys.forEach((comp) => {
                        const section = document.createElement('div');
                        section.className = 'config-section collapsible collapsed';

                        const h = document.createElement('h3');
                        h.className = 'config-section-title';
                        h.style.cursor = 'pointer';
                        h.setAttribute('role', 'button');
                        h.setAttribute('tabindex', '0');
                        h.setAttribute('aria-expanded', 'false');
                        const label = document.createElement('span');
                        label.className = 'config-section-label';
                        label.textContent = comp;
                        const toggleSection = () => {
                            const isCollapsed = section.classList.toggle('collapsed');
                            h.setAttribute('aria-expanded', isCollapsed ? 'false' : 'true');
                        };
                        h.addEventListener('click', toggleSection);
                        h.addEventListener('keydown', (ev) => {
                            if (ev.key === 'Enter' || ev.key === ' ') {
                                ev.preventDefault();
                                toggleSection();
                            }
                        });

                        h.appendChild(label);
                        section.appendChild(h);

                        const sectionNote = (groups[comp] && groups[comp][0] && groups[comp][0].component_description) ? groups[comp][0].component_description : '';
                        if (sectionNote) {
                            const note = document.createElement('div');
                            note.className = 'config-section-note';
                            note.textContent = sectionNote;
                            section.appendChild(note);
                        }

                        const listEl = document.createElement('div');
                        listEl.className = 'config-list';
                        section.appendChild(listEl);

                        container.appendChild(section);
                        renderList(groups[comp], listEl);
                    });
                };

                const setConfigSectionsCollapsed = (collapse) => {
                    document.querySelectorAll('.config-section.collapsible').forEach((section) => {
                        section.classList.toggle('collapsed', collapse);
                        const title = section.querySelector('.config-section-title');
                        if (title) title.setAttribute('aria-expanded', collapse ? 'false' : 'true');
                    });
                };

                const accentItem = items.find(it => (it.key && it.key === 'WEBUI_ACCENT_COLOR') || (it.label && /accent\s*color/i.test(it.label)));
                const accentCard = document.getElementById('config-accent-card');
                const accentContainer = document.getElementById('config-accent-input-container');
                const themeCard = document.getElementById('config-theme-card');
                const themeContainer = document.getElementById('config-theme-input-container');
                const omitAccent = !!accentItem && ((accentCard && accentContainer) || (themeCard && themeContainer));

                const general = items.filter((item) => !item.advanced && (!omitAccent || item.key !== 'WEBUI_ACCENT_COLOR'));
                const advanced = items.filter((item) => item.advanced && (!omitAccent || item.key !== 'WEBUI_ACCENT_COLOR'));

                const settingsHelper = window.SynthSettings;
                if (settingsHelper && typeof settingsHelper.bindConfigSearch === 'function') {
                    settingsHelper.bindConfigSearch({
                        general,
                        advanced,
                        renderGrouped,
                        configGeneralListEl,
                        configAdvancedListEl,
                        statusEl: document.getElementById('config-search-status')
                    });
                } else {
                    renderGrouped(general, configGeneralListEl);
                    renderGrouped(advanced, configAdvancedListEl);
                }

                if (configExpandAll && !configExpandAll.dataset.bound) {
                    configExpandAll.dataset.bound = '1';
                    configExpandAll.addEventListener('click', () => setConfigSectionsCollapsed(false));
                }
                if (configCollapseAll && !configCollapseAll.dataset.bound) {
                    configCollapseAll.dataset.bound = '1';
                    configCollapseAll.addEventListener('click', () => setConfigSectionsCollapsed(true));
                }


                // --- Render a prominent Accent control at the top of Settings (Appearance card)
                try {
                    const renderAccentControl = async (card, container) => {
                        if (!card || !container || !accentItem) return;
                        card.style.display = 'block';
                        container.innerHTML = '';
                        const current = (typeof accentItem.value === 'string' && accentItem.value) ? accentItem.value : (accentItem.default || '#6bfefe');
                        const isEditable = !(accentItem.readonly || accentItem.env_override);
                        let previewVal = null;
                        const isValidHex = (val) => /^#([0-9a-f]{3}){1,2}$/i.test(String(val || ''));

                        const presets = (window.__SYNTH_CONFIG && window.__SYNTH_CONFIG.WEBUI_ACCENT_PRESETS) || ['#6bfefe','#ff6bd6','#18c98c','#ffd166','#ff9ecb'];
                        const presetsWrap = document.createElement('div');
                        presetsWrap.className = 'accent-presets';
                        const setActivePreset = (val) => {
                            const needle = String(val || '').toLowerCase();
                            presetsWrap.querySelectorAll('button[data-color]').forEach((btn) => {
                                const btnColor = String(btn.getAttribute('data-color') || '').toLowerCase();
                                btn.classList.toggle('is-active', !!needle && btnColor === needle);
                            });
                        };
                        presets.forEach((c) => {
                            const b = document.createElement('button');
                            b.type = 'button';
                            b.style.background = c;
                            b.title = c;
                            b.setAttribute('data-color', c);
                            b.addEventListener('click', () => {
                                previewVal = c;
                                try { document.documentElement.style.setProperty('--accent', c); const [r,g,b2] = _hexToRgb(c); document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b2}, 0.16)`); document.documentElement.style.setProperty('--accent-r', String(r)); document.documentElement.style.setProperty('--accent-g', String(g)); document.documentElement.style.setProperty('--accent-b', String(b2)); document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(c)); document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(c)); } catch (e) {}
                                updateColorDot(c);
                                setActivePreset(c);
                            });
                            presetsWrap.appendChild(b);
                        });

                        const colorInput = document.createElement('input');
                        colorInput.type = 'color';
                        colorInput.value = current;
                        colorInput.className = 'accent-color-input';
                        colorInput.disabled = !isEditable;
                        colorInput.addEventListener('input', (ev) => {
                            previewVal = ev.target.value;
                            try { const c = previewVal; document.documentElement.style.setProperty('--accent', c); const [r,g,b2] = _hexToRgb(c); document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b2}, 0.16)`); document.documentElement.style.setProperty('--accent-r', String(r)); document.documentElement.style.setProperty('--accent-g', String(g)); document.documentElement.style.setProperty('--accent-b', String(b2)); document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(c)); document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(c)); } catch (e) {}
                            updateColorDot(previewVal);
                        });

                        const colorDot = document.createElement('button');
                        colorDot.type = 'button';
                        colorDot.className = 'accent-color-dot';
                        colorDot.disabled = !isEditable;
                        const updateColorDot = (val) => {
                            if (isValidHex(val)) {
                                colorDot.style.background = val;
                                colorDot.textContent = '';
                                colorDot.setAttribute('aria-label', `Color ${val}`);
                            } else {
                                colorDot.style.background = 'transparent';
                                colorDot.textContent = '🎨';
                                colorDot.setAttribute('aria-label', 'Pick accent color');
                            }
                        };
                        updateColorDot(current);
                        setActivePreset(current);
                        colorDot.addEventListener('click', () => {
                            if (!colorInput.disabled) colorInput.click();
                        });

                        const applyBtn = document.createElement('button');
                        applyBtn.type = 'button';
                        applyBtn.className = 'apply';
                        applyBtn.textContent = 'Apply';
                        applyBtn.disabled = !isEditable;
                        applyBtn.addEventListener('click', async () => {
                            const val = previewVal || colorInput.value;
                            try {
                                const payload = { key: accentItem.key, value: val };
                                const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                                if (res.ok) {
                                    setActivePreset(val);
                                    await refreshConfig();
                                } else {
                                    const txt = await res.text();
                                    window.showToast && window.showToast('Save failed: ' + txt, true);
                                }
                            } catch (e) {
                                window.showToast && window.showToast('Save failed', true);
                            }
                        });

                        const cancelBtn = document.createElement('button');
                        cancelBtn.type = 'button';
                        cancelBtn.className = 'cancel';
                        cancelBtn.textContent = 'Cancel';
                        cancelBtn.disabled = !isEditable;
                        cancelBtn.addEventListener('click', () => {
                            previewVal = null;
                            colorInput.value = current;
                            try { const c = current; document.documentElement.style.setProperty('--accent', c); const [r,g,b2] = _hexToRgb(c); document.documentElement.style.setProperty('--accent-soft', `rgba(${r}, ${g}, ${b2}, 0.16)`); document.documentElement.style.setProperty('--accent-r', String(r)); document.documentElement.style.setProperty('--accent-g', String(g)); document.documentElement.style.setProperty('--accent-b', String(b2)); document.documentElement.style.setProperty('--accent-contrast', pickAccentContrastFromHex(c)); document.documentElement.style.setProperty('--accent-dark', pickAccentDarkFromHex(c)); } catch (e) {}
                            updateColorDot(current);
                            setActivePreset(current);
                        });

                        const resetBtn = document.createElement('button');
                        resetBtn.type = 'button';
                        resetBtn.textContent = 'Reset';
                        resetBtn.disabled = !isEditable;
                        resetBtn.addEventListener('click', async () => {
                            const def = accentItem.default || '#6bfefe';
                            previewVal = def;
                            colorInput.value = def;
                            updateColorDot(def);
                            setActivePreset(def);
                            try { const payload = { key: accentItem.key, value: def }; const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); if (res.ok) await refreshConfig(); } catch (e) { window.showToast && window.showToast('Save failed', true); }
                        });

                        const actions = document.createElement('div');
                        actions.className = 'accent-actions';
                        actions.appendChild(applyBtn);
                        actions.appendChild(cancelBtn);
                        actions.appendChild(resetBtn);

                        container.appendChild(presetsWrap);
                        container.appendChild(colorDot);
                        container.appendChild(colorInput);
                        container.appendChild(actions);
                    };

                    if (accentItem) {
                        await renderAccentControl(accentCard, accentContainer);
                        await renderAccentControl(themeCard, themeContainer);
                    } else {
                        if (accentCard) accentCard.style.display = 'none';
                        if (themeCard) themeCard.style.display = 'none';
                    }
                } catch (e) {
                    console.error('[synth_webui] Failed to render theme preview', e);
                }
            } catch (e) {
                console.error('[synth_webui] Failed to load configuration', e);
                const configGeneralListEl = document.getElementById('config-general-list');
                const configAdvancedListEl = document.getElementById('config-advanced-list');
                if (configGeneralListEl) configGeneralListEl.innerHTML = '<div class="meta">Failed to load configuration.</div>';
                if (configAdvancedListEl) configAdvancedListEl.innerHTML = '<div class="meta">Failed to load configuration.</div>';
            }
        }
        window.refreshConfig = window.refreshConfig || refreshConfig;

        // If the settings page is opened very early during startup the server may
        // still be populating configuration from the DB. Provide a short retry
        // so the UI will pick up DB-backed values when they become available.
        async function refreshConfigWithRetries(retries = 2, delayMs = 600) {
            await refreshConfig();
            for (let i = 0; i < retries; i++) {
                // If any config row still equals its default, attempt a re-fetch
                const anyDefault = Array.from(document.querySelectorAll('.config-row')).some((r) => {
                    try {
                        const inp = r.querySelector('.config-input input, .config-input textarea, .config-input select');
                        if (!inp) return false;
                        const defaultAttr = r.getAttribute('data-default');
                        if (!defaultAttr) return false;
                        // Compare rendered value to default string
                        return String(inp.value || '').trim() === String(defaultAttr || '').trim();
                    } catch (e) { return false; }
                });
                if (!anyDefault) break;
                await new Promise((res) => setTimeout(res, delayMs));
                await refreshConfig();
            }
        }
        window.refreshConfigWithRetries = window.refreshConfigWithRetries || refreshConfigWithRetries;

        // -----------------------------------------------------------------------------
        // Core UI wiring (navigation + chat controls + WebSocket)
        // -----------------------------------------------------------------------------
        (function(){
            'use strict';

            async function loadComponentsSummary() {
                try {
                    const componentsCortexSummaryEl = document.getElementById('components-cortex-summary');
                    const componentsCortexListEl = document.getElementById('components-cortex-list');
                    const componentsInterfacesListEl = document.getElementById('components-interfaces-list');
                    const componentsPluginsListEl = document.getElementById('components-plugins-list');
                    const componentsVoxListEl = document.getElementById('components-vox-list');
                    const componentsAurisListEl = document.getElementById('components-auris-list');
                    const componentsLiveListEl = document.getElementById('components-live-list');
                    const componentsIrisListEl = document.getElementById('components-iris-list');
                    if (!componentsCortexListEl && !componentsInterfacesListEl && !componentsPluginsListEl && !componentsVoxListEl && !componentsAurisListEl && !componentsLiveListEl && !componentsIrisListEl) return;
                    const [res, cfgRes] = await Promise.all([
                        fetch('/api/components'),
                        fetch('/api/config').catch(() => null),
                    ]);
                    // Build a map of component name → config items for inline editing
                    let _componentConfigMap = {};
                    try {
                        if (cfgRes && cfgRes.ok) {
                            const cfgPayload = await cfgRes.json();
                            const cfgItems = Array.isArray(cfgPayload.items) ? cfgPayload.items : [];
                            cfgItems.forEach((ci) => {
                                if (ci && ci.component) {
                                    if (!_componentConfigMap[ci.component]) _componentConfigMap[ci.component] = [];
                                    _componentConfigMap[ci.component].push(ci);
                                }
                            });
                        }
                    } catch (e) { console.debug('[synth_webui] Failed to load config for components', e); }

                    // Handle non-2xx responses gracefully and display server-provided
                    // error details in the UI without throwing. This avoids breaking
                    // the Components tab when the server returns 500 and does not
                    // require restarting the backend.
                    let data = null;
                    if (!res.ok) {
                        try {
                            const text = await res.text();
                            try { data = JSON.parse(text); } catch (e) { data = null; }
                            const errText = (data && (data.detail || data.error || JSON.stringify(data))) || text || `HTTP ${res.status}`;
                            console.error('[synth_webui] Components endpoint error:', res.status, errText);
                            if (componentsCortexListEl) componentsCortexListEl.innerHTML = `<div class="meta">Failed to load components: ${safeEscapeHtml(errText)}</div>`;
                            if (componentsInterfacesListEl) componentsInterfacesListEl.innerHTML = `<div class="meta">Failed to load components: ${safeEscapeHtml(errText)}</div>`;
                            if (componentsPluginsListEl) componentsPluginsListEl.innerHTML = `<div class="meta">Failed to load components: ${safeEscapeHtml(errText)}</div>`;
                            if (componentsVoxListEl) componentsVoxListEl.innerHTML = `<div class="meta">Failed to load components: ${safeEscapeHtml(errText)}</div>`;
                            if (componentsAurisListEl) componentsAurisListEl.innerHTML = `<div class="meta">Failed to load components: ${safeEscapeHtml(errText)}</div>`;
                            if (componentsLiveListEl) componentsLiveListEl.innerHTML = `<div class="meta">Failed to load components: ${safeEscapeHtml(errText)}</div>`;
                            if (componentsIrisListEl) componentsIrisListEl.innerHTML = `<div class="meta">Failed to load components: ${safeEscapeHtml(errText)}</div>`;
                        } catch (e) {
                            console.error('[synth_webui] Failed to read components error body', e);
                            if (componentsCortexListEl) componentsCortexListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                            if (componentsInterfacesListEl) componentsInterfacesListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                            if (componentsPluginsListEl) componentsPluginsListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                        }
                        return;
                    }
                    data = await res.json();

                    // Cortex summary
                    if (componentsCortexSummaryEl && data.cortex) {
                        componentsCortexSummaryEl.textContent = `Active engine: ${data.cortex.active_engine || '—'} (cortex: ${data.cortex.active_kind || '—'})`;
                    }

                    const cortexKindSelect = document.getElementById('cortex-kind-select');
                    const engineSelect = document.getElementById('cortex-engine-select');
                    const engineModelLabel = document.getElementById('cortex-engine-model');
                    const engineLabel = document.getElementById('cortex-engine-label');
                    const engineLoginStateLabel = document.getElementById('cortex-engine-login-state');
                    const engineLoginBtn = document.getElementById('cortex-login-btn');
                    const engineLoginWarning = document.getElementById('cortex-login-warning');
                    const engineLoginWarningSelkies = document.getElementById('cortex-login-warning-selkies');
                    const engineLoginWarningUrl = document.getElementById('cortex-login-warning-url');
                    const devToggle = document.getElementById('dev-components-toggle');

                    const getEngineByName = (name) => {
                        const engines = (data.cortex && Array.isArray(data.cortex.engines)) ? data.cortex.engines : [];
                        return engines.find((engine) => engine && engine.name === name) || null;
                    };

                    const resolveSelkiesLoginUrl = async () => {
                        if (resolveSelkiesLoginUrl.cache) return resolveSelkiesLoginUrl.cache;
                        let cfg = null;
                        try {
                            const r = await fetch('/api/selkies');
                            if (r.ok) cfg = await r.json();
                        } catch (e) {
                            cfg = null;
                        }

                        const rawHost = (cfg && cfg.host) || window.location.hostname || '127.0.0.1';
                        const loopback = (rawHost === '127.0.0.1' || rawHost === 'localhost' || rawHost === '0.0.0.0');
                        const host = loopback ? window.location.hostname : rawHost;

                        const detected = (cfg && cfg.detected_protocol) || null;
                        const detectedPort = (cfg && cfg.detected_port) || null;
                        const hasHttps = !!(cfg && cfg.https_port);
                        const hasHttp = !!(cfg && cfg.http_port);

                        let proto = 'https';
                        let port = (cfg && cfg.https_port) || (detected === 'https' ? detectedPort : null) || (cfg && cfg.http_port) || detectedPort || 3000;
                        if (detected === 'http' && !hasHttps) {
                            proto = 'http';
                            port = (cfg && cfg.http_port) || detectedPort || 3000;
                        } else if (!hasHttps && hasHttp) {
                            proto = 'http';
                            port = (cfg && cfg.http_port) || detectedPort || 3000;
                        }

                        const url = `${proto}://${host}:${port}`;
                        resolveSelkiesLoginUrl.cache = url;
                        return url;
                    };

                    // Helper to render engines list and select for a particular cortex kind
                    const renderForCortex = (kind) => {
                        const byCortex = (data.cortex && data.cortex.by_cortex) || {};
                        let engines = (Array.isArray(byCortex[kind]) ? byCortex[kind].slice() : []);
                        // For llm_provider, only show external endpoint engines
                        if (kind === 'llm_provider') engines = engines.filter(e => e.is_external);
                        engines = engines.sort((a, b) => {
                            const an = (a.display_name || a.name || '').toLowerCase();
                            const bn = (b.display_name || b.name || '').toLowerCase();
                            return an.localeCompare(bn);
                        });

                        // Populate engine select
                        if (engineSelect) {
                            engineSelect.innerHTML = '';
                            engines.forEach((engine) => {
                                const opt = document.createElement('option');
                                opt.value = engine.name;
                                opt.textContent = engine.display_name || engine.name || 'Engine';
                                if (engine.active) opt.selected = true;
                                engineSelect.appendChild(opt);
                            });
                        }

                        // Update info field with active engine for this cortex
                        const active = engines.find(e => e.active) || engines.find(e => e.name === (data.cortex && data.cortex.active_engine)) || engines[0] || null;
                        if (engineModelLabel) engineModelLabel.textContent = `model: ${active ? (active.current_model || active.display_name || active.name || '—') : '—'}`;
                        if (engineLabel) engineLabel.textContent = active ? (active.label || active.description || '') : '';
                        const loginState = active ? (active.login_state || (active.logged_in ? 'logged' : 'unlogged')) : '—';
                        if (engineLoginStateLabel) engineLoginStateLabel.textContent = `state: ${loginState}`;

                        // --- Searchable model picker for engines with supported_models ---
                        const modelPicker = document.getElementById('cortex-model-picker');
                        const modelSearch = document.getElementById('cortex-model-search');
                        const modelDropdown = document.getElementById('cortex-model-dropdown');
                        if (modelPicker && modelSearch && modelDropdown) {
                            const models = (active && Array.isArray(active.supported_models)) ? active.supported_models : [];
                            if (models.length > 1) {
                                modelPicker.style.display = '';
                                modelSearch.value = active.current_model || '';
                                modelSearch.placeholder = `Search ${models.length} models…`;
                                const MAX_VIS = 80;
                                const renderModelList = (filter) => {
                                    modelDropdown.innerHTML = '';
                                    const q = (filter || '').toLowerCase();
                                    let count = 0;
                                    for (const m of models) {
                                        if (q && !m.toLowerCase().includes(q)) continue;
                                        if (++count > MAX_VIS) {
                                            const more = document.createElement('div');
                                            more.style.cssText = 'padding:0.35rem 0.6rem;color:var(--text-soft,#aaa);font-size:0.8rem;';
                                            more.textContent = `${models.length - MAX_VIS}+ more — refine your search`;
                                            modelDropdown.appendChild(more);
                                            break;
                                        }
                                        const row = document.createElement('div');
                                        row.textContent = m;
                                        row.style.cssText = 'padding:0.4rem 0.7rem;cursor:pointer;font-size:0.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
                                        if (m === active.current_model) row.style.fontWeight = '700';
                                        row.addEventListener('mouseenter', () => { row.style.background = 'var(--accent-dim, rgba(107,254,254,0.12))'; });
                                        row.addEventListener('mouseleave', () => { row.style.background = ''; });
                                        row.addEventListener('mousedown', (ev) => {
                                            ev.preventDefault();
                                            modelSearch.value = m;
                                            modelDropdown.style.display = 'none';
                                            // Persist model selection via API
                                            fetch('/api/components/cortex/model', {
                                                method: 'POST',
                                                headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({ engine: active.name, model: m })
                                            }).then((res) => {
                                                if (!res.ok) throw new Error('HTTP ' + res.status);
                                                if (engineModelLabel) engineModelLabel.textContent = `model: ${m}`;
                                                if (window.showToast) window.showToast('Saved', false);
                                            }).catch((err) => {
                                                console.error('[synth_webui] Failed to set model', err);
                                                alert('Failed to set model: ' + err.message);
                                            });
                                        });
                                        modelDropdown.appendChild(row);
                                    }
                                    if (count === 0) {
                                        const empty = document.createElement('div');
                                        empty.style.cssText = 'padding:0.5rem 0.7rem;color:var(--text-soft,#aaa);font-size:0.85rem;';
                                        empty.textContent = 'No matching models';
                                        modelDropdown.appendChild(empty);
                                    }
                                };

                                // Keep render function current every pass so stale-closure issues
                                // don't arise when available models change after first bind.
                                modelSearch._renderList = renderModelList;

                                // Wire events only once
                                if (!modelSearch.dataset.bound) {
                                    modelSearch.addEventListener('focus', () => {
                                        modelSearch.select();
                                        modelSearch._renderList(''); // always show ALL models on focus
                                        modelDropdown.style.display = '';
                                    });
                                    modelSearch.addEventListener('input', () => { modelSearch._renderList(modelSearch.value); });
                                    modelSearch.addEventListener('blur', () => { setTimeout(() => { modelDropdown.style.display = 'none'; }, 180); });
                                    modelSearch.addEventListener('keydown', (ev) => {
                                        if (ev.key === 'Enter') {
                                            ev.preventDefault();
                                            modelDropdown.style.display = 'none';
                                            const val = modelSearch.value.trim();
                                            if (val && active) {
                                                fetch('/api/components/cortex/model', {
                                                    method: 'POST',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({ engine: active.name, model: val })
                                                }).then((res) => {
                                                    if (!res.ok) throw new Error('HTTP ' + res.status);
                                                    if (engineModelLabel) engineModelLabel.textContent = `model: ${val}`;
                                                    if (window.showToast) window.showToast('Saved', false);
                                                }).catch((err) => {
                                                    console.error('[synth_webui] Failed to set model', err);
                                                });
                                            }
                                        } else if (ev.key === 'Escape') {
                                            modelDropdown.style.display = 'none';
                                            modelSearch.blur();
                                        }
                                    });
                                    modelSearch.dataset.bound = '1';
                                }
                            } else {
                                modelPicker.style.display = 'none';
                            }
                        }
                        const isSeleniumKind = String(kind || '').toLowerCase().includes('selenium');
                        if (engineLoginBtn) {
                            engineLoginBtn.style.display = isSeleniumKind ? '' : 'none';
                            engineLoginBtn.disabled = !active || !active.loaded;
                            engineLoginBtn.textContent = active && active.logged_in ? 'Logged' : 'Login';
                        }
                        if (engineLoginWarning) {
                            engineLoginWarning.style.display = isSeleniumKind ? 'block' : 'none';
                        }
                        if (engineLoginWarningUrl) {
                            const loginUrl = active ? (active.login_url || active.service_url || '') : '';
                            engineLoginWarningUrl.textContent = loginUrl || '—';
                        }
                        if (engineLoginWarningSelkies) {
                            engineLoginWarningSelkies.textContent = 'https://{host}:{port}';
                            if (isSeleniumKind) {
                                resolveSelkiesLoginUrl().then((url) => {
                                    engineLoginWarningSelkies.textContent = url;
                                }).catch(() => {});
                            }
                        }

                        // --- Per-endpoint extra config editor (external endpoints only) ---
                        const cfgWrap = document.getElementById('cortex-engine-config-wrap');
                        const cfgArea = document.getElementById('cortex-engine-config');
                        const cfgSave = document.getElementById('cortex-engine-config-save');
                        const cfgStatus = document.getElementById('cortex-engine-config-status');
                        if (cfgWrap && cfgArea) {
                            if (active && active.is_external && active.endpoint_id != null) {
                                cfgWrap.style.display = '';
                                const ec = active.extra_config || {};
                                cfgArea.value = Object.keys(ec).length ? JSON.stringify(ec, null, 2) : '';
                                if (cfgStatus) cfgStatus.textContent = '';
                                if (cfgSave) {
                                    cfgSave._epId = active.endpoint_id;
                                    if (!cfgSave.dataset.bound) {
                                        cfgSave.dataset.bound = '1';
                                        cfgSave.addEventListener('click', async () => {
                                            const setCfgStatus = (msg, ok) => {
                                                if (!cfgStatus) return;
                                                cfgStatus.textContent = msg;
                                                cfgStatus.style.color = ok ? 'var(--success,#27ae60)' : 'var(--danger,#c0392b)';
                                            };
                                            const raw = (cfgArea.value || '').trim();
                                            let parsed = {};
                                            if (raw) {
                                                try {
                                                    parsed = JSON.parse(raw);
                                                } catch (err) {
                                                    setCfgStatus('Invalid JSON: ' + err.message, false);
                                                    return;
                                                }
                                                if (typeof parsed !== 'object' || Array.isArray(parsed) || parsed === null) {
                                                    setCfgStatus('Extra Config must be a JSON object.', false);
                                                    return;
                                                }
                                            }
                                            try {
                                                const res = await fetch(`/api/external-endpoints/${cfgSave._epId}`, {
                                                    method: 'PUT',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({ extra_config: parsed }),
                                                });
                                                if (!res.ok) throw new Error('HTTP ' + res.status);
                                                setCfgStatus('Saved — reload this engine to apply.', true);
                                                if (window.showToast) window.showToast('Engine config saved', false);
                                            } catch (err) {
                                                setCfgStatus('Save failed: ' + err.message, false);
                                            }
                                        });
                                    }
                                }
                            } else {
                                cfgWrap.style.display = 'none';
                            }
                        }
                    };

                    // Populate cortex kind select
                    if (cortexKindSelect && data.cortex && Array.isArray(data.cortex.available_kinds)) {
                        cortexKindSelect.innerHTML = '';
                        data.cortex.available_kinds.forEach((k) => {
                            const opt = document.createElement('option');
                            opt.value = k;
                            opt.textContent = k.toUpperCase();
                            if (k === data.cortex.active_kind) opt.selected = true;
                            cortexKindSelect.appendChild(opt);
                        });

                        // Bind change handler
                        if (!cortexKindSelect.dataset.bound) {
                            cortexKindSelect.addEventListener('change', () => {
                                const kind = cortexKindSelect.value;
                                renderForCortex(kind);
                                // Also update the detailed cards shown under the selector
                                try {
                                    if (componentsCortexListEl) {
                                        const byCortex = (data.cortex && data.cortex.by_cortex) || {};
                                        let kindEngines = byCortex[kind] || [];
                                        if (kind === 'llm_provider') kindEngines = kindEngines.filter(e => e.is_external);
                                        renderDetailsList(kindEngines, componentsCortexListEl);
                                    }
                                } catch (e) { console.debug('[synth_webui] renderForCortex: failed to re-render cards', e); }
                            });
                            cortexKindSelect.dataset.bound = '1';
                        }
                    }

                    // Initial render
                    const initialKind = (data.cortex && data.cortex.active_kind) || (data.cortex && data.cortex.available_kinds && data.cortex.available_kinds[0]) || 'llm_provider';
                    renderForCortex(initialKind);

                    // Also make sure the initial cards reflect the selected cortex kind
                    try {
                        const byCortex = (data.cortex && data.cortex.by_cortex) || {};
                        if (componentsCortexListEl) {
                            let initEngines = byCortex[initialKind] || [];
                            if (initialKind === 'llm_provider') initEngines = initEngines.filter(e => e.is_external);
                            renderDetailsList(initEngines, componentsCortexListEl);
                        }
                    } catch (e) { console.debug('[synth_webui] init: failed to render initial cortex cards', e); }
                    // Bind engineSelect change to switch engine
                        if (engineSelect && !engineSelect.dataset.bound) {
                        engineSelect.addEventListener('change', async () => {
                            const selected = engineSelect.value;
                            if (!selected) return;
                            try {
                                const res = await fetch('/api/components/cortex', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ name: selected })
                                });
                                if (!res.ok) throw new Error('HTTP ' + res.status);
                                await loadComponentsSummary();
                            } catch (e) {
                                console.error('[synth_webui] Failed to switch engine', e);
                                alert('Failed to switch engine.');
                            }
                        });
                        engineSelect.dataset.bound = '1';
                    }

                    if (engineLoginBtn && !engineLoginBtn.dataset.bound) {
                            engineLoginBtn.addEventListener('click', async () => {
                                const selected = engineSelect.value;
                                if (!selected) return;
                                try {
                                    const engine = getEngineByName(selected);
                                    if (engine) {
                                        try {
                                            const selkiesUrl = await resolveSelkiesLoginUrl();
                                            if (selkiesUrl) {
                                                window.open(selkiesUrl, '_blank', 'noopener');
                                            }
                                        } catch (e) {
                                            console.debug('[synth_webui] Failed to open Selkies URL', e);
                                        }
                                    }
                                    const res = await fetch('/api/components/cortex/login', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ name: selected })
                                    });
                                    if (!res.ok) throw new Error('HTTP ' + res.status);
                                    await loadComponentsSummary();
                                } catch (e) {
                                    console.error('[synth_webui] Failed to start cortex login', e);
                                    alert('Cortex login flow failed to start.');
                                }
                            });
                            engineLoginBtn.dataset.bound = '1';
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

                            // Special: provide an "Open Selkies" button for the built-in Selkies desktop
                            // and for any interface explicitly marked as external with Selkies data.
                            try {
                                const isSelk = (item && (item.name === 'selkies_desktop' || item.is_external && (item.selkies_protocol || item.selkies_port)));
                                if (isSelk) {
                                    const openBtn = document.createElement('button');
                                    openBtn.className = 'pill';
                                    openBtn.textContent = 'Login';
                                    openBtn.title = 'Open Selkies login page in a new tab';
                                    openBtn.style.marginLeft = '8px';
                                    openBtn.addEventListener('click', async (ev) => {
                                        ev.stopPropagation();
                                        ev.preventDefault();
                                        openBtn.disabled = true;
                                        try {
                                            // Try to get authoritative Selkies host/ports from the server
                                            let cfg = null;
                                            try {
                                                const r = await fetch('/api/selkies');
                                                if (r.ok) cfg = await r.json();
                                            } catch (e) {
                                                console.debug('[synth_webui] /api/selkies not available, falling back to component data');
                                            }

                                            // Determine host (substitute loopback with page host for external access)
                                            const rawHost = (cfg && cfg.host) || window.location.hostname || '127.0.0.1';
                                            const loopback = (rawHost === '127.0.0.1' || rawHost === 'localhost' || rawHost === '0.0.0.0');
                                            const host = loopback ? window.location.hostname : rawHost;

                                            // Determine protocol/port preference (prefer HTTPS)
                                            const detected = (cfg && cfg.detected_protocol) || null;
                                            const detectedPort = (cfg && cfg.detected_port) || null;
                                            const hasHttps = (cfg && cfg.https_port) || item.selkies_protocol === 'https' || item.selkies_port;
                                            const hasHttp = (cfg && cfg.http_port) || item.selkies_protocol === 'http' || item.selkies_port;

                                            let proto = 'https';
                                            let port = (cfg && cfg.https_port) || (detected === 'https' ? detectedPort : null) || item.selkies_port || detectedPort || 3000;
                                            if (detected === 'http' && !hasHttps) {
                                                proto = 'http';
                                                port = (cfg && cfg.http_port) || (detected === 'http' ? detectedPort : null) || item.selkies_port || detectedPort || 3000;
                                            } else if (!hasHttps && hasHttp) {
                                                proto = 'http';
                                                port = (cfg && cfg.http_port) || detectedPort || item.selkies_port || 3000;
                                            }

                                            // Prefer a dedicated login path if Selkies exposes it; otherwise open root
                                            const loginPath = (cfg && cfg.login_path) || '/';
                                            const url = `${proto}://${host}:${port}${loginPath}`;
                                            window.open(url, '_blank');
                                        } catch (err) {
                                            console.error('[synth_webui] Failed to open Selkies login', err);
                                            alert('Failed to open Selkies Web Desktop login page.');
                                        } finally {
                                            openBtn.disabled = false;
                                        }
                                    });
                                    summaryActions.appendChild(openBtn);
                                }
                            } catch (e) { /* ignore UI helper errors */ }

                            // Special: "Run Now" button for the Grillo diary consolidator.
                            // Enqueues a diary_consolidation beat immediately without
                            // changing the day-selection logic (never today; most recent
                            // unconsolidated day first).
                            try {
                                if (item && item.name === 'grillo_diary_consolidator') {
                                    const runBtn = document.createElement('button');
                                    runBtn.className = 'pill';
                                    runBtn.textContent = 'Run Now';
                                    runBtn.title = 'Enqueue a diary consolidation beat now';
                                    runBtn.style.marginLeft = '8px';
                                    runBtn.addEventListener('click', async (ev) => {
                                        ev.stopPropagation();
                                        ev.preventDefault();
                                        runBtn.disabled = true;
                                        const originalText = runBtn.textContent;
                                        runBtn.textContent = 'Scheduling…';
                                        try {
                                            const resp = await fetch('/api/components/run', {
                                                method: 'POST',
                                                headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({ name: 'grillo_diary_consolidator', action: 'run_now' })
                                            });
                                            let body = null;
                                            try { body = await resp.json(); } catch (e) { /* ignore */ }
                                            const result = (body && body.result) || {};
                                            if (resp.ok && result.status === 'scheduled') {
                                                runBtn.textContent = `Scheduled (priority ${result.priority})`;
                                            } else if (result.status === 'empty') {
                                                runBtn.textContent = 'Nothing to do';
                                            } else if (result.status === 'disabled') {
                                                runBtn.textContent = 'Disabled';
                                            } else {
                                                const msg = (result && result.message) || (body && body.detail) || 'Failed';
                                                runBtn.textContent = 'Error';
                                                console.error('[synth_webui] Diary consolidation Run Now failed:', msg);
                                                alert(`Diary consolidation Run Now failed: ${msg}`);
                                            }
                                        } catch (err) {
                                            console.error('[synth_webui] Diary consolidation Run Now request failed', err);
                                            runBtn.textContent = 'Error';
                                            alert('Diary consolidation Run Now request failed.');
                                        } finally {
                                            setTimeout(() => {
                                                runBtn.textContent = originalText;
                                                runBtn.disabled = false;
                                            }, 4000);
                                        }
                                    });
                                    summaryActions.appendChild(runBtn);
                                }
                            } catch (e) { /* ignore UI helper errors */ }

                            summary.appendChild(summaryActions);
                            details.appendChild(summary);

                            // Short label (engine-provided) shown as muted helper text
                            if (item.label) {
                                const lbl = document.createElement('div');
                                lbl.className = 'component-helper';
                                lbl.style.color = 'var(--muted)';
                                lbl.style.fontSize = '0.92rem';
                                lbl.style.marginBottom = '6px';
                                lbl.textContent = item.label;
                                details.appendChild(lbl);
                            }

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
                                const list = document.createElement('ul');
                                list.className = 'component-actions-list';
                                item.actions.forEach((action) => {
                                    const li = document.createElement('li');
                                    li.className = 'component-action';
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
                            // ── Inline config editing for cortex engines ──────────
                            const compName = item.name || '';
                            const cfgItems = _componentConfigMap[compName] || [];
                            if (cfgItems.length) {
                                const cfgSection = document.createElement('div');
                                cfgSection.className = 'component-config-section';
                                cfgSection.style.cssText = 'margin-top:12px; padding-top:10px; border-top:1px solid var(--border, #444);';

                                const cfgHeader = document.createElement('div');
                                cfgHeader.style.cssText = 'display:flex; align-items:center; gap:8px; margin-bottom:8px; cursor:pointer; user-select:none;';
                                const cfgToggleIcon = document.createElement('span');
                                cfgToggleIcon.textContent = '▶';
                                cfgToggleIcon.style.cssText = 'font-size:0.7rem; transition:transform 0.2s;';
                                const cfgTitle = document.createElement('span');
                                cfgTitle.style.cssText = 'font-size:0.85rem; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em;';
                                cfgTitle.textContent = 'Configuration';
                                cfgHeader.appendChild(cfgToggleIcon);
                                cfgHeader.appendChild(cfgTitle);

                                const cfgBody = document.createElement('div');
                                cfgBody.style.display = 'none';

                                cfgHeader.addEventListener('click', () => {
                                    const open = cfgBody.style.display !== 'none';
                                    cfgBody.style.display = open ? 'none' : '';
                                    cfgToggleIcon.style.transform = open ? '' : 'rotate(90deg)';
                                });

                                // Separate normal vs advanced items
                                const normalCfg = cfgItems.filter(ci => !ci.advanced);
                                const advancedCfg = cfgItems.filter(ci => ci.advanced);

                                const buildCfgRow = (ci) => {
                                    const row = document.createElement('div');
                                    row.style.cssText = 'display:flex; flex-direction:column; gap:3px; margin-bottom:10px;';

                                    const lbl = document.createElement('label');
                                    lbl.style.cssText = 'font-size:0.88rem; font-weight:600; color:var(--text);';
                                    lbl.textContent = ci.label || ci.key;
                                    row.appendChild(lbl);

                                    if (ci.description) {
                                        const helpText = document.createElement('div');
                                        helpText.style.cssText = 'font-size:0.78rem; color:var(--muted); margin-bottom:2px;';
                                        helpText.textContent = ci.description;
                                        row.appendChild(helpText);
                                    }

                                    const val = ci.value === null || ci.value === undefined ? '' : ci.value;
                                    const editable = !!ci.editable && !ci.env_override;

                                    const saveCfg = async (newVal, el) => {
                                        try {
                                            if (el) el.disabled = true;
                                            const r = await fetch('/api/config', {
                                                method: 'POST',
                                                headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({ key: ci.key, value: newVal })
                                            });
                                            if (!r.ok) {
                                                const t = await r.text();
                                                window.showToast && window.showToast('Save failed: ' + t, true);
                                            } else {
                                                const out = await r.json();
                                                window.showToast && window.showToast('Saved', false);
                                                if (out && out.requires_reload) {
                                                    window.showToast && window.showToast(out.message || 'Reload recommended', false);
                                                }
                                            }
                                        } catch (e) {
                                            window.showToast && window.showToast('Save failed', true);
                                        } finally {
                                            if (el) el.disabled = !editable;
                                        }
                                    };

                                    let inputEl = null;

                                    if (ci.ui_type === 'bool' || ci.value_type === 'bool') {
                                        const wrap = document.createElement('div');
                                        wrap.style.cssText = 'display:flex; align-items:center; gap:8px;';
                                        const cb = document.createElement('input');
                                        cb.type = 'checkbox';
                                        cb.checked = val === true || val === 1 || val === '1' || val === 'true';
                                        cb.disabled = !editable;
                                        cb.id = `comp-cfg-${ci.key}`;
                                        const toggleLbl = document.createElement('label');
                                        toggleLbl.className = 'toggle-switch';
                                        toggleLbl.setAttribute('for', cb.id);
                                        const slider = document.createElement('span');
                                        slider.className = 'toggle-slider';
                                        toggleLbl.appendChild(slider);
                                        cb.addEventListener('change', () => saveCfg(cb.checked, cb));
                                        wrap.appendChild(cb);
                                        wrap.appendChild(toggleLbl);
                                        inputEl = wrap;
                                    } else if (ci.ui_type === 'select' && Array.isArray(ci.options) && ci.options.length) {
                                        const sel = document.createElement('select');
                                        sel.style.cssText = 'padding:6px 10px; background:var(--background); color:var(--text); border:1px solid var(--primary); border-radius:6px; font-size:0.88rem; max-width:400px;';
                                        ci.options.forEach(opt => {
                                            const o = document.createElement('option');
                                            o.value = opt; o.textContent = opt;
                                            if (String(val) === String(opt)) o.selected = true;
                                            sel.appendChild(o);
                                        });
                                        sel.disabled = !editable;
                                        sel.addEventListener('change', () => saveCfg(sel.value, sel));
                                        inputEl = sel;
                                    } else if (ci.ui_type === 'textarea' || (ci.value_type === 'json' && ci.ui_type !== 'tags')) {
                                        const ta = document.createElement('textarea');
                                        ta.rows = 3;
                                        ta.style.cssText = 'width:100%; max-width:500px; padding:6px 10px; background:var(--background); color:var(--text); border:1px solid var(--border,#444); border-radius:6px; font-family:monospace; font-size:0.85rem; resize:vertical;';
                                        ta.value = typeof val === 'string' ? val : JSON.stringify(val, null, 2);
                                        ta.disabled = !editable;
                                        ta.addEventListener('keydown', (ev) => {
                                            if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); saveCfg(ta.value, ta); }
                                        });
                                        ta.addEventListener('blur', () => saveCfg(ta.value, ta));
                                        inputEl = ta;
                                    } else if (ci.ui_type === 'combobox') {
                                        // Searchable dropdown with free-text fallback
                                        const models = (item && Array.isArray(item.supported_models)) ? item.supported_models : [];
                                        const wrap = document.createElement('div');
                                        wrap.style.cssText = 'position:relative; max-width:400px; width:100%;';
                                        const inp = document.createElement('input');
                                        inp.type = 'text';
                                        inp.autocomplete = 'off';
                                        inp.style.cssText = 'padding:6px 10px; background:var(--background); color:var(--text); border:1px solid var(--primary); border-radius:6px; font-size:0.88rem; width:100%; box-sizing:border-box;';
                                        inp.value = typeof val === 'string' ? val : JSON.stringify(val);
                                        inp.disabled = !editable;
                                        inp.placeholder = models.length ? `Search ${models.length} models…` : '';
                                        wrap.appendChild(inp);

                                        if (models.length) {
                                            const dd = document.createElement('div');
                                            dd.style.cssText = 'display:none; position:absolute; top:100%; left:0; right:0; max-height:220px; overflow-y:auto; border:1px solid var(--border,#444); border-radius:6px; background:var(--bg-card,#1a1a2e); z-index:999; margin-top:2px;';
                                            const MAX_SHOW = 60;
                                            const renderDd = (filter) => {
                                                dd.innerHTML = '';
                                                const q = (filter || '').toLowerCase();
                                                let count = 0;
                                                for (const m of models) {
                                                    if (q && !m.toLowerCase().includes(q)) continue;
                                                    if (++count > MAX_SHOW) {
                                                        const more = document.createElement('div');
                                                        more.style.cssText = 'padding:0.3rem 0.6rem; color:var(--muted); font-size:0.78rem;';
                                                        more.textContent = `${models.length - MAX_SHOW}+ more — refine search`;
                                                        dd.appendChild(more);
                                                        break;
                                                    }
                                                    const row = document.createElement('div');
                                                    row.textContent = m;
                                                    row.style.cssText = 'padding:0.35rem 0.6rem; cursor:pointer; font-size:0.85rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
                                                    if (m === val) row.style.fontWeight = '700';
                                                    row.addEventListener('mouseenter', () => { row.style.background = 'var(--accent-dim, rgba(107,254,254,0.12))'; });
                                                    row.addEventListener('mouseleave', () => { row.style.background = ''; });
                                                    row.addEventListener('mousedown', (ev) => {
                                                        ev.preventDefault();
                                                        inp.value = m;
                                                        dd.style.display = 'none';
                                                        saveCfg(m, inp);
                                                    });
                                                    dd.appendChild(row);
                                                }
                                                if (count === 0) {
                                                    const empty = document.createElement('div');
                                                    empty.style.cssText = 'padding:0.4rem 0.6rem; color:var(--muted); font-size:0.82rem;';
                                                    empty.textContent = 'No matching models';
                                                    dd.appendChild(empty);
                                                }
                                            };
                                            inp.addEventListener('focus', () => { renderDd(inp.value); dd.style.display = ''; });
                                            inp.addEventListener('input', () => { renderDd(inp.value); });
                                            inp.addEventListener('blur', () => { setTimeout(() => { dd.style.display = 'none'; }, 180); });
                                            wrap.appendChild(dd);
                                        }
                                        inp.addEventListener('keydown', (ev) => {
                                            if (ev.key === 'Enter') { ev.preventDefault(); saveCfg(inp.value, inp); }
                                        });
                                        inputEl = wrap;
                                    } else {
                                        // Default: text / password / number input
                                        const inp = document.createElement('input');
                                        inp.type = ci.ui_type === 'password' ? 'password'
                                            : (ci.value_type === 'int' || ci.value_type === 'float' || ci.ui_type === 'number') ? 'number' : 'text';
                                        inp.autocomplete = ci.ui_type === 'password' ? 'new-password' : 'off';
                                        inp.style.cssText = 'padding:6px 10px; background:var(--background); color:var(--text); border:1px solid var(--border,#444); border-radius:6px; font-size:0.88rem; max-width:400px; width:100%;';
                                        inp.value = typeof val === 'string' ? val : JSON.stringify(val);
                                        inp.disabled = !editable;
                                        inp.addEventListener('keydown', (ev) => {
                                            if (ev.key === 'Enter') { ev.preventDefault(); saveCfg(inp.value, inp); }
                                        });
                                        inp.addEventListener('blur', () => saveCfg(inp.value, inp));
                                        inputEl = inp;
                                    }

                                    if (inputEl) row.appendChild(inputEl);
                                    return row;
                                };

                                normalCfg.forEach(ci => cfgBody.appendChild(buildCfgRow(ci)));

                                if (advancedCfg.length) {
                                    const advHeader = document.createElement('div');
                                    advHeader.style.cssText = 'display:flex; align-items:center; gap:6px; margin-top:8px; margin-bottom:6px; cursor:pointer; user-select:none;';
                                    const advIcon = document.createElement('span');
                                    advIcon.textContent = '▶';
                                    advIcon.style.cssText = 'font-size:0.65rem; transition:transform 0.2s;';
                                    const advLabel = document.createElement('span');
                                    advLabel.style.cssText = 'font-size:0.8rem; font-weight:600; color:var(--muted);';
                                    advLabel.textContent = 'Advanced';
                                    advHeader.appendChild(advIcon);
                                    advHeader.appendChild(advLabel);

                                    const advBody = document.createElement('div');
                                    advBody.style.display = 'none';
                                    advancedCfg.forEach(ci => advBody.appendChild(buildCfgRow(ci)));

                                    advHeader.addEventListener('click', (ev) => {
                                        ev.stopPropagation();
                                        const open = advBody.style.display !== 'none';
                                        advBody.style.display = open ? 'none' : '';
                                        advIcon.style.transform = open ? '' : 'rotate(90deg)';
                                    });

                                    cfgBody.appendChild(advHeader);
                                    cfgBody.appendChild(advBody);
                                }

                                cfgSection.appendChild(cfgHeader);
                                cfgSection.appendChild(cfgBody);
                                details.appendChild(cfgSection);
                            }

                            container.appendChild(details);
                        });
                    };

                    // Render engine list for the selected cortex kind
                    const toRenderKind = initialKind || 'llm_provider';
                    const byCortex = (data.cortex && data.cortex.by_cortex) || {};
                    let toRenderEngines = byCortex[toRenderKind] || [];
                    if (toRenderKind === 'llm_provider') toRenderEngines = toRenderEngines.filter(e => e.is_external);
                    if (componentsCortexListEl) renderDetailsList(toRenderEngines, componentsCortexListEl);
                    if (componentsInterfacesListEl) renderDetailsList(data.interfaces || [], componentsInterfacesListEl);
                    if (componentsPluginsListEl) renderDetailsList(data.plugins || [], componentsPluginsListEl);

                    // ── Audio registry selectors (Vox / Auris / Live) ──────────────
                    const setupRegistrySelect = (selectId, infoId, labelId, descId, engines, configKey) => {
                        const sel = document.getElementById(selectId);
                        const info = document.getElementById(infoId);
                        const lbl = document.getElementById(labelId);
                        const desc = document.getElementById(descId);
                        if (!sel) return;
                        if (!engines || !engines.length) {
                            sel.style.display = 'none';
                            return;
                        }
                        sel.innerHTML = '';
                        engines.forEach((eng) => {
                            const opt = document.createElement('option');
                            opt.value = eng.name;
                            opt.textContent = eng.display_name || eng.name;
                            if (eng.active) opt.selected = true;
                            sel.appendChild(opt);
                        });
                        const updateInfo = () => {
                            const current = engines.find(e => e.name === sel.value) || engines[0] || null;
                            if (current && info) {
                                info.style.display = '';
                                if (lbl) lbl.textContent = current.display_name || current.name || '—';
                                if (desc) desc.textContent = current.description || current.label || '—';
                            }
                        };
                        updateInfo();
                        if (!sel.dataset.bound) {
                            sel.addEventListener('change', async () => {
                                updateInfo();
                                if (!configKey) return;
                                try {
                                    const r = await fetch('/api/config', {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ key: configKey, value: sel.value })
                                    });
                                    if (!r.ok) throw new Error('HTTP ' + r.status);
                                    window.showToast && window.showToast(selectId.replace('-engine-select', '').toUpperCase() + ' engine updated to ' + sel.value);
                                    await loadComponentsSummary();
                                } catch (e) {
                                    console.error('[synth_webui] Failed to switch engine for ' + configKey, e);
                                    window.showToast && window.showToast('Failed to switch engine', true);
                                }
                            });
                            sel.dataset.bound = '1';
                        }
                    };

                    setupRegistrySelect('vox-engine-select',  'vox-engine-info',  'vox-engine-label',  'vox-engine-description',  data.vox  || [], 'ACTIVE_VOX_ENGINE');
                    setupRegistrySelect('auris-engine-select','auris-engine-info','auris-engine-label','auris-engine-description', data.auris || [], 'ACTIVE_AURIS_ENGINE');
                    setupRegistrySelect('iris-engine-select', 'iris-engine-info', 'iris-engine-label', 'iris-engine-description',  data.iris || [], 'ACTIVE_IRIS_ENGINE');
                    setupRegistrySelect('live-engine-select', 'live-engine-info', 'live-engine-label', 'live-engine-description',  data.live || [], 'LIVE_CORTEX');  // persist selected live engine via LIVE_CORTEX config

                    // ── Per-model metadata helpers (capability filter + language flags) ──
                    // Deterministic language-code → flag emoji map. Codes coming from
                    // speech_options.languages[].code may be lower- or uppercase
                    // (OpenVoice uses EN/ZH/…). We normalize to lowercase and map the
                    // language to a representative regional flag. No keyword matching —
                    // this is a fixed lookup on a structured ISO-ish code.
                    const LANG_FLAG_MAP = {
                        en: '🇬🇧', it: '🇮🇹', zh: '🇨🇳', ja: '🇯🇵', ko: '🇰🇷',
                        es: '🇪🇸', fr: '🇫🇷', de: '🇩🇪', pt: '🇵🇹', ru: '🇷🇺',
                        ar: '🇸🇦', hi: '🇮🇳', nl: '🇳🇱', pl: '🇵🇱', tr: '🇹🇷',
                        sv: '🇸🇪', no: '🇳🇴', da: '🇩🇰', fi: '🇫🇮', cs: '🇨🇿',
                        el: '🇬🇷', he: '🇮🇱', th: '🇹🇭', vi: '🇻🇳', id: '🇮🇩',
                        uk: '🇺🇦', ro: '🇷🇴', hu: '🇭🇺',
                    };
                    const MAX_FLAGS = 6;
                    const langCodeToFlag = (code) => {
                        if (!code) return '';
                        const key = String(code).trim().toLowerCase().split(/[-_]/)[0];
                        return LANG_FLAG_MAP[key] || '';
                    };
                    // Build the "<name> 🇬🇧🇮🇹…(+X)" label suffix from a model's metadata.
                    const modelFlagSuffix = (meta) => {
                        if (!meta || !Array.isArray(meta.languages) || !meta.languages.length) return '';
                        const flags = [];
                        const seen = new Set();
                        for (const lang of meta.languages) {
                            const code = (lang && (lang.code || lang)) || '';
                            const flag = langCodeToFlag(code);
                            if (flag && !seen.has(flag)) { seen.add(flag); flags.push(flag); }
                        }
                        if (!flags.length) return '';
                        if (flags.length <= MAX_FLAGS) return ' ' + flags.join('');
                        return ' ' + flags.slice(0, MAX_FLAGS).join('') + ' +' + (flags.length - MAX_FLAGS);
                    };
                    // Filter a model list to those whose metadata reports a given
                    // capability true. Models without metadata are kept (permissive)
                    // so endpoints that never returned rich metadata still list models.
                    const filterModelsByCapability = (models, metaList, capKey) => {
                        const metaById = new Map();
                        (metaList || []).forEach((m) => {
                            if (m && (m.id || m.model_id)) metaById.set(m.id || m.model_id, m);
                        });
                        // If no metadata at all, keep everything.
                        if (!metaById.size) return models.map((m) => ({ id: m, meta: null }));
                        return models
                            .map((m) => ({ id: m, meta: metaById.get(m) || null }))
                            .filter(({ meta }) => {
                                if (!meta) return true; // permissive for unknown models
                                const caps = meta.capabilities || {};
                                // Permissive when the endpoint declares no
                                // capabilities for this model (common for TTS
                                // models whose metadata reports an empty
                                // capabilities object, e.g. Harmony's kitten /
                                // openvoice / chatterbox_multilingual entries).
                                if (!Object.keys(caps).length) return true;
                                return !!caps[capKey];
                            });
                    };

                    // ── Iris model selector ──────────────────────────────────
                    const irisModelSel = document.getElementById('iris-model-select');
                    const irisEngineSel = document.getElementById('iris-engine-select');

                    const populateIrisModelSelect = (engineName) => {
                        if (!irisModelSel) return;
                        const engine = (data.iris || []).find((e) => e.name === engineName);
                        const allModels = (engine && engine.available_models) ? engine.available_models : [];
                        const metaList = (engine && engine.models_meta) ? engine.models_meta : [];
                        const entries = filterModelsByCapability(allModels, metaList, 'vision');
                        if (!entries.length) {
                            irisModelSel.style.display = 'none';
                            irisModelSel.innerHTML = '';
                            return;
                        }
                        irisModelSel.innerHTML = '';
                        entries.forEach(({ id, meta }) => {
                            const opt = document.createElement('option');
                            opt.value = id;
                            opt.textContent = id + modelFlagSuffix(meta);
                            irisModelSel.appendChild(opt);
                        });
                        const ids = entries.map((e) => e.id);
                        // Pre-select: prefer the saved global IRIS_DEFAULT_MODEL, then engine default
                        const saved = data.iris_current_model || (engine && engine.default_model) || '';
                        irisModelSel.value = ids.includes(saved) ? saved : ids[0];
                        irisModelSel.style.display = '';
                    };

                    if (irisEngineSel) {
                        populateIrisModelSelect(irisEngineSel.value);
                        if (!irisEngineSel.dataset.irisModelBound) {
                            irisEngineSel.addEventListener('change', () => populateIrisModelSelect(irisEngineSel.value));
                            irisEngineSel.dataset.irisModelBound = '1';
                        }
                    }

                    if (irisModelSel && !irisModelSel.dataset.bound) {
                        irisModelSel.addEventListener('change', async () => {
                            try {
                                const r = await fetch('/api/config', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ key: 'IRIS_DEFAULT_MODEL', value: irisModelSel.value }),
                                });
                                if (!r.ok) throw new Error('HTTP ' + r.status);
                                window.showToast && window.showToast('Iris model set to ' + irisModelSel.value);
                            } catch (e) {
                                console.error('[synth_webui] Failed to set IRIS_DEFAULT_MODEL', e);
                                window.showToast && window.showToast('Failed to save Iris model', true);
                            }
                        });
                        irisModelSel.dataset.bound = '1';
                    }
                    // ────────────────────────────────────────────────────────

                    // ── Vox / Auris model selectors (generic, mirrors Iris) ───
                    // The chosen model is persisted into the external endpoint's
                    // extra_config (tts_model / stt_model) via
                    // /api/components/<subsystem>/model — the legacy
                    // VOX_DEFAULT_MODEL / AURIS_DEFAULT_MODEL config keys are dead
                    // (never read by the bridges) and are no longer written here.
                    const setupAudioModelSelect = (modelSelId, engineSelId, engineList, currentModel, saveUrl, boundFlag, capKey) => {
                        const modelSel = document.getElementById(modelSelId);
                        const engineSel = document.getElementById(engineSelId);
                        if (!modelSel) return;

                        const populate = (engineName) => {
                            const engine = (engineList || []).find((e) => e.name === engineName);
                            const allModels = (engine && engine.available_models) ? engine.available_models : [];
                            const metaList = (engine && engine.models_meta) ? engine.models_meta : [];
                            const entries = filterModelsByCapability(allModels, metaList, capKey);
                            if (!entries.length) {
                                modelSel.style.display = 'none';
                                modelSel.innerHTML = '';
                                return;
                            }
                            modelSel.innerHTML = '';
                            entries.forEach(({ id, meta }) => {
                                const opt = document.createElement('option');
                                opt.value = id;
                                opt.textContent = id + modelFlagSuffix(meta);
                                modelSel.appendChild(opt);
                            });
                            const ids = entries.map((e) => e.id);
                            const saved = currentModel || (engine && engine.default_model) || '';
                            modelSel.value = ids.includes(saved) ? saved : ids[0];
                            modelSel.style.display = '';
                        };

                        if (engineSel) {
                            populate(engineSel.value);
                            if (!engineSel.dataset[boundFlag]) {
                                engineSel.addEventListener('change', () => populate(engineSel.value));
                                engineSel.dataset[boundFlag] = '1';
                            }
                        }

                        if (!modelSel.dataset.bound) {
                            modelSel.addEventListener('change', async () => {
                                const engineName = engineSel ? engineSel.value : '';
                                if (!engineName) {
                                    window.showToast && window.showToast('Select an engine first', true);
                                    return;
                                }
                                try {
                                    const r = await fetch(saveUrl, {
                                        method: 'POST',
                                        headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ engine: engineName, model: modelSel.value }),
                                    });
                                    if (!r.ok) throw new Error('HTTP ' + r.status);
                                    window.showToast && window.showToast('Model set to ' + modelSel.value);
                                } catch (e) {
                                    console.error('[synth_webui] Failed to set model via ' + saveUrl, e);
                                    window.showToast && window.showToast('Failed to save model', true);
                                }
                            });
                            modelSel.dataset.bound = '1';
                        }
                    };

                    setupAudioModelSelect('vox-model-select', 'vox-engine-select', data.vox || [], data.vox_current_model || '', '/api/components/vox/model', 'voxModelBound', 'vox');
                    setupAudioModelSelect('auris-model-select', 'auris-engine-select', data.auris || [], data.auris_current_model || '', '/api/components/auris/model', 'aurisModelBound', 'auris');
                    // ────────────────────────────────────────────────────────

                    // ── Live voice configuration ──────────────────────────────
                    const liveVoiceCfg = document.getElementById('live-voice-config');
                    const liveVoiceNameSel = document.getElementById('live-voice-name');
                    const liveVoiceStyleTa = document.getElementById('live-voice-style');
                    const liveEngineSel = document.getElementById('live-engine-select');

                    function updateLiveVoiceVisibility() {
                        if (!liveVoiceCfg || !liveEngineSel) return;
                        liveVoiceCfg.style.display = (liveEngineSel.value && liveEngineSel.value !== 'disabled') ? '' : 'none';
                    }
                    updateLiveVoiceVisibility();
                    if (liveEngineSel && !liveEngineSel.dataset.liveVoiceBound) {
                        liveEngineSel.addEventListener('change', updateLiveVoiceVisibility);
                        liveEngineSel.dataset.liveVoiceBound = '1';
                    }

                    // Load current values from config
                    if (liveVoiceNameSel || liveVoiceStyleTa) {
                        try {
                            const cfgR = await fetch('/api/config');
                            if (cfgR.ok) {
                                const cfgD = await cfgR.json();
                                const cfgI = Array.isArray(cfgD.items) ? cfgD.items : [];
                                const voiceItem = cfgI.find(i => i.key === 'LIVE_VOICE_NAME');
                                const styleItem = cfgI.find(i => i.key === 'LIVE_VOICE_STYLE');
                                if (voiceItem && liveVoiceNameSel) {
                                    const opts = liveVoiceNameSel.options;
                                    for (let i = 0; i < opts.length; i++) {
                                        if (opts[i].value === String(voiceItem.value)) { opts[i].selected = true; break; }
                                    }
                                }
                                if (styleItem && liveVoiceStyleTa) {
                                    liveVoiceStyleTa.value = String(styleItem.value || '');
                                }
                            }
                        } catch (e) { console.debug('[synth_webui] Failed to load live voice config', e); }
                    }

                    // Save on change
                    if (liveVoiceNameSel && !liveVoiceNameSel.dataset.bound) {
                        liveVoiceNameSel.addEventListener('change', async () => {
                            try {
                                const r = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: 'LIVE_VOICE_NAME', value: liveVoiceNameSel.value }) });
                                if (r.ok) window.showToast && window.showToast('Voice set to ' + liveVoiceNameSel.value);
                                else window.showToast && window.showToast('Failed to save voice', true);
                            } catch (e) { window.showToast && window.showToast('Failed to save voice', true); }
                        });
                        liveVoiceNameSel.dataset.bound = '1';
                    }
                    if (liveVoiceStyleTa && !liveVoiceStyleTa.dataset.bound) {
                        const saveStyle = async () => {
                            try {
                                const r = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: 'LIVE_VOICE_STYLE', value: liveVoiceStyleTa.value }) });
                                if (r.ok) window.showToast && window.showToast('Voice style saved');
                                else window.showToast && window.showToast('Failed to save style', true);
                            } catch (e) { window.showToast && window.showToast('Failed to save style', true); }
                        };
                        liveVoiceStyleTa.addEventListener('blur', saveStyle);
                        liveVoiceStyleTa.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); saveStyle(); } });
                        liveVoiceStyleTa.dataset.bound = '1';
                    }

                    // ── Live session feature toggles ─────────────────────────
                    const liveAffective = document.getElementById('live-affective-dialog');
                    const liveProactive = document.getElementById('live-proactive-audio');
                    const liveThinkingBudget = document.getElementById('live-thinking-budget');

                    // Load current values
                    if (liveAffective || liveProactive || liveThinkingBudget) {
                        try {
                            const cfgR = await fetch('/api/config');
                            if (cfgR.ok) {
                                const cfgD = await cfgR.json();
                                const cfgI = Array.isArray(cfgD.items) ? cfgD.items : [];
                                const aff = cfgI.find(i => i.key === 'LIVE_AFFECTIVE_DIALOG');
                                const pro = cfgI.find(i => i.key === 'LIVE_PROACTIVE_AUDIO');
                                const tb = cfgI.find(i => i.key === 'LIVE_THINKING_BUDGET');
                                if (aff && liveAffective) liveAffective.checked = !!aff.value;
                                if (pro && liveProactive) liveProactive.checked = !!pro.value;
                                if (tb && liveThinkingBudget) liveThinkingBudget.value = tb.value || 0;
                            }
                        } catch (e) { /* non-fatal */ }
                    }

                    // Helper to save a boolean/number config key
                    const saveLiveCfg = async (key, value, label) => {
                        try {
                            const r = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, value }) });
                            if (r.ok) window.showToast && window.showToast(label + ' saved');
                            else window.showToast && window.showToast('Failed to save ' + label, true);
                        } catch (e) { window.showToast && window.showToast('Failed to save ' + label, true); }
                    };

                    if (liveAffective && !liveAffective.dataset.bound) {
                        liveAffective.addEventListener('change', () => saveLiveCfg('LIVE_AFFECTIVE_DIALOG', liveAffective.checked, 'Affective dialog'));
                        liveAffective.dataset.bound = '1';
                    }
                    if (liveProactive && !liveProactive.dataset.bound) {
                        liveProactive.addEventListener('change', () => saveLiveCfg('LIVE_PROACTIVE_AUDIO', liveProactive.checked, 'Proactive audio'));
                        liveProactive.dataset.bound = '1';
                    }
                    if (liveThinkingBudget && !liveThinkingBudget.dataset.bound) {
                        liveThinkingBudget.addEventListener('change', () => saveLiveCfg('LIVE_THINKING_BUDGET', parseInt(liveThinkingBudget.value, 10) || 0, 'Thinking budget'));
                        liveThinkingBudget.dataset.bound = '1';
                    }

                    // ── Kitten TTS speaker selector ───────────────────────────
                    const kittenSpeakerSelect = document.getElementById('kitten-speaker-select');
                    const kittenModelSelect = document.getElementById('kitten-model-select');
                    const kittenPlayBtn = document.getElementById('kitten-play-btn');
                    let kittenSpeakerList = [];
                    let kittenModelList = [];

                    function populateKittenModels(currentValue) {
                        if (!kittenModelSelect) return;
                        kittenModelSelect.innerHTML = '';
                        if (!kittenModelList.length) {
                            const opt = document.createElement('option');
                            opt.value = '';
                            opt.textContent = 'No downloaded models';
                            opt.disabled = true;
                            opt.selected = true;
                            kittenModelSelect.appendChild(opt);
                            return;
                        }
                        let matched = false;
                        kittenModelList.forEach(m => {
                            const opt = document.createElement('option');
                            opt.value = m.model_id;
                            opt.textContent = m.display_name || m.model_id;
                            if (String(currentValue) === m.model_id) {
                                opt.selected = true;
                                matched = true;
                            }
                            kittenModelSelect.appendChild(opt);
                        });
                        if (!matched && kittenModelSelect.options.length) {
                            kittenModelSelect.selectedIndex = 0;
                        }
                    }

                    async function loadKittenModels() {
                        try {
                            const r = await fetch('/api/models?plugin_id=vox_kitten');
                            if (r.ok) {
                                const data = await r.json();
                                const models = Array.isArray(data.models) ? data.models : [];
                                kittenModelList = models.filter(m => m.downloaded === true);
                            } else {
                                kittenModelList = [];
                            }
                        } catch (e) {
                            console.error('[synth_webui] failed to load kitten models', e);
                            kittenModelList = [];
                        }
                    }

                    function populateKittenSpeakers(currentValue) {
                        if (!kittenSpeakerSelect) return;
                        kittenSpeakerSelect.innerHTML = '';
                        kittenSpeakerList.forEach(s => {
                            const opt = document.createElement('option');
                            opt.value = s.code;
                            opt.textContent = s.name || s.code;
                            if (String(currentValue) === s.code) opt.selected = true;
                            kittenSpeakerSelect.appendChild(opt);
                        });
                    }

                    async function loadKittenSpeakers() {
                        try {
                            const r = await fetch('/api/vox/speakers?engine=kitten');
                            if (r.ok) {
                                kittenSpeakerList = await r.json();
                            } else {
                                kittenSpeakerList = [];
                            }
                        } catch (e) {
                            console.error('[synth_webui] failed to load kitten speakers', e);
                            kittenSpeakerList = [];
                        }
                    }
                    async function updateKittenControlsVisibility() {
                        if (!kittenSpeakerSelect) return;
                        const voxSel = document.getElementById('vox-engine-select');
                        if (voxSel && voxSel.value === 'kitten') {
                            // ensure we have the speaker + model lists before populating
                            await loadKittenSpeakers();
                            await loadKittenModels();
                            if (kittenModelSelect) kittenModelSelect.style.display = '';
                            kittenSpeakerSelect.style.display = '';
                            kittenPlayBtn.style.display = '';
                            try {
                                const r = await fetch('/api/config');
                                if (r.ok) {
                                    const cfg = await r.json();
                                    const items = Array.isArray(cfg.items) ? cfg.items : [];
                                    const voiceItem = items.find(i => i.key === 'KITTEN_VOICE');
                                    populateKittenSpeakers(voiceItem && voiceItem.value ? voiceItem.value : 'en_1');
                                    const modelItem = items.find(i => i.key === 'KITTEN_MODEL');
                                    populateKittenModels(modelItem && modelItem.value ? modelItem.value : 'builtin');
                                } else {
                                    populateKittenSpeakers('en_1');
                                    populateKittenModels('builtin');
                                }
                            } catch (e) {
                                populateKittenSpeakers('en_1');
                                populateKittenModels('builtin');
                            }
                        } else {
                            if (kittenModelSelect) kittenModelSelect.style.display = 'none';
                            kittenSpeakerSelect.style.display = 'none';
                            if (kittenPlayBtn) kittenPlayBtn.style.display = 'none';
                        }
                    }
                    if (kittenSpeakerSelect && !kittenSpeakerSelect.dataset.bound) {
                        kittenSpeakerSelect.addEventListener('change', async () => {
                            const speaker = kittenSpeakerSelect.value;
                            try {
                                await fetch('/api/config', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ key: 'KITTEN_VOICE', value: speaker })
                                });
                                window.showToast && window.showToast('Kitten speaker set to ' + speaker);
                            } catch (e) {
                                console.error('[synth_webui] Failed to set KITTEN_VOICE', e);
                                window.showToast && window.showToast('Failed to save speaker', true);
                            }
                        });
                        kittenSpeakerSelect.dataset.bound = '1';
                    }
                    if (kittenModelSelect && !kittenModelSelect.dataset.bound) {
                        kittenModelSelect.addEventListener('change', async () => {
                            const model = kittenModelSelect.value;
                            if (!model) return;
                            try {
                                await fetch('/api/config', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ key: 'KITTEN_MODEL', value: model })
                                });
                                window.showToast && window.showToast('Kitten model set to ' + model);
                            } catch (e) {
                                console.error('[synth_webui] Failed to set KITTEN_MODEL', e);
                                window.showToast && window.showToast('Failed to save model', true);
                            }
                        });
                        kittenModelSelect.dataset.bound = '1';
                    }
                    if (kittenPlayBtn && !kittenPlayBtn.dataset.bound) {
                        kittenPlayBtn.addEventListener('click', () => {
                            const code = kittenSpeakerSelect.value;
                            const audio = new Audio(`/api/vox/sample?engine=kitten&speaker=${code}`);
                            audio.play();
                        });
                        kittenPlayBtn.dataset.bound = '1';
                    }
                    // Expose a refresh hook so the Manage-Models modal can rebuild the
                    // downloaded-model combo after a download/delete without a page reload.
                    window.refreshKittenModelSelect = updateKittenControlsVisibility;
                    // show kitten controls now + wire engine change event
                    updateKittenControlsVisibility();
                    const voxEngineSel = document.getElementById('vox-engine-select');
                    if (voxEngineSel) {
                        voxEngineSel.addEventListener('change', updateKittenControlsVisibility);
                    }

                    // ── Vosk language selector ─────────────────────────────────
                    const voskLangSelect = document.getElementById('auris-vosk-language');
                    const VOSK_LANGUAGES = [
                        {code:'en-us', label:'English (US)'},
                        {code:'it-it', label:'Italiano'},
                        {code:'fr-fr', label:'Français'},
                        {code:'es-es', label:'Español'},
                    ];
                    function populateVoskLanguages(){
                        if(!voskLangSelect) return;
                        voskLangSelect.innerHTML = '';
                        VOSK_LANGUAGES.forEach(l=>{
                            const opt = document.createElement('option');
                            opt.value = l.code;
                            opt.textContent = l.label;
                            voskLangSelect.appendChild(opt);
                        });
                    }
                    async function updateVoskLangVisibility(){
                        if(!voskLangSelect) return;
                        const aurisSel = document.getElementById('auris-engine-select');
                        if(aurisSel && aurisSel.value === 'vosk'){
                            voskLangSelect.style.display = '';
                            populateVoskLanguages();
                            // fetch current config value
                            try{
                                const r = await fetch('/api/config');
                                if(r.ok){
                                    const cfg = await r.json();
                                    const item = Array.isArray(cfg.items)? cfg.items.find(i=>i.key==='VOSK_LANGUAGE') : null;
                                    if(item && item.value) voskLangSelect.value = item.value;
                                }
                            } catch (e) { /* ignore */ }
                        } else {
                            voskLangSelect.style.display = 'none';
                        }
                    }
                    if (voskLangSelect && !voskLangSelect.dataset.bound) {
                        voskLangSelect.addEventListener('change', async () => {
                            const lang = voskLangSelect.value;
                            try {
                                await fetch('/api/config', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ key: 'VOSK_LANGUAGE', value: lang })
                                });
                                const r2 = await fetch('/api/auris/vosk/download', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                                    body: `language=${encodeURIComponent(lang)}`
                                });
                                if (!r2.ok) throw new Error('download failed');
                                window.showToast && window.showToast('Vosk language set to ' + lang + ', model download started');
                            } catch (e) {
                                console.error('[synth_webui] Vosk language update failed', e);
                                window.showToast && window.showToast('Failed to set Vosk language', true);
                            }
                        });
                        voskLangSelect.dataset.bound = '1';
                    }
                    // ensure visibility reflects current engine choice
                    updateVoskLangVisibility();
                    // re-run when auris engine selection changes
                    const aurisSel = document.getElementById('auris-engine-select');
                    if(aurisSel){
                        aurisSel.addEventListener('change', updateVoskLangVisibility);
                    }

                    if (componentsVoxListEl)   renderDetailsList(data.vox   || [], componentsVoxListEl);
                    if (componentsAurisListEl) renderDetailsList(data.auris || [], componentsAurisListEl);
                    if (componentsLiveListEl)  renderDetailsList(data.live  || [], componentsLiveListEl);
                    if (componentsIrisListEl)   renderDetailsList(data.iris  || [], componentsIrisListEl);

                    // Render cortex scope selectors (Grillo / Trainer / Live)
                    try {
                        const cortexScopesEl = document.getElementById('cortex-scopes');
                        if (cortexScopesEl && data.cortex && Array.isArray(data.cortex.scopes) && data.cortex.scopes.length) {
                            cortexScopesEl.innerHTML = '';
                            const scopeHeader = document.createElement('div');
                            scopeHeader.style.cssText = 'width:100%; font-size:0.85rem; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px;';
                            scopeHeader.textContent = 'Scope overrides';
                            cortexScopesEl.appendChild(scopeHeader);
                            data.cortex.scopes.forEach((scope) => {
                                const wrap = document.createElement('div');
                                wrap.style.cssText = 'display:flex; align-items:center; gap:6px;';
                                const lbl = document.createElement('label');
                                lbl.style.cssText = 'font-size:0.88rem; color:var(--muted); white-space:nowrap;';
                                lbl.textContent = scope.label + ':';
                                const sel = document.createElement('select');
                                sel.style.cssText = 'padding:5px 10px; background:var(--background); color:var(--text); border:1px solid var(--primary); border-radius:8px; font-size:0.88rem;';
                                (scope.options || []).forEach((opt) => {
                                    const o = document.createElement('option');
                                    o.value = opt;
                                    o.textContent = opt;
                                    if (opt === scope.value) o.selected = true;
                                    sel.appendChild(o);
                                });
                                sel.addEventListener('change', async () => {
                                    try {
                                        const res = await fetch('/api/config', {
                                            method: 'POST',
                                            headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify({ key: scope.key, value: sel.value })
                                        });
                                        if (!res.ok) throw new Error('HTTP ' + res.status);
                                        window.showToast && window.showToast(scope.label + ' cortex updated');
                                    } catch (e) {
                                        console.error('[synth_webui] Failed to update scope cortex', e);
                                        window.showToast && window.showToast('Failed to update ' + scope.label + ' cortex', true);
                                        sel.value = scope.value; // revert
                                    }
                                });
                                wrap.appendChild(lbl);
                                wrap.appendChild(sel);
                                cortexScopesEl.appendChild(wrap);
                            });
                        }
                    } catch (e) { console.debug('[synth_webui] scope selectors render failed', e); }
                } catch (e) {
                    console.error('[synth_webui] Failed to load components', e);
                    const componentsCortexListEl = document.getElementById('components-cortex-list');
                    const componentsInterfacesListEl = document.getElementById('components-interfaces-list');
                    const componentsPluginsListEl = document.getElementById('components-plugins-list');
                    if (componentsCortexListEl) componentsCortexListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    if (componentsInterfacesListEl) componentsInterfacesListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    if (componentsPluginsListEl) componentsPluginsListEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    const componentsVoxListErrEl = document.getElementById('components-vox-list');
                    const componentsAurisListErrEl = document.getElementById('components-auris-list');
                    const componentsLiveListErrEl = document.getElementById('components-live-list');
                    const componentsIrisListErrEl = document.getElementById('components-iris-list');
                    if (componentsVoxListErrEl) componentsVoxListErrEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    if (componentsAurisListErrEl) componentsAurisListErrEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    if (componentsLiveListErrEl) componentsLiveListErrEl.innerHTML = '<div class="meta">Failed to load components.</div>';
                    if (componentsIrisListErrEl) componentsIrisListErrEl.innerHTML = '<div class="meta">Failed to load components.</div>';
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

                // Ensure About tab init is called when this tab becomes active
                try {
                    if (tab === 'about') {
                        if (window.SynthWebUI && typeof window.SynthWebUI.initAboutTab === 'function') {
                            window.SynthWebUI.initAboutTab();
                        }
                    }
                } catch (e) { /* ignore */ }

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

                        // Reset states and ensure nav is closed by default
                        header.classList.remove('topbar--compact', 'topbar--wrap', 'nav-open');
                        const hamburger = header.querySelector('.hamburger');
                        if (hamburger) hamburger.setAttribute('aria-expanded', 'false');

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
                        // Prefer compact/hamburger already when the nav would require 2 lines
                        if (lines >= 2) {
                            compact = true;
                        }
                        if (compact && brandText) {
                            header.classList.add('topbar--compact');
                            // avoid adding wrap when compact: we prefer hamburger + collapsed nav
                        } else {
                            const brandWidthCompact = brand.getBoundingClientRect().width || 0;
                            const availableCompact = Math.max(0, headerWidth - paddingLeft - paddingRight - brandWidthCompact - headerGap);
                            const wrap = navRequired > (availableCompact + tolerance);
                            if (wrap) {
                                header.classList.add('topbar--wrap');
                            }
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
                            try { window.activeTab = tab; if (localStorage && localStorage.setItem) localStorage.setItem('synth-webui-active-tab', tab); } catch (e) { /* ignore */ }

                            // If the desktop is embedded in an iframe, post a message to request a section load
                            if (window.SynthConfig && window.SynthConfig.DESKTOP_IFRAME) {
                                // Reveal immediately for the iframe shell; it manages its own content.
                                setActiveTab(tab);
                                try {
                                    const iframe = document.getElementById('desktop-iframe');
                                    if (iframe && iframe.contentWindow) {
                                        iframe.contentWindow.postMessage({ type: 'load', section: tab }, window.location.origin);
                                    }
                                } catch (e) { /* ignore */ }
                            } else {
                                // Load the section content BEFORE revealing the panel, so the user
                                // never sees an empty panel on the first click of a lazy tab.
                                if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                                    await window.SynthWebUI.loadSection(tab);
                                }
                                setActiveTab(tab);
                            }

                            if (tab === 'history' && window.SynthWebUI && typeof window.SynthWebUI.initHistoryTab === 'function') {
                                try { window.SynthWebUI.initHistoryTab(); } catch (e) { /* ignore */ }
                            }

                            // Ensure page-level scrolling behavior matches the active tab
                            _adjustPageScroll(tab);
                        } catch (e) {
                            console.warn('[synth_webui] tab switch failed', e);
                            // On failure, still reveal the panel so the user isn't stuck on a stale tab.
                            try { setActiveTab(tab); } catch (_) { /* ignore */ }
                        }
                    });
                });

                // Add controls for hamburger and brand click (compatible with both shell variants)
                try {
                    const header = document.querySelector('header.top-bar');
                    const hamburger = document.querySelector('.hamburger');
                    const brandEl = document.querySelector('.brand');
                    const navEl = document.querySelector('nav.main-nav');
                    if (hamburger && header) {
                        hamburger.addEventListener('click', () => {
                            const expanded = header.classList.toggle('nav-open');
                            hamburger.setAttribute('aria-expanded', expanded ? 'true' : 'false');
                            // Also toggle .open on the nav (used by the other base template)
                            try { if (navEl) navEl.classList.toggle('open'); } catch (e) { /* ignore */ }
                        });
                    }
                    // Close the collapsed menu after a nav item is selected
                    document.querySelectorAll('.nav-btn[data-tab]').forEach((b) => {
                        b.addEventListener('click', () => {
                            try {
                                if (header) header.classList.remove('nav-open');
                                if (hamburger) hamburger.setAttribute('aria-expanded', 'false');
                                if (navEl) navEl.classList.remove('open');
                            } catch (e) { /* ignore */ }
                        });
                    });
                    // Clicking the brand/logo goes to the Home tab
                    if (brandEl) {
                        brandEl.style.cursor = 'pointer';
                        brandEl.addEventListener('click', () => {
                            setActiveTab('home');
                            try { window.activeTab = 'home'; if (localStorage && localStorage.setItem) localStorage.setItem('synth-webui-active-tab', 'home'); } catch (e) { /* ignore */ }
                            try {
                                if (header) header.classList.remove('nav-open');
                                if (hamburger) hamburger.setAttribute('aria-expanded', 'false');
                                if (navEl) navEl.classList.remove('open');
                            } catch (e) { /* ignore */ }
                        });
                        brandEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); brandEl.click(); } });
                    }
                } catch (e) { /* ignore */ }

                // Restore last active tab and load its section once.
                let restoredTab = 'home';
                try {
                    const saved = (localStorage && localStorage.getItem && localStorage.getItem('synth-webui-active-tab')) || 'home';
                    restoredTab = saved;
                    setActiveTab(saved);
                    try { setupDesktopIframe(saved); } catch (e) { /* ignore */ }
                    // Ensure page scroll state matches the restored tab (fix for settings not scrollable)
                    try { _adjustPageScroll(saved); } catch (e) { /* ignore */ }
                } catch (e) {
                    restoredTab = 'home';
                    setActiveTab('home');
                    try { setupDesktopIframe('home'); } catch (e) { /* ignore */ }
                }

                try {
                    if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                        // Always warm the history tab (chat history is needed early).
                        window.SynthWebUI.loadSection('history').then(() => {
                            if (window.SynthWebUI && typeof window.SynthWebUI.initHistoryTab === 'function') {
                                window.SynthWebUI.initHistoryTab();
                            }
                        });
                        // Also load the restored active tab so a page reload lands on visible content
                        // instead of an empty panel (only 'history' was warmed before).
                        if (restoredTab && restoredTab !== 'history' && restoredTab !== 'home') {
                            window.SynthWebUI.loadSection(restoredTab).catch(() => { /* ignore */ });
                        }
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
                    import('./chat-window.mjs?v=20260323-expression-fix').then((mod) => {
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
                    const backupBtn = document.getElementById('create-database-backup');
                    const backupStatus = document.getElementById('database-backup-status');
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
                    if (backupBtn) {
                        backupBtn.addEventListener('click', async () => {
                            backupBtn.disabled = true;
                            if (backupStatus) backupStatus.textContent = 'Creating backup…';
                            try {
                                const response = await fetch('/api/database/backup', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                });
                                const payload = await response.json().catch(() => ({}));
                                if (!response.ok || !payload.success) {
                                    throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
                                }
                                const filename = payload.filename || payload.path || 'backup completed';
                                if (backupStatus) backupStatus.textContent = `Backup created: ${filename}`;
                                try { if (window.showToast) window.showToast(`Database backup created: ${filename}`, false); } catch (e) { /* ignore */ }
                            } catch (error) {
                                const message = error && error.message ? error.message : 'Backup failed';
                                if (backupStatus) backupStatus.textContent = `Backup failed: ${message}`;
                                try { if (window.showToast) window.showToast(`Database backup failed: ${message}`, true); } catch (e) { /* ignore */ }
                            } finally {
                                backupBtn.disabled = false;
                            }
                        });
                    }
                    initNotifications();
                    window.__synth_settings_initialized = true;
                }
                refreshConfig();
            }

            function initPluginsTab() {
                loadComponentsSummary();
            }
            // Also expose as initEnginesTab so that the Engines tab triggers the engine selector UI
            function initEnginesTab() {
                loadComponentsSummary();
                initModelManager();
            }

            // ── Manage Models modal (shared by Vox / Auris / Iris) ──────────
            // NOTE: this must be idempotent and re-run every time the Engines
            // section is (re-)rendered — the section HTML is re-injected on each
            // navigation, producing fresh button/modal DOM nodes. A global
            // "initialized" guard would leave the new buttons unbound and make
            // them appear broken. All internal bindings are guarded per-element
            // via dataset.bound, so re-running is safe.
            function initModelManager() {
                const modal = document.getElementById('model-manager-modal');
                const listEl = document.getElementById('model-manager-list');
                const titleEl = document.getElementById('model-manager-title');
                const subtitleEl = document.getElementById('model-manager-subtitle');
                const errorEl = document.getElementById('model-manager-error');
                const closeBtn = document.getElementById('model-manager-close');
                const rowTpl = document.getElementById('model-manager-row-tpl');
                if (!modal || !listEl || !rowTpl) return;

                const SUBSYSTEMS = {
                    vox: { label: 'Vox — Text-to-Speech', tag: 'tts', prefix: 'vox_' },
                    auris: { label: 'Auris — Speech-to-Text', tag: 'stt', prefix: 'auris_' },
                    iris: { label: 'Iris — Vision', tag: 'vision', prefix: 'iris_' },
                };
                const pollTimers = {};

                const showError = (msg) => {
                    if (!errorEl) return;
                    if (msg) { errorEl.textContent = msg; errorEl.style.display = ''; }
                    else { errorEl.textContent = ''; errorEl.style.display = 'none'; }
                };

                const closeModal = () => {
                    modal.style.display = 'none';
                    Object.keys(pollTimers).forEach((k) => { clearInterval(pollTimers[k]); delete pollTimers[k]; });
                };

                if (closeBtn && !closeBtn.dataset.bound) {
                    closeBtn.addEventListener('click', closeModal);
                    closeBtn.dataset.bound = '1';
                }
                if (!modal.dataset.bound) {
                    modal.addEventListener('click', (ev) => { if (ev.target === modal) closeModal(); });
                    modal.dataset.bound = '1';
                }

                const belongsToSubsystem = (model, sub) => {
                    const cfg = SUBSYSTEMS[sub];
                    if (!cfg) return false;
                    const tags = Array.isArray(model.tags) ? model.tags : [];
                    if (tags.includes(cfg.tag)) return true;
                    return typeof model.plugin_id === 'string' && model.plugin_id.startsWith(cfg.prefix);
                };

                const setDownloadingState = (row, model) => {
                    const dlBtn = row.querySelector('.mm-download-btn');
                    const delBtn = row.querySelector('.mm-delete-btn');
                    const progWrap = row.querySelector('.mm-progress-wrap');
                    const progBar = row.querySelector('.mm-progress-bar');
                    const badge = row.querySelector('.mm-downloaded-badge');
                    if (model.downloading) {
                        dlBtn.textContent = 'Downloading…';
                        dlBtn.disabled = true;
                        if (delBtn) delBtn.style.display = 'none';
                        if (progWrap) progWrap.style.display = '';
                        if (progBar) progBar.style.width = Math.round((model.download_progress || 0) * 100) + '%';
                    } else if (model.downloaded) {
                        dlBtn.textContent = 'Re-download';
                        dlBtn.disabled = false;
                        if (delBtn) delBtn.style.display = '';
                        if (progWrap) progWrap.style.display = 'none';
                        if (badge) badge.style.display = '';
                    } else {
                        dlBtn.textContent = 'Download';
                        dlBtn.disabled = false;
                        if (delBtn) delBtn.style.display = 'none';
                        if (progWrap) progWrap.style.display = 'none';
                        if (badge) badge.style.display = 'none';
                    }
                };

                const pollProgress = (modelId, row) => {
                    if (pollTimers[modelId]) return;
                    pollTimers[modelId] = setInterval(async () => {
                        try {
                            const r = await fetch('/api/models/' + encodeURIComponent(modelId) + '/progress');
                            if (!r.ok) throw new Error('HTTP ' + r.status);
                            const raw = await r.json();
                            // The /progress endpoint reports `in_progress` and `progress`,
                            // while setDownloadingState expects `downloading` and
                            // `download_progress`. Normalise the field names here.
                            const m = {
                                downloaded: raw.downloaded,
                                downloading: raw.in_progress,
                                download_progress: raw.progress,
                            };
                            setDownloadingState(row, m);
                            if (!m.downloading) {
                                clearInterval(pollTimers[modelId]);
                                delete pollTimers[modelId];
                                window.showToast && window.showToast(m.downloaded ? 'Model downloaded' : 'Download stopped');
                                // Refresh the Kitten downloaded-model combo so a freshly
                                // downloaded model appears without a page reload.
                                if (m.downloaded && typeof window.refreshKittenModelSelect === 'function') {
                                    window.refreshKittenModelSelect();
                                }
                            }
                        } catch (e) {
                            clearInterval(pollTimers[modelId]);
                            delete pollTimers[modelId];
                        }
                    }, 1500);
                };

                const renderRow = (model) => {
                    const frag = rowTpl.content.cloneNode(true);
                    const row = frag.querySelector('.mm-row');
                    row.querySelector('.mm-name').textContent = model.display_name || model.model_id;
                    const desc = row.querySelector('.mm-desc');
                    desc.textContent = model.description || '';
                    const sizeEl = row.querySelector('.mm-size');
                    sizeEl.textContent = model.size_mb ? '· ' + model.size_mb + ' MB' : '';
                    setDownloadingState(row, model);

                    const dlBtn = row.querySelector('.mm-download-btn');
                    dlBtn.addEventListener('click', async () => {
                        showError('');
                        dlBtn.disabled = true;
                        dlBtn.textContent = 'Starting…';
                        try {
                            const r = await fetch('/api/models/' + encodeURIComponent(model.model_id) + '/download', { method: 'POST' });
                            if (!r.ok) throw new Error('HTTP ' + r.status);
                            model.downloading = true;
                            setDownloadingState(row, model);
                            pollProgress(model.model_id, row);
                        } catch (e) {
                            showError('Failed to start download: ' + e.message);
                            setDownloadingState(row, model);
                        }
                    });

                    const delBtn = row.querySelector('.mm-delete-btn');
                    delBtn.addEventListener('click', async () => {
                        if (!window.confirm('Delete downloaded model "' + (model.display_name || model.model_id) + '"?')) return;
                        showError('');
                        delBtn.disabled = true;
                        try {
                            const r = await fetch('/api/models/' + encodeURIComponent(model.model_id), { method: 'DELETE' });
                            if (!r.ok) throw new Error('HTTP ' + r.status);
                            model.downloaded = false;
                            setDownloadingState(row, model);
                            window.showToast && window.showToast('Model removed');
                            // Refresh the Kitten downloaded-model combo so the removed
                            // model disappears without a page reload.
                            if (typeof window.refreshKittenModelSelect === 'function') {
                                window.refreshKittenModelSelect();
                            }
                        } catch (e) {
                            showError('Failed to delete model: ' + e.message);
                            delBtn.disabled = false;
                        }
                    });

                    return frag;
                };

                const openModal = async (sub) => {
                    const cfg = SUBSYSTEMS[sub];
                    if (!cfg) return;
                    showError('');
                    if (titleEl) titleEl.textContent = 'Manage Models — ' + cfg.label;
                    if (subtitleEl) subtitleEl.textContent = 'Download, update or remove locally-managed models for the ' + sub + ' subsystem.';
                    listEl.innerHTML = '<div class="meta">Loading…</div>';
                    modal.style.display = 'flex';
                    try {
                        const r = await fetch('/api/models');
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        const data = await r.json();
                        const models = (Array.isArray(data) ? data : (data.models || [])).filter((m) => belongsToSubsystem(m, sub));
                        listEl.innerHTML = '';
                        if (!models.length) {
                            const emptyMsg = sub === 'iris'
                                ? 'Iris has no locally-managed models — vision runs through external endpoints only (e.g. selenium-llm-engine). Configure it in the External Engines section.'
                                : 'No locally-managed models available for this subsystem.';
                            listEl.innerHTML = '<div class="meta">' + emptyMsg + '</div>';
                            return;
                        }
                        models.forEach((m) => {
                            listEl.appendChild(renderRow(m));
                            if (m.downloading) {
                                const row = listEl.lastElementChild;
                                if (row) pollProgress(m.model_id, row);
                            }
                        });
                    } catch (e) {
                        listEl.innerHTML = '';
                        showError('Failed to load models: ' + e.message);
                    }
                };

                [['vox-manage-models-btn', 'vox'], ['auris-manage-models-btn', 'auris'], ['iris-manage-models-btn', 'iris']].forEach(([btnId, sub]) => {
                    const btn = document.getElementById(btnId);
                    if (btn && !btn.dataset.bound) {
                        btn.addEventListener('click', () => openModal(sub));
                        btn.dataset.bound = '1';
                    }
                });
            }

            function initLogsTab() {
                if (window.__synth_logs_initialized) return;
                const logOutput = document.getElementById('log-output');
                if (!logOutput) return;
                const logAutoscroll = document.getElementById('logs-autoscroll');
                const logFilters = document.querySelectorAll('.log-filter');
                const logSearchInput = document.getElementById('log-search');
                const logsRefreshBtn = document.getElementById('logs-refresh');
                const logsSubtabButtons = document.querySelectorAll('.logs-subnav-btn[data-logs-subtab]');
                const logsSubtabPanels = document.querySelectorAll('.logs-subpanel[data-logs-subtab]');
                const logsFailuresOutput = document.getElementById('logs-failures-output');
                const logsFailuresPagination = document.getElementById('logs-failures-pagination');
                const logsFailuresRefreshBtn = document.getElementById('logs-failures-refresh');
                const logsFailuresSearch = document.getElementById('logs-failures-search');
                const logsFailuresCode = document.getElementById('logs-failures-code');
                const logsFailuresStage = document.getElementById('logs-failures-stage');
                const logsFailuresSort = document.getElementById('logs-failures-sort');
                const logsState = window.__synth_logs_state || {
                    currentSubtab: 'live',
                    failurePage: 1,
                    failurePerPage: 20,
                    failureSearch: '',
                    failureCode: '',
                    failureStage: '',
                    failureSort: 'desc',
                    failureTotalPages: 1,
                    loadingFailures: false,
                };
                window.__synth_logs_state = logsState;

                function debounce(fn, wait) {
                    let timer = null;
                    return (...args) => {
                        if (timer) window.clearTimeout(timer);
                        timer = window.setTimeout(() => fn(...args), wait);
                    };
                }

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

                function switchLogsSubtab(nextSubtab) {
                    logsState.currentSubtab = nextSubtab;
                    logsSubtabButtons.forEach((button) => {
                        const active = button.dataset.logsSubtab === nextSubtab;
                        button.classList.toggle('active', active);
                        button.setAttribute('aria-selected', active ? 'true' : 'false');
                    });
                    logsSubtabPanels.forEach((panel) => {
                        panel.classList.toggle('active', panel.dataset.logsSubtab === nextSubtab);
                    });
                    if (nextSubtab === 'failures') {
                        loadFailureLog();
                        return;
                    }
                    applyFilters();
                }

                function formatFailureDate(value) {
                    if (!value) return 'Unknown time';
                    const date = new Date(value);
                    if (Number.isNaN(date.getTime())) return safeEscapeHtml(value);
                    return safeEscapeHtml(date.toLocaleString());
                }

                function renderFailurePagination(payload) {
                    if (!logsFailuresPagination) return;
                    const page = Number(payload.page || logsState.failurePage || 1);
                    const totalPages = Number(payload.total_pages || 1);
                    const totalCount = Number(payload.total_count || 0);
                    logsState.failureTotalPages = totalPages;
                    logsFailuresPagination.innerHTML = `
                        <div class="logs-page-summary">${safeEscapeHtml(String(totalCount))} entries</div>
                        <div class="logs-page-buttons">
                            <button type="button" class="logs-pagination-btn" data-page-action="prev" ${page <= 1 ? 'disabled' : ''}>Previous</button>
                            <span>Page ${safeEscapeHtml(String(page))} / ${safeEscapeHtml(String(totalPages))}</span>
                            <button type="button" class="logs-pagination-btn" data-page-action="next" ${page >= totalPages ? 'disabled' : ''}>Next</button>
                        </div>
                    `;

                    logsFailuresPagination.querySelectorAll('[data-page-action]').forEach((button) => {
                        button.addEventListener('click', () => {
                            const action = button.dataset.pageAction;
                            if (action === 'prev' && logsState.failurePage > 1) {
                                logsState.failurePage -= 1;
                                loadFailureLog();
                            }
                            if (action === 'next' && logsState.failurePage < logsState.failureTotalPages) {
                                logsState.failurePage += 1;
                                loadFailureLog();
                            }
                        });
                    });
                }

                function renderFailureEntries(payload) {
                    if (!logsFailuresOutput) return;
                    const entries = Array.isArray(payload.entries) ? payload.entries : [];
                    if (!entries.length) {
                        logsFailuresOutput.innerHTML = '<div class="logs-empty-state">No failure entries match the current filters.</div>';
                        renderFailurePagination(payload);
                        return;
                    }

                    logsFailuresOutput.innerHTML = entries.map((entry) => {
                        const reason = safeEscapeHtml(entry.reason || 'Unknown failure');
                        const preview = entry.content_preview ? `<div class="logs-failure-preview">${safeEscapeHtml(entry.content_preview)}</div>` : '';
                        const meta = [
                            entry.engine ? `Engine: ${entry.engine}` : '',
                            entry.model ? `Model: ${entry.model}` : '',
                            entry.interface_path ? `Interface: ${entry.interface_path}` : '',
                            entry.chat_id ? `Chat: ${entry.chat_id}` : '',
                            entry.thread_id ? `Thread: ${entry.thread_id}` : '',
                            `At: ${formatFailureDate(entry.created_at)}`,
                        ].filter(Boolean).map((value) => `<span>${safeEscapeHtml(value)}</span>`).join('');

                        return `
                            <article class="logs-failure-entry" data-failure-id="${safeEscapeHtml(String(entry.id))}">
                                <div class="logs-failure-entry-header">
                                    <div>
                                        <div class="logs-failure-title">
                                            <span class="logs-failure-pill code">${safeEscapeHtml(entry.failure_code || 'llm_failure')}</span>
                                            <span class="logs-failure-pill stage">${safeEscapeHtml(entry.stage || 'unknown')}</span>
                                        </div>
                                        <div class="logs-failure-meta">${meta}</div>
                                    </div>
                                    <button class="logs-failure-delete" type="button" data-delete-failure="${safeEscapeHtml(String(entry.id))}">Delete</button>
                                </div>
                                <div class="logs-failure-reason">${reason}</div>
                                ${preview}
                            </article>
                        `;
                    }).join('');

                    logsFailuresOutput.querySelectorAll('[data-delete-failure]').forEach((button) => {
                        button.addEventListener('click', async () => {
                            const failureId = button.dataset.deleteFailure;
                            if (!failureId) return;
                            if (!window.confirm('Delete this failure entry? This cannot be undone.')) return;
                            try {
                                const response = await fetch(`/api/log-failures/${encodeURIComponent(failureId)}`, { method: 'DELETE' });
                                if (!response.ok) {
                                    const payloadText = await response.text();
                                    throw new Error(payloadText || `HTTP ${response.status}`);
                                }
                                if (window.showToast) window.showToast('Failure entry deleted', false);
                                loadFailureLog();
                            } catch (error) {
                                console.error('[logs] failed to delete failure entry', error);
                                if (window.showToast) window.showToast('Failed to delete failure entry', true);
                            }
                        });
                    });

                    renderFailurePagination(payload);
                }

                async function loadFailureLog() {
                    if (!logsFailuresOutput || logsState.loadingFailures) return;
                    logsState.loadingFailures = true;
                    logsFailuresOutput.innerHTML = '<div class="logs-empty-state">Loading failure log...</div>';

                    const params = new URLSearchParams({
                        page: String(logsState.failurePage),
                        per_page: String(logsState.failurePerPage),
                        search: logsState.failureSearch || '',
                        failure_code: logsState.failureCode || '',
                        stage: logsState.failureStage || '',
                        sort: logsState.failureSort || 'desc',
                    });

                    try {
                        const response = await fetch(`/api/log-failures?${params.toString()}`);
                        const payload = await response.json();
                        if (!response.ok || !payload.success) {
                            throw new Error((payload && payload.error) || `HTTP ${response.status}`);
                        }
                        renderFailureEntries(payload);
                    } catch (error) {
                        console.error('[logs] failed to load failure log', error);
                        logsFailuresOutput.innerHTML = '<div class="logs-empty-state">Failed to load failure log.</div>';
                        if (logsFailuresPagination) logsFailuresPagination.innerHTML = '';
                    } finally {
                        logsState.loadingFailures = false;
                    }
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
                logsSubtabButtons.forEach((button) => {
                    button.addEventListener('click', () => {
                        const nextSubtab = button.dataset.logsSubtab || 'live';
                        switchLogsSubtab(nextSubtab);
                    });
                });
                if (logsFailuresRefreshBtn) {
                    logsFailuresRefreshBtn.addEventListener('click', () => {
                        loadFailureLog();
                    });
                }
                if (logsFailuresSearch) {
                    logsFailuresSearch.addEventListener('input', debounce(() => {
                        logsState.failureSearch = logsFailuresSearch.value || '';
                        logsState.failurePage = 1;
                        loadFailureLog();
                    }, 350));
                }
                if (logsFailuresCode) {
                    logsFailuresCode.addEventListener('change', () => {
                        logsState.failureCode = logsFailuresCode.value || '';
                        logsState.failurePage = 1;
                        loadFailureLog();
                    });
                }
                if (logsFailuresStage) {
                    logsFailuresStage.addEventListener('change', () => {
                        logsState.failureStage = logsFailuresStage.value || '';
                        logsState.failurePage = 1;
                        loadFailureLog();
                    });
                }
                if (logsFailuresSort) {
                    logsFailuresSort.addEventListener('change', () => {
                        logsState.failureSort = logsFailuresSort.value || 'desc';
                        logsState.failurePage = 1;
                        loadFailureLog();
                    });
                }
                switchLogsSubtab(logsState.currentSubtab || 'live');
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
                            const total = (payload.cortex && payload.cortex.engines ? payload.cortex.engines.length : 0)
                                + (payload.interfaces ? payload.interfaces.length : 0)
                                + (payload.plugins ? payload.plugins.length : 0);
                            componentsEl.textContent = String(total);
                        }
                    }
                } catch (e) { /* ignore */ }

                window.__synth_about_initialized = true;

                // Show the inline Ko‑fi button inside About (no global overlay). The
                // `.kofi-button` anchor is styled in the About template and remains
                // visible only when the About tab is active.
                try {
                    const fallbackEl = document.querySelector('.kofi-button');
                    if (fallbackEl) {
                        fallbackEl.style.display = 'inline-flex';
                        fallbackEl.setAttribute('aria-label', 'Support the project on Ko‑fi');
                    }
                } catch (e) { /* ignore */ }
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
            window.SynthWebUI.initPluginsTab = initPluginsTab;
            window.SynthWebUI.initEnginesTab = initEnginesTab;
            window.SynthWebUI.loadEnginesSummary = loadComponentsSummary;
            window.SynthWebUI.initModelManager = initModelManager;
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
