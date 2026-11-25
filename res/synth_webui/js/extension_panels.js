/* Setup panels drawn for extensions (core/extensions/panels.py).
 * The extension sends data, never code: every node here is built with DOM calls and
 * textContent, so nothing it sends is interpreted as HTML or script.
 * Hooked in through window.synthExtensionPanels, called by the interface detail pane.
 */
(function () {
    'use strict';
    const BASE = '/api/extensions/panels';

    async function request(path, body) {
        const opts = body === undefined
            ? { method: 'GET' }
            : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
        const resp = await fetch(BASE + '/' + path, opts);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
    }

    function el(tag, props) {
        const node = document.createElement(tag);
        Object.assign(node, props || {});
        return node;
    }

    function draw(card, item, entry) {
        const state = entry.state || {};
        card.textContent = '';
        if (state.title) card.appendChild(el('h4', { textContent: state.title, style: 'margin:0 0 6px' }));
        const fail = (msg) => { if (window.showToast) window.showToast(msg, true); };
        const send = async (action, values) => {
            try {
                const result = await request(
                    encodeURIComponent(item.name) + '/' + encodeURIComponent(entry.panel) + '/action',
                    { action, values: values || {} });
                if (result && result.ok === false && result.error) fail(result.error);
            } catch (e) { fail('Panel: ' + e.message); }
            refresh(card, item);
        };
        (state.blocks || []).forEach((block) => {
            if (block.type === 'text') {
                card.appendChild(el('div', { textContent: block.text }));
            } else if (block.type === 'error') {
                card.appendChild(el('div', { className: 'component-error', textContent: block.text }));
            } else if (block.type === 'button') {
                card.appendChild(el('button', { className: 'pill', textContent: block.label, onclick: () => send(block.action) }));
            } else if (block.type === 'form') {
                const inputs = {};
                (block.fields || []).forEach((field) => {
                    const input = el('input', {
                        type: field.kind === 'password' ? 'password' : field.kind === 'tel' ? 'tel' : 'text',
                        placeholder: field.placeholder || field.label || '',
                        value: field.value || '',
                        style: 'margin-right:6px',
                    });
                    if (field.kind === 'code') input.autocomplete = 'one-time-code';
                    if (field.kind === 'password') input.autocomplete = 'current-password';
                    inputs[field.name] = input;
                    card.appendChild(input);
                });
                card.appendChild(el('button', {
                    className: 'pill', textContent: block.submit,
                    onclick: () => {
                        const values = {};
                        Object.keys(inputs).forEach((k) => { values[k] = inputs[k].value; });
                        send(block.action, values);
                    },
                }));
            }
        });
    }

    function refresh(host, item) {
        request(encodeURIComponent(item.name)).then((data) => {
            host.textContent = '';
            (data.panels || []).forEach((entry) => {
                const card = el('div', {
                    className: 'plugin-detail-desc',
                    style: 'margin:10px 0;padding:10px;border:1px solid var(--border-color,#8884);border-radius:8px',
                });
                host.appendChild(card);
                draw(card, item, entry);
            });
        }).catch(() => { host.textContent = ''; });
    }

    window.synthExtensionPanels = function (item, pane) {
        const host = el('div');
        pane.appendChild(host);
        refresh(host, item);
    };
})();
