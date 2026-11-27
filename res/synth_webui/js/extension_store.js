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

    function row(x, root) {
        const buttons = [];
        const button = (label, path, body, confirmText) => el('button', {
            className: 'pill', textContent: label, style: 'margin-right:4px',
            onclick: () => act(path, body, root, confirmText),
        });
        if (!x.installed) {
            buttons.push(button('Install', '/install', { id: x.id }));
        } else {
            if (x.update) buttons.push(button('Update', '/update', { id: x.id }));
            buttons.push(button(x.enabled ? 'Turn off' : 'Turn on', x.enabled ? '/disable' : '/enable', { id: x.id }));
            buttons.push(button('Roll back', '/rollback', { id: x.id }));
            buttons.push(button('Uninstall', '/remove', { id: x.id }));
        }
        const tags = [];
        if (x.experimental) tags.push('experimental');
        if (x.default_install) tags.push('default');
        if (x.installed && !x.enabled) tags.push('off');
        return el('tr', {}, [
            el('td', {}, [el('strong', { textContent: x.name || x.id }), el('div', { className: 'meta', textContent: x.id })]),
            el('td', { textContent: x.installed || '-' }),
            el('td', { textContent: (x.available || '-') + (x.repo ? ' (' + x.repo + ')' : '') }),
            el('td', { textContent: tags.join(', ') }),
            el('td', {}, buttons),
        ]);
    }

    let lastError = '';

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
        const table = el('table', { style: 'width:100%' }, [
            el('tr', {}, ['Extension', 'Installed', 'Available', '', ''].map((h) => el('th', { textContent: h, style: 'text-align:left' }))),
        ]);
        data.extensions.forEach((x) => table.appendChild(row(x, root)));
        root.appendChild(table);
        if (!data.extensions.length) {
            root.appendChild(el('div', { className: 'meta', textContent: data.repositories.length
                ? 'Nothing is offered by your repositories.'
                : 'Nothing is offered yet: add a repository in the Repositories tab.' }));
        }
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

    window.SynthExtensionUpdates = { button: updateButton, refresh: loadUpdates };

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
