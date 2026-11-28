/* The "More" menu: windows extensions put in the WebUI (core/extensions/ui.py).
 * An extension declares a window (title, icon, a page of its own files); this script,
 * which is the Core's, draws the menu and the window. Every node is built with DOM calls
 * and textContent, so nothing an extension sends is read as HTML or script. The page
 * itself runs in a sandboxed frame (see core/extensions/web.py).
 * The Activity and Logs slots are sub-tabs drawn by the server when it serves those
 * sections, so they need nothing here.
 */
(function () {
    'use strict';
    const ENTRIES_URL = '/api/extensions/ui';
    const REFRESH_MS = 30000;
    const open = new Map();
    let entries = [];
    let button = null;
    let menu = null;

    function el(tag, props) {
        const node = document.createElement(tag);
        Object.assign(node, props || {});
        return node;
    }

    function frameFor(entry) {
        const sandbox = 'allow-scripts allow-forms allow-popups allow-downloads allow-modals' +
            (entry.same_origin ? ' allow-same-origin' : '');
        const frame = el('iframe', { title: entry.title, referrerPolicy: 'no-referrer' });
        frame.setAttribute('sandbox', sandbox);
        frame.src = entry.url;
        frame.style.cssText = 'width:100%;height:100%;border:0;background:transparent';
        return frame;
    }

    // A frame is a whole view under the top bar (like the avatar's 3D page), not a window:
    // it cannot be moved or resized, and choosing any tab of the bar puts it away.
    let frameHost = null;

    function closeFrame() {
        if (frameHost) { frameHost.remove(); frameHost = null; }
        document.body.classList.remove('ext-frame-open');
        const veil = document.getElementById('ext-frame-veil');
        if (veil) veil.remove();
    }

    // While a frame is up, floating windows (chat, etc.) and the minimized-window stack would
    // sit on top of it; they are put away for the duration and come back when it closes.
    function ensureFrameVeil() {
        if (document.getElementById('ext-frame-veil')) return;
        const style = el('style');
        style.id = 'ext-frame-veil';
        style.textContent = 'body.ext-frame-open .winbox, body.ext-frame-open #synth-minimized-stack' +
            '{display:none !important}';
        document.head.appendChild(style);
    }

    function openFrame(entry) {
        closeFrame();
        const topbar = document.querySelector('header.top-bar');
        const top = topbar ? Math.round(topbar.getBoundingClientRect().bottom) : 0;
        frameHost = el('section', { className: 'ext-frame-view' });
        frameHost.setAttribute('aria-label', entry.title);
        frameHost.style.cssText = 'position:fixed;left:0;right:0;bottom:0;top:' + top + 'px;' +
            'z-index:900;background:var(--background)';
        const close = el('button', { type: 'button', className: 'ext-frame-close', textContent: '✕' });
        close.title = 'Close ' + entry.title;
        close.setAttribute('aria-label', 'Close ' + entry.title);
        close.style.cssText = 'position:absolute;bottom:12px;left:12px;z-index:2;width:34px;height:34px;' +
            'border-radius:50%;border:1px solid var(--accent);background:var(--surface-alt);' +
            'color:var(--text-soft);font-size:1rem;cursor:pointer;opacity:.85';
        close.addEventListener('click', closeFrame);
        frameHost.appendChild(frameFor(entry));
        frameHost.appendChild(close);
        document.body.appendChild(frameHost);
        ensureFrameVeil();
        document.body.classList.add('ext-frame-open');
    }

    function openWindow(entry) {
        if (entry.presentation === 'frame') { openFrame(entry); return; }
        const id = 'ext-window-' + entry.extension_id.replace('/', '-') + '-' + entry.id;
        const existing = open.get(id);
        if (existing) {
            try { existing.focus(); return; } catch (e) { open.delete(id); }
        }
        const holder = el('div');
        holder.style.cssText = 'width:100%;height:100%';
        holder.appendChild(frameFor(entry));
        if (typeof window.WinBox !== 'function') {
            window.open(entry.url, '_blank', 'noopener,noreferrer');
            return;
        }
        const topbar = document.querySelector('header.top-bar');
        const box = new window.WinBox({
            id: id,
            title: (entry.icon ? entry.icon + ' ' : '') + entry.title,
            mount: holder,
            root: document.getElementById('desktop-root') || undefined,
            x: 'center',
            y: 'center',
            top: topbar ? topbar.getBoundingClientRect().height : 0,
            width: '70%',
            height: '70%',
            class: 'synth-winbox no-full modern',
            onclose: function () { open.delete(id); return false; },
        });
        open.set(id, box);
    }

    function closeMenu() {
        if (menu) menu.hidden = true;
        if (button) button.setAttribute('aria-expanded', 'false');
    }

    // The collapsed (hamburger) menu closes when a tab is chosen (main.js); choosing an
    // entry of More is choosing a page too, so it closes it as well.
    function closeNav() {
        const header = document.querySelector('header.top-bar');
        const hamburger = document.querySelector('.hamburger');
        const nav = document.querySelector('nav.main-nav');
        if (header) header.classList.remove('nav-open');
        if (hamburger) hamburger.setAttribute('aria-expanded', 'false');
        if (nav) nav.classList.remove('open');
    }

    function ensureButton() {
        if (button) return;
        const nav = document.querySelector('nav.main-nav');
        if (!nav) return;
        button = el('button', { type: 'button', className: 'nav-btn nav-more' });
        button.setAttribute('aria-haspopup', 'true');
        button.setAttribute('aria-expanded', 'false');
        button.appendChild(el('span', { className: 'icon', textContent: '⋯' }));
        button.appendChild(el('span', { textContent: 'More' }));
        menu = el('div', { className: 'ext-more-menu' });
        menu.hidden = true;
        menu.setAttribute('role', 'menu');
        menu.style.cssText = 'position:fixed;z-index:60000;min-width:200px;padding:6px;' +
            'border-radius:12px;background:var(--surface-alt);border:1px solid var(--accent);' +
            'box-shadow:0 18px 40px -20px rgba(0,0,0,.6)';
        button.addEventListener('click', (event) => {
            event.stopPropagation();
            if (!menu.hidden) { closeMenu(); return; }
            const rect = button.getBoundingClientRect();
            menu.style.top = Math.round(rect.bottom + 6) + 'px';
            menu.style.left = Math.max(8, Math.round(rect.right - 220)) + 'px';
            menu.hidden = false;
            button.setAttribute('aria-expanded', 'true');
        });
        document.addEventListener('click', closeMenu);
        // Choosing a tab of the bar leaves the frame (the More button itself does not).
        nav.addEventListener('click', (event) => {
            const tab = event.target && event.target.closest ? event.target.closest('.nav-btn') : null;
            if (tab && tab !== button) closeFrame();
        });
        document.addEventListener('keydown', (event) => { if (event.key === 'Escape') { closeMenu(); closeFrame(); } });
        // Before About, the last entry of the bar.
        const about = nav.querySelector('[data-tab="about"]');
        nav.insertBefore(button, about);
        document.body.appendChild(menu);
    }

    function render() {
        const more = entries.filter((entry) => entry.slot === 'more');
        if (!more.length) {
            closeFrame();
            if (button) { button.remove(); button = null; }
            if (menu) { menu.remove(); menu = null; }
            return;
        }
        ensureButton();
        if (!menu) return;
        menu.textContent = '';
        more.forEach((entry) => {
            const item = el('button', { type: 'button', className: 'ext-more-item' });
            item.setAttribute('role', 'menuitem');
            item.style.cssText = 'display:flex;gap:.6rem;align-items:center;width:100%;' +
                'padding:.55rem .8rem;border:0;border-radius:8px;background:transparent;' +
                'color:var(--text-soft);font-size:1rem;cursor:pointer;text-align:left';
            item.appendChild(el('span', { textContent: entry.icon || '▫' }));
            item.appendChild(el('span', { textContent: entry.title }));
            item.addEventListener('click', () => { closeMenu(); closeNav(); openWindow(entry); });
            menu.appendChild(item);
        });
    }

    async function refresh() {
        try {
            const response = await fetch(ENTRIES_URL);
            if (!response.ok) return;
            const data = await response.json();
            entries = Array.isArray(data.entries) ? data.entries : [];
            render();
        } catch (e) { /* the menu simply stays as it was */ }
    }

    function start() {
        refresh();
        setInterval(refresh, REFRESH_MS);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
    window.synthExtensionSlots = { refresh: refresh };
})();
