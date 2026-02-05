// main.js — lightweight loader and helpers for the Synth WebUI
(function(){
    'use strict';

    // Minimal config accessor
    window.SynthConfig = window.__SYNTH_CONFIG || {};

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
        const statusIndicator = document.querySelector('.connection-status .indicator');
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
        // Track original nav parent so we can temporarily move nav out of header
        // when opening mobile menu to avoid clipping caused by header's backdrop-filter
        let navOriginalParent = null;
        let navOriginalNextSibling = null;
        let navWasMoved = false;
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
        // Chat state helpers (restore/save/typing indicator)
        // -----------------------------------------------------------------------------
        function addTypingIndicator() {
            try {
                const messagesEl = messages || document.getElementById('messages');
                if (!messagesEl) return;
                if (messagesEl.querySelector('.typing-indicator')) return;
                const container = document.createElement('div');
                container.className = 'message-container synth';
                const bubble = document.createElement('div');
                bubble.className = 'bubble synth typing-indicator';
                bubble.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
                container.appendChild(bubble);
                messagesEl.appendChild(container);
                messagesEl.scrollTop = messagesEl.scrollHeight;
                try { localStorage.setItem(TYPING_INDICATOR_KEY, '1'); } catch (e) { /* ignore */ }
            } catch (e) { /* ignore */ }
        }

        function removeTypingIndicator() {
            try {
                const messagesEl = messages || document.getElementById('messages');
                if (!messagesEl) return;
                const indicator = messagesEl.querySelector('.typing-indicator');
                if (indicator && indicator.parentElement) indicator.parentElement.remove();
                try { localStorage.removeItem(TYPING_INDICATOR_KEY); } catch (e) { /* ignore */ }
            } catch (e) { /* ignore */ }
        }

        function saveChatState() {
            try {
                const chatEl = chatPanel || document.getElementById('chat');
                if (!chatEl) return;
                const device = (typeof window !== 'undefined' && window.innerWidth && window.innerWidth <= 768) ? 'mobile' : 'desktop';
                const stateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}-${device}` : `${CHAT_WINDOW_STATE_KEY}-${device}`;
                let state = 'normal';
                if (chatEl.classList.contains('minimized')) state = 'minimized';
                else if (chatEl.classList.contains('maximized')) state = 'maximized';
                else if (chatEl.classList.contains('expanded')) state = 'expanded';
                try { localStorage.setItem(stateKey, state); } catch (e) { /* ignore */ }

                const rectKey = sessionId ? `${CHAT_RECT_KEY}-${sessionId}-${device}` : `${CHAT_RECT_KEY}-${device}`;
                try {
                    const rect = chatEl.getBoundingClientRect();
                    const payload = {
                        left: Math.round(rect.left),
                        top: Math.round(rect.top),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    };
                    localStorage.setItem(rectKey, JSON.stringify(payload));
                } catch (e) { /* ignore */ }
            } catch (e) { /* ignore */ }
        }

        function restoreChatState() {
            try {
                const chatEl = chatPanel || document.getElementById('chat');
                if (!chatEl) return;
                const device = (typeof window !== 'undefined' && window.innerWidth && window.innerWidth <= 768) ? 'mobile' : 'desktop';

                // Restore rect
                try {
                    const rectKey = sessionId ? `${CHAT_RECT_KEY}-${sessionId}-${device}` : `${CHAT_RECT_KEY}-${device}`;
                    const rectRaw = localStorage.getItem(rectKey) || localStorage.getItem(sessionId ? `${CHAT_RECT_KEY}-${sessionId}` : CHAT_RECT_KEY) || localStorage.getItem(CHAT_RECT_KEY);
                    if (rectRaw) {
                        const rect = JSON.parse(rectRaw);
                        if (typeof rect.left === 'number') chatEl.style.left = rect.left + 'px';
                        if (typeof rect.top === 'number') chatEl.style.top = rect.top + 'px';
                        if (typeof rect.width === 'number' && rect.width >= 260) chatEl.style.width = rect.width + 'px';
                        if (typeof rect.height === 'number' && rect.height >= 180) chatEl.style.height = rect.height + 'px';
                        chatEl.style.right = 'auto';
                        chatEl.style.bottom = 'auto';
                    }
                } catch (e) { /* ignore */ }

                // Restore window state
                try {
                    const stateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}-${device}` : `${CHAT_WINDOW_STATE_KEY}-${device}`;
                    const localState = localStorage.getItem(stateKey) || localStorage.getItem(sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}` : CHAT_WINDOW_STATE_KEY) || localStorage.getItem(CHAT_WINDOW_STATE_KEY);
                    if (localState === 'minimized') {
                        chatEl.classList.add('hidden');
                        chatEl.classList.remove('maximized', 'expanded', 'minimized');
                        if (chatToggleBtn) chatToggleBtn.style.display = 'flex';
                    } else if (localState === 'maximized') {
                        chatEl.classList.remove('hidden', 'expanded', 'minimized');
                        chatEl.classList.add('maximized');
                        if (chatToggleBtn) chatToggleBtn.style.display = 'none';
                    } else if (localState === 'expanded') {
                        chatEl.classList.remove('hidden', 'maximized', 'minimized');
                        chatEl.classList.add('expanded');
                        if (chatToggleBtn) chatToggleBtn.style.display = 'none';
                    } else {
                        chatEl.classList.remove('maximized', 'expanded', 'hidden', 'minimized');
                        if (chatToggleBtn) chatToggleBtn.style.display = 'none';
                    }
                } catch (e) { /* ignore */ }

                // Restore typing indicator if server indicates processing
                (async () => {
                    try {
                        if (!sessionId) return;
                        const res = await fetch('/api/chat/session_meta?session_id=' + encodeURIComponent(sessionId));
                        if (!res.ok) return;
                        const out = await res.json();
                        const meta = out && out.meta ? out.meta : {};
                        if (meta && meta.processing) addTypingIndicator();
                    } catch (e) { /* ignore */ }
                })();
            } catch (e) { /* ignore */ }
        }

        window.addTypingIndicator = window.addTypingIndicator || addTypingIndicator;
        window.removeTypingIndicator = window.removeTypingIndicator || removeTypingIndicator;
        window.saveChatState = window.saveChatState || saveChatState;
        window.restoreChatState = window.restoreChatState || restoreChatState;
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
                const hamburger = document.querySelector('.hamburger');
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

                navButtons.forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const tab = btn.getAttribute('data-tab');
                        if (!tab) return;
                        try {
                            setActiveTab(tab);
                            try { window.activeTab = tab; if (localStorage && localStorage.setItem) localStorage.setItem('synth-webui-active-tab', tab); } catch (e) { /* ignore */ }
                            if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                                await window.SynthWebUI.loadSection(tab);
                            }
                            if (tab === 'history' && window.SynthWebUI && typeof window.SynthWebUI.initHistoryTab === 'function') {
                                try { window.SynthWebUI.initHistoryTab(); } catch (e) { /* ignore */ }
                            }
                        } catch (e) {
                            console.warn('[synth_webui] tab switch failed', e);
                        }
                        if (nav && nav.classList.contains('open')) {
                            nav.classList.remove('open');
                        }
                    });
                });

                if (hamburger && nav) {
                    hamburger.addEventListener('click', () => {
                        nav.classList.toggle('open');
                    });
                }

                // Restore last active tab and load its section once.
                try {
                    const saved = (localStorage && localStorage.getItem && localStorage.getItem('synth-webui-active-tab')) || 'home';
                    setActiveTab(saved);
                    if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                        window.SynthWebUI.loadSection(saved);
                    }
                } catch (e) {
                    setActiveTab('home');
                    if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                        window.SynthWebUI.loadSection('home');
                    }
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
            }

            function getSynthDisplayName() {
                try {
                    if (window.SynthConfig && window.SynthConfig.BRAND_NAME) return window.SynthConfig.BRAND_NAME;
                    const headerName = document.querySelector('.brand-text h1');
                    if (headerName && headerName.textContent) return headerName.textContent.trim();
                } catch (e) { /* ignore */ }
                return 'SyntH';
            }

            function appendMessage(container, sender, text) {
                if (!container) return;
                const wrapper = document.createElement('div');
                wrapper.className = `message-container ${sender}`;

                const bubble = document.createElement('div');
                bubble.className = `bubble ${sender}`;
                const senderLabel = sender === 'synth' ? getSynthDisplayName() : 'You';
                bubble.innerHTML = `<div class="bubble-sender">${senderLabel}</div>${safeEscapeHtml(text)}`;
                wrapper.appendChild(bubble);
                container.appendChild(wrapper);
                container.scrollTop = container.scrollHeight;

                if (sender === 'synth') {
                    try { maybeNotify(text); } catch (e) { /* ignore */ }
                    try { removeTypingIndicator(); } catch (e) { /* ignore */ }
                }
            }

            function setupChatControls() {
                const chatPanel = document.getElementById('chat');
                const chatToggleBtn = document.getElementById('chat-toggle');
                const chatMinBtn = document.getElementById('chat-minimize');
                const chatMaxBtn = document.getElementById('chat-maximize');

                if (!chatPanel) return;

                function showChat() {
                    if (!chatPanel) return;
                    chatPanel.classList.remove('minimized');
                    chatPanel.classList.remove('hidden');
                    if (chatToggleBtn) chatToggleBtn.style.display = 'none';
                }

                function hideChat() {
                    if (!chatPanel) return;
                    chatPanel.classList.add('minimized');
                    chatPanel.classList.add('hidden');
                    if (chatToggleBtn) {
                        chatToggleBtn.style.display = 'flex';
                        try {
                            if (window.__synth_web_debug_enabled) {
                                const dock = document.getElementById('synth-minimized-stack');
                                if (dock && chatToggleBtn.parentElement !== dock) {
                                    dock.appendChild(chatToggleBtn);
                                    chatToggleBtn.style.position = 'static';
                                    chatToggleBtn.style.right = '';
                                    chatToggleBtn.style.bottom = '';
                                }
                            }
                        } catch (e) { /* ignore */ }
                    }
                }

                function toggleMaximize() {
                    if (!chatPanel) return;
                    const isMax = chatPanel.classList.contains('maximized');
                    chatPanel.classList.toggle('maximized', !isMax);
                    if (!isMax) {
                        chatPanel.classList.remove('minimized');
                        chatPanel.classList.remove('hidden');
                        if (chatToggleBtn) chatToggleBtn.style.display = 'none';
                    }
                }

                if (chatToggleBtn) chatToggleBtn.addEventListener('click', showChat);
                if (chatMinBtn) chatMinBtn.addEventListener('click', hideChat);
                if (chatMaxBtn) chatMaxBtn.addEventListener('click', toggleMaximize);
            }

            function setupChatMessaging() {
                if (window.__synth_chat_initialized) return;
                const statusLabel = document.getElementById('status-label');
                const statusIndicator = document.querySelector('.connection-status .indicator');
                const messages = document.getElementById('messages');
                const input = document.getElementById('input');
                const form = document.getElementById('composer');
                const sendBtn = document.getElementById('send');

                if (!messages || !input || !form || !sendBtn) return;

                let ws = null;

                function updateSendState() {
                    if (!sendBtn || !input) return;
                    const hasText = input.value.trim().length > 0;
                    const wsReady = ws && ws.readyState === WebSocket.OPEN;
                    sendBtn.disabled = !(hasText && wsReady);
                }

                function connectWs() {
                    try {
                        const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
                        ws = new WebSocket(`${protocol}://${window.location.host}/ws`);
                        window.chatWs = ws;

                        ws.onopen = () => {
                            if (statusLabel) statusLabel.textContent = 'Connected';
                            if (statusIndicator) statusIndicator.classList.add('online');
                            updateSendState();
                        };
                        ws.onclose = () => {
                            if (statusLabel) statusLabel.textContent = 'Disconnected';
                            if (statusIndicator) statusIndicator.classList.remove('online');
                            updateSendState();
                        };
                        ws.onerror = () => {
                            if (statusLabel) statusLabel.textContent = 'Disconnected';
                            if (statusIndicator) statusIndicator.classList.remove('online');
                            updateSendState();
                        };
                        window.pendingAnimationCommands = window.pendingAnimationCommands || pendingAnimationCommands;
                        ws.onmessage = (event) => {
                            try {
                                const data = JSON.parse(event.data);
                                if (data && data.type === 'message') {
                                    appendMessage(messages, data.sender === 'synth' ? 'synth' : 'user', data.text || '');
                                } else if (data && data.type === 'action_state') {
                                    const phase = String(data.phase || '').toUpperCase();
                                    if (phase === 'THINKING' || phase === 'WRITING' || phase === 'CORRECTING') {
                                        try { addTypingIndicator(); } catch (e) { /* ignore */ }
                                    } else if (phase === 'IDLE') {
                                        try { removeTypingIndicator(); } catch (e) { /* ignore */ }
                                    }
                                } else if (data && data.type === 'animation') {
                                    if (window.VRMAnimations && typeof window.VRMAnimations.play === 'function') {
                                        window.VRMAnimations.play(data.state, {
                                            animation: data.animation || null,
                                            playOnce: data.loop === false,
                                            playSection: data.play_section || null,
                                            descriptor: data.descriptor || null
                                        });
                                    } else {
                                        window.pendingAnimationCommands = window.pendingAnimationCommands || [];
                                        window.pendingAnimationCommands.push(data);
                                    }
                                } else if (data && data.type === 'animation_state') {
                                    try {
                                        window.__synth_last_rich_animation_state = data.animation_state || data;
                                    } catch (e) { /* ignore */ }
                                }
                            } catch (e) {
                                // ignore non-JSON
                            }
                        };
                    } catch (e) {
                        console.error('[synth_webui] WebSocket init failed', e);
                    }
                }

                if (input) {
                    input.addEventListener('input', updateSendState);
                }

                if (form) {
                    form.addEventListener('submit', (e) => {
                        e.preventDefault();
                        if (!input || !ws || ws.readyState !== WebSocket.OPEN) return;
                        const text = input.value.trim();
                        if (!text) return;
                        try {
                            ws.send(JSON.stringify({ text }));
                            appendMessage(messages, 'user', text);
                            input.value = '';
                            updateSendState();
                        } catch (e) {
                            console.warn('[synth_webui] send failed', e);
                        }
                    });
                }

                connectWs();
                window.__synth_chat_initialized = true;
            }

            function initHomeTab() {
                if (window.__synth_home_initialized) return;
                const messagesEl = document.getElementById('messages');
                const inputEl = document.getElementById('input');
                if (!messagesEl || !inputEl) return;
                setupChatControls();
                setupChatMessaging();
                window.__synth_home_initialized = true;
                try {
                    if (typeof window.restoreChatState === 'function') {
                        window.restoreChatState();
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
                        return;
                    }
                    try {
                        const permission = await Notification.requestPermission();
                        if (permission === 'granted') {
                            try { localStorage.setItem(NOTIFY_KEY, '1'); } catch (e) { /* ignore */ }
                            setStatus(true, 'Enabled');
                        } else {
                            setStatus(false, permission === 'denied' ? 'Blocked by browser' : 'Disabled');
                        }
                    } catch (e) {
                        setStatus(false, 'Disabled');
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

            window.SynthWebUI = window.SynthWebUI || {};
            window.SynthWebUI.initHomeTab = initHomeTab;
            window.SynthWebUI.initSettingsTab = initSettingsTab;
            window.SynthWebUI.initComponentsTab = initComponentsTab;
            window.SynthWebUI.initLogsTab = initLogsTab;
            window.SynthWebUI.initAboutTab = initAboutTab;

            document.addEventListener('DOMContentLoaded', () => {
                setupNavigation();
            });
        })();
