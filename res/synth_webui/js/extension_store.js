/* Plugins > Catalog and Repositories: extensions and where they come from (core/extensions/webui_store.py).
 * Everything that comes from a repository (names, descriptions, warnings) is shown with
 * textContent, never as HTML.
 */
(function () {
    'use strict';
    const BASE = '/api/extensions/store';

    async function call(path, body) {
        const opts = body === undefined
            ? { method: 'GET' }
            : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
        const resp = await fetch(BASE + path, opts);
        if (!resp.ok) {
            let detail = '';
            try { detail = (await resp.json()).detail || ''; } catch (e) { /* not JSON */ }
            throw new Error('HTTP ' + resp.status + (detail ? ': ' + detail : ''));
        }
        return resp.json();
    }

    function el(tag, props, children) {
        const node = document.createElement(tag);
        Object.assign(node, props || {});
        (children || []).forEach((c) => node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
        return node;
    }

    function toast(message, isError) {
        if (window.showToast) window.showToast(message, !!isError);
    }

    async function act(path, body, root, confirmText) {
        try {
            let result = await call(path, body);
            if (result.needs_confirmation) {
                if (!window.confirm(result.error + '\n\n' + (confirmText || 'Continue?'))) return;
                result = await call(path, Object.assign({}, body, { confirm_untrusted: true }));
            }
            if (!result.ok) { toast(result.error || 'Failed', true); return; }
            (result.warnings || []).forEach((w) => toast(w, true));
            if (result.requested && result.requested.length && !(result.granted || []).length) {
                const wants = result.requested.join(', ');
                if (window.confirm('This extension asks for: ' + wants + '\n\nGrant these capabilities now?')) {
                    await call('/grant', { id: body.id });
                }
            }
            toast('Done');
        } catch (e) {
            toast('Extensions: ' + e.message, true);
        }
        render(root);
    }

    function labelled(text, input) {
        return el('label', { style: 'display:flex;flex-direction:column;gap:2px;margin:0 10px 8px 0;font-size:0.85rem' }, [text, input]);
    }

    function repoForm(root) {
        const field = (placeholder, width) => el('input', { type: 'text', placeholder, style: 'width:' + width });
        const name = field('official', '9em');
        const location = field('folder, https:// URL or Git URL', '24em');
        const branch = field('branch (Git only)', '9em');
        const key = field('base64, empty: unsigned', '24em');
        const trusted = el('input', { type: 'checkbox' });
        const add = el('button', {
            className: 'pill', textContent: 'Add repository', style: 'margin-bottom:8px',
            onclick: () => act('/repo/add', {
                name: name.value, location: location.value, branch: branch.value,
                key: key.value, trusted: trusted.checked,
            }, root),
        });
        return el('div', { style: 'margin-top:14px' }, [
            el('h3', { textContent: 'Add a repository' }),
            el('div', { style: 'display:flex;flex-wrap:wrap;align-items:flex-end' }, [
                labelled('Name', name), labelled('Location', location), labelled('Branch', branch),
                labelled('Publisher public key', key),
                labelled('Trusted', trusted), add,
            ]),
        ]);
    }

    function repoTable(data, root) {
        if (!data.repositories.length) {
            return el('div', { className: 'meta', textContent: 'No repository yet: add one below.' });
        }
        const offered = {};
        data.extensions.forEach((x) => { if (x.repo) offered[x.repo] = (offered[x.repo] || 0) + 1; });
        const table = el('table', { style: 'width:100%' }, [
            el('tr', {}, ['Name', 'Kind', 'Location', 'Key', 'Trusted', 'Extensions', ''].map(
                (h) => el('th', { textContent: h, style: 'text-align:left' }))),
        ]);
        data.repositories.forEach((r) => {
            table.appendChild(el('tr', {}, [
                el('td', {}, [el('strong', { textContent: r.name })]),
                el('td', { textContent: r.kind }),
                el('td', { textContent: r.location + (r.branch ? ' @ ' + r.branch : ''), style: 'word-break:break-all' }),
                el('td', { textContent: r.fingerprint || 'none' }),
                el('td', { textContent: r.trusted ? 'yes' : 'no' }),
                el('td', { textContent: String(offered[r.name] || 0) }),
                el('td', {}, [el('button', {
                    className: 'pill', textContent: 'Remove',
                    onclick: () => {
                        if (window.confirm('Remove the repository "' + r.name + '"? What it installed stays installed.')) {
                            act('/repo/remove', { name: r.name }, root);
                        }
                    },
                })]),
            ]));
        });
        return table;
    }

    // ---- Catalog: like Installed, a list with icons on the left, the details on the right ----

    const LOGO = '/static/synth_logo_bg.png';
    let selectedId = '';
    let catalogFilter = '';

    function iconFor(x) {
        const img = el('img', { className: 'plugin-banner-icon', alt: '' });
        img.src = x.installed ? BASE + '/icon/' + x.id : LOGO;
        img.onerror = () => { img.onerror = null; img.src = LOGO; };
        return img;
    }

    function tagsOf(x) {
        const tags = [];
        if (x.update) tags.push('update ' + x.installed + ' → ' + x.available);
        if (x.installed && !x.enabled) tags.push('off');
        if (!x.installed) tags.push('not installed');
        if (x.experimental) tags.push('experimental');
        if (x.default_install) tags.push('default');
        return tags;
    }

    function listItem(x, select) {
        const item = el('div', { className: 'plugin-banner' + (x.id === selectedId ? ' selected' : '') });
        item.setAttribute('role', 'option');
        item.tabIndex = 0;
        item.appendChild(iconFor(x));
        item.appendChild(el('span', { className: 'plugin-banner-name', textContent: x.name || x.id }));
        const controls = el('span', { className: 'plugin-banner-controls' });
        if (x.update) controls.appendChild(el('span', { className: 'plugins-update-count', textContent: '⬆' }));
        if (x.installed) {
            controls.appendChild(el('span', {
                className: 'plugin-led',
                title: x.enabled ? 'on' : 'off',
                style: 'width:10px;height:10px;border-radius:50%;display:inline-block;background:'
                    + (x.enabled ? 'var(--success,#18c98c)' : 'var(--muted,#888)'),
            }));
        }
        item.appendChild(controls);
        item.addEventListener('click', () => select(x.id));
        item.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); select(x.id); }
        });
        return item;
    }

    function detail(x, root) {
        const pane = el('div', { className: 'plugin-detail' });
        const head = el('div', { style: 'display:flex;align-items:center;gap:14px;margin-bottom:10px' });
        const icon = iconFor(x);
        icon.style.cssText = 'width:44px;height:44px';
        head.appendChild(icon);
        const title = el('div');
        title.appendChild(el('h3', { textContent: x.name || x.id, style: 'margin:0' }));
        title.appendChild(el('div', { className: 'meta', textContent: x.id }));
        head.appendChild(title);
        pane.appendChild(head);
        pane.appendChild(el('div', {
            className: 'plugin-detail-desc',
            textContent: x.description || 'No description.',
        }));
        if (x.authors && x.authors.length) {
            pane.appendChild(el('div', { className: 'plugin-detail-author meta', textContent: 'By ' + x.authors.join(' and ') }));
        }
        const facts = [
            ['Installed', x.installed || 'no'],
            ['Available', x.available ? x.available + (x.repo ? ' (' + x.repo + ')' : '') : '–'],
        ];
        const grid = el('dl', { style: 'display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:12px 0' });
        facts.forEach(([k, v]) => { grid.appendChild(el('dt', { textContent: k, className: 'meta' })); grid.appendChild(el('dd', { textContent: v, style: 'margin:0' })); });
        pane.appendChild(grid);
        const tags = tagsOf(x);
        if (tags.length) pane.appendChild(el('div', { className: 'plugins-update-count', textContent: tags.join(' · '), style: 'margin-bottom:10px' }));
        const buttons = el('div', { style: 'display:flex;flex-wrap:wrap;gap:8px' });
        const button = (label, path, body, confirmText) => buttons.appendChild(el('button', {
            className: 'pill', type: 'button', textContent: label,
            onclick: () => act(path, body, root, confirmText),
        }));
        if (!x.installed) {
            button('Install', '/install', { id: x.id });
        } else {
            if (x.update) button('Update to ' + x.available, '/update', { id: x.id });
            button(x.enabled ? 'Turn off' : 'Turn on', x.enabled ? '/disable' : '/enable', { id: x.id });
            button('Roll back', '/rollback', { id: x.id });
            button('Uninstall', '/remove', { id: x.id }, 'Its data is removed with it.');
        }
        pane.appendChild(buttons);
        return pane;
    }

    function catalogView(data, root) {
        const all = data.extensions.slice().sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
        if (!selectedId || !all.some((x) => x.id === selectedId)) selectedId = all.length ? all[0].id : '';
        const wrap = el('div', { className: 'catalog-two-col', style: 'display:grid;grid-template-columns:minmax(220px,340px) 1fr;gap:18px;align-items:start;margin-top:10px' });
        const left = el('div');
        const search = el('input', {
            type: 'search', placeholder: 'Search the catalog…', value: catalogFilter,
            className: 'plugins-search-input', style: 'margin-bottom:10px;width:100%',
        });
        const list = el('div', { className: 'plugin-list', style: 'display:flex;flex-direction:column;gap:6px' });
        list.setAttribute('role', 'listbox');
        const right = el('div', { className: 'plugin-detail-pane' });
        const draw = () => {
            list.textContent = '';
            const needle = catalogFilter.trim().toLowerCase();
            const shown = all.filter((x) => !needle || (x.name + ' ' + x.id + ' ' + x.description).toLowerCase().includes(needle));
            const groups = [['Updates', shown.filter((x) => x.update)],
                ['Installed', shown.filter((x) => x.installed && !x.update)],
                ['Available', shown.filter((x) => !x.installed)]];
            groups.forEach(([label, items]) => {
                if (!items.length) return;
                list.appendChild(el('div', { className: 'plugin-category-header', textContent: label, style: 'margin-top:6px' }));
                items.forEach((x) => list.appendChild(listItem(x, (id) => { selectedId = id; draw(); })));
            });
            if (!shown.length) list.appendChild(el('div', { className: 'meta', textContent: 'Nothing matches.' }));
            right.textContent = '';
            const current = all.find((x) => x.id === selectedId);
            right.appendChild(current ? detail(current, root) : el('div', { className: 'meta', textContent: 'Select an extension on the left to see what it is.' }));
        };
        search.addEventListener('input', () => { catalogFilter = search.value; draw(); });
        left.appendChild(search);
        left.appendChild(list);
        wrap.appendChild(left);
        wrap.appendChild(right);
        draw();
        return wrap;
    }

    async function overview(root, what) {
        try {
            return await call('');
        } catch (e) {
            lastError = what + ' could not be read (' + e.message + ').';
            root.textContent = '';
            return null;
        }
    }

    function problems(data, root) {
        (data.problems || []).forEach((p) => root.appendChild(el('div', { className: 'component-error', textContent: p })));
    }

    async function renderCatalog(root) {
        const data = await overview(root, 'The catalog');
        if (!data) {
            root.appendChild(el('div', { className: 'component-error', textContent: lastError }));
            return;
        }
        root.textContent = '';
        problems(data, root);
        if (!data.extensions.length) {
            root.appendChild(el('div', { className: 'meta', textContent: data.repositories.length
                ? 'Nothing is offered by your repositories.'
                : 'Nothing is offered yet: add a repository in the Repositories tab.' }));
            return;
        }
        root.appendChild(catalogView(data, root));
    }

    async function renderRepositories(root) {
        const data = await overview(root, 'The repositories');
        if (!data) {
            // Say why, and keep the way to add a repository: that is what fixes it.
            root.appendChild(el('div', { className: 'component-error', textContent: lastError }));
            root.appendChild(repoForm(root));
            return;
        }
        root.textContent = '';
        problems(data, root);
        root.appendChild(repoTable(data, root));
        root.appendChild(repoForm(root));
    }

    // Both views read the same overview; an action redraws whichever are on the page.
    function render() { start(true); }

    // ---- Updates: "Update all" and the count on the Installed tab, a button per plugin ----

    const available = new Map();      // extension id -> { installed, available }
    const updateButtons = new Set();  // { id, node }

    function refreshButtons() {
        updateButtons.forEach((entry) => {
            if (!entry.node.isConnected) { updateButtons.delete(entry); return; }
            const found = available.get(entry.id);
            entry.node.hidden = !found;
            if (found) entry.node.title = 'Update ' + entry.id + ' to ' + found.available + ' (now ' + found.installed + ')';
        });
    }

    function renderUpdateBar() {
        const bar = document.getElementById('plugins-update-bar');
        if (!bar) return;
        bar.textContent = '';
        const count = available.size;
        if (!count) return;
        bar.appendChild(el('span', {
            className: 'plugins-update-count',
            textContent: count + (count === 1 ? ' updatable plugin' : ' updatable plugins'),
        }));
        bar.appendChild(el('button', {
            type: 'button', className: 'pill', textContent: 'Update all',
            onclick: async (ev) => {
                const button = ev.currentTarget;
                button.disabled = true;
                try {
                    const result = await call('/update-all', {});
                    const failed = (result.results || []).filter((r) => !r.ok);
                    if (result.updated) toast('Updated ' + result.updated + (result.updated === 1 ? ' plugin' : ' plugins'));
                    failed.forEach((r) => toast(r.id + ': ' + (r.error || 'failed'), true));
                    if (!result.updated && !failed.length) toast(result.error || 'Nothing to update', !!result.error);
                } catch (e) {
                    toast('Update all: ' + e.message, true);
                }
                await loadUpdates(true);
                reloadComponents();
            },
        }));
    }

    function reloadComponents() {
        const hub = window.SynthWebUI;
        if (hub && typeof hub.loadEnginesSummary === 'function') hub.loadEnginesSummary();
        start(true);
    }

    async function loadUpdates(refresh) {
        try {
            const data = await call('/updates' + (refresh ? '?refresh=true' : ''));
            available.clear();
            (data.updates || []).forEach((u) => available.set(u.id, u));
        } catch (e) { /* the page just shows no update */ }
        renderUpdateBar();
        refreshButtons();
    }

    // The button of one plugin: hidden until its extension has an update.
    function updateButton(extensionId) {
        const node = el('button', { type: 'button', className: 'plugin-banner-update', hidden: true });
        node.setAttribute('aria-label', 'Update ' + extensionId);
        node.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M12 4l7 8h-4.5v8h-5v-8H5z" fill="currentColor"/></svg>';
        node.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            node.disabled = true;
            try {
                const result = await call('/update', { id: extensionId });
                if (result.ok) toast('Updated ' + extensionId);
                else toast(result.error || 'Update failed', true);
            } catch (e) {
                toast('Update: ' + e.message, true);
            }
            node.disabled = false;
            await loadUpdates(true);
            reloadComponents();
        });
        updateButtons.add({ id: extensionId, node });
        refreshButtons();
        return node;
    }

    // The red button of the Installed detail pane: removes the extension and its data.
    function uninstallButton(extensionId, label) {
        const node = el('button', { type: 'button', className: 'pill btn-danger', textContent: 'Uninstall' });
        node.style.marginTop = '8px';
        node.addEventListener('click', async () => {
            const what = label || extensionId;
            if (!window.confirm('Uninstall ' + what + '?\n\nIts files and its data are removed.')) return;
            node.disabled = true;
            try {
                const result = await call('/remove', { id: extensionId });
                if (result.ok) toast('Uninstalled ' + what);
                else toast(result.error || 'Uninstall failed', true);
            } catch (e) {
                toast('Uninstall: ' + e.message, true);
            }
            node.disabled = false;
            await loadUpdates(true);
            reloadComponents();
        });
        return node;
    }

    window.SynthExtensionUpdates = { button: updateButton, refresh: loadUpdates, uninstallButton };

    // The Plugins section is drawn again on every visit: fill the bar whenever it appears.
    let barTimer = null;
    new MutationObserver(() => {
        clearTimeout(barTimer);
        barTimer = setTimeout(() => {
            const bar = document.getElementById('plugins-update-bar');
            if (bar && !bar.dataset.ready) { bar.dataset.ready = '1'; renderUpdateBar(); loadUpdates(false); }
        }, 150);
    }).observe(document.documentElement, { childList: true, subtree: true });

    function start(refresh) {
        [['extension-catalog', renderCatalog], ['extension-repositories', renderRepositories]].forEach(([id, draw]) => {
            const host = document.getElementById(id);
            if (host && (refresh || !host.dataset.ready)) { host.dataset.ready = '1'; draw(host); }
        });
    }
    document.addEventListener('DOMContentLoaded', () => start(false));
    // Opening either tab shows the current state, not what it was at page load.
    document.addEventListener('click', (ev) => {
        const t = ev.target;
        if (t && t.matches && t.matches('.plugins-subnav-btn[data-plugins-subtab="catalog"], .plugins-subnav-btn[data-plugins-subtab="repositories"]')) {
            setTimeout(() => start(true), 0);
        }
    });
    start(false);
})();
