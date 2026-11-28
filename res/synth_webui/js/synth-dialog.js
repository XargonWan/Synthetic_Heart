// synth-dialog.js: questions and notices as a modal INSIDE Synth, never the browser's own
// confirm()/alert() box (which says "localhost:8000 says" and sits outside the page).
//
//   const ok = await SynthDialog.confirm({
//       title: 'Uninstall Telegram?', message: 'Its files and data are removed.',
//       details: ['log', 'config'],           // optional: shown as a list of chips
//       confirmLabel: 'Uninstall', danger: true,
//   });
//   await SynthDialog.alert('Something went wrong', 'Error');
(function () {
    'use strict';
    if (window.SynthDialog) return;

    function el(tag, props, children) {
        const node = document.createElement(tag);
        Object.keys(props || {}).forEach((key) => {
            if (key === 'style') node.style.cssText = props[key];
            else if (key in node) node[key] = props[key];
            else node.setAttribute(key, props[key]);
        });
        (children || []).forEach((child) => node.appendChild(child));
        return node;
    }

    // One dialog at a time; a second request waits for the first.
    let queue = Promise.resolve();

    function show(options) {
        return new Promise((resolve) => {
            const opts = options || {};
            const previous = document.activeElement;
            const veil = el('div', {
                className: 'synth-dialog-veil',
                style: 'position:fixed;inset:0;z-index:70000;display:flex;align-items:center;justify-content:center;'
                    + 'padding:16px;background:rgba(0,0,0,.6);',
            });
            const card = el('div', {
                className: 'synth-dialog card',
                style: 'width:min(460px,100%);max-height:90vh;overflow:auto;padding:20px 22px;border-radius:14px;'
                    + 'background:var(--surface-alt,#1b1b2a);color:var(--text,#eee);border:1px solid var(--accent,#4f46e5);'
                    + 'box-shadow:0 24px 60px -20px rgba(0,0,0,.7);',
            });
            card.setAttribute('role', 'dialog');
            card.setAttribute('aria-modal', 'true');
            const titleId = 'synth-dialog-title-' + Math.random().toString(36).slice(2, 8);
            card.setAttribute('aria-labelledby', titleId);
            if (opts.title) card.appendChild(el('h3', { id: titleId, textContent: opts.title, style: 'margin:0 0 10px;font-size:1.05rem' }));
            if (opts.message) card.appendChild(el('div', { textContent: opts.message, style: 'white-space:pre-wrap;line-height:1.45;font-size:.92rem' }));
            if (opts.details && opts.details.length) {
                const chips = el('div', { style: 'display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 4px' });
                opts.details.forEach((d) => chips.appendChild(el('code', {
                    textContent: d,
                    style: 'padding:3px 8px;border-radius:999px;background:rgba(255,255,255,.08);font-size:.8rem',
                })));
                card.appendChild(chips);
            }
            if (opts.question) card.appendChild(el('div', { textContent: opts.question, style: 'margin-top:12px;font-weight:600;font-size:.92rem' }));
            const row = el('div', { style: 'display:flex;justify-content:flex-end;gap:10px;margin-top:18px' });
            const finish = (value) => {
                document.removeEventListener('keydown', onKey, true);
                veil.remove();
                if (previous && previous.focus) { try { previous.focus(); } catch (e) { /* gone */ } }
                resolve(value);
            };
            let cancel = null;
            if (!opts.noCancel) {
                cancel = el('button', { type: 'button', className: 'pill secondary', textContent: opts.cancelLabel || 'Cancel' });
                cancel.addEventListener('click', () => finish(false));
                row.appendChild(cancel);
            }
            const ok = el('button', {
                type: 'button',
                className: opts.danger ? 'pill btn-danger' : 'pill',
                textContent: opts.confirmLabel || 'OK',
            });
            ok.addEventListener('click', () => finish(true));
            row.appendChild(ok);
            card.appendChild(row);
            veil.appendChild(card);
            const onKey = (event) => {
                if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(false); }
                else if (event.key === 'Enter' && document.activeElement !== cancel) { event.preventDefault(); finish(true); }
                else if (event.key === 'Tab') {
                    const buttons = [cancel, ok].filter(Boolean);
                    const at = buttons.indexOf(document.activeElement);
                    event.preventDefault();
                    buttons[(at + (event.shiftKey ? buttons.length - 1 : 1)) % buttons.length].focus();
                }
            };
            document.addEventListener('keydown', onKey, true);
            veil.addEventListener('mousedown', (event) => { if (event.target === veil && !opts.noCancel) finish(false); });
            document.body.appendChild(veil);
            ok.focus();
        });
    }

    function enqueue(options) {
        const run = queue.then(() => show(options));
        queue = run.catch(() => undefined);
        return run;
    }

    window.SynthDialog = {
        confirm: (options) => enqueue(options),
        alert: (message, title) => enqueue({ title: title || '', message: String(message), noCancel: true }).then(() => undefined),
    };
})();
