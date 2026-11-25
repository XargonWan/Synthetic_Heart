// Runs res/synth_webui/js/turn_cards.js against a minimal DOM and prints what it drew.
const fs = require('fs');
const path = require('path');

class Node {
    constructor(tag) {
        this.tagName = tag; this.children = []; this.className = ''; this._text = ''; this.dataset = {};
        this.attrs = {}; this.listeners = {}; this.hidden = false; this.id = '';
        const self = this;
        this.classList = {
            toggle(name, force) {
                const set = new Set(self.className.split(/\s+/).filter(Boolean));
                const on = force === undefined ? !set.has(name) : force;
                if (on) set.add(name); else set.delete(name);
                self.className = Array.from(set).join(' ');
            },
        };
    }
    set textContent(v) { this._text = String(v); if (v === '') this.children = []; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
    appendChild(c) { this.children.push(c); return c; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
    click() { (this.listeners.click || []).forEach((f) => f({})); }
    find(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach((c) => c.find && c.find(pred, out)); return out; }
}
const ids = {};
global.document = {
    createElement: (tag) => new Node(tag),
    createTextNode: (t) => { const n = new Node('#text'); n._text = t; return n; },
    getElementById: (id) => ids[id] || null,
    head: { appendChild: (n) => { ids[n.id] = n; } },
};
const fetched = [];
global.fetch = async (url) => {
    fetched.push(url);
    const turns = url.startsWith('/api/turns/live') ? [processing] : [done, failed];
    return { json: async () => ({ success: true, turns }) };
};
global.window = global;
const processing = { id: 'p1', status: 'processing', kind: 'chat', interface: 'synth_webui', interface_path: 'synth_webui/s1',
    chat_name: 'Session', priority_label: 'General', merged: 1, queue_seconds: 0.4, processing_seconds: 3.25,
    trigger: { text: 'hello there' }, corrector: { turns: 0, converged: null }, attempts: 0, reply: null, error: null,
    sections: [{ name: 'recon', seconds: 0.2, count: 1, data: { contributions: 3 } }] };
const done = { ...processing, id: 'd1', status: 'processed', finished_at: 1700000100, started_at: 1700000000, queued_at: 1699999999,
    processing_seconds: 12.5, corrector: { turns: 2, converged: true }, attempts: 3,
    reply: { text: '{"actions": [{"type": "send_message"}]}', valid: true },
    sections: [{ name: 'body', seconds: 9.1, count: 1, data: { engine: 'x' } }, { name: 'memories', seconds: 0, count: 1, data: { recalled: [{ id: 1 }] } }] };
const failed = { ...processing, id: 'f1', status: 'failed', processing_seconds: 61, error: 'the engine timed out',
    reply: { text: 'not json at all', valid: false }, sections: [] };

vm = require('vm');
vm.runInThisContext(fs.readFileSync(path.join(__dirname, '../../res/synth_webui/js/turn_cards.js'), 'utf8'));
const T = window.SynthTurnCards;
const out = {};

const card = T.renderCard(done);
out.pillText = card.find((n) => n.className === 'tc-pill')[0].textContent;
out.rootClass = card.className;
out.preview = card.find((n) => n.className === 'tc-preview')[0].textContent;
out.sectionTitles = card.find((n) => n.className === 'tc-section-title').map((n) => n.textContent);
const body = card.children[1];
out.collapsedAtFirst = body.hidden;
card.find((n) => n.className === 'tc-head')[0].click();
out.openAfterClick = !body.hidden;
out.openClass = card.className.includes('tc-open');
out.jsonKeys = card.find((n) => n.className === 'k').map((n) => n.textContent);
out.replyNote = card.find((n) => n.className.startsWith('tc-reply-note'))[0].textContent;
out.statsText = card.find((n) => n.className === 'tc-stats')[0].textContent;

const f = T.renderCard(failed);
out.failedPill = f.find((n) => n.className === 'tc-pill')[0].textContent;
out.failedClass = f.className;
out.failedNote = f.find((n) => n.className.startsWith('tc-reply-note'))[0].textContent;
out.failedError = f.find((n) => n.className === 'tc-error')[0].textContent;

const live = T.renderCard(processing);
out.liveClass = live.className;

// A hostile value is text, never markup.
const evil = T.renderCard({ ...done, id: 'e1', trigger: { text: '<img src=x onerror=alert(1)>' }, sections: [] });
out.evilPreview = evil.find((n) => n.className === 'tc-preview')[0].textContent;
out.evilTags = evil.find((n) => n.tagName === 'img').length;

// The expanded state survives a redraw.
const again = T.renderCard(done);
out.stillOpen = !again.children[1].hidden;

(async () => {
    const holder = new Node('div'); ids['live'] = holder;
    await T.loadLive(holder);
    out.liveHidden = holder.hidden; out.liveTitle = holder.find((n) => n.className === 'tc-group-title')[0].textContent;
    const list = new Node('div');
    await T.loadFinished(list, { status: 'failed', search: 'x y', limit: 5 });
    out.finishedCount = list.find((n) => n.tagName === 'article').length;
    out.requested = fetched;
    const empty = new Node('div');
    global.fetch = async () => ({ json: async () => ({ success: true, turns: [] }) });
    await T.loadFinished(empty, {});
    out.emptyText = empty.textContent;
    global.fetch = async () => { throw new Error('down'); };
    const broken = new Node('div');
    await T.loadFinished(broken, {});
    out.brokenText = broken.textContent;
    console.log(JSON.stringify(out));
})();
