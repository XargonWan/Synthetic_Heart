/* Plugins > Store: repositories and extensions (core/extensions/webui_store.py).
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
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
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
            toast('Store: ' + e.message, true);
        }
        render(root);
    }

    function repoForm(root) {
        const field = (placeholder, width) => el('input', { type: 'text', placeholder, style: 'margin:0 6px 6px 0;width:' + width });
        const name = field('name', '8em');
        const location = field('folder, https:// URL or Git URL', '22em');
        const key = field('publisher public key (base64)', '22em');
        const trusted = el('input', { type: 'checkbox' });
        const add = el('button', {
            className: 'pill', textContent: 'Add repository',
            onclick: () => act('/repo/add', {
                name: name.value, location: location.value, key: key.value, trusted: trusted.checked,
            }, root),
        });
        return el('div', { style: 'margin:8px 0' }, [
            name, location, key,
            el('label', { style: 'margin-right:8px' }, [trusted, ' trusted']), add,
        ]);
    }

    function repoList(data, root) {
        const wrap = el('div');
        if (!data.repositories.length) {
            wrap.appendChild(el('div', { className: 'meta', textContent: 'No repository yet: add one below.' }));
        }
        data.repositories.forEach((r) => {
            wrap.appendChild(el('div', { style: 'margin:3px 0' }, [
                el('strong', { textContent: r.name }),
                ' ' + r.kind + ', ' + (r.fingerprint ? 'key ' + r.fingerprint : 'no key') + ', '
                    + (r.trusted ? 'trusted' : 'untrusted') + ', ' + r.location + ' ',
                el('button', {
                    className: 'pill', textContent: 'Remove',
                    onclick: () => act('/repo/remove', { name: r.name }, root),
                }),
            ]));
        });
        return wrap;
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

    async function render(root) {
        let data;
        try {
            data = await call('');
        } catch (e) {
            root.textContent = 'Store unavailable.';
            return;
        }
        root.textContent = '';
        root.appendChild(el('h3', { textContent: 'Repositories' }));
        root.appendChild(repoList(data, root));
        root.appendChild(repoForm(root));
        (data.problems || []).forEach((p) => root.appendChild(el('div', { className: 'component-error', textContent: p })));
        root.appendChild(el('h3', { textContent: 'Extensions' }));
        const table = el('table', { style: 'width:100%' }, [
            el('tr', {}, ['Extension', 'Installed', 'Available', '', ''].map((h) => el('th', { textContent: h, style: 'text-align:left' }))),
        ]);
        data.extensions.forEach((x) => table.appendChild(row(x, root)));
        root.appendChild(table);
        if (!data.extensions.length) {
            root.appendChild(el('div', { className: 'meta', textContent: 'Nothing is offered yet.' }));
        }
    }

    function start(refresh) {
        const host = document.getElementById('extension-store');
        if (host && (refresh || !host.dataset.ready)) { host.dataset.ready = '1'; render(host); }
    }
    document.addEventListener('DOMContentLoaded', () => start(false));
    // Opening the Store tab shows the current state, not what it was at page load.
    document.addEventListener('click', (ev) => {
        const t = ev.target;
        if (t && t.matches && t.matches('.plugins-subnav-btn[data-plugins-subtab="store"]')) setTimeout(() => start(true), 0);
    });
    start(false);
})();
