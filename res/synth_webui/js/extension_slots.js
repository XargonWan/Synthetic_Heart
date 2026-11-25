/* The "More..." menu: windows extensions put in the WebUI (core/extensions/ui.py).
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
        const sandbox = 'allow-scripts allow-forms allow-popups allow-downloads' +
            (entry.same_origin ? ' allow-same-origin' : '');
        const frame = el('iframe', { title: entry.title, referrerPolicy: 'no-referrer' });
        frame.setAttribute('sandbox', sandbox);
        frame.src = entry.url;
        frame.style.cssText = 'width:100%;height:100%;border:0;background:transparent';
        return frame;
    }

    function openWindow(entry) {
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

    function ensureButton() {
        if (button) return;
        const nav = document.querySelector('nav.main-nav');
        if (!nav) return;
        button = el('button', { type: 'button', className: 'nav-btn nav-more' });
        button.setAttribute('aria-haspopup', 'true');
        button.setAttribute('aria-expanded', 'false');
        button.appendChild(el('span', { className: 'icon', textContent: '⋯' }));
        button.appendChild(el('span', { textContent: 'More…' }));
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
        document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenu(); });
        nav.appendChild(button);
        document.body.appendChild(menu);
    }

    function render() {
        const more = entries.filter((entry) => entry.slot === 'more');
        if (!more.length) {
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
            item.addEventListener('click', () => { closeMenu(); openWindow(entry); });
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
