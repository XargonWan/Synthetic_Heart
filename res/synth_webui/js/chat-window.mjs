// chat-window.mjs — encapsulated Chat window management using SynthWindowManager/WinBox

// Experimental flag carried from server via __SYNTH_CONFIG -> main.js
const MULTI_SESSION = (typeof window !== 'undefined' && window.MULTI_SESSION) || false;

// Inject chat-specific styles (keeps chat CSS encapsulated inside this module)
function injectChatStyles() {
    try {
        if (document.getElementById('synth-chat-styles')) return;
        const style = document.createElement('style');
        style.id = 'synth-chat-styles';
        style.textContent = `
        /* chat styles (injected by chat-window.mjs) */
        .synth-chat { display:flex; flex-direction:column; height:100%; min-height:0; width:100%; position:relative; }
        .synth-chat-header { width:100%; display:flex; align-items:center; justify-content:flex-start; padding:0.35rem 0.6rem 0 0.6rem; }
        .synth-chat-body { flex:1 1 auto; min-height:0; overflow:hidden; transition: overflow 0s; -ms-overflow-style: none; scrollbar-width: none; }
        .synth-chat-body::-webkit-scrollbar { width: 0; height: 0; }
        .synth-chat-body.at-max { overflow-y:auto; }
        .synth-chat-footer { flex:0 0 auto; position:sticky; bottom:0; z-index:2; background: inherit; }
        .synth-chat-composer { width:100%; display:flex; gap:0.6rem; align-items:flex-end; padding:0.6rem 1rem; box-sizing:border-box; }
        #input { flex:1 1 auto; min-height:2.4rem; max-height:7rem; resize:none; }
        #send { flex:0 0 auto; border-radius:50%; height:2.6rem; width:2.6rem; cursor:pointer; transition: background 0.2s, transform 0.15s; }
        #send.send-mode { /* text ready */ }
        #send.mic-mode  { background: var(--accent, #6bfefe); color: #111; }
        .attach-btn { flex:0 0 auto; border-radius:50%; height:2.6rem; width:2.6rem; cursor:pointer; background: rgba(255,255,255,0.06); color: var(--text); border: 1px solid rgba(255,255,255,0.1); }
        .attach-btn:hover { background: rgba(255,255,255,0.12); }
        .attach-btn:disabled { cursor: not-allowed; opacity: 0.45; background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.6); border-color: rgba(255,255,255,0.06); }
        .attachment-preview { margin: 0.5rem 1rem 0.4rem; padding: 0.55rem 0.75rem; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
        .attachment-chip { display: inline-flex; align-items: center; gap: 0.45rem; padding: 0.35rem 0.65rem; border-radius: 999px; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); font-size: 0.9rem; }
        .attachment-chip button { background: transparent; border: none; color: var(--text); cursor: pointer; padding: 0; margin: 0; font-size: 1rem; line-height: 1; }
        .attachment-image-preview { max-width: 120px; max-height: 80px; border-radius: 10px; object-fit: cover; cursor: pointer; border: 1px solid rgba(255,255,255,0.12); }
        .attachment-audio { width: 100%; max-width: 240px; margin-top: 0.4rem; }
        .attachment-link { color: var(--text); text-decoration: underline; }
        #send.mic-mode.ambient-speaking { animation: synth-mic-pulse-ambient 1.2s ease-in-out infinite; }
        #send.recording { background: #d94; color: #fff; animation: synth-mic-pulse 0.9s ease-in-out infinite; }
        /* Brighter pulse while voice is actively detected (toggle mode) */
        #send.recording.speaking {
            background: #e63;
            animation: synth-mic-pulse-active 0.45s ease-in-out infinite;
            box-shadow: 0 0 0 4px rgba(230,60,30,0.35);
        }
        #send.processing {
            background: rgba(255,255,255,0.08);
            color: transparent;
            border: 2px solid var(--accent, #6bfefe);
            cursor: pointer;
            position: relative;
            overflow: hidden;
        }
        #send.processing::before {
            content: '';
            position: absolute;
            inset: 4px;
            border: 2px solid transparent;
            border-top-color: var(--accent, #6bfefe);
            border-radius: 50%;
            animation: synth-stt-spin 0.75s linear infinite;
        }
        #send.processing::after {
            content: '✕';
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.8rem;
            color: var(--accent, #6bfefe);
            opacity: 0;
            transition: opacity 0.15s;
        }
        #send.processing:hover::after  { opacity: 1; }
        #send.processing:hover::before { opacity: 0.3; }
        @keyframes synth-stt-spin {
            from { transform: rotate(0deg); }
            to   { transform: rotate(360deg); }
        }
        @keyframes synth-mic-pulse {
            0%,100% { transform:scale(1);   box-shadow:0 0 0 0   rgba(210,100,0,0.5); }
            50%      { transform:scale(1.12); box-shadow:0 0 0 7px rgba(210,100,0,0);   }
        }
        @keyframes synth-mic-pulse-ambient {
            0%,100% { transform:scale(1);    box-shadow:0 0 0 0   rgba(107,254,254,0.45); }
            50%      { transform:scale(1.07); box-shadow:0 0 0 6px rgba(107,254,254,0);    }
        }
        @keyframes synth-mic-pulse-active {
            0%,100% { transform:scale(1);    box-shadow:0 0 0 0   rgba(230,60,30,0.7); }
            50%      { transform:scale(1.18); box-shadow:0 0 0 9px rgba(230,60,30,0);   }
        }
        /* User audio processing indicator */
        .user-audio-indicator { display:flex; flex-direction:column; align-items:flex-end; margin-bottom:0.4rem; }
        .user-audio-indicator .bubble {
            background: rgba(255,255,255,0.06);
            border: 1.5px solid var(--accent, #6bfefe);
            color: var(--text-soft);
            display:flex; align-items:center; gap:0.5rem; padding:0.7rem 1rem 0.7rem;
        }
        .user-audio-indicator .bubble.audio-error {
            border-color: #d94;
            color: #d94;
        }
        .user-audio-indicator .mic-label { font-size:0.8rem; opacity:0.7; }
        .user-audio-indicator .dot {
            width:7px; height:7px; border-radius:50%;
            background: var(--accent, #6bfefe);
            display:inline-block;
            animation: typingDot 1.4s ease-in-out infinite;
        }
        .user-audio-indicator .dot:nth-child(2) { animation-delay:0.2s; }
        .user-audio-indicator .dot:nth-child(3) { animation-delay:0.4s; }
        .synth-chat-archive { margin-left:auto; margin-top:0.6rem; margin-right:0.6rem; display:flex; gap:0.5rem; align-items:center; }
        .synth-chat-archive .pill { padding:0.4rem 0.6rem; }
        /* Message day separator */
        .message-day { display:flex; justify-content:center; align-items:center; margin: 0.6rem 0; }
        .message-day .day-label { background: rgba(255,255,255,0.04); color: var(--text); padding: 0.35rem 0.8rem; border-radius: 999px; font-weight:600; font-size:0.9rem; }
        /* Message timestamp in bubble */
        .bubble { position: relative; padding: 1rem 1.2rem 1.8rem; }
        .bubble-time { position: absolute; right: 8px; bottom: 6px; font-size: 0.75rem; color: var(--text-soft); opacity: 0.9; }
        /* Synth bubbles with attached TTS audio – click to replay */
        .bubble.synth.clickable-audio { cursor: pointer; }
        .bubble.synth.clickable-audio::after {
            content: '🔊';
            position: absolute;
            bottom: 6px;
            left: 10px;
            font-size: 0.75rem;
            opacity: 0.5;
            pointer-events: none;
        }
        .bubble.synth.clickable-audio:hover::after { opacity: 0.9; }
        `, document.head.appendChild(style);
    } catch (e) { /* ignore */ }
}

function createChatTemplate() {
    try {
        injectChatStyles();
        if (document.getElementById('chat-window-template')) return;
        const tpl = document.createElement('template');
        tpl.id = 'chat-window-template';
        tpl.innerHTML = `
            <div class="synth-chat">
                <div class="synth-chat-header">
                    <div class="synth-chat-archive">
                        <button id="chat-archive" title="Archive current chat" type="button" class="pill">📦 Archive Chat</button>
                        <button id="chat-restore" title="Open archives" type="button" class="pill">🗂️ Open Archives</button>
                    </div>
                </div>

                <div class="synth-chat-body">
                    <div id="messages" class="synth-chat-messages" role="log" aria-live="polite"></div>
                </div>

                <div class="synth-chat-footer synth-chat-toolbar">
                    <form id="composer" class="synth-chat-composer" autocomplete="off">
                        <button id="attach-file" type="button" class="attach-btn" title="Attach file">📎</button>
                        <textarea id="input" placeholder="Type a message…" rows="2"></textarea>
                        <button id="send" type="button">➤</button>
                        <input id="file-input" type="file" accept="image/*,audio/*,video/*,.pdf,.txt,.doc,.docx,.ppt,.pptx,.xlsx" style="display:none" />
                    </form>
                    <div id="attachment-preview" class="attachment-preview" style="display:none"></div>
                </div>
            </div>
        `;
        document.body.appendChild(tpl);
    } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Auto-resize the WinBox chat window to fit content, anchored to the bottom.
// Grows upward until 65% viewport height; only then enables scroll in body.
// ---------------------------------------------------------------------------
function _attachChatAutoResize(winbox, mount) {
    if (!winbox || !mount) return;

    const TITLE_BAR_H = 44; // approximate WinBox title-bar height in px
    const getMaxH = () => Math.floor(window.innerHeight * 0.65);
    let _rafId = null;

    function _resize() {
        _rafId = null;
        try {
            // Don't interfere while the user has minimized/maximized
            if (winbox.min || winbox.max || winbox.full) return;

            const header = mount.querySelector('.synth-chat-header');
            const body   = mount.querySelector('.synth-chat-body');
            const footer = mount.querySelector('.synth-chat-footer');
            if (!body) return;

            const headerH = header ? header.offsetHeight : 0;
            const footerH = footer ? footer.offsetHeight : 0;
            // scrollHeight gives the natural (un-clipped) height of the messages
            const bodyNatural = body.scrollHeight;
            const contentH = headerH + bodyNatural + footerH;
            const maxH = getMaxH();
            const targetH = Math.min(contentH + TITLE_BAR_H + 8, maxH);
            const minH = headerH + footerH + TITLE_BAR_H + 24;
            const finalH = Math.max(minH, targetH);

            // Toggle the at-max class to enable scrollbar only when capped
            body.classList.toggle('at-max', finalH >= maxH);

            if (Math.abs((winbox.height || 0) - finalH) < 3) return;

            // Keep the window bottom-anchored: move y up by the height delta
            const oldBottom = (winbox.y || 0) + (winbox.height || 0);
            const newY = Math.max(0, oldBottom - finalH);
            winbox.resize(winbox.width, finalH);
            winbox.move(winbox.x, newY);
        } catch (e) { /* ignore */ }
    }

    function _schedule() {
        if (_rafId) cancelAnimationFrame(_rafId);
        _rafId = requestAnimationFrame(_resize);
    }

    // Watch for new messages
    const messagesEl = mount.querySelector('#messages');
    if (messagesEl) {
        const mo = new MutationObserver(_schedule);
        mo.observe(messagesEl, { childList: true, subtree: true, characterData: true });
    }

    // Also observe window resize (viewport changes)
    window.addEventListener('resize', _schedule, { passive: true });

    // Initial sizing — wait one tick for history to load from localStorage
    _schedule();
    setTimeout(_schedule, 120);
}

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
            if (!tpl) {
                // attempt to lazy load section if available
                if (window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
                    try { await window.SynthWebUI.loadSection('home'); } catch (e) { /* ignore */ }
                    tpl = document.getElementById('chat-window-template');
                }
            }
            if (!tpl) {
                // create the chat template and styles dynamically from this module
                try { createChatTemplate(); } catch (e) { /* ignore */ }
                tpl = document.getElementById('chat-window-template');
            }
            if (tpl && mount.children.length === 0) {
                mount.appendChild(tpl.content.cloneNode(true));
            }
            try { bindArchiveButton(); } catch (e) { /* ignore */ }
            // Ensure chat UI event handlers are initialized after the template is mounted
            try { if (typeof initChatUI === 'function') initChatUI(); } catch (e) { /* ignore */ }
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
                height: 180,
                x: 18,
                y: 'bottom',
                overflow: false,

                iconText: '💬',
                dockLabel: 'Chat',
                dockButton: chatToggleBtn || null,
                dockClass: 'chat-toggle-btn',
                className: 'synth-winbox no-close'
            });

            // Auto-grow the WinBox when messages are added (anchored to bottom)
            try { _attachChatAutoResize(winbox, mount); } catch (e) { /* ignore */ }

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

// Scroll the .synth-chat-body ancestor (the real scroll container) to the bottom.
// Falls back to scrolling the element itself when no ancestor is found.
function _scrollToBottom(el) {
    try {
        const body = el ? el.closest('.synth-chat-body') : null;
        if (body) {
            body.scrollTop = body.scrollHeight;
        } else if (el) {
            el.scrollTop = el.scrollHeight;
        }
    } catch (e) { /* ignore */ }
}
const CHAT_RECT_KEY = 'synth-webui-chat-rect';
const TYPING_INDICATOR_KEY = 'synth-webui-typing-indicator';
const TYPING_INDICATOR_TS_KEY = 'synth-webui-typing-indicator-ts';
const TYPING_INDICATOR_DEFAULT_TTL_S = 300; // seconds - fallback when no RESPONSE_TIMEOUT exposed

function _clearTypingTimeout() {
    try {
        if (window.__synth_typing_indicator_timeout) {
            clearTimeout(window.__synth_typing_indicator_timeout);
            window.__synth_typing_indicator_timeout = null;
        }
    } catch (e) { /* ignore */ }
}

function _scheduleTypingTimeout() {
    try {
        _clearTypingTimeout();
        // Use exposed RESPONSE_TIMEOUT (in seconds) if available, otherwise default
        const ttlS = (typeof window.RESPONSE_TIMEOUT === 'number' && window.RESPONSE_TIMEOUT > 0) ? Number(window.RESPONSE_TIMEOUT) : TYPING_INDICATOR_DEFAULT_TTL_S;
        // Safety: convert to ms and schedule removal
        window.__synth_typing_indicator_timeout = setTimeout(() => {
            try { removeTypingIndicator(); } catch (e) { /* ignore */ }
        }, Math.max(1000, ttlS * 1000));
    } catch (e) { /* ignore */ }
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

function addTypingIndicator() {
    try {
        const messagesEl = document.getElementById('messages');
        if (!messagesEl) return;
        if (messagesEl.querySelector('.typing-indicator')) {
            // If it's already present, refresh timestamp and reschedule timeout
            try { localStorage.setItem(TYPING_INDICATOR_TS_KEY, String(Date.now())); } catch (e) { /* ignore */ }
            try { _scheduleTypingTimeout(); } catch (e) { /* ignore */ }
            return;
        }
        const container = document.createElement('div');
        container.className = 'message-container synth';
        const bubble = document.createElement('div');
        bubble.className = 'bubble synth typing-indicator';
        bubble.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
        container.appendChild(bubble);
        messagesEl.appendChild(container);
        _scrollToBottom(messagesEl);
        try { localStorage.setItem(TYPING_INDICATOR_KEY, '1'); } catch (e) { /* ignore */ }
        try { localStorage.setItem(TYPING_INDICATOR_TS_KEY, String(Date.now())); } catch (e) { /* ignore */ }
        try { _scheduleTypingTimeout(); } catch (e) { /* ignore */ }
    } catch (e) { /* ignore */ }
}

function removeTypingIndicator() {
    try {
        const messagesEl = document.getElementById('messages');
        if (!messagesEl) return;
        const indicator = messagesEl.querySelector('.typing-indicator');
        if (indicator && indicator.parentElement) indicator.parentElement.remove();
        try { localStorage.removeItem(TYPING_INDICATOR_KEY); } catch (e) { /* ignore */ }
        try { localStorage.removeItem(TYPING_INDICATOR_TS_KEY); } catch (e) { /* ignore */ }
        try { _clearTypingTimeout(); } catch (e) { /* ignore */ }
        try { _clearTypingIndicatorProcessingTimer(); } catch (e) { /* ignore */ }
        _pendingSynthResponse = false;
    } catch (e) { /* ignore */ }
}

function appendMessage(container, sender, text, ts, attachments) {
    if (!container) return;

    // Determine timestamp (fallback to now)
    const dt = ts ? (typeof ts === 'number' ? new Date(ts) : new Date(ts)) : new Date();
    // date key for day grouping: YYYY-MM-DD
    const dayKey = dt.getFullYear() + '-' + (dt.getMonth()+1).toString().padStart(2,'0') + '-' + dt.getDate().toString().padStart(2,'0');

    // If day changed since last rendered message, insert a day separator
    try {
        const lastDay = container.dataset.lastDay || null;
        if (!lastDay || lastDay !== dayKey) {
            const dayEl = document.createElement('div');
            dayEl.className = 'message-day';
            const label = document.createElement('div');
            label.className = 'day-label';
            try {
                label.textContent = dt.toLocaleDateString(undefined, { month: 'long', day: 'numeric' });
            } catch (e) {
                label.textContent = `${dt.getMonth()+1}/${dt.getDate()}`;
            }
            dayEl.appendChild(label);
            container.appendChild(dayEl);
            container.dataset.lastDay = dayKey;
        }
    } catch (e) { /* ignore day grouping errors */ }

    const wrapper = document.createElement('div');
    wrapper.className = `message-container ${sender}`;

    const bubble = document.createElement('div');
    bubble.className = `bubble ${sender}`;
    const senderLabel = sender === 'synth' ? (window.SynthConfig && (window.SynthConfig.SYNTH_NAME || window.SynthConfig.BRAND_NAME) ? (window.SynthConfig.SYNTH_NAME || window.SynthConfig.BRAND_NAME) : 'SyntH') : 'You';

    // Format time for the small timestamp
    let timeText = '';
    try {
        timeText = dt.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    } catch (e) {
        const hh = dt.getHours().toString().padStart(2,'0');
        const mm = dt.getMinutes().toString().padStart(2,'0');
        timeText = `${hh}:${mm}`;
    }

    // Use safe-escaped content and preserve newlines
    const escaped = safeEscapeHtml(text).replace(/\n/g, '<br>');
    let attachmentHtml = '';
    if (attachments && attachments.length) {
        const parts = [];
        for (const file of attachments) {
            if (!file || !file.url) continue;
            const filename = safeEscapeHtml(file.filename || file.url.split('/').pop() || 'attachment');
            const mime = String(file.mime_type || '').toLowerCase();
            if (mime.startsWith('image/')) {
                parts.push(
                    `<a href="${safeEscapeHtml(file.url)}" target="_blank" rel="noreferrer"><img class="attachment-image-preview" src="${safeEscapeHtml(file.url)}" alt="${filename}" /></a>`
                );
            } else if (mime.startsWith('audio/')) {
                parts.push(
                    `<audio class="attachment-audio" controls src="${safeEscapeHtml(file.url)}"></audio>`
                );
            } else {
                parts.push(
                    `<a class="attachment-link" href="${safeEscapeHtml(file.url)}" target="_blank" rel="noreferrer">${filename}</a>`
                );
            }
        }
        if (parts.length) {
            attachmentHtml = `<div class="bubble-attachments">${parts.join('')}</div>`;
        }
    }
    bubble.innerHTML = `<div class="bubble-sender">${senderLabel}</div><div class="bubble-content">${escaped}</div>${attachmentHtml}<div class="bubble-time">${timeText}</div>`;
    wrapper.appendChild(bubble);
    container.appendChild(wrapper);
    // Scroll the actual scrollable ancestor (.synth-chat-body) to the bottom.
    // #messages itself has overflow:hidden — the parent body is the real scroll container.
    _scrollToBottom(container);

    if (sender === 'synth') {
        _pendingSynthResponse = false;
        _clearTypingIndicatorProcessingTimer();
        try { if (window.SynthChat && typeof window.SynthChat.maybeNotify === 'function') window.SynthChat.maybeNotify(text); } catch (e) { /* ignore */ }
        try { removeTypingIndicator(); } catch (e) { /* ignore */ }
    }
}

// ── User audio processing indicator (3-dots bubble while STT runs) ─────────
let _userAudioIndicatorEl = null;

function _addUserAudioIndicator(container) {
    _removeUserAudioIndicator();
    if (!container) return;
    const wrap = document.createElement('div');
    wrap.className = 'user-audio-indicator';
    wrap.innerHTML = `
        <div class="bubble user">
            <span class="mic-label">🎤</span>
            <span class="dot"></span><span class="dot"></span><span class="dot"></span>
        </div>`;
    container.appendChild(wrap);
    _userAudioIndicatorEl = wrap;
    _scrollToBottom(container);
}

function _removeUserAudioIndicator() {
    if (_userAudioIndicatorEl) {
        try { _userAudioIndicatorEl.remove(); } catch (e) { /* ignore */ }
        _userAudioIndicatorEl = null;
    }
}

function _replaceUserAudioIndicator(container, text, ts) {
    _removeUserAudioIndicator();
    if (container && text) appendMessage(container, 'user', text, ts || Date.now());
}

function _setUserAudioError(container, msg) {
    if (_userAudioIndicatorEl) {
        const bubble = _userAudioIndicatorEl.querySelector('.bubble');
        if (bubble) {
            bubble.classList.add('audio-error');
            bubble.innerHTML = `<span>${msg || '❌ Transcription failed'}</span>`;
        }
        // auto-remove after 4 s
        setTimeout(_removeUserAudioIndicator, 4000);
    } else if (container) {
        // fallback: insert a transient error row
        const wrap = document.createElement('div');
        wrap.className = 'user-audio-indicator';
        wrap.innerHTML = `<div class="bubble user audio-error"><span>${msg || '❌ Transcription failed'}</span></div>`;
        container.appendChild(wrap);
        _scrollToBottom(container);
        setTimeout(() => { try { wrap.remove(); } catch(e){} }, 4000);
    }
}
// ─────────────────────────────────────────────────────────────────────────────

export function initChatUI() {
    try {
        const IRIS_ENABLED = (typeof window !== 'undefined' && window.__SYNTH_CONFIG && window.__SYNTH_CONFIG.IRIS_ENABLED !== undefined)
            ? !!window.__SYNTH_CONFIG.IRIS_ENABLED
            : true;

        if (window.__synth_chat_initialized) {
            const existingMessages = document.getElementById('messages');
            const existingInput = document.getElementById('input');
            const existingForm = document.getElementById('composer');
            const existingSend = document.getElementById('send');
            if (existingMessages && existingInput && existingForm && existingSend) {
                return;
            }
        }
        const statusLabel = document.getElementById('status-label');
        const statusIndicator = document.querySelector('.connection-status .indicator');
        const messages = document.getElementById('messages');
        const input = document.getElementById('input');
        const form = document.getElementById('composer');
        const sendBtn = document.getElementById('send');

        if (!messages || !input || !form || !sendBtn) return;

        try { bindArchiveButton(); } catch (e) { /* ignore */ }

        const getMessagesEl = () => document.getElementById('messages') || messages;
        const attachBtn = document.getElementById('attach-file');
        const fileInput = document.getElementById('file-input');
        const attachmentPreview = document.getElementById('attachment-preview');
        let pendingAttachment = null;
        let pendingAttachmentMeta = null;
        let ws = window.chatWs || null;
        let _typingAnimActive = false; // true when user is typing; reset after submit
        let _pendingSynthResponse = false; // wait for a final synth response before hiding typing dots
        let _typingIndicatorProcessingTimer = null;

        function _clearTypingIndicatorProcessingTimer() {
            if (_typingIndicatorProcessingTimer) {
                clearTimeout(_typingIndicatorProcessingTimer);
                _typingIndicatorProcessingTimer = null;
            }
        }

        async function _checkProcessingMetaAndMaybeRemoveTypingIndicator() {
            _clearTypingIndicatorProcessingTimer();
            const sid = window.sessionId || (typeof sessionId !== 'undefined' ? sessionId : null);
            if (!sid) return;

            try {
                const base = _getApiBase();
                const res = await fetch(
                    (base || '') + '/api/chat/session_meta?session_id=' +
                    encodeURIComponent(sid)
                );
                if (!res.ok) return;
                const out = await res.json().catch(() => null);
                const meta = out && out.meta ? out.meta : {};
                if (!meta.processing) {
                    _pendingSynthResponse = false;
                    removeTypingIndicator();
                } else {
                    _typingIndicatorProcessingTimer = setTimeout(
                        _checkProcessingMetaAndMaybeRemoveTypingIndicator,
                        1000
                    );
                }
            } catch (e) {
                _typingIndicatorProcessingTimer = setTimeout(
                    _checkProcessingMetaAndMaybeRemoveTypingIndicator,
                    1000
                );
            }
        }

        function renderAttachmentPreview() {
            if (!attachmentPreview) return;
            if (!pendingAttachmentMeta) {
                attachmentPreview.style.display = 'none';
                attachmentPreview.innerHTML = '';
                return;
            }
            attachmentPreview.style.display = 'flex';
            const safeName = SynthUtils ? SynthUtils.escapeHtml(pendingAttachmentMeta.filename) : pendingAttachmentMeta.filename;
            attachmentPreview.innerHTML = `
                <div class="attachment-chip">
                    <span>${safeName}</span>
                    <button type="button" id="clear-attachment" title="Remove attached file">✕</button>
                </div>
            `;
            const clearBtn = attachmentPreview.querySelector('#clear-attachment');
            if (clearBtn) {
                clearBtn.addEventListener('click', () => {
                    pendingAttachment = null;
                    pendingAttachmentMeta = null;
                    fileInput.value = '';
                    renderAttachmentPreview();
                    updateButtonMode();
                });
            }
        }

        async function uploadAttachment(file) {
            if (!file) return null;
            try {
                const fd = new FormData();
                fd.append('file', file, file.name);
                const resp = await fetch('/api/chat/attachments', {
                    method: 'POST',
                    body: fd,
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => null);
                    throw new Error((err && err.detail) || resp.statusText || 'Upload failed');
                }
                return await resp.json();
            } catch (e) {
                console.warn('[chat-window] attachment upload failed:', e);
                return null;
            }
        }

        function updateAttachmentControls() {
            if (!attachBtn || !fileInput) return;
            attachBtn.disabled = !IRIS_ENABLED;
            fileInput.disabled = !IRIS_ENABLED;
            attachBtn.title = IRIS_ENABLED ? 'Attach file' : 'Attachments disabled while Iris is off';
        }

        if (attachBtn && fileInput) {
            updateAttachmentControls();
            attachBtn.addEventListener('click', () => {
                if (attachBtn.disabled) return;
                try { fileInput.click(); } catch (e) { console.warn('[chat-window] attach click failed', e); }
            });
            fileInput.addEventListener('change', async (e) => {
                try {
                    const file = e.target.files && e.target.files[0];
                    if (!file) return;
                    const wasDisabled = attachBtn.disabled;
                    if (!wasDisabled) {
                        attachBtn.disabled = true;
                    }
                    pendingAttachment = file;
                    pendingAttachmentMeta = await uploadAttachment(file);
                    if (!wasDisabled) {
                        attachBtn.disabled = false;
                    }
                    if (!pendingAttachmentMeta) {
                        pendingAttachment = null;
                        fileInput.value = '';
                        attachmentPreview.style.display = 'none';
                        return;
                    }
                    renderAttachmentPreview();
                    updateButtonMode();
                } catch (err) {
                    console.warn('[chat-window] file input change failed', err);
                }
            });
        }

        // ── Button mode (➤ send / 🎤 mic / ⏹ recording / ⏳ processing) ──
        let micMediaRecorder = null;
        let micAudioChunks   = [];
        let micStream        = null;   // shared: ambient VAD + recording
        let micAmbientCtx    = null;
        let micAmbientAnalyser = null;
        let micVADInterval   = null;
        let micRecordingMode = null;   // 'ptt' | 'toggle' | null
        let micPressTimer    = null;   // setTimeout id OR string 'long'
        let micSilenceTimer  = null;
        let micSpeaking      = false;
        let micSilenceMs     = 0;
        let micHasSpoken     = false;  // true once voice is detected during current recording
        let _sttAbortController = null;  // AbortController for in-flight STT request
        let _sileroWs     = null;   // WebSocket to /api/audio/stream (Silero VAD)
        let _sileroNode   = null;   // ScriptProcessorNode feeding PCM to Silero
        let _sileroActive = false;  // true once Silero WS has confirmed 'ready'
        const MIC_LONG_PRESS_MS   = 300;   // ms before PTT activates
        const MIC_SILENCE_SEND_MS = 1800;  // ms of silence after speech → auto-send (toggle)
        const MIC_VAD_THRESHOLD   = 0.015; // RMS volume threshold

        // ── Button mode (➤ send / 🎤 mic / ⏹ recording / ⏳ processing) ──
        function updateButtonMode() {
            if (!sendBtn || !input) return;
            const hasText = input.value.trim().length > 0;
            const hasAttachment = !!pendingAttachmentMeta;
            const wsReady = ws && ws.readyState === WebSocket.OPEN;
            if (_sttAbortController) {
                // STT in progress: spin + click = cancel
                sendBtn.type        = 'button';
                sendBtn.textContent = '';
                sendBtn.disabled    = false;
                sendBtn.className   = 'processing';
                sendBtn.title       = 'Tap to cancel transcription';
            } else if (micRecordingMode) {
                sendBtn.type        = 'button';
                sendBtn.textContent = '⏹';
                sendBtn.disabled    = false;
                sendBtn.className   = 'recording';
                sendBtn.title       = micRecordingMode === 'ptt' ? 'Release to send (PTT)' : 'Tap to stop & send';
            } else if (hasText || hasAttachment) {
                sendBtn.type        = 'submit';
                sendBtn.textContent = '➤';
                sendBtn.disabled    = !wsReady;
                sendBtn.className   = 'send-mode';
                sendBtn.title       = hasText ? 'Send message (Enter)' : 'Send attached file';
            } else {
                sendBtn.type        = 'button';
                sendBtn.textContent = '🎤';
                sendBtn.disabled    = false;
                sendBtn.className   = 'mic-mode';
                sendBtn.title       = 'Hold to record (PTT) • Tap to toggle recording';
            }
        }

        // Backwards-compatible helper for older code that expects this to exist.
        function updateSendState() {
            updateButtonMode();
        }

        // ── Ambient VAD (volume-based, triggers look-at-camera always) ───────
        function _startVADLoop(stream) {
            if (micVADInterval) return;
            try {
                micAmbientCtx = micAmbientCtx || new (window.AudioContext || window.webkitAudioContext)();
                const src = micAmbientCtx.createMediaStreamSource(stream);
                micAmbientAnalyser = micAmbientCtx.createAnalyser();
                micAmbientAnalyser.fftSize = 512;
                src.connect(micAmbientAnalyser);
                micSpeaking = false; micSilenceMs = 0;
                const buf = new Float32Array(micAmbientAnalyser.fftSize);
                micVADInterval = setInterval(() => {
                    try {
                        if (!micAmbientAnalyser) return;
                        micAmbientAnalyser.getFloatTimeDomainData(buf);
                        const rms = Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length);
                        if (rms > MIC_VAD_THRESHOLD) {
                            micSilenceMs = 0;
                            if (!micSpeaking) {
                                micSpeaking = true;
                                try { if (typeof window._synthVADLookAtCamera === 'function') window._synthVADLookAtCamera(true); } catch (_) { /* ignore */ }
                            }
                            // Button animation and auto-send driven by Silero when it is active;
                            // fall back to RMS loop when Silero is unavailable.
                            if (!_sileroActive) {
                                if (micRecordingMode === 'toggle') {
                                    clearTimeout(micSilenceTimer); micSilenceTimer = null;
                                    // Mark that voice was detected at least once in this recording
                                    micHasSpoken = true;
                                    // Visual: brighter pulse while speaking
                                    if (sendBtn) sendBtn.classList.add('speaking');
                                } else if (!micRecordingMode) {
                                    // Ambient VAD: pulsate the mic button subtly even outside recording
                                    if (sendBtn) sendBtn.classList.add('ambient-speaking');
                                }
                            }
                        } else {
                            micSilenceMs += 100;
                            if (micSpeaking && micSilenceMs > 400) {
                                micSpeaking = false;
                                try { if (typeof window._synthVADLookAtCamera === 'function') window._synthVADLookAtCamera(false); } catch (_) { /* ignore */ }
                                // Visual: dim back to idle pulse when silence detected
                                if (!_sileroActive) {
                                    if (sendBtn) sendBtn.classList.remove('speaking');
                                    if (sendBtn) sendBtn.classList.remove('ambient-speaking');
                                }
                            }
                            // Auto-send only if user actually spoke (avoid sending empty clips).
                            // When Silero is active, auto-send is driven by _applySileroVAD.
                            if (!_sileroActive && micRecordingMode === 'toggle' && micHasSpoken && !micSilenceTimer && micSilenceMs >= MIC_SILENCE_SEND_MS) {
                                micSilenceTimer = setTimeout(() => { stopRecordingAndSend(); }, 0);
                            }
                        }
                    } catch (_) { /* ignore */ }
                }, 100);
            // Attempt Silero VAD (backend) for higher-quality voice detection.
            // Falls back silently to the RMS loop above if unavailable.
            _startSileroVAD(stream);
            } catch (e) { console.warn('[chat-window] VAD loop failed:', e); }
        }

        // ── Silero VAD (backend WS) — upgrades RMS loop when available ──────
        function _startSileroVAD(stream) {
            if (_sileroWs || !stream) return;
            try {
                // Derive WebSocket URL using the same port logic as _getApiBase
                const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws';
                let wsHost = window.location.host;
                const p = window.location.port;
                if (p === '3000' || p === '3001' || p === '9007') {
                    wsHost = window.location.hostname + ':9009';
                }
                const vadWs = new WebSocket(`${wsProto}://${wsHost}/api/audio/stream`);
                _sileroWs = vadWs;
                vadWs.binaryType = 'arraybuffer';

                // Dedicated 16 kHz AudioContext so the browser resamples the mic stream.
                // Silero expects exactly 16 kHz s16le PCM.
                const vadCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
                const vadSrc = vadCtx.createMediaStreamSource(stream);
                // bufferSize 512 = 32 ms at 16 kHz — matches _CHUNK_SAMPLES_16K in vad_service.py
                // eslint-disable-next-line no-undef
                const vadProc = vadCtx.createScriptProcessor(512, 1, 1);
                _sileroNode = vadProc;

                vadProc.onaudioprocess = (e) => {
                    if (!_sileroActive || !_sileroWs || _sileroWs.readyState !== WebSocket.OPEN) return;
                    const f32 = e.inputBuffer.getChannelData(0);
                    const i16 = new Int16Array(f32.length);
                    for (let i = 0; i < f32.length; i++) {
                        i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32767 | 0));
                    }
                    try { vadWs.send(i16.buffer); } catch (_) { /* ignore */ }
                };
                vadSrc.connect(vadProc);
                vadProc.connect(vadCtx.destination);

                vadWs.onopen = () => {
                    try { vadWs.send(JSON.stringify({ sample_rate: 16000, engine: 'vad' })); } catch (_) { /* ignore */ }
                };
                vadWs.onmessage = (ev) => {
                    try {
                        const d = JSON.parse(String(ev.data));
                        if (d.type === 'ready') {
                            _sileroActive = true;
                            console.debug('[chat-window] Silero VAD active — button animation upgraded from RMS to Silero');
                        } else if (d.type === 'vad' && d.signal) {
                            _applySileroVAD(d.signal === 'speech_start');
                        }
                    } catch (_) { /* ignore */ }
                };
                vadWs.onerror = () => { _sileroActive = false; };
                vadWs.onclose = () => { _sileroActive = false; _sileroWs = null; };
            } catch (e) {
                console.debug('[chat-window] Silero VAD unavailable, using RMS fallback:', e);
                _sileroActive = false;
                _sileroWs = null;
            }
        }

        // Apply Silero VAD events to button animation and auto-send logic.
        // Called by _startSileroVAD onmessage; mirrors the RMS loop's visual logic.
        function _applySileroVAD(speaking) {
            if (speaking) {
                micSilenceMs = 0;
                if (!micSpeaking) {
                    micSpeaking = true;
                    try { if (typeof window._synthVADLookAtCamera === 'function') window._synthVADLookAtCamera(true); } catch (_) { /* ignore */ }
                }
                if (micRecordingMode === 'toggle') {
                    clearTimeout(micSilenceTimer); micSilenceTimer = null;
                    micHasSpoken = true;
                    if (sendBtn) sendBtn.classList.add('speaking');
                } else if (!micRecordingMode) {
                    if (sendBtn) sendBtn.classList.add('ambient-speaking');
                }
            } else {
                // speech_end: Silero already applied hysteresis server-side — act immediately.
                micSilenceMs = MIC_SILENCE_SEND_MS; // saturate to unblock auto-send guard
                if (micSpeaking) {
                    micSpeaking = false;
                    try { if (typeof window._synthVADLookAtCamera === 'function') window._synthVADLookAtCamera(false); } catch (_) { /* ignore */ }
                    if (sendBtn) sendBtn.classList.remove('speaking');
                    if (sendBtn) sendBtn.classList.remove('ambient-speaking');
                }
                if (micRecordingMode === 'toggle' && micHasSpoken && !micSilenceTimer) {
                    micSilenceTimer = setTimeout(() => { stopRecordingAndSend(); }, 0);
                }
            }
        }

        async function _ensureMicStream() {
            if (micStream && micStream.active) return micStream;
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
                micStream = stream;
                _startVADLoop(stream);
                return stream;
            } catch (e) { console.warn('[chat-window] Mic permission denied:', e); return null; }
        }

        // ── Recording ─────────────────────────────────────────────────
        async function startRecording(mode) {
            if (micRecordingMode) return;
            const stream = await _ensureMicStream();
            if (!stream) return;
            try {
                micAudioChunks = [];
                micHasSpoken   = false;  // reset for each new recording
                const mt = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus'
                    : (MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '');
                micMediaRecorder = mt ? new MediaRecorder(stream, { mimeType: mt }) : new MediaRecorder(stream);
                micMediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) micAudioChunks.push(e.data); };
                micMediaRecorder.start(100);
                micRecordingMode = mode;
                updateButtonMode();
            } catch (e) { console.warn('[chat-window] startRecording failed:', e); micRecordingMode = null; updateButtonMode(); }
        }

        // helper to compute base URL for API calls.  When the UI is served
        // directly from the backend (port 9009/9010) we can use relative paths.
        // However in the typical dev container the static assets are fronted by
        // nginx on port 3000/3001 which does *not* proxy /api, so relative fetches
        // will return 404.  Detect that case and point to the backend port
        // explicitly instead.
        function _getApiBase() {
            try {
                const port = window.location.port;
                // backend listens with TLS on 9009 and HTTP on 9010
                if (port === '3000' || port === '3001' || port === '9007') {
                    const proto = window.location.protocol === 'https:' ? 'https' : 'http';
                    return `${proto}://${window.location.hostname}:9009`;
                }
            } catch (_) {
                /* ignore */
            }
            return '';
        }

        async function stopRecordingAndSend() {
            clearTimeout(micSilenceTimer); micSilenceTimer = null;
            if (!micMediaRecorder || micRecordingMode === null) return;
            micRecordingMode = null;
            if (sendBtn) sendBtn.classList.remove('speaking');  // clear VAD visual
            updateButtonMode();
            await new Promise((resolve) => {
                micMediaRecorder.onstop = async () => {
                    try {
                        const mt   = (micMediaRecorder && micMediaRecorder.mimeType) || 'audio/webm';
                        const blob = new Blob(micAudioChunks, { type: mt });
                        micAudioChunks = [];
                        if (blob.size < 512) { resolve(); return; }
                        const fd = new FormData();
                        fd.append('file', blob, 'recording.webm');
                        const base = _getApiBase();

                        // Show user-side 3-dot bubble immediately
                        const activeMessages = getMessagesEl();
                        if (activeMessages) _addUserAudioIndicator(activeMessages);

                        // Enter processing state on the button
                        _sttAbortController = new AbortController();
                        updateButtonMode();

                        let data = null;
                        let aborted = false;
                        try {
                            const resp = await fetch(base + '/api/audio/upload', {
                                method: 'POST',
                                body: fd,
                                signal: _sttAbortController.signal,
                            });
                            const json = await resp.json().catch(() => null);
                            if (!resp.ok) {
                                const errMsg = (json && json.error) ? json.error : `HTTP ${resp.status}`;
                                throw new Error(errMsg);
                            }
                            data = json;
                        } catch (fetchErr) {
                            if (fetchErr && fetchErr.name === 'AbortError') {
                                aborted = true;
                                console.info('[chat-window] STT request cancelled by user');
                            } else {
                                console.warn('[chat-window] STT failed:', fetchErr);
                                _setUserAudioError(messages, '❌ ' + (fetchErr.message || 'Transcription failed'));
                            }
                        } finally {
                            _sttAbortController = null;
                            updateButtonMode();
                        }

                        if (aborted) {
                            // User cancelled — remove indicator silently
                            _removeUserAudioIndicator();
                        } else {
                            const text = (data && data.text) ? String(data.text).trim() : '';
                            if (text) {
                                // Replace 3-dot bubble with the real transcribed text
                                _replaceUserAudioIndicator(getMessagesEl(), text, Date.now());
                                if (ws && ws.readyState === WebSocket.OPEN) {
                                    // Mark as voice input so the server triggers TTS auto-inject
                                    ws.send(JSON.stringify({ text, is_voice_input: true }));
                                }
                            } else if (data) {
                                // Response OK but no text
                                _setUserAudioError(messages, '⚠️ No speech detected');
                            }
                        }
                    } catch (e) {
                        console.warn('[chat-window] mic transcription failed:', e);
                        _setUserAudioError(messages, '❌ Error: ' + (e.message || 'unknown'));
                    }
                    resolve();
                };
                try { micMediaRecorder.stop(); } catch (_) { resolve(); }
                micMediaRecorder = null;
            });
        }

        // ── PTT + toggle button interactions ───────────────────────────────
        sendBtn.addEventListener('pointerdown', (e) => {
            // If STT upload in progress, a click will cancel it (handled on pointerup)
            if (_sttAbortController) { e.preventDefault(); return; }
            if (input.value.trim().length > 0) return;
            e.preventDefault();
            micPressTimer = setTimeout(() => { micPressTimer = 'long'; startRecording('ptt'); }, MIC_LONG_PRESS_MS);
        });
        sendBtn.addEventListener('pointerup', (e) => {
            // Cancel in-flight STT request if the user taps during processing
            if (_sttAbortController) {
                e.preventDefault();
                _sttAbortController.abort();
                return;
            }
            if (input.value.trim().length > 0) return;
            e.preventDefault();
            if (micPressTimer === 'long') {
                stopRecordingAndSend();
            } else {
                clearTimeout(micPressTimer); micPressTimer = null;
                if (micRecordingMode === 'toggle') { stopRecordingAndSend(); }
                else { _ensureMicStream().then((s) => { if (s) startRecording('toggle'); }); }
            }
        });
        sendBtn.addEventListener('pointerleave',  () => { if (micPressTimer !== 'long') { clearTimeout(micPressTimer); micPressTimer = null; } });
        sendBtn.addEventListener('pointercancel', () => { if (micPressTimer !== 'long') { clearTimeout(micPressTimer); micPressTimer = null; } });

        let _wsReconnectTimer = null;
        let _wsReconnectDelay = 1500;  // ms; doubles on each failure, caps at 30s
        const _WS_MAX_DELAY   = 30000;

        function _scheduleReconnect() {
            if (_wsReconnectTimer) return;
            _wsReconnectTimer = setTimeout(() => {
                _wsReconnectTimer = null;
                if (!ws || ws.readyState === WebSocket.CLOSED) connectWs();
            }, _wsReconnectDelay);
            _wsReconnectDelay = Math.min(_wsReconnectDelay * 2, _WS_MAX_DELAY);
        }

        // ── Shared lipsync-aware audio player ─────────────────────────────
        // Handles Web Audio API setup so both tts-play and click-to-replay
        // get proper lipsync.  Guards stopLipsync with an identity check to
        // avoid stale event listeners clobbering the state.
        function __synthPlayWithLipsync(url, text, audioDuration) {
            // Disconnect & stop previous audio
            if (window.__synthLipSyncSource) {
                try { window.__synthLipSyncSource.disconnect(); } catch (_) { /* ignore */ }
                window.__synthLipSyncSource = null;
            }
            if (window.__synthLipSyncAudio) {
                try {
                    window.__synthLipSyncAudio.pause();
                    window.__synthLipSyncAudio.removeAttribute('src');
                    window.__synthLipSyncAudio.load();
                } catch (_) { /* ignore */ }
            }
            window.__synthIsLipSyncing = false;

            // Store spoken text and duration for text-based viseme estimation
            window.__synthLipSyncText = text || null;
            window.__synthLipSyncDuration = (typeof audioDuration === 'number' && audioDuration > 0) ? audioDuration : null;
            window.__synthLipSyncTimeline = null;

            const audio = new Audio(url);
            window.__synthLipSyncAudio = audio;

            // Set up Web Audio API analyser for lipsync
            try {
                if (!window.__synthLipSyncCtx) {
                    window.__synthLipSyncCtx = new (window.AudioContext || window.webkitAudioContext)();
                }
                const ctx = window.__synthLipSyncCtx;
                if (ctx.state === 'suspended') ctx.resume();

                const source = ctx.createMediaElementSource(audio);
                const analyser = ctx.createAnalyser();
                analyser.fftSize = 256;
                analyser.smoothingTimeConstant = 0.2;
                source.connect(analyser);
                analyser.connect(ctx.destination);

                window.__synthLipSyncSource = source;
                window.__synthLipSyncAnalyser = analyser;
                window.__synthLipSyncData = null;
            } catch (audioCtxErr) {
                console.warn('[chat-window] Web Audio API setup failed, playing without lipsync:', audioCtxErr);
                window.__synthLipSyncAnalyser = null;
            }

            // Guard: only the *current* audio instance may toggle lipsync
            const thisAudio = audio;
            audio.addEventListener('play', () => {
                if (window.__synthLipSyncAudio === thisAudio) window.__synthIsLipSyncing = true;
            });
            const stopLipsync = () => {
                if (window.__synthLipSyncAudio === thisAudio) {
                    window.__synthIsLipSyncing = false;
                    window.__synthLipSyncTimeline = null;
                }
            };
            audio.addEventListener('ended', stopLipsync);
            audio.addEventListener('pause', stopLipsync);
            audio.addEventListener('error', stopLipsync);

            audio.play().catch((err) => {
                if (window.__synthLipSyncAudio === thisAudio) window.__synthIsLipSyncing = false;
                if (err && err.name === 'NotAllowedError') {
                    console.debug('[chat-window] Autoplay blocked; user can tap bubble to play');
                    window.__synthPendingAudio = window.__synthPendingAudio || [];
                    window.__synthPendingAudio.push({ url, text, audioDuration });
                }
            });
        }

        function connectWs() {
            try {
                const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
                ws = new WebSocket(`${protocol}://${window.location.host}/ws`);
                window.chatWs = ws;

                ws.onopen = () => {
                    _wsReconnectDelay = 1500;  // reset backoff on success
                    if (statusLabel) statusLabel.textContent = 'Connected';
                    if (statusIndicator) statusIndicator.classList.add('online');
                    updateButtonMode();
                    // Try to start ambient VAD immediately if mic permission already granted.
                    // This lets Synth look at the camera even when not in recording mode.
                    // Silently ignored if permission hasn't been granted yet.
                    _ensureMicStream().catch(() => {});
                    // After the server pushes history messages on connect, scroll to bottom.
                    // We use a small delay so the messages have time to render.
                    setTimeout(() => { try { _scrollToBottom(getMessagesEl()); } catch (e) { /* ignore */ } }, 400);
                    // Send hello so server knows our capabilities
                    try {
                        ws.send(JSON.stringify({
                            type: 'hello',
                            client_type: 'web',
                            capabilities: ['url_fetch'],
                            has_assets: []
                        }));
                    } catch (e) { /* ignore */ }
                };
                ws.onclose = () => {
                    if (statusLabel) statusLabel.textContent = 'Reconnecting…';
                    if (statusIndicator) statusIndicator.classList.remove('online');
                    updateButtonMode();
                    _scheduleReconnect();
                };
                ws.onerror = () => {
                    if (statusLabel) statusLabel.textContent = 'Reconnecting…';
                    if (statusIndicator) statusIndicator.classList.remove('online');
                    updateButtonMode();
                };
                window.pendingAnimationCommands = window.pendingAnimationCommands || window.pendingAnimationCommands;
                ws.onmessage = async (event) => {
                    try {
                        const data = JSON.parse(event.data);

                        // Server-provided persistent session id (set early so restoreChatState can use it)
                        if (data && data.type === 'session' && data.session_id) {
                            try {
                                window.sessionId = data.session_id;
                                if (typeof sessionId !== 'undefined') sessionId = data.session_id;
                                console.debug('[chat-window] session id set from WS:', data.session_id);
                                // Attempt immediate restore now that sessionId is known
                                try { if (typeof restoreChatState === 'function') restoreChatState(); } catch (e) { /* ignore */ }
                            } catch (e) { /* ignore */ }
                            return;
                        }

                        if (data && data.type === 'message_ack') {
                            _pendingSynthResponse = true;
                            return;
                        }
                        if (data && data.type === 'message') {
                            const ts = data.ts || data.timestamp || Date.now();
                            const attachments = data.attachments || (data.data && data.data.attachments) || [];
                            appendMessage(getMessagesEl(), data.sender === 'synth' ? 'synth' : 'user', data.text || '', ts, attachments);
                            // Restore click-to-replay audio metadata when replaying history.
                            const ttsUrl = data.tts_url || (data.data && data.data.tts_url);
                            if (ttsUrl && data.sender === 'synth') {
                                try {
                                    const activeMessages = getMessagesEl();
                                    if (activeMessages) {
                                        const bubbles = activeMessages.querySelectorAll('.bubble.synth:not(.typing-indicator)');
                                        const lastBubble = bubbles.length > 0 ? bubbles[bubbles.length - 1] : null;
                                        if (lastBubble) {
                                            lastBubble.dataset.ttsUrl = ttsUrl;
                                            lastBubble.classList.add('clickable-audio');
                                        }
                                    }
                                } catch (e) { /* ignore */ }
                            }
                        } else if (data && data.type === 'action_state') {
                            const phase = String(data.phase || '').toUpperCase();
                            console.log('[chat-window] action_state received:', phase, data.action_id || null, data.component || null);
                            if (phase === 'THINKING' || phase === 'WRITING' || phase === 'TALKING') {
                                // Cancel any pending debounced IDLE removal
                                if (window.__synth_idle_removal_timer) {
                                    clearTimeout(window.__synth_idle_removal_timer);
                                    window.__synth_idle_removal_timer = null;
                                }
                                addTypingIndicator();
                            } else {
                                // Debounce IDLE removal: if WRITING is immediately followed by IDLE
                                // (e.g. fast follow-up messages from background tasks), the indicator
                                // would flash for 0ms and never render. A short delay ensures the dots
                                // are visible for at least one frame before being removed.
                                // If the actual message arrives first (appendMessage → removeTypingIndicator),
                                // the timer becomes a harmless no-op.
                                clearTimeout(window.__synth_idle_removal_timer);
                                window.__synth_idle_removal_timer = setTimeout(() => {
                                    window.__synth_idle_removal_timer = null;
                                    if (_pendingSynthResponse) {
                                        _checkProcessingMetaAndMaybeRemoveTypingIndicator();
                                    } else {
                                        removeTypingIndicator();
                                    }
                                }, 600);
                            }
                        } else if (data && data.type === 'vrm_animation_v2') {
                            // Karada v2 Protocol - Server ONLY sends: state + descriptor + started_at
                            // NO phase control, NO timing control, NO frame-level logic
                            // Client handles intro→loop→outro locally using descriptor + local clock
                            console.log('[chat-window] vrm_animation_v2 received:', data.state, data.descriptor);

                            let resolvedEntry = null;
                            let animationRef = null;
                            let descriptorData = null;
                            try {
                                if (data.descriptor && window.karadaResolveAnimationDescriptor) {
                                    resolvedEntry = await window.karadaResolveAnimationDescriptor(data.descriptor);
                                }
                                animationRef = resolvedEntry ? (resolvedEntry.animation_url || null) : null;
                                descriptorData = resolvedEntry ? (resolvedEntry.descriptor_data || null) : null;
                            } catch (e) { /* ignore */ }

                            // Get the pre-loaded clip from the cache. If it isn't ready
                            // yet (e.g. the preload for this state hasn't finished), load
                            // it on demand via the Karada v2 handler and play once ready.
                            // The Karada v2 engine is authoritative — there is no legacy
                            // renderer fallback.
                            let clip = null;
                            try {
                                if (window.VRMAnimations && typeof window.VRMAnimations._getCachedAnimation === 'function') {
                                    clip = window.VRMAnimations._getCachedAnimation(data.state, animationRef);
                                }
                            } catch (e) { /* ignore */ }

                            if (!clip && animationRef && window.animationHandler
                                && typeof window.animationHandler._awaitAnimationReady === 'function') {
                                try {
                                    clip = await window.animationHandler._awaitAnimationReady(
                                        data.state, animationRef, 8000,
                                    );
                                } catch (e) { /* ignore */ }
                            }

                            if (clip && window.karadaPlayAnimation) {
                                window.karadaPlayAnimation({
                                    state: data.state,
                                    animationFile: animationRef,
                                    descriptor: descriptorData,
                                    descriptorId: data.descriptor || null,
                                    startedAt: data.started_at,
                                    clip: clip,
                                });
                            } else {
                                console.warn('[chat-window] Karada v2 clip unavailable, skipping animation:', data.state, animationRef);
                            }

                            // Track the canonical tuple for restore deduplication and debug resyncs.
                            try {
                                if (data.state && data.descriptor && data.started_at != null) {
                                    window.__synth_current_animation_id = `${data.state}|${data.descriptor}|${data.started_at}`;
                                }
                            } catch (e) { /* ignore */ }

                            // Cache animation state for recovery
                            try {
                                if (data.state) {
                                    window.__synth_current_animation_state = {
                                        state: data.state,
                                        animation: animationRef,
                                        descriptor_id: data.descriptor || null,
                                        descriptor: descriptorData,
                                        started_at: data.started_at,
                                    };
                                }
                            } catch (e) { /* ignore */ }
                        } else if (data && data.type === 'vrm_preload') {
                            // Preload an animation file into the cache
                            try {
                                if (window.VRMAnimations && typeof window.VRMAnimations.preload === 'function') {
                                    window.VRMAnimations.preload(data.state, data.file, data.descriptor);
                                }
                            } catch (e) { /* ignore */ }
                        } else if (data && data.type === 'vrm_expression_set') {
                            try {
                                // Remove previous facial_expression source before adding
                                // the new one (replace semantics, not accumulate).
                                if (window.animationHandler && typeof window.animationHandler.removeExpressionSourcesByTag === 'function') {
                                    window.animationHandler.removeExpressionSourcesByTag('facial_expression');
                                }
                                if (window.animationHandler && typeof window.animationHandler.addExpressionSource === 'function') {
                                    // prefer pre-resolved targets dict sent by Python (GAP 1A);
                                    // fall back to {name: intensity} for backward compat.
                                    const tgt = (data.targets && typeof data.targets === 'object')
                                        ? Object.assign({}, data.targets)
                                        : (data.name ? {[String(data.name)]: Number(data.intensity) || 0} : {});
                                    window.animationHandler.addExpressionSource({targets: tgt, priority: 25, source: 'facial_expression'});
                                }
                            } catch (e) { /* ignore */ }
                        } else if (data && data.type === 'vrm_expression_clear') {
                            try {
                                if (window.animationHandler && typeof window.animationHandler.removeExpressionSourcesByTag === 'function') {
                                    window.animationHandler.removeExpressionSourcesByTag('facial_expression');
                                } else if (window.animationHandler && typeof window.animationHandler.clearExpressionSources === 'function') {
                                    window.animationHandler.clearExpressionSources();
                                }
                            } catch (e) { /* ignore */ }
                        } else if (data && data.type === 'vrm_face') {
                            // Update VRM blend-shape face values
                            try {
                                if (window.VRMAnimations && typeof window.VRMAnimations.setFaceValues === 'function') {
                                    window.VRMAnimations.setFaceValues(data.values);
                                }
                            } catch (e) { /* ignore */ }
                        } else if (data && data.type === 'vrm_model') {
                            // Switch/load a new VRM model
                            try {
                                if (typeof window.refreshVRM === 'function') {
                                    window.refreshVRM(data.url, data.name);
                                }
                            } catch (e) { /* ignore */ }
                        } else if (data && typeof data.type === 'string' && data.type.indexOf('archive') === 0) {
                            // Backend notified about archive changes (create/delete/restore)
                            try {
                                console.debug('[chat-window] Received archive event via WS:', data);
                                const panel = window.__archive_modal_instance;
                                if (panel && typeof panel.dispatchEvent === 'function') {
                                    panel.dispatchEvent(new CustomEvent('archive:refresh', { detail: data }));
                                }
                            } catch (e) { /* ignore */ }
                        } else if (data && data.type === 'tts-play' && data.url) {
                            // ── Vox TTS audio playback ────────────────────────────────────
                            try {
                                const voxEnabled = (typeof window.VOX_ENABLED !== 'undefined')
                                    ? window.VOX_ENABLED
                                    : (window.__SYNTH_CONFIG && window.__SYNTH_CONFIG.VOX_ENABLED !== undefined
                                        ? window.__SYNTH_CONFIG.VOX_ENABLED
                                        : true);

                                // Cache management — always, so history survives voxEnabled toggling
                                const cacheLimit = (typeof window.VOX_AUDIO_CACHE_SIZE === 'number' && window.VOX_AUDIO_CACHE_SIZE > 0)
                                    ? window.VOX_AUDIO_CACHE_SIZE
                                    : ((window.__SYNTH_CONFIG && window.__SYNTH_CONFIG.VOX_AUDIO_CACHE_SIZE) || 40);
                                window.__synth_tts_cache = window.__synth_tts_cache || [];
                                window.__synth_tts_cache.push({ url: data.url, ts: Date.now() });
                                while (window.__synth_tts_cache.length > cacheLimit) {
                                    window.__synth_tts_cache.shift();
                                }

                                // Attach URL to the last synth bubble — always, so click-to-replay
                                // works even when autoplay is blocked or voxEnabled is false.
                                // Exclude typing indicators: they are transient and get removed
                                // when the real message arrives, which would lose the TTS URL.
                                try {
                                    const activeMessages = getMessagesEl();
                                    if (activeMessages) {
                                        const synthBubbles = activeMessages.querySelectorAll('.bubble.synth:not(.typing-indicator)');
                                        let lastBubble = synthBubbles.length > 0 ? synthBubbles[synthBubbles.length - 1] : null;
                                        // Safety net: if no synth bubble exists yet (race between
                                        // message and tts-play events) create one from the caption.
                                        if (!lastBubble && data.text && typeof appendMessage === 'function') {
                                            appendMessage(activeMessages, 'synth', data.text, Date.now());
                                            const updated = activeMessages.querySelectorAll('.bubble.synth:not(.typing-indicator)');
                                            lastBubble = updated.length > 0 ? updated[updated.length - 1] : null;
                                        }
                                        if (lastBubble) {
                                            lastBubble.dataset.ttsUrl = data.url;
                                            if (data.text) lastBubble.dataset.ttsText = data.text;
                                            lastBubble.classList.add('clickable-audio');
                                        }
                                    }
                                } catch (e) { /* ignore */ }

                                // Auto-play only when vox is enabled
                                if (voxEnabled) {
                                    try { __synthPlayWithLipsync(data.url, data.text, data.audio_duration_s); } catch (e) { /* ignore */ }
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
            input.addEventListener('input', updateButtonMode);

            // Trigger 'think' animation when user starts typing, so Synth looks
            // attentive before the message is even sent.
            input.addEventListener('input', () => {
                try {
                    const hasText = input.value.trim().length > 0;
                    if (hasText && !_typingAnimActive) {
                        _typingAnimActive = true;
                        try {
                            fetch('/api/animation_state', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ state: 'think' })
                            }).catch(() => { /* ignore network errors */ });
                        } catch (e) { /* ignore */ }
                    } else if (!hasText) {
                        _typingAnimActive = false;
                    }
                } catch (e) { /* ignore */ }
            });

            // Send on Enter (Shift+Enter for newline)
            input.addEventListener('keydown', (ev) => {
                try {
                    if (ev.key === 'Enter' && !ev.shiftKey) {
                        ev.preventDefault();
                        // Trigger the same submit handler the form uses
                        if (form) {
                            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                        }
                    }
                } catch (e) { /* ignore */ }
            });
        }

        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                if (!input || !ws || ws.readyState !== WebSocket.OPEN) {
                    try { console.warn('[chat-window] submit ignored: ws not open', { hasInput: !!input, hasWs: !!ws, readyState: ws ? ws.readyState : null }); } catch (err) { /* ignore */ }
                    return;
                }
                const text = input.value.trim();
                const hasAttachment = !!pendingAttachmentMeta;
                if (!text && !hasAttachment) return;

                const payload = { text };
                if (hasAttachment) {
                    payload.attachments = [pendingAttachmentMeta];
                }
                try {
                    console.log('[chat-window] sending message via WS, len=', text.length, 'attachments=', payload.attachments ? payload.attachments.length : 0);
                    ws.send(JSON.stringify(payload));
                    appendMessage(getMessagesEl(), 'user', text, Date.now(), payload.attachments);
                    input.value = '';
                    pendingAttachment = null;
                    pendingAttachmentMeta = null;
                    if (fileInput) fileInput.value = '';
                    renderAttachmentPreview();
                    _typingAnimActive = false; // backend takes over think→write→idle from here
                    updateButtonMode();
                    // keep focus on input
                    try { input.focus(); } catch (e) { /* ignore */ }
                } catch (e) {
                    console.warn('[chat-window] send failed', e);
                }
            });
        }

        // ── Click-to-replay delegated handler ──────────────────────────────────
        if (!window.__synth_tts_click_bound) {
            document.addEventListener('click', (ev) => {
                try {
                    const bubble = ev.target ? ev.target.closest('.bubble.synth.clickable-audio') : null;
                    if (!bubble) return;
                    const url = bubble.dataset.ttsUrl;
                    if (!url) return;
                    const text = bubble.dataset.ttsText || null;
                    try { __synthPlayWithLipsync(url, text); } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            }, true);
            window.__synth_tts_click_bound = true;
        }

        // ── Autoplay unlock on first user interaction ───────────────────────────
        // When the browser blocks autoplay (NotAllowedError), audio URLs are
        // enqueued in window.__synthPendingAudio. The first gesture (click, key,
        // touch) unlocks the AudioContext and drains the queue.
        try {
            const _drainPendingAudio = () => {
                try {
                    const q = window.__synthPendingAudio;
                    if (!q || !q.length) return;
                    window.__synthPendingAudio = [];
                    // Play only the most recent pending audio (latest message)
                    const item = q[q.length - 1];
                    const url = (typeof item === 'string') ? item : item.url;
                    const text = (typeof item === 'object' && item) ? item.text : undefined;
                    const dur = (typeof item === 'object' && item) ? item.audioDuration : undefined;
                    try { __synthPlayWithLipsync(url, text, dur); } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            };
            const _unlockHandler = () => {
                _drainPendingAudio();
                try { document.removeEventListener('click', _unlockHandler, true); } catch (e) { /* ignore */ }
                try { document.removeEventListener('keydown', _unlockHandler, true); } catch (e) { /* ignore */ }
                try { document.removeEventListener('touchstart', _unlockHandler, true); } catch (e) { /* ignore */ }
            };
            document.addEventListener('click', _unlockHandler, true);
            document.addEventListener('keydown', _unlockHandler, true);
            document.addEventListener('touchstart', _unlockHandler, true);
        } catch (e) { /* ignore */ }

        if (!ws || ws.readyState === WebSocket.CLOSED) connectWs();
        updateButtonMode(); // set initial icon state
        try {
            if (typeof localStorage !== 'undefined' && localStorage.getItem && localStorage.getItem(TYPING_INDICATOR_KEY)) {
                // Respect configured RESPONSE_TIMEOUT: if typing indicator timestamp is older than allowed, clear it
                try {
                    const tsRaw = localStorage.getItem(TYPING_INDICATOR_TS_KEY);
                    if (tsRaw) {
                        const ts = Number(tsRaw) || 0;
                        const ttlS = (typeof window.RESPONSE_TIMEOUT === 'number' && window.RESPONSE_TIMEOUT > 0) ? Number(window.RESPONSE_TIMEOUT) : TYPING_INDICATOR_DEFAULT_TTL_S;
                        const ageMs = Date.now() - ts;
                        if (ageMs > (ttlS * 1000)) {
                            // expired - remove persisted indicator
                            try { localStorage.removeItem(TYPING_INDICATOR_KEY); } catch (e) { /* ignore */ }
                            try { localStorage.removeItem(TYPING_INDICATOR_TS_KEY); } catch (e) { /* ignore */ }
                        } else {
                            addTypingIndicator();
                        }
                    } else {
                        // No timestamp provided - be conservative and restore indicator, scheduling a safety timeout
                        addTypingIndicator();
                    }
                } catch (e) { try { addTypingIndicator(); } catch (e) { /* ignore */ } }
            }
        } catch (e) { /* ignore */ }
        window.__synth_chat_initialized = true;

    } catch (e) {
        console.warn('[chat-window] initChatUI failed', e);
    }
}

async function openArchives() {
    try {
        let mod = window.ArchiveWindow;
        if (!mod || !mod.createArchiveModal) {
            try {
                // dynamic import with cache-busting to avoid stale module instances during hot-reload
                const _ts = (window.__synth_assets_bust || Date.now());
                mod = await import(`/js/archive-window.mjs?t=${_ts}`);
                if (mod && mod.createArchiveModal) {
                    window.ArchiveWindow = window.ArchiveWindow || {};
                    window.ArchiveWindow.createArchiveModal = mod.createArchiveModal;
                }
            } catch (e) {
                console.warn('[chat-window] Failed to import archive-window module', e);
            }
        }
        const creator = (window.ArchiveWindow && window.ArchiveWindow.createArchiveModal)
            ? window.ArchiveWindow.createArchiveModal
            : (mod && mod.createArchiveModal ? mod.createArchiveModal : null);
        if (!creator) throw new Error('Archive module not available');
        const modal = creator();
        try {
            if (window.SynthWindowManager && typeof window.SynthWindowManager.restore === 'function') {
                try {
                    window.SynthWindowManager.restore('archives');
                    // Ask underlying panel to refresh its list if available
                    try { const panel = window.__archive_modal_instance; if (panel) panel.dispatchEvent(new CustomEvent('archive:refresh')); } catch (e) { /* ignore */ }

                    // Defensive check: if the restored WinBox exists but its body is empty (stale instance),
                    // re-attach a fresh panel instance to ensure content is visible and up-to-date.
                    try {
                        const winboxEl = document.getElementById('archives');
                        if (winboxEl) {
                            const wbBody = winboxEl.querySelector('.wb-body');
                            const isEmptyBody = wbBody && (!wbBody.firstElementChild || wbBody.childElementCount === 0);
                            if (isEmptyBody) {
                                // Create or reuse a fresh panel and mount it into the WinBox body
                                try {
                                    const freshPanel = window.__archive_modal_instance || creator();
                                    if (freshPanel && wbBody) {
                                        // Clean any stray nodes and append the panel
                                        while (wbBody.firstChild) wbBody.removeChild(wbBody.firstChild);
                                        wbBody.appendChild(freshPanel);
                                        // Ensure the window-level refs are consistent
                                        try { window.__archive_modal_instance = freshPanel; } catch (e) {}
                                        try { if (window.__archive_modal_winbox && typeof window.__archive_modal_winbox.mount === 'function') window.__archive_modal_winbox.mount(freshPanel); } catch (e) {}
                                        try { freshPanel.dispatchEvent(new CustomEvent('archive:refresh')); } catch (e) {}
                                    }
                                } catch (e) { /* ignore mounting errors */ }
                            }
                        }
                    } catch (e) { /* ignore */ }

                    // If the archive panel is already visible after restore, return.
                    const panelCheck = document.getElementById('archive-panel');
                    if (panelCheck && panelCheck.offsetParent !== null) return;
                    // Otherwise fall through and show the newly created modal (robust against stale/hidden winbox)
                } catch (e) {
                    // ignore and continue to show modal
                }
            }
        } catch (e) {}
        try { if (modal && modal.style) { modal.style.display = 'flex'; try { modal.dispatchEvent(new CustomEvent('archive:refresh')); } catch (e) {} } } catch (e) {}
    } catch (err) {
        const msg = 'Open archives error: ' + (err && err.message ? err.message : err);
        try { if (window.showToast) window.showToast(msg, true); } catch (e) {}
        console.error('[chat-window] Open archives failed', err);
    }
}

function bindArchiveButton() {
    try {
        const chatRestoreBtn = document.getElementById('chat-restore');
        if (chatRestoreBtn && !chatRestoreBtn.dataset.synthBound) {
            chatRestoreBtn.addEventListener('click', async () => { await openArchives(); });
            chatRestoreBtn.dataset.synthBound = '1';
        }
        const chatArchiveBtn = document.getElementById('chat-archive');
        if (chatArchiveBtn && !chatArchiveBtn.dataset.synthBound) {
            chatArchiveBtn.addEventListener('click', async () => {
                try {
                    const messagesEl = document.getElementById('messages');
                    const hasMessages = (messagesEl && messagesEl.children && messagesEl.children.length > 0) || (Array.isArray(historyBuffer) && historyBuffer.length > 0);
                    if (!hasMessages) {
                        try { if (window.showToast) window.showToast('Chat is empty. Nothing to archive.', true); } catch (e) {}
                        return;
                    }
                    try { if (window.showToast) window.showToast('Archiving chat...', false); } catch (e) {}
                    // Ensure session context persisted (best-effort)
                    try { if (window.SynthChat && typeof window.SynthChat.saveChatState === 'function') window.SynthChat.saveChatState(); } catch (e) { /* ignore */ }
                    // Attempt archive request (include session_id if available)
                    const payload = { session_id: typeof sessionId !== 'undefined' ? sessionId : null };
                    let out = null;
                    try {
                        const res = await fetch((window.__getApiBase ? window.__getApiBase() : '') + '/api/chat/archive', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                        out = res.ok ? await res.json() : null;
                    } catch (e) { out = null; }

                    if (out && out.success) {
                        try { if (window.showToast) window.showToast('Chat archived: ' + out.archive_id, false); } catch (e) {}
                        if (messagesEl) messagesEl.innerHTML = '';
                        historyBuffer = [];
                        try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* ignore */ }
                        try { saveChatState(); } catch (e) { /* ignore */ }
                    } else {
                        try { if (window.showToast) window.showToast('Archive failed', true); } catch (e) {}
                    }
                } catch (err) {
                    console.error('[chat-window] Archive failed', err);
                    try { if (window.showToast) window.showToast('Archive error: ' + (err && err.message ? err.message : String(err)), true); } catch (e) {}
                }
            });
            chatArchiveBtn.dataset.synthBound = '1';
        }
    } catch (e) { /* ignore */ }
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
        const storage = MULTI_SESSION ? sessionStorage : localStorage;
        const stateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}` : `${CHAT_WINDOW_STATE_KEY}`;
        let state = 'normal';
        if (chatEl.classList.contains('minimized')) state = 'minimized';
        else if (chatEl.classList.contains('maximized')) state = 'maximized';
        else if (chatEl.classList.contains('expanded')) state = 'expanded';
        try { storage.setItem(stateKey, state); } catch (e) { /* ignore */ }

        const rectKey = sessionId ? `${CHAT_RECT_KEY}-${sessionId}` : `${CHAT_RECT_KEY}`;
        try {
            const rect = chatEl.getBoundingClientRect();
            const payload = {
                left: Math.round(rect.left),
                top: Math.round(rect.top),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
            };
            storage.setItem(rectKey, JSON.stringify(payload));
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
        // Restore rect
        try {
            const storage = MULTI_SESSION ? sessionStorage : localStorage;
        const rectKey = sessionId ? `${CHAT_RECT_KEY}-${sessionId}` : `${CHAT_RECT_KEY}`;
            const legacyMobileKey = sessionId ? `${CHAT_RECT_KEY}-${sessionId}-mobile` : `${CHAT_RECT_KEY}-mobile`;
            const legacyDesktopKey = sessionId ? `${CHAT_RECT_KEY}-${sessionId}-desktop` : `${CHAT_RECT_KEY}-desktop`;
            const rectRaw = storage.getItem(rectKey) || storage.getItem(legacyDesktopKey) || storage.getItem(legacyMobileKey) || storage.getItem(sessionId ? `${CHAT_RECT_KEY}-${sessionId}` : CHAT_RECT_KEY) || storage.getItem(CHAT_RECT_KEY);
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
                        try { (MULTI_SESSION ? sessionStorage : localStorage).removeItem(rectKey); } catch (e) { /* ignore */ }
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
            const storage = MULTI_SESSION ? sessionStorage : localStorage;
        const stateKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}` : `${CHAT_WINDOW_STATE_KEY}`;
            const legacyMobileKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}-mobile` : `${CHAT_WINDOW_STATE_KEY}-mobile`;
            const legacyDesktopKey = sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}-desktop` : `${CHAT_WINDOW_STATE_KEY}-desktop`;
            const localState = storage.getItem(stateKey) || storage.getItem(legacyDesktopKey) || storage.getItem(legacyMobileKey) || storage.getItem(sessionId ? `${CHAT_WINDOW_STATE_KEY}-${sessionId}` : CHAT_WINDOW_STATE_KEY) || storage.getItem(CHAT_WINDOW_STATE_KEY);
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
                    const res = await fetch((window.__getApiBase ? window.__getApiBase() : '') + '/api/chat/session_meta?session_id=' + encodeURIComponent(sessionId));
                    if (!res.ok) return;
                    const out = await res.json();
                    const meta = out && out.meta ? out.meta : {};
                    if (meta && meta.processing) {
                        // If server provides an explicit expiry/started timestamp, honor it to avoid stale indicators
                        try {
                            const now = Date.now();
                            // Accept ISO timestamps or numeric ms timestamps
                            const expiresRaw = meta.processing_expires_at || meta.processing_expires || null;
                            const startedRaw = meta.processing_started_at || meta.processing_started || null;
                            let expired = false;
                            if (expiresRaw) {
                                let expiresTs = 0;
                                if (typeof expiresRaw === 'number') expiresTs = Number(expiresRaw);
                                else expiresTs = Date.parse(String(expiresRaw)) || 0;
                                if (expiresTs && now >= expiresTs) expired = true;
                            } else if (startedRaw) {
                                let startedTs = 0;
                                if (typeof startedRaw === 'number') startedTs = Number(startedRaw);
                                else startedTs = Date.parse(String(startedRaw)) || 0;
                                const ttlS = (typeof window.RESPONSE_TIMEOUT === 'number' && window.RESPONSE_TIMEOUT > 0) ? Number(window.RESPONSE_TIMEOUT) : TYPING_INDICATOR_DEFAULT_TTL_S;
                                if (startedTs && (now - startedTs) > (ttlS * 1000)) expired = true;
                            }
                            if (expired) {
                                try { removeTypingIndicator(); } catch (e) { /* ignore */ }
                            } else {
                                addTypingIndicator();
                            }
                        } catch (e) {
                            try { addTypingIndicator(); } catch (e) { /* ignore */ }
                        }
                    } else try { removeTypingIndicator(); } catch (e) { /* ignore */ }
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