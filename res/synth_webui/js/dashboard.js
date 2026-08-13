/**
 * dashboard.js — "Control Deck" Dashboard tab for the SyntH WebUI.
 *
 * Renders every adjustable config value as a CNC-style control:
 * knobs for numbers, pink toggles for booleans, selects, chip editors,
 * color pickers, JSON textareas and plain inputs — grouped per component,
 * with a sidebar index, live readouts, plugin run/toggle commands and an
 * agent prompt runner.
 *
 * Registered as window.SynthWebUI.initDashboardTab so main.js loadSection()
 * invokes it automatically when the tab is shown. The shared DOM ids live in
 * core/webui_templates/sections/dashboard.html.
 */
(function () {
    'use strict';

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    var _items = [];             // config items from /api/config
    var _groups = [];            // computed group descriptors
    var _keyToGroup = {};        // item.key -> group key
    var _components = [];        // plugins from /api/components
    var _about = null;           // from /api/about
    var _emotion = null;         // from /api/emotion-state
    var _activeGroup = 'all';
    var _filters = { search: '', advanced: false };
    var _configError = null;
    var _writeTimers = {};       // per-key debounce timers
    var _searchTimer = null;
    var _dashIds = [
        'dash-readouts', 'dash-search', 'dash-advanced', 'dash-refresh',
        'dash-sidebar-count', 'dash-group-index', 'dash-rack', 'dash-toasts'
    ];

    // -----------------------------------------------------------------------
    // API helpers
    // -----------------------------------------------------------------------

    function apiGet(path) {
        return fetch(path, { headers: { 'Accept': 'application/json' } }).then(function (resp) {
            if (!resp.ok) {
                return resp.json().then(function (body) {
                    throw new Error((body && (body.detail || body.error)) || ('HTTP ' + resp.status));
                });
            }
            return resp.json();
        });
    }

    function apiPost(path, payload) {
        return fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(payload || {})
        }).then(function (resp) {
            if (!resp.ok) {
                return resp.json().then(function (body) {
                    throw new Error((body && (body.detail || body.error)) || ('HTTP ' + resp.status));
                });
            }
            return resp.json();
        });
    }

    // -----------------------------------------------------------------------
    // Small utilities
    // -----------------------------------------------------------------------

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function clamp(v, lo, hi) {
        return Math.min(hi, Math.max(lo, v));
    }

    function humanizeUptime(sec) {
        var s = Math.max(0, Math.floor(Number(sec) || 0));
        var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
        var m = Math.floor((s % 3600) / 60), r = s % 60;
        var parts = [];
        if (d) { parts.push(d + 'd'); }
        if (h) { parts.push(h + 'h'); }
        if (m) { parts.push(m + 'm'); }
        parts.push(r + 's');
        return parts.join(' ');
    }

    function toast(msg, kind) {
        kind = kind || 'info';
        var box = document.getElementById('dash-toasts');
        if (!box) { return; }
        var t = document.createElement('div');
        t.className = 'dash-toast dash-toast-' + kind;
        t.textContent = msg;
        box.appendChild(t);
        while (box.children.length > 4) { box.firstChild.remove(); }
        setTimeout(function () {
            t.classList.add('dash-toast-out');
            setTimeout(function () { if (t.parentNode) { t.parentNode.removeChild(t); } }, 320);
        }, 3600);
    }

    function isDashboardActive() {
        if (!document.body || !document.body.dataset) { return false; }
        var ds = document.body.dataset;
        return ds.activeTab === 'dashboard' ||
            ds['active-tab'] === 'dashboard' ||
            (typeof window !== 'undefined' && window.activeTab === 'dashboard');
    }

    // -----------------------------------------------------------------------
    // Data loading
    // -----------------------------------------------------------------------

    function loadConfig() {
        return apiGet('/api/config').then(function (data) {
            var raw = Array.isArray(data) ? data : (data && data.items) || [];
            _items = raw.filter(function (it) { return it && it.key; });
            buildGroups();
            _configError = null;
        }).catch(function (err) {
            _configError = err && err.message ? err.message : 'failed to fetch /api/config';
            throw err;
        });
    }

    function loadComponents() {
        return apiGet('/api/components').then(function (data) {
            _components = (data && data.plugins) || (Array.isArray(data) ? data : []);
        });
    }

    function loadReadouts() {
        return Promise.all([apiGet('/api/about'), apiGet('/api/emotion-state')]).then(function (res) {
            _about = res[0] || null;
            _emotion = res[1] || null;
            renderReadouts();
        }).catch(function () {
            // readouts are best-effort; never block the dashboard on them
        });
    }

    function loadAll() {
        var refresh = document.getElementById('dash-refresh');
        if (refresh) { refresh.disabled = true; }
        Promise.resolve().then(function () {
            return loadConfig();
        }).then(function () {
            return loadComponents();
        }).then(function () {
            return loadReadouts();
        }).catch(function () {
            // handled by renderRack error card
        }).then(function () {
            renderGroupIndex();
            renderRack();
            renderReadouts();
            if (refresh) { refresh.disabled = false; }
        });
    }

    // -----------------------------------------------------------------------
    // Grouping
    // -----------------------------------------------------------------------

    function buildGroups() {
        var map = {};
        var order = [];
        _keyToGroup = {};
        for (var i = 0; i < _items.length; i++) {
            var it = _items[i];
            var key = it.component || it.group || 'Misc';
            if (!map[key]) {
                map[key] = { key: key, label: it.component_label || it.group || key, description: it.component_description || '', items: [] };
                order.push(key);
            }
            map[key].items.push(it);
            _keyToGroup[it.key] = key;
        }
        order.sort(function (a, b) {
            var la = (map[a].label || '').toLowerCase();
            var lb = (map[b].label || '').toLowerCase();
            return la < lb ? -1 : (la > lb ? 1 : 0);
        });
        _groups = order.map(function (k) { return map[k]; });
    }

    function groupOfItem(item) {
        return _keyToGroup[item.key] || item.component || item.group || 'Misc';
    }

    // -----------------------------------------------------------------------
    // Filters
    // -----------------------------------------------------------------------

    function widgetKind(item) {
        var vt = String(item.value_type || '').toLowerCase();
        var ut = String(item.ui_type || '').toLowerCase();
        if (ut === 'file') { return 'file'; }
        if (vt === 'bool' || vt === 'boolean') { return 'bool'; }
        if (vt === 'int' || vt === 'float' || vt === 'number') { return 'num'; }
        if (vt === 'select' || vt === 'enum' || ut === 'select' || ut === 'combobox-with-options') { return 'select'; }
        if (vt === 'tags' || vt === 'tag-combobox' || vt === 'action-list') { return 'chips'; }
        if (vt === 'color') { return 'color'; }
        if (vt === 'json') { return 'json'; }
        if (vt === 'password') { return 'password'; }
        if (vt === 'textarea') { return 'textarea'; }
        return 'text';
    }

    function matchesFilters(item) {
        if (_filters.advanced === false && item.advanced === true) { return false; }
        var q = String(_filters.search || '').trim().toLowerCase();
        if (!q) { return true; }
        var hay = [
            item.key, item.label || '', item.description || '',
            item.group || '', item.component || '', item.component_label || ''
        ].join(' ').toLowerCase();
        return hay.indexOf(q) !== -1;
    }

    function groupHasVisible(group) {
        for (var i = 0; i < group.items.length; i++) {
            var it = group.items[i];
            if (widgetKind(it) !== 'file' && matchesFilters(it)) { return true; }
        }
        return false;
    }

    // -----------------------------------------------------------------------
    // Sidebar index
    // -----------------------------------------------------------------------

    function renderGroupIndex() {
        var el = document.getElementById('dash-group-index');
        var countEl = document.getElementById('dash-sidebar-count');
        if (!el) { return; }
        el.innerHTML = '';
        var addEntry = function (key, label, count) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'dash-group-entry' + (key === _activeGroup ? ' active' : '');
            b.innerHTML = '<span class="dash-group-name"></span><span class="dash-count"></span>';
            b.querySelector('.dash-group-name').textContent = label;
            b.querySelector('.dash-count').textContent = String(count);
            b.addEventListener('click', function () {
                _activeGroup = key;
                renderGroupIndex();
                renderRack();
            });
            el.appendChild(b);
        };
        addEntry('all', 'All', _items.length);
        for (var i = 0; i < _groups.length; i++) {
            addEntry(_groups[i].key, _groups[i].label, _groups[i].items.length);
        }
        if (countEl) { countEl.textContent = String(_groups.length + 1); }
    }

    // -----------------------------------------------------------------------
    // Rack
    // -----------------------------------------------------------------------

    function renderRack() {
        var rack = document.getElementById('dash-rack');
        if (!rack) { return; }
        rack.innerHTML = '';

        if (_configError) {
            var errCard = document.createElement('div');
            errCard.className = 'dash-card';
            errCard.innerHTML =
                '<div class="dash-card-head"><h3>Config unavailable</h3></div>' +
                '<div class="dash-card-error"></div>' +
                '<button type="button" class="dash-cmd-btn">Retry</button>';
            errCard.querySelector('.dash-card-error').textContent =
                'Could not load /api/config: ' + _configError;
            errCard.querySelector('.dash-cmd-btn').addEventListener('click', function () {
                _configError = null;
                renderRack();
                loadAll();
            });
            rack.appendChild(errCard);
            return;
        }

        renderCommandsCard(rack);

        if (_activeGroup === 'all') {
            for (var i = 0; i < _groups.length; i++) {
                if (groupHasVisible(_groups[i])) { renderGroupCard(rack, _groups[i]); }
            }
        } else {
            var g = null;
            for (var j = 0; j < _groups.length; j++) {
                if (_groups[j].key === _activeGroup) { g = _groups[j]; break; }
            }
            if (g) { renderGroupCard(rack, g); }
        }

        if (rack.children.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'dash-card';
            empty.innerHTML = '<div class="dash-card-head"><h3>Nothing here</h3></div>' +
                '<div class="dash-card-desc">No knobs match the current filters.</div>';
            rack.appendChild(empty);
        }
    }

    function renderGroupCard(rack, group) {
        var card = document.createElement('div');
        card.className = 'dash-card';
        card.innerHTML =
            '<div class="dash-card-head"><h3></h3></div>' +
            (group.description ? '<div class="dash-card-desc"></div>' : '') +
            '<div class="dash-widgets"></div>';
        card.querySelector('.dash-card-head h3').textContent = group.label;
        if (group.description) { card.querySelector('.dash-card-desc').textContent = group.description; }
        var widgets = card.querySelector('.dash-widgets');
        var any = false;
        for (var i = 0; i < group.items.length; i++) {
            var it = group.items[i];
            if (widgetKind(it) === 'file' || !matchesFilters(it)) { continue; }
            renderWidget(widgets, it);
            any = true;
        }
        if (!any) { return; }
        rack.appendChild(card);
    }

    function renderGroupCardForItem(item) {
        var rack = document.getElementById('dash-rack');
        if (!rack) { return; }
        var g = null;
        for (var i = 0; i < _groups.length; i++) {
            if (_groups[i].key === groupOfItem(item)) { g = _groups[i]; break; }
        }
        if (!g) { return; }
        var card = null;
        var nodes = rack.querySelectorAll('.dash-card');
        for (var j = 0; j < nodes.length; j++) {
            var titleEl = nodes[j].querySelector('.dash-card-head h3');
            if (titleEl && titleEl.textContent === g.label) { card = nodes[j]; break; }
        }
        if (card) { card.parentNode.removeChild(card); }
        if (groupHasVisible(g)) { renderGroupCard(rack, g); }
    }

    // -----------------------------------------------------------------------
    // Widget rendering
    // -----------------------------------------------------------------------

    function renderWidget(host, item) {
        var kind = widgetKind(item);
        if (kind === 'file') { return; }
        var locked = item.editable === false;
        var wrap = document.createElement('div');
        wrap.className = 'dash-widget dash-widget-' + kind + (locked ? ' locked' : '');

        var badges = [];
        if (item.advanced === true) {
            badges.push('<span class="dash-widget-badge dash-badge-adv">advanced</span>');
        }
        if (locked) {
            badges.push('<span class="dash-widget-badge dash-badge-lock">⚠ env / read-only</span>');
        }
        var head = document.createElement('div');
        head.className = 'dash-widget-head';
        var label = document.createElement('span');
        label.className = 'dash-widget-label';
        label.textContent = item.label || item.key;
        var badgeBox = document.createElement('span');
        badgeBox.className = 'dash-widget-badges';
        badgeBox.innerHTML = badges.join('') || '';
        head.appendChild(label);
        head.appendChild(badgeBox);
        wrap.appendChild(head);

        if (item.description) {
            var desc = document.createElement('div');
            desc.className = 'dash-widget-desc';
            desc.textContent = item.description;
            wrap.appendChild(desc);
        }

        var body = document.createElement('div');
        body.className = 'dash-widget-body';
        wrap.appendChild(body);

        switch (kind) {
            case 'bool': renderBoolWidget(item, body, locked); break;
            case 'num': renderNumWidget(item, body, locked); break;
            case 'select': renderSelectWidget(item, body, locked); break;
            case 'chips': renderChipsWidget(item, body, locked); break;
            case 'color': renderColorWidget(item, body, locked); break;
            case 'json': renderJsonWidget(item, body, locked); break;
            case 'password': renderTextWidget(item, body, locked, 'password'); break;
            case 'textarea': renderTextWidget(item, body, locked, 'textarea'); break;
            default: renderTextWidget(item, body, locked, 'text'); break;
        }

        host.appendChild(wrap);
    }

    function castValue(item, v) {
        var vt = String(item.value_type || '').toLowerCase();
        if (vt === 'int') { return Math.round(Number(v)); }
        if (vt === 'float' || vt === 'number') { return Number(v); }
        if (vt === 'bool' || vt === 'boolean') { return Boolean(v); }
        return v;
    }

    function optionList(item) {
        var opts = item.options || (item.constraints && item.constraints.options) || [];
        if (!Array.isArray(opts)) {
            var out = [];
            var keys = Object.keys(opts);
            for (var i = 0; i < keys.length; i++) {
                var k = keys[i];
                var o = opts[k];
                out.push({ value: k, label: (o && typeof o === 'object' && o.label) ? o.label : String(o) });
            }
            return out;
        }
        return opts.map(function (o) {
            if (o && typeof o === 'object') {
                return { value: String(o.value != null ? o.value : o.name), label: String(o.label || o.name || o.value || '') };
            }
            return { value: String(o), label: String(o) };
        });
    }

    function arrayValue(item) {
        var v = item.value;
        if (typeof v === 'string') {
            try { v = JSON.parse(v); } catch (e) { v = v ? v.split(/[\s,]+/) : []; }
        }
        return Array.isArray(v) ? v.slice() : [];
    }

    // ---- bool -------------------------------------------------------------

    function renderBoolWidget(item, body, locked) {
        var row = document.createElement('div');
        row.className = 'dash-toggle-row';
        var checked = item.value === true || item.value === 'true' || item.value === 1 || item.value === '1';
        row.innerHTML =
            '<label class="dash-toggle-wrap"><input type="checkbox" class="dash-toggle"><span class="dash-toggle-track"></span></label>';
        var input = row.querySelector('.dash-toggle');
        input.checked = checked;
        input.disabled = locked;
        input.addEventListener('change', function () {
            scheduleWrite(item, castValue(item, input.checked));
        });
        body.appendChild(row);
    }

    // ---- select -----------------------------------------------------------

    function renderSelectWidget(item, body, locked) {
        var sel = document.createElement('select');
        sel.className = 'dash-select';
        sel.disabled = locked;
        var opts = optionList(item);
        var cur = item.value;
        var found = false;
        for (var i = 0; i < opts.length; i++) {
            var o = opts[i];
            var selected = String(o.value) === String(cur);
            if (selected) { found = true; }
            var opt = document.createElement('option');
            opt.value = o.value;
            opt.textContent = o.label;
            opt.selected = selected;
            sel.appendChild(opt);
        }
        if (!found && cur !== '' && cur != null) {
            var extra = document.createElement('option');
            extra.value = String(cur);
            extra.textContent = String(cur);
            extra.selected = true;
            sel.appendChild(extra);
        }
        sel.addEventListener('change', function () {
            scheduleWrite(item, castValue(item, sel.value));
        });
        body.appendChild(sel);
    }

    // ---- chips ------------------------------------------------------------

    function renderChipsWidget(item, body, locked) {
        var box = document.createElement('div');
        box.className = 'dash-chips';
        var input = document.createElement('input');
        input.className = 'dash-chip-input';
        input.placeholder = 'add…';
        input.autocomplete = 'off';
        input.disabled = locked;
        var listId = 'dash-chips-' + item.key.replace(/[^A-Za-z0-9_]/g, '_');
        input.setAttribute('list', listId);
        var datalist = document.createElement('datalist');
        datalist.id = listId;
        var opts = optionList(item);
        for (var i = 0; i < opts.length; i++) {
            var opt = document.createElement('option');
            opt.value = opts[i].value;
            datalist.appendChild(opt);
        }
        box.appendChild(datalist);
        body.appendChild(box);

        var paint = function () {
            var arr = arrayValue(item);
            var existing = box.querySelectorAll('.dash-chip');
            var firstChip = box.firstChild;
            while (firstChip && firstChip.tagName === 'DIV') { firstChip.remove(); }
            var before = box.querySelector('.dash-chip-input');
            for (var j = 0; j < arr.length; j++) {
                var chip = document.createElement('span');
                chip.className = 'dash-chip';
                var txt = document.createElement('span');
                txt.textContent = String(arr[j]);
                var x = document.createElement('button');
                x.type = 'button';
                x.className = 'dash-chip-x';
                x.textContent = '×';
                x.disabled = locked;
                x.setAttribute('aria-label', 'remove');
                x.addEventListener('click', function () {
                    var next = arrayValue(item).filter(function (v) { return String(v) !== txt.textContent; });
                    item.value = next;
                    paint();
                    scheduleWrite(item, next);
                });
                chip.appendChild(txt);
                chip.appendChild(x);
                box.insertBefore(chip, before);
            }
            void existing;
        };

        var commitInput = function () {
            var val = String(input.value || '').trim();
            if (!val) { return; }
            var arr = arrayValue(item);
            var dup = false;
            for (var i = 0; i < arr.length; i++) {
                if (String(arr[i]) === val) { dup = true; break; }
            }
            if (!dup) {
                arr.push(val);
                item.value = arr;
                scheduleWrite(item, arr);
            }
            input.value = '';
            paint();
        };
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commitInput(); }
            else if (e.key === 'Backspace' && !input.value) {
                var arr = arrayValue(item);
                arr.pop();
                item.value = arr;
                paint();
                scheduleWrite(item, arr);
            }
        });
        input.addEventListener('blur', commitInput);

        box.appendChild(input);
        paint();
    }

    // ---- color ------------------------------------------------------------

    function renderColorWidget(item, body, locked) {
        var row = document.createElement('div');
        row.className = 'dash-color-row';
        var picker = document.createElement('input');
        picker.type = 'color';
        picker.className = 'dash-color';
        picker.disabled = locked;
        var cur = String(item.value || '').trim();
        if (!/^#[0-9a-fA-F]{3,8}$/.test(cur)) { cur = '#ff2bd6'; }
        picker.value = cur;
        var valEl = document.createElement('span');
        valEl.className = 'dash-color-value';
        valEl.textContent = String(item.value || '');
        picker.addEventListener('input', function () {
            scheduleWrite(item, picker.value);
        });
        row.appendChild(picker);
        row.appendChild(valEl);
        body.appendChild(row);
    }

    // ---- json -------------------------------------------------------------

    function renderJsonWidget(item, body, locked) {
        var ta = document.createElement('textarea');
        ta.className = 'dash-text dash-json';
        ta.disabled = locked;
        ta.value = (typeof item.value === 'string') ? item.value : JSON.stringify(item.value, null, 2);
        var commit = function () {
            var raw = ta.value.trim();
            var parsed;
            try { parsed = raw ? JSON.parse(raw) : null; }
            catch (e) { toast('Invalid JSON for ' + (item.label || item.key) + ': ' + e.message, 'err'); return; }
            scheduleWrite(item, parsed);
        };
        ta.addEventListener('change', commit);
        ta.addEventListener('blur', commit);
        body.appendChild(ta);
    }

    // ---- text / password / textarea --------------------------------------

    function renderTextWidget(item, body, locked, kind) {
        var el;
        if (kind === 'textarea') {
            el = document.createElement('textarea');
            el.className = 'dash-text';
        } else {
            el = document.createElement('input');
            el.type = kind === 'password' ? 'password' : 'text';
            el.className = 'dash-input';
        }
        el.disabled = locked;
        if (item.value != null) { el.value = String(item.value); }
        el.addEventListener('change', function () {
            scheduleWrite(item, castValue(item, el.value));
        });
        body.appendChild(el);
    }

    // ---- knob (numeric) ---------------------------------------------------

    function renderNumWidget(item, body, locked) {
        var isInt = String(item.value_type || '').toLowerCase() === 'int';
        var base = Number(item.default) || 0;
        var nom = Math.max(1, 2 * Math.abs(base), 100);
        var min = base >= 0 ? 0 : -nom;
        var max = base >= 0 ? nom : 0;
        var sens = nom / 200;
        var step = isInt ? 1 : 0.5;
        var cur = Number(item.value);
        if (!Number.isFinite(cur)) { cur = base; }

        var wrap = document.createElement('div');
        wrap.className = 'dash-num-body';
        wrap.innerHTML =
            '<div class="dash-knob" role="slider" tabindex="0"><div class="dash-knob-arc"></div><div class="dash-knob-needle"></div><div class="dash-knob-cap"></div></div>' +
            '<div class="dash-num-side">' +
            '  <span class="dash-num-value"></span>' +
            '  <div class="dash-num-btns"><button type="button" class="dash-num-btn" data-delta="1">+</button><button type="button" class="dash-num-btn" data-delta="-1">−</button></div>' +
            '</div>';

        var knob = wrap.querySelector('.dash-knob');
        var valueEl = wrap.querySelector('.dash-num-value');
        var buttons = wrap.querySelectorAll('.dash-num-btn');
        if (locked) {
            knob.classList.add('locked');
            knob.setAttribute('aria-disabled', 'true');
            for (var bi = 0; bi < buttons.length; bi++) { buttons[bi].disabled = true; }
        }
        knob.setAttribute('aria-valuemin', String(min));
        knob.setAttribute('aria-valuemax', String(max));

        var paint = function () {
            var frac = clamp((cur - min) / (max - min || 1), 0, 1);
            knob.style.setProperty('--dash-knob-frac', String(frac));
            knob.style.setProperty('--dash-knob-angle', String(frac * 270 - 135) + 'deg');
            knob.setAttribute('aria-valuenow', String(cur));
            valueEl.textContent = isInt ? String(Math.round(cur)) : Number(cur).toFixed(2);
        };
        var commit = function (v) {
            v = isInt ? Math.round(v) : Math.round(v * 100) / 100;
            cur = v;
            paint();
            scheduleWrite(item, v);
        };
        paint();

        var dragging = false, startY = 0, startV = 0;
        knob.addEventListener('pointerdown', function (e) {
            if (locked || e.button !== 0) { return; }
            dragging = true;
            startY = e.clientY;
            startV = cur;
            knob.classList.add('dragging');
            if (knob.setPointerCapture) { knob.setPointerCapture(e.pointerId); }
            e.preventDefault();
        });
        knob.addEventListener('pointermove', function (e) {
            if (!dragging) { return; }
            commit(startV + (startY - e.clientY) * sens);
        });
        var endDrag = function () {
            if (!dragging) { return; }
            dragging = false;
            knob.classList.remove('dragging');
        };
        knob.addEventListener('pointerup', endDrag);
        knob.addEventListener('pointercancel', endDrag);
        knob.addEventListener('wheel', function (e) {
            if (locked) { return; }
            e.preventDefault();
            commit(cur + (e.deltaY < 0 ? step : -step));
        }, { passive: false });
        knob.addEventListener('dblclick', function () {
            if (locked) { return; }
            var raw = window.prompt('Enter exact value for "' + (item.label || item.key) + '"', String(cur));
            if (raw === null) { return; }
            var n = Number(raw);
            if (!Number.isFinite(n)) { toast('Invalid number: ' + raw, 'err'); return; }
            commit(n);
        });
        knob.addEventListener('keydown', function (e) {
            if (locked) { return; }
            if (e.key === 'ArrowUp' || e.key === 'ArrowRight') { e.preventDefault(); commit(cur + step); }
            else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') { e.preventDefault(); commit(cur - step); }
            else if (e.key === 'Home') { e.preventDefault(); commit(min); }
            else if (e.key === 'End') { e.preventDefault(); commit(max); }
        });
        for (var i = 0; i < buttons.length; i++) {
            buttons[i].addEventListener('click', function () {
                if (locked) { return; }
                commit(cur + Number(this.getAttribute('data-delta')) * step);
            });
        }

        body.appendChild(wrap);
    }

    // -----------------------------------------------------------------------
    // Write path
    // -----------------------------------------------------------------------

    function scheduleWrite(item, value) {
        if (item.editable === false) {
            toast((item.label || item.key) + ' is read-only', 'err');
            return;
        }
        var key = item.key;
        if (_writeTimers[key]) { clearTimeout(_writeTimers[key]); }
        if (item._confirmed === undefined) { item._confirmed = item.value; }
        item.value = value;
        _writeTimers[key] = setTimeout(function () {
            delete _writeTimers[key];
            doWrite(item, value);
        }, 450);
    }

    function doWrite(item, value) {
        apiPost('/api/config', { key: item.key, value: value }).then(function (data) {
            item.value = value;
            item._confirmed = value;
            if (data && data.requires_reload) {
                toast(item.key + ' updated — a reload is required to take effect', 'info');
            }
        }).catch(function (err) {
            item.value = item._confirmed;
            toast('Failed to save ' + item.key + ': ' + (err && err.message ? err.message : err), 'err');
            renderGroupCardForItem(item);
        });
    }

    // -----------------------------------------------------------------------
    // Commands card
    // -----------------------------------------------------------------------

    function renderCommandsCard(rack) {
        var card = document.createElement('div');
        card.className = 'dash-card dash-commands';
        card.innerHTML =
            '<div class="dash-card-head"><h3>Commands</h3></div>' +
            '<div class="dash-card-desc">Run plugin actions, flip plugin power and send a one-shot agent prompt.</div>' +
            '<div class="dash-card-divider"></div>' +
            '<div class="dash-cmd-list"></div>';
        var list = card.querySelector('.dash-cmd-list');

        // (a) runnable plugins
        var anyRun = false;
        for (var i = 0; i < _components.length; i++) {
            var c = _components[i];
            if (c && c.runnable === true) {
                anyRun = true;
                var row = document.createElement('div');
                row.className = 'dash-cmd-row';
                var name = document.createElement('span');
                name.className = 'dash-cmd-name';
                name.textContent = c.display_name || c.name;
                name.title = c.description || '';
                var acts = document.createElement('span');
                acts.className = 'dash-cmd-actions';
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'dash-cmd-btn';
                btn.textContent = c.run_label || 'Run';
                btn.title = c.run_title || '';
                btn.addEventListener('click', function (comp, b) {
                    return function () {
                        b.disabled = true;
                        apiPost('/api/components/run', { name: comp.name, action: comp.run_action || 'run_now' }).then(function (data) {
                            var res = data && data.result;
                            var msg = '';
                            if (typeof res === 'string') { msg = res; }
                            else if (res && typeof res === 'object') {
                                msg = res.summary || res.message || res.status || ('ok');
                            }
                            toast((comp.display_name || comp.name) + ': ' + (msg || 'done'), 'ok');
                        }).catch(function (err) {
                            toast('Failed to run ' + (comp.display_name || comp.name) + ': ' + (err && err.message ? err.message : err), 'err');
                        }).then(function () {
                            b.disabled = false;
                        });
                    };
                }(c, btn));
                acts.appendChild(btn);
                row.appendChild(name);
                row.appendChild(acts);
                list.appendChild(row);
            }
        }

        // (b) disable_allowed toggles (non-MCP)
        var anyToggle = false;
        for (var t = 0; t < _components.length; t++) {
            var ct = _components[t];
            if (ct && ct.disable_allowed === true && !ct.is_mcp) {
                anyToggle = true;
                var trow = document.createElement('div');
                trow.className = 'dash-cmd-row';
                var tname = document.createElement('span');
                tname.className = 'dash-cmd-name';
                tname.textContent = (ct.display_name || ct.name) + (ct.enabled === false ? '  (disabled)' : '');
                var tacts = document.createElement('span');
                tacts.className = 'dash-cmd-actions';
                var label = document.createElement('label');
                label.className = 'dash-toggle-wrap dash-cmd-toggle';
                label.innerHTML = '<input type="checkbox" class="dash-toggle"><span class="dash-toggle-track"></span>';
                var check = label.querySelector('.dash-toggle');
                check.checked = ct.enabled !== false;
                check.addEventListener('change', function (comp, box, cb, labelEl) {
                    return function () {
                        var target = box.checked;
                        if (!target) {
                            if (!window.confirm('Disable ' + (comp.display_name || comp.name) + '?')) {
                                box.checked = true;
                                return;
                            }
                        }
                        apiPost('/api/components/toggle', { name: comp.name, enabled: target }).then(function () {
                            toast((target ? 'Enabled ' : 'Disabled ') + (comp.display_name || comp.name), 'ok');
                            labelEl.textContent = (comp.display_name || comp.name) + (target ? '' : '  (disabled)');
                        }).catch(function (err) {
                            toast('Failed to toggle ' + (comp.display_name || comp.name) + ': ' + (err && err.message ? err.message : err), 'err');
                            box.checked = !target;
                        });
                    };
                }(ct, check, null, tname));
                tacts.appendChild(label);
                trow.appendChild(tname);
                trow.appendChild(tacts);
                list.appendChild(trow);
            }
        }

        // (c) agent run row
        var arow = document.createElement('div');
        arow.className = 'dash-cmd-row dash-agent-row';
        var agentInput = document.createElement('input');
        agentInput.className = 'dash-input';
        agentInput.type = 'text';
        agentInput.placeholder = 'One-shot agent prompt…';
        var agentBtn = document.createElement('button');
        agentBtn.type = 'button';
        agentBtn.className = 'dash-cmd-btn';
        agentBtn.textContent = 'Run Agent';
        var runAgent = function () {
            var prompt = String(agentInput.value || '').trim();
            if (!prompt) { toast('Enter a prompt first', 'err'); return; }
            agentBtn.disabled = true;
            apiPost('/api/agent/run', { prompt: prompt }).then(function (data) {
                var res = data && data.result;
                var summary = '';
                if (typeof res === 'string') { summary = res; }
                else if (res && typeof res === 'object') {
                    summary = res.summary || res.message || res.status ||
                        (Array.isArray(res) ? (res.length + ' steps') : '');
                }
                toast('Agent: ' + (summary || ('task ' + (data.task_id || '')) || 'done'), 'ok');
                agentInput.value = '';
            }).catch(function (err) {
                toast('Agent run failed: ' + (err && err.message ? err.message : err), 'err');
            }).then(function () {
                agentBtn.disabled = false;
            });
        };
        agentBtn.addEventListener('click', runAgent);
        agentInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') { runAgent(); } });
        arow.appendChild(agentInput);
        arow.appendChild(agentBtn);
        list.appendChild(arow);

        card.dataset.cmdCount = String((anyRun ? 1 : 0) + (anyToggle ? 1 : 0));
        rack.appendChild(card);
    }

    // -----------------------------------------------------------------------
    // Readouts
    // -----------------------------------------------------------------------

    function renderReadouts() {
        var el = document.getElementById('dash-readouts');
        if (!el) { return; }
        el.innerHTML = '';
        var add = function (label, value, tone) {
            var chip = document.createElement('div');
            chip.className = 'dash-readout' + (tone ? ' dash-readout-' + tone : '');
            var l = document.createElement('span');
            l.className = 'dash-readout-label';
            l.textContent = label;
            var v = document.createElement('span');
            v.className = 'dash-readout-value';
            v.textContent = value;
            chip.appendChild(l);
            chip.appendChild(v);
            el.appendChild(chip);
        };

        if (_about) {
            if (_about.uptime != null) { add('uptime', humanizeUptime(_about.uptime)); }
            if (_about.sessions != null) { add('sessions', String(_about.sessions)); }
            if (_about.components != null) { add('components', String(_about.components)); }
            if (_about.version) { add('version', String(_about.version)); }
        }
        if (_emotion && _emotion.dominant_emotion) {
            var emo = String(_emotion.dominant_emotion);
            var intensity = _emotion.emotions && _emotion.emotions[_emotion.dominant_emotion];
            if (intensity != null) { emo += ' ' + Number(intensity).toFixed(2); }
            add('mood', emo, 'mood');
        }
        add('controls', String(_items.length));
    }

    // -----------------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------------

    function initDashboardTab() {
        var section = document.getElementById('tab-dashboard');
        if (!section) { return; }
        if (section.dataset.dashLoaded) { return; }
        section.dataset.dashLoaded = '1';

        var missing = [];
        for (var i = 0; i < _dashIds.length; i++) {
            if (!document.getElementById(_dashIds[i])) { missing.push(_dashIds[i]); }
        }
        if (missing.length) {
            toast('Dashboard DOM incomplete: ' + missing.join(', '), 'err');
        }

        var search = document.getElementById('dash-search');
        if (search) {
            search.addEventListener('input', function () {
                if (_searchTimer) { clearTimeout(_searchTimer); }
                _searchTimer = setTimeout(function () {
                    _filters.search = search.value;
                    renderRack();
                }, 200);
            });
        }

        var adv = document.getElementById('dash-advanced');
        if (adv) {
            adv.addEventListener('change', function () {
                _filters.advanced = adv.checked;
                renderRack();
            });
        }

        var refresh = document.getElementById('dash-refresh');
        if (refresh) {
            refresh.addEventListener('click', loadAll);
        }

        // Poll readouts only while the dashboard tab is the active one.
        setInterval(function () {
            if (isDashboardActive()) { loadReadouts(); }
        }, 15000);

        loadAll();
    }

    function initWhenReady() {
        var section = document.getElementById('tab-dashboard');
        if (section && section.classList && section.classList.contains('active')) {
            initDashboardTab();
        }
    }

    window.SynthWebUI = window.SynthWebUI || {};
    window.SynthWebUI.initDashboardTab = initDashboardTab;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initWhenReady);
    } else {
        initWhenReady();
    }
})();
