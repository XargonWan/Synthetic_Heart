/* Turn cards: one expandable card per turn (core/turn_cards.py).
 *
 * A card is born when the message queue hands an item to the consumer, is filled in as the
 * turn moves (recon, body, corrector, actions, debrief, emotions, recalled memories, each
 * with the time it took) and, once over, stays in Activity > Turns labelled "processed" or
 * "failed". Collapsed, a card is one line like a queue entry; expanded it shows everything.
 *
 * Everything is built with DOM calls and textContent: nothing a turn contains is ever read
 * as HTML or script.
 */
(function () {
    'use strict';

    const SECTION_TITLES = {
        trigger: 'Trigger',
        recon: 'Recon',
        body: 'Body (LLM)',
        corrector: 'Corrector',
        actions: 'Actions',
        debrief: 'Debrief',
        emotions: 'Emotions',
        memories: 'Recalled memories',
        agent: 'Agent',
    };
    const STATUS_LABELS = { processing: 'Processing', processed: 'Processed', failed: 'Failed', queued: 'Queued' };
    const expanded = new Set();

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function seconds(value) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '–';
        const n = Number(value);
        if (n < 1) return Math.round(n * 1000) + ' ms';
        if (n < 100) return n.toFixed(1) + ' s';
        const m = Math.floor(n / 60);
        return m + ' min ' + Math.round(n - m * 60) + ' s';
    }

    function when(epoch) {
        if (!epoch) return '–';
        try { return new Date(epoch * 1000).toLocaleString(); } catch (e) { return String(epoch); }
    }

    function injectStyles() {
        if (document.getElementById('turn-cards-styles')) return;
        const style = document.createElement('style');
        style.id = 'turn-cards-styles';
        style.textContent = `
        .tc-list { display: flex; flex-direction: column; gap: 10px; }
        .tc-group-title { margin: 14px 0 6px; font-size: .8rem; letter-spacing: .08em; text-transform: uppercase; color: var(--text-soft, #b9badc); }
        .tc-card { --tc-accent: var(--accent, #6bfefe); border: 1px solid var(--border, rgba(255,255,255,.1)); border-left: 4px solid var(--tc-accent);
            border-radius: 14px; background: var(--surface, rgba(24,24,36,.9)); overflow: hidden; }
        .tc-card.tc-processing { --tc-accent: var(--warn, #f3c04d); }
        .tc-card.tc-processed { --tc-accent: var(--success, #18c98c); }
        .tc-card.tc-failed { --tc-accent: var(--error, #ff7b93); }
        .tc-card.tc-queued { --tc-accent: var(--text-soft, #b9badc); }
        .tc-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; padding: 10px 14px; cursor: pointer; user-select: none; }
        .tc-head:hover { background: var(--accent-soft, rgba(107,254,254,.08)); }
        .tc-chevron { width: 1em; color: var(--text-soft, #b9badc); transition: transform .15s ease; }
        .tc-card.tc-open .tc-chevron { transform: rotate(90deg); }
        .tc-pill { padding: 2px 10px; border-radius: 999px; font-size: .72rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
            color: #0d0d16; background: var(--tc-accent); }
        .tc-processing .tc-pill { animation: tc-pulse 1.4s ease-in-out infinite; }
        @keyframes tc-pulse { 50% { opacity: .55; } }
        .tc-kind { padding: 1px 8px; border-radius: 8px; font-size: .72rem; border: 1px solid var(--border, rgba(255,255,255,.18)); color: var(--text-soft, #b9badc); }
        .tc-source { font-size: .85rem; color: var(--text-soft, #b9badc); }
        .tc-preview { flex: 1 1 260px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text, #f5f5ff); }
        .tc-stats { display: flex; gap: 10px; font-size: .78rem; color: var(--text-soft, #b9badc); white-space: nowrap; }
        .tc-stat b { color: var(--text, #f5f5ff); font-weight: 600; }
        .tc-body { padding: 4px 14px 14px; display: flex; flex-direction: column; gap: 12px; border-top: 1px solid var(--border, rgba(255,255,255,.08)); }
        .tc-body[hidden] { display: none; }
        .tc-section { border: 1px solid var(--border, rgba(255,255,255,.08)); border-radius: 10px; background: var(--surface-alt, rgba(18,18,28,.75)); overflow: hidden; }
        .tc-section-head { display: flex; align-items: baseline; gap: 10px; padding: 6px 12px; background: rgba(255,255,255,.04);
            border-bottom: 1px solid var(--border, rgba(255,255,255,.08)); }
        .tc-section-title { font-weight: 700; color: var(--tc-accent); }
        .tc-section-meta { font-size: .78rem; color: var(--text-soft, #b9badc); }
        .tc-section-body { padding: 8px 12px; }
        .tc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 14px; }
        .tc-grid dt { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--text-soft, #b9badc); }
        .tc-grid dd { margin: 0; font-weight: 600; }
        .tc-json { margin: 0; max-height: 360px; overflow: auto; white-space: pre-wrap; word-break: break-word; font: .8rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
        .tc-json .k { color: var(--accent, #6bfefe); } .tc-json .s { color: #9ad69a; } .tc-json .n { color: #f3c04d; }
        .tc-json .b { color: #c79bff; } .tc-json .z { color: var(--text-soft, #b9badc); } .tc-json .p { color: var(--text-soft, #b9badc); }
        .tc-reply-note { font-size: .78rem; margin-bottom: 6px; }
        .tc-reply-note.ok { color: var(--success, #18c98c); } .tc-reply-note.last { color: var(--warn, #f3c04d); }
        .tc-reply-text { white-space: pre-wrap; word-break: break-word; }
        .tc-error { color: var(--error, #ff7b93); white-space: pre-wrap; }
        .tc-empty { padding: 24px; text-align: center; color: var(--text-soft, #b9badc); }
        @media (max-width: 640px) { .tc-stats { width: 100%; flex-wrap: wrap; } .tc-preview { flex-basis: 100%; } }
        `;
        document.head.appendChild(style);
    }

    /* A formatted, coloured JSON value. */
    function jsonNode(value) {
        const pre = el('pre', 'tc-json');
        const put = (text, cls) => { const s = el('span', cls, text); pre.appendChild(s); };
        const walk = (v, depth) => {
            const pad = '  '.repeat(depth);
            if (v === null) return put('null', 'z');
            if (typeof v === 'string') return put(JSON.stringify(v), 's');
            if (typeof v === 'number') return put(String(v), 'n');
            if (typeof v === 'boolean') return put(String(v), 'b');
            if (Array.isArray(v)) {
                if (!v.length) return put('[]', 'p');
                put('[\n', 'p');
                v.forEach((item, i) => {
                    put(pad + '  ', 'p'); walk(item, depth + 1);
                    put(i < v.length - 1 ? ',\n' : '\n', 'p');
                });
                return put(pad + ']', 'p');
            }
            const keys = Object.keys(v || {});
            if (!keys.length) return put('{}', 'p');
            put('{\n', 'p');
            keys.forEach((key, i) => {
                put(pad + '  ', 'p'); put(JSON.stringify(key), 'k'); put(': ', 'p'); walk(v[key], depth + 1);
                put(i < keys.length - 1 ? ',\n' : '\n', 'p');
            });
            return put(pad + '}', 'p');
        };
        walk(value, 0);
        return pre;
    }

    function section(title, meta, body, extraClass) {
        const box = el('section', 'tc-section' + (extraClass ? ' ' + extraClass : ''));
        const head = el('div', 'tc-section-head');
        head.appendChild(el('span', 'tc-section-title', title));
        if (meta) head.appendChild(el('span', 'tc-section-meta', meta));
        box.appendChild(head);
        const inner = el('div', 'tc-section-body');
        inner.appendChild(body);
        box.appendChild(inner);
        return box;
    }

    function summary(card) {
        const grid = el('dl', 'tc-grid');
        const rows = [
            ['Queued', when(card.queued_at)],
            ['Started', when(card.started_at)],
            ['Finished', card.finished_at ? when(card.finished_at) : 'running'],
            ['Waited in queue', seconds(card.queue_seconds)],
            [card.status === 'processing' ? 'Running for' : 'Processing', seconds(card.processing_seconds)],
            ['Priority', card.priority_label || '–'],
            ['Merged items', card.merged > 1 ? card.merged : '1'],
            ['LLM answers', card.attempts],
            ['Corrector turns', card.corrector ? card.corrector.turns : 0],
            ['Corrected', card.corrector && card.corrector.converged !== null ? (card.corrector.converged ? 'yes' : 'no') : '–'],
        ];
        rows.forEach(([name, value]) => {
            const wrap = el('div');
            wrap.appendChild(el('dt', '', name));
            wrap.appendChild(el('dd', '', value));
            grid.appendChild(wrap);
        });
        return grid;
    }

    // Why nothing came back: the model call that failed (engine and reason), the error that
    // ended the turn, or - when neither was recorded - that the model was never asked.
    function noAnswerNote(card) {
        const failures = Array.isArray(card.failures) ? card.failures : [];
        if (failures.length) {
            const last = failures[failures.length - 1];
            const who = last.engine ? ' (' + last.engine + ')' : '';
            const more = failures.length > 1 ? ' [' + failures.length + ' failures]' : '';
            return 'No answer arrived: the model call failed' + who + ': ' + last.reason + more;
        }
        if (card.error) return 'No answer arrived: ' + card.error;
        if (card.status === 'processing' || card.status === 'queued') return 'No answer yet.';
        if (!card.attempts) return 'No answer arrived: the model was not asked in this turn.';
        return 'No answer arrived.';
    }

    function replyNode(card) {
        const box = el('div');
        if (!card.reply) {
            box.appendChild(el('div', 'tc-reply-note last', noAnswerNote(card)));
            return box;
        }
        box.appendChild(el(
            'div',
            'tc-reply-note ' + (card.reply.valid ? 'ok' : 'last'),
            card.reply.valid
                ? 'The correct answer.'
                : 'No correct answer came: this is the last one that arrived.'
        ));
        let parsed = null;
        try { parsed = JSON.parse(card.reply.text); } catch (e) { parsed = null; }
        if (parsed !== null && typeof parsed === 'object') box.appendChild(jsonNode(parsed));
        else box.appendChild(el('div', 'tc-reply-text', card.reply.text));
        return box;
    }

    function body(card) {
        const wrap = el('div', 'tc-body');
        wrap.appendChild(section('Summary', null, summary(card)));
        if (card.trigger && Object.keys(card.trigger).length) {
            wrap.appendChild(section(SECTION_TITLES.trigger, card.interface_path || card.interface, jsonNode(card.trigger)));
        }
        (card.sections || []).forEach((item) => {
            const meta = [];
            if (item.seconds) meta.push(seconds(item.seconds));
            if (item.count > 1) meta.push(item.count + '×');
            const hasData = item.data && Object.keys(item.data).length;
            wrap.appendChild(section(
                SECTION_TITLES[item.name] || item.name,
                meta.join(' · '),
                hasData ? jsonNode(item.data) : el('span', 'tc-section-meta', 'timing only')
            ));
        });
        wrap.appendChild(section('Reply', null, replyNode(card)));
        if (card.error) wrap.appendChild(section('Error', null, el('div', 'tc-error', card.error)));
        return wrap;
    }

    function renderCard(card) {
        const open = expanded.has(card.id);
        const root = el('article', `tc-card tc-${card.status}` + (open ? ' tc-open' : ''));
        root.dataset.id = card.id;
        const head = el('div', 'tc-head');
        head.setAttribute('role', 'button');
        head.setAttribute('tabindex', '0');
        head.setAttribute('aria-expanded', open ? 'true' : 'false');
        head.appendChild(el('span', 'tc-chevron', '▸'));
        head.appendChild(el('span', 'tc-pill', STATUS_LABELS[card.status] || card.status));
        head.appendChild(el('span', 'tc-kind', (card.kind || 'chat').replace(/_/g, ' ')));
        const source = [card.interface_path || card.interface, card.chat_name].filter(Boolean).join(' · ');
        if (source) head.appendChild(el('span', 'tc-source', source));
        const text = card.trigger && (card.trigger.text || card.trigger.description) || '';
        head.appendChild(el('span', 'tc-preview', text || '(no text)'));
        const stats = el('span', 'tc-stats');
        const stat = (label, value) => {
            const s = el('span', 'tc-stat'); s.appendChild(document.createTextNode(label + ' '));
            s.appendChild(el('b', '', value)); stats.appendChild(s);
        };
        stat('⏱', seconds(card.processing_seconds));
        if (card.queue_seconds !== null && card.queue_seconds !== undefined) stat('wait', seconds(card.queue_seconds));
        if (card.corrector && card.corrector.turns) stat('corrector', card.corrector.turns);
        head.appendChild(stats);
        root.appendChild(head);
        const panel = body(card);
        panel.hidden = !open;
        root.appendChild(panel);
        const toggle = () => {
            const now = panel.hidden;
            panel.hidden = !now;
            root.classList.toggle('tc-open', now);
            head.setAttribute('aria-expanded', now ? 'true' : 'false');
            if (now) expanded.add(card.id); else expanded.delete(card.id);
        };
        head.addEventListener('click', toggle);
        head.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
        });
        return root;
    }

    function renderCards(container, cards, emptyText) {
        injectStyles();
        container.textContent = '';
        if (!cards.length) { container.appendChild(el('div', 'tc-empty', emptyText)); return; }
        const list = el('div', 'tc-list');
        cards.forEach((card) => list.appendChild(renderCard(card)));
        container.appendChild(list);
    }

    async function loadLive(container) {
        if (!container) return;
        try {
            const response = await fetch('/api/turns/live');
            const data = await response.json();
            const cards = data && data.success ? data.turns : [];
            container.hidden = !cards.length;
            container.textContent = '';
            if (cards.length) {
                injectStyles();
                container.appendChild(el('div', 'tc-group-title', 'Running now'));
                renderCards(container.appendChild(el('div')), cards, '');
            }
        } catch (e) { container.hidden = true; }
    }

    async function loadFinished(container, state) {
        if (!container) return;
        const params = new URLSearchParams({ limit: String((state && state.limit) || 100) });
        if (state && state.status) params.set('status', state.status);
        if (state && state.search) params.set('search', state.search);
        try {
            const response = await fetch('/api/turns?' + params.toString());
            const data = await response.json();
            if (!data || !data.success) throw new Error((data && data.error) || 'failed');
            renderCards(container, data.turns, 'No turns yet. They appear here once the Synth has handled a message, a beat or an action.');
        } catch (e) {
            injectStyles();
            container.textContent = '';
            container.appendChild(el('div', 'tc-empty', 'Failed to load the turns.'));
        }
    }

    window.SynthTurnCards = { renderCard, renderCards, loadLive, loadFinished, jsonNode, seconds };
})();
