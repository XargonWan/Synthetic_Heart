// chat-window.mjs — encapsulated Chat window management using SynthWindowManager/WinBox

export async function createChatWindow() {
    try {
        // Ensure WinBox is available
        if (!window.SynthWindowManager || typeof window.SynthWindowManager.create !== 'function') {
            if (typeof window.SynthWindowManager === 'undefined' && typeof window.WinBox === 'undefined' && typeof window.SynthWindowManager !== 'undefined') {
                try { await window.SynthWindowManager.ensureWinBoxAssets(); } catch (e) { /* ignore */ }
            }
        }

        // Locate mount element. If missing, attempt to (re)load the home section to ensure the template exists.
        let mount = document.getElementById('chat');
        if (!mount) {
            try { if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') await window.SynthWebUI.loadSection('home'); } catch (e) { /* ignore */ }
            mount = document.getElementById('chat');
        }
        if (!mount) {
            // Still missing — avoid spamming the console, return gracefully for now.
            console.debug('[chat-window] mount element #chat not found after loading home');
            return null;
        }

        // Clear any inline positioning styles or classes left from previous mounts
        try {
            mount.style.left = '';
            mount.style.top = '';
            mount.style.right = '';
            mount.style.bottom = '';
            // Also clear the shorthand inset property if present
            mount.style.inset = '';
            // Remove any window state classes to ensure a clean mount
            try { mount.classList.remove('hidden', 'maximized', 'expanded', 'minimized'); } catch (e) { /* ignore */ }
        } catch (e) { /* ignore */ }

        // If template not present yet, try to lazy-load the Home section so the template is available
        try {
            let tpl = document.getElementById('chat-window-template');
            if (!tpl && window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                try {
                    // loadSection will inject the template into the home panel
                    await window.SynthWebUI.loadSection('home');
                } catch (e) { /* ignore */ }
                tpl = document.getElementById('chat-window-template');
            }
            if (tpl && mount.children.length === 0) {
                mount.appendChild(tpl.content.cloneNode(true));
            }
        } catch (e) { /* ignore */ }

        // If already created, return existing instance
        try {
            if (window.SynthWindowManager && typeof window.SynthWindowManager.has === 'function' && window.SynthWindowManager.has('chat')) {
                return window.SynthWindowManager.get('chat');
            }
        } catch (e) { /* ignore */ }

        // Dock button optionally present in markup
        const chatToggleBtn = document.getElementById('chat-toggle');

        // Create the WinBox-managed chat window using the central manager
        if (window.SynthWindowManager && typeof window.SynthWindowManager.create === 'function') {
            const winbox = window.SynthWindowManager.create({
                id: 'chat',
                title: 'Chat',
                mount: mount,
                width: 420,
                height: '70%',
                x: 24,
                y: 'bottom',
                iconText: '💬',
                dockLabel: 'Restore Chat',
                dockButton: chatToggleBtn || null,
                dockClass: 'chat-toggle-btn',
                className: 'synth-winbox no-full no-close chat-window'
            });

            // Slight title size tweak for visibility
            try {
                const winEl = winbox.window || winbox.dom || winbox.g;
                const titleEl = winEl ? winEl.querySelector('.wb-title') : null;
                if (titleEl) titleEl.classList.add('synth-winbox-title-large');
            } catch (e) { /* ignore */ }

            return winbox;
        }

        console.warn('[chat-window] SynthWindowManager.create not available');
        return null;
    } catch (e) {
        console.error('[chat-window] createChatWindow failed', e);
        return null;
    }
}

// --- Chat UI helpers migrated here ---
const CHAT_WINDOW_STATE_KEY = 'synth-webui-window-state';
const CHAT_RECT_KEY = 'synth-webui-chat-rect';
const TYPING_INDICATOR_KEY = 'synth-webui-typing-indicator';

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

function addTypingIndicator() {
    try {
        const messagesEl = document.getElementById('messages');
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
        const messagesEl = document.getElementById('messages');
        if (!messagesEl) return;
        const indicator = messagesEl.querySelector('.typing-indicator');
        if (indicator && indicator.parentElement) indicator.parentElement.remove();
        try { localStorage.removeItem(TYPING_INDICATOR_KEY); } catch (e) { /* ignore */ }
    } catch (e) { /* ignore */ }
}

function appendMessage(container, sender, text) {
    if (!container) return;
    const wrapper = document.createElement('div');
    wrapper.className = `message-container ${sender}`;

    const bubble = document.createElement('div');
    bubble.className = `bubble ${sender}`;
    const senderLabel = sender === 'synth' ? (window.SynthConfig && (window.SynthConfig.SYNTH_NAME || window.SynthConfig.BRAND_NAME) ? (window.SynthConfig.SYNTH_NAME || window.SynthConfig.BRAND_NAME) : 'SyntH') : 'You';
    bubble.innerHTML = `<div class="bubble-sender">${senderLabel}</div>${safeEscapeHtml(text)}`;
    wrapper.appendChild(bubble);
    container.appendChild(wrapper);
    container.scrollTop = container.scrollHeight;

    if (sender === 'synth') {
        try { if (window.SynthChat && typeof window.SynthChat.maybeNotify === 'function') window.SynthChat.maybeNotify(text); } catch (e) { /* ignore */ }
        try { removeTypingIndicator(); } catch (e) { /* ignore */ }
    }
}

export function initChatUI() {
    try {
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
                window.pendingAnimationCommands = window.pendingAnimationCommands || window.pendingAnimationCommands;
                ws.onmessage = (event) => {
                    try {
                        const data = JSON.parse(event.data);
                        if (data && data.type === 'message') {
                            appendMessage(messages, data.sender === 'synth' ? 'synth' : 'user', data.text || '');
                        } else if (data && data.type === 'action_state') {
                            const phase = String(data.phase || '').toUpperCase();
                            if (phase === 'THINKING' || phase === 'WRITING' || phase === 'TALKING') {
                                addTypingIndicator();
                            } else {
                                removeTypingIndicator();
                            }
                        } else if (data && data.type === 'animation') {
                            try {
                                if (window.VRMAnimations && typeof window.VRMAnimations.play === 'function') {
                                    window.VRMAnimations.play(data.state, {
                                        animation: data.animation,
                                        playOnce: data.loop === false,
                                        playSection: data.play_section,
                                        descriptor: data.descriptor
                                    });
                                }
                            } catch (e) { /* ignore */ }
                        }
                    } catch (e) {
                        // ignore non-JSON
                    }
                };
            } catch (e) {
                console.error('[chat-window] WebSocket init failed', e);
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
                    console.warn('[chat-window] send failed', e);
                }
            });
        }

        connectWs();
        window.__synth_chat_initialized = true;

    } catch (e) {
        console.warn('[chat-window] initChatUI failed', e);
    }
}

// Save/restore helpers
function saveChatState() {
    try {
        if (window.SynthWindowManager && window.SynthWindowManager.has && window.SynthWindowManager.has('chat')) {
            try { if (typeof window.SynthWindowManager.saveState === 'function') window.SynthWindowManager.saveState('chat'); } catch (e) { /* ignore */ }
            return;
        }
        const chatEl = document.getElementById('chat');
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
        if (window.SynthWindowManager && window.SynthWindowManager.has && window.SynthWindowManager.has('chat')) {
            try { if (typeof window.SynthWindowManager.restoreState === 'function') window.SynthWindowManager.restoreState('chat'); } catch (e) { /* ignore */ }
            return;
        }
        const chatEl = document.getElementById('chat');
        if (!chatEl) return;
        const device = (typeof window !== 'undefined' && window.innerWidth && window.innerWidth <= 768) ? 'mobile' : 'desktop';

        // Restore rect
        try {
            const rectKey = sessionId ? `${CHAT_RECT_KEY}-${sessionId}-${device}` : `${CHAT_RECT_KEY}-${device}`;
            const rectRaw = localStorage.getItem(rectKey) || localStorage.getItem(sessionId ? `${CHAT_RECT_KEY}-${sessionId}` : CHAT_RECT_KEY) || localStorage.getItem(CHAT_RECT_KEY);
            if (rectRaw) {
                const rect = JSON.parse(rectRaw);
                // If the stored rect is positioned near the top of the viewport (likely an accidental top-left placement),
                // prefer a safe bottom-left default for better UX.
                const viewportH = (typeof window !== 'undefined' && window.innerHeight) ? window.innerHeight : 0;
                const topThreshold = Math.max(80, Math.floor(viewportH * 0.15));
                const placedAtTop = (typeof rect.top === 'number') ? (rect.top <= topThreshold) : false;

                if (placedAtTop) {
                    // Use bottom-left default and remove the saved rect so we don't reapply a broken state repeatedly
                    try {
                        chatEl.style.left = '18px';
                        chatEl.style.bottom = '18px';
                        chatEl.style.top = '';
                        chatEl.style.right = 'auto';
                        if (typeof rect.width === 'number' && rect.width >= 260) chatEl.style.width = rect.width + 'px';
                        if (typeof rect.height === 'number' && rect.height >= 180) chatEl.style.height = rect.height + 'px';
                        try { localStorage.removeItem(rectKey); } catch (e) { /* ignore */ }
                    } catch (e) { /* ignore */ }
                } else {
                    if (typeof rect.left === 'number') chatEl.style.left = rect.left + 'px';
                    if (typeof rect.top === 'number') chatEl.style.top = rect.top + 'px';
                    if (typeof rect.width === 'number' && rect.width >= 260) chatEl.style.width = rect.width + 'px';
                    if (typeof rect.height === 'number' && rect.height >= 180) chatEl.style.height = rect.height + 'px';
                    chatEl.style.right = 'auto';
                    chatEl.style.bottom = 'auto';
                }
            } else {
                // No saved rect: ensure default bottom-left placement
                try { chatEl.style.left = '18px'; chatEl.style.bottom = '18px'; chatEl.style.top = ''; chatEl.style.right = 'auto'; } catch (e) { /* ignore */ }
            }
        } catch (e) { /* ignore */ }

        // Restore window state
        try {
            const stateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}-${device}` : `${CHAT_WINDOW_STATE_KEY}-${device}`;
            const localState = localStorage.getItem(stateKey) || localStorage.getItem(sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}` : CHAT_WINDOW_STATE_KEY) || localStorage.getItem(CHAT_WINDOW_STATE_KEY);
            const chatToggleBtn = document.getElementById('chat-toggle');
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

            // Restore typing indicator if server indicates processing
            (async () => {
                try {
                    if (!sessionId) {
                        try { restoreChatState._retry = (restoreChatState._retry || 0) + 1; } catch (e) { /* ignore */ }
                        if ((restoreChatState._retry || 0) <= 10) {
                            setTimeout(() => { try { restoreChatState(); } catch (e) {} }, 200);
                        }
                        return;
                    }
                    const res = await fetch('/api/chat/session_meta?session_id=' + encodeURIComponent(sessionId));
                    if (!res.ok) return;
                    const out = await res.json();
                    const meta = out && out.meta ? out.meta : {};
                    if (meta && meta.processing) addTypingIndicator();
                } catch (e) { /* ignore */ }
            })();
        } catch (e) { /* ignore */ }
    } catch (e) { /* ignore */ }
}

// Expose minimal API for backwards compatibility
try {
    window.SynthChat = window.SynthChat || {};
    window.SynthChat.initChatUI = initChatUI;
    window.SynthChat.appendMessage = appendMessage;
    window.SynthChat.addTypingIndicator = addTypingIndicator;
    window.SynthChat.removeTypingIndicator = removeTypingIndicator;
    window.SynthChat.saveChatState = saveChatState;
    window.SynthChat.restoreChatState = restoreChatState;
} catch (e) { /* ignore */ }

export default { createChatWindow, initChatUI, saveChatState, restoreChatState }