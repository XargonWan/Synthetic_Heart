/* Telegram (user account) login card.
 * Hooked into the interface detail pane through window.synthInterfaceDetailHooks
 * (see renderPluginDetail in main.js). Plain DOM building: no server data is
 * ever interpreted as HTML.
 */
(function () {
    'use strict';
    const BASE = '/api/telegram/login';
    window.synthInterfaceDetailHooks = window.synthInterfaceDetailHooks || {};

    async function call(path, body) {
        const opts = body === undefined
            ? { method: 'GET' }
            : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
        const resp = await fetch(BASE + '/' + path, opts);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
    }

    function el(tag, props, children) {
        const node = document.createElement(tag);
        Object.assign(node, props || {});
        (children || []).forEach((c) => node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
        return node;
    }

    function render(card, st) {
        card.textContent = '';
        const login = st.login || {};
        const fail = (msg) => { if (window.showToast) window.showToast(msg, true); };
        const act = (path, body) => async () => {
            try { render(card, await call(path, body && body())); } catch (e) { fail('Telegram: ' + e.message); }
        };

        card.appendChild(el('h4', { textContent: 'Account login', style: 'margin:0 0 6px' }));

        if (st.connected) {
            card.appendChild(el('div', { textContent: 'Connected as ' + (st.account || 'unknown account') + '.' }));
            card.appendChild(el('button', { className: 'pill', textContent: 'Log out', onclick: act('logout', () => ({})) }));
            return;
        }
        if (!st.has_credentials) {
            card.appendChild(el('div', {
                textContent: 'Set Telegram API ID and API Hash (from my.telegram.org) in the settings above, save, then reload the interface.'
            }));
            return;
        }
        if (login.error) card.appendChild(el('div', { className: 'component-error', textContent: login.error }));

        const input = (type, placeholder, value) => el('input', { type, placeholder, value: value || '', style: 'margin-right:6px' });
        if (login.state === 'code_sent') {
            card.appendChild(el('div', { textContent: 'Code sent to ' + (login.phone || 'your account') + ' (check the Telegram app or SMS).' }));
            const code = input('text', 'Login code');
            code.autocomplete = 'one-time-code';
            card.appendChild(code);
            card.appendChild(el('button', { className: 'pill', textContent: 'Verify', onclick: act('verify_code', () => ({ code: code.value })) }));
            card.appendChild(el('button', { className: 'pill', textContent: 'Cancel', onclick: act('cancel', () => ({})) }));
        } else if (login.state === 'password_needed') {
            card.appendChild(el('div', { textContent: 'Two-step verification is enabled: enter your Telegram password.' }));
            const pwd = input('password', 'Password');
            pwd.autocomplete = 'current-password';
            card.appendChild(pwd);
            card.appendChild(el('button', { className: 'pill', textContent: 'Verify', onclick: act('verify_password', () => ({ password: pwd.value })) }));
            card.appendChild(el('button', { className: 'pill', textContent: 'Cancel', onclick: act('cancel', () => ({})) }));
        } else if (login.state === 'authorized' || st.has_session) {
            card.appendChild(el('div', { textContent: 'Logged in. Reload the interface to connect.' }));
            card.appendChild(el('button', { className: 'pill', textContent: 'Log out', onclick: act('logout', () => ({})) }));
        } else {
            const phone = input('tel', '+391234567890', st.phone);
            card.appendChild(phone);
            card.appendChild(el('button', { className: 'pill', textContent: 'Send code', onclick: act('send_code', () => ({ phone: phone.value })) }));
        }
    }

    window.synthInterfaceDetailHooks.telegram = function (item, pane) {
        const card = el('div', { className: 'plugin-detail-desc', style: 'margin:10px 0;padding:10px;border:1px solid var(--border-color,#8884);border-radius:8px' });
        pane.appendChild(card);
        call('status').then((st) => render(card, st)).catch(() => {
            card.textContent = 'Login panel unavailable.';
        });
    };
})();
