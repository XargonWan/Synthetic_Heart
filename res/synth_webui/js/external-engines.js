/**
 * external-engines.js — Manages the External Engines section UI.
 *
 * Registered as window.SynthWebUI.initExternal_enginesTab so the main
 * loadSection() helper invokes it automatically after the HTML is injected.
 */
(function () {
    'use strict';

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    let _endpoints = [];

    // -----------------------------------------------------------------------
    // API helpers
    // -----------------------------------------------------------------------

    async function apiFetch(path, options = {}) {
        const resp = await fetch(path, {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        });
        if (!resp.ok) {
            let msg = `HTTP ${resp.status}`;
            try { const body = await resp.json(); msg = body.detail || msg; } catch (e) { /* ignore */ }
            throw new Error(msg);
        }
        return resp.json();
    }

    // -----------------------------------------------------------------------
    // DOM helpers
    // -----------------------------------------------------------------------

    function setStatus(msg, color) {
        const el = document.getElementById('ext-ep-status');
        if (el) { el.textContent = msg; el.style.color = color || ''; }
    }

    function subsystemLabel(key) {
        return { cortex: 'Cortex', vox: 'Vox', auris: 'Auris', live: 'Live', vision: 'Vision' }[key] || key;
    }

    function protocolBadgeColor(proto) {
        return { openai: '#19a97b', gemini: '#4285f4', anthropic: '#d97706', custom: '#7b61ff' }[proto] || '#888';
    }

    // -----------------------------------------------------------------------
    // Render list
    // -----------------------------------------------------------------------

    async function loadEndpoints() {
        try {
            setStatus('Loading…', '');
            const data = await apiFetch('/api/external-endpoints');
            _endpoints = data.endpoints || [];
            renderList();
            setStatus('');
        } catch (e) {
            setStatus('Error: ' + e.message, 'var(--danger,#c0392b)');
        }
    }

    function renderList() {
        const container = document.getElementById('ext-ep-list');
        if (!container) return;
        if (_endpoints.length === 0) {
            container.innerHTML = '<div class="meta" style="color:var(--muted);">No external endpoints configured yet. Click <strong>+ Add Endpoint</strong> to get started.</div>';
            return;
        }
        container.innerHTML = '';
        for (const ep of _endpoints) {
            container.appendChild(buildCard(ep));
        }
    }

    function buildCard(ep) {
        const tpl = document.getElementById('ext-ep-card-tpl');
        if (!tpl) return document.createElement('div');
        const node = tpl.content.cloneNode(true);
        const card = node.querySelector('.ext-ep-card');
        card.dataset.id = ep.id;

        // Header
        card.querySelector('.ext-ep-label').textContent = ep.display_label || ep.name;
        const protoBadge = card.querySelector('.ext-ep-protocol');
        protoBadge.textContent = ep.protocol;
        protoBadge.style.background = protocolBadgeColor(ep.protocol);

        const enabledBadge = card.querySelector('.ext-ep-enabled-badge');
        if (ep.enabled) {
            enabledBadge.textContent = 'enabled';
            enabledBadge.style.background = 'var(--success-bg,#155724)';
            enabledBadge.style.color = '#d4edda';
        } else {
            enabledBadge.textContent = 'disabled';
            enabledBadge.style.background = 'var(--border,#444)';
            enabledBadge.style.color = 'var(--muted)';
        }

        const probeBadge = card.querySelector('.ext-ep-probe-badge');
        const probeColors = { success: '#155724', failed: '#721c24', pending: '#856404', never: '' };
        probeBadge.textContent = 'probe: ' + ep.probe_status;
        probeBadge.style.background = (probeColors[ep.probe_status] !== undefined ? probeColors[ep.probe_status] : '') || 'var(--border,#444)';
        probeBadge.style.color = ep.probe_status === 'never' ? 'var(--muted)' : '#fff';

        // URL
        card.querySelector('.ext-ep-url').textContent = ep.base_url;

        // Subsystems
        const subsysEl = card.querySelector('.ext-ep-subsystems');
        const effectiveMap = ep.effective_subsystem_map || {};
        for (const [key, val] of Object.entries(effectiveMap)) {
            if (key === 'vision') continue; // not yet implemented
            const pill = document.createElement('label');
            pill.style.cssText = 'display:flex;align-items:center;gap:4px;cursor:pointer;font-size:0.85rem;';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = !!val;
            cb.dataset.key = key;
            cb.addEventListener('change', () => handleSubsystemToggle(ep.id, key, cb.checked, card));
            pill.appendChild(cb);
            pill.append(subsystemLabel(key));
            subsysEl.appendChild(pill);
        }

        // Model selector
        const modelSelect = card.querySelector('.ext-ep-model-select');
        const models = Array.isArray(ep.available_models) ? ep.available_models : [];
        if (models.length === 0) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = ep.probe_status === 'never' ? '— probe first —' : '— none found —';
            modelSelect.appendChild(opt);
        } else {
            for (const m of models) {
                const opt = document.createElement('option');
                opt.value = m;
                opt.textContent = m;
                if (m === ep.default_model) opt.selected = true;
                modelSelect.appendChild(opt);
            }
        }
        if (ep.default_model && models && models.includes && models.includes(ep.default_model)) {
            modelSelect.value = ep.default_model;
        }

        // Probe time
        if (ep.last_probe_at) {
            const probeEl = card.querySelector('.ext-ep-probe-time');
            if (probeEl) probeEl.textContent = 'Last probed: ' + ep.last_probe_at;
        }

        // Buttons
        card.querySelector('.ext-ep-edit-btn').addEventListener('click', () => openModal(ep));
        const toggleBtn = card.querySelector('.ext-ep-toggle-btn');
        toggleBtn.textContent = ep.enabled ? 'Disable' : 'Enable';
        toggleBtn.style.background = ep.enabled ? 'var(--warning,#e67e22)' : 'var(--success,#27ae60)';
        toggleBtn.style.color = '#fff';
        toggleBtn.addEventListener('click', () => handleToggle(ep.id, !ep.enabled));

        card.querySelector('.ext-ep-probe-btn').addEventListener('click', () => handleProbe(ep.id));
        card.querySelector('.ext-ep-delete-btn').addEventListener('click', () => handleDelete(ep.id, ep.display_label || ep.name));
        card.querySelector('.ext-ep-model-test').addEventListener('click', () => {
            const model = modelSelect.value;
            handleTestModel(ep.id, model, card);
        });

        return card;
    }

    // -----------------------------------------------------------------------
    // Action handlers
    // -----------------------------------------------------------------------

    async function handleToggle(id, enable) {
        try {
            setStatus('Updating…', '');
            const action = enable ? 'enable' : 'disable';
            await apiFetch(`/api/external-endpoints/${id}/${action}`, { method: 'POST' });
            await loadEndpoints();
        } catch (e) {
            setStatus('Error: ' + e.message, 'var(--danger,#c0392b)');
        }
    }

    async function handleProbe(id) {
        try {
            setStatus('Probing endpoint…', '');
            const result = await apiFetch(`/api/external-endpoints/${id}/probe`, { method: 'POST' });
            const capStr = Object.entries(result.capabilities || {})
                .filter(([, v]) => v).map(([k]) => k).join(', ') || 'none';
            setStatus(
                `Probe ${result.status}: capabilities=[${capStr}] models=${result.models?.length ?? 0}`,
                result.status === 'success' ? 'var(--success,#27ae60)' : 'var(--danger,#c0392b)'
            );
            await loadEndpoints();
        } catch (e) {
            setStatus('Probe error: ' + e.message, 'var(--danger,#c0392b)');
        }
    }

    async function handleDelete(id, name) {
        if (!confirm(`Delete external endpoint "${name}"?\n\nThis will also remove it from all subsystem registries.`)) return;
        try {
            setStatus('Deleting…', '');
            await apiFetch(`/api/external-endpoints/${id}`, { method: 'DELETE' });
            await loadEndpoints();
            setStatus('');
        } catch (e) {
            setStatus('Error: ' + e.message, 'var(--danger,#c0392b)');
        }
    }

    async function handleSubsystemToggle(id, key, value, card) {
        try {
            const current = _endpoints.find(e => e.id === id);
            if (!current) return;
            const overrides = { ...(current.subsystem_map || {}), [key]: value };
            await apiFetch(`/api/external-endpoints/${id}/mapping`, {
                method: 'PUT',
                body: JSON.stringify(overrides),
            });
            // Update local state without full reload
            current.subsystem_map = overrides;
            current.effective_subsystem_map = { ...(current.capabilities || {}), ...overrides };
        } catch (e) {
            setStatus('Error: ' + e.message, 'var(--danger,#c0392b)');
            await loadEndpoints(); // revert UI
        }
    }

    async function handleTestModel(id, model, card) {
        const echoEl = card ? card.querySelector('.ext-ep-model-test-echo') : null;
        if (echoEl) { echoEl.textContent = 'Testing…'; echoEl.style.color = 'var(--muted)'; }
        try {
            const result = await apiFetch(`/api/external-endpoints/${id}/ping`, {
                method: 'POST',
                body: JSON.stringify({ model: model || null }),
            });
            if (echoEl) {
                if (result.ok) {
                    echoEl.textContent = `✓ "${result.echo}"`;
                    echoEl.style.color = 'var(--success,#27ae60)';
                } else {
                    echoEl.textContent = `✗ ${result.echo}`;
                    echoEl.style.color = 'var(--danger,#c0392b)';
                }
            }
        } catch (e) {
            if (echoEl) {
                echoEl.textContent = `✗ ${e.message}`;
                echoEl.style.color = 'var(--danger,#c0392b)';
            }
        }
    }

    // -----------------------------------------------------------------------
    // Add / Edit modal
    // -----------------------------------------------------------------------

    function openModal(ep = null) {
        const modal = document.getElementById('ext-ep-modal');
        if (!modal) return;
        setModalError('');
        document.getElementById('ext-ep-modal-title').textContent = ep ? 'Edit Endpoint' : 'Add External Endpoint';
        document.getElementById('ext-ep-form-id').value = ep ? ep.id : '';
        document.getElementById('ext-ep-form-name').value = ep ? ep.name : '';
        document.getElementById('ext-ep-form-name').disabled = !!ep; // name is immutable after creation
        document.getElementById('ext-ep-form-label').value = ep ? ep.display_label : '';
        document.getElementById('ext-ep-form-protocol').value = ep ? ep.protocol : 'openai';
        document.getElementById('ext-ep-form-url').value = ep ? ep.base_url : '';
        document.getElementById('ext-ep-form-key').value = '';
        modal.style.display = 'flex';
    }

    function closeModal() {
        const modal = document.getElementById('ext-ep-modal');
        if (modal) modal.style.display = 'none';
    }

    function setModalError(msg) {
        const el = document.getElementById('ext-ep-modal-error');
        if (!el) return;
        if (msg) {
            el.textContent = msg;
            el.style.display = 'block';
        } else {
            el.textContent = '';
            el.style.display = 'none';
        }
    }

    async function handleFormSubmit(e) {
        e.preventDefault();
        setModalError('');
        const id = document.getElementById('ext-ep-form-id').value;
        const name = (document.getElementById('ext-ep-form-name').value || '').trim();
        const base_url = (document.getElementById('ext-ep-form-url').value || '').trim();
        const display_label = (document.getElementById('ext-ep-form-label').value || '').trim();
        const protocol = document.getElementById('ext-ep-form-protocol').value || 'openai';
        const key = (document.getElementById('ext-ep-form-key').value || '');

        // Client-side validation
        if (!id && !name) { setModalError('Name is required.'); return; }
        if (!base_url) { setModalError('Base URL is required.'); return; }

        const payload = { display_label, protocol, base_url };
        if (!id) payload.name = name;
        if (key) payload.api_key = key;

        try {
            setStatus('Saving and probing…', '');
            let response;
            if (id) {
                response = await apiFetch(`/api/external-endpoints/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify(payload),
                });
            } else {
                response = await apiFetch('/api/external-endpoints', {
                    method: 'POST',
                    body: JSON.stringify(payload),
                });
            }

            await loadEndpoints();

            const probe = response.probe || {};
            if (probe.status === 'failed') {
                // Save succeeded — keep modal open with a warning
                const probeErr = probe.error || 'unknown error';
                setModalError(
                    'Saved! But the probe failed: ' + probeErr +
                    '\nThe endpoint is saved but may not be reachable. ' +
                    'Check that the base URL is accessible from inside the container ' +
                    '(use host.docker.internal instead of localhost if needed).'
                );
                setStatus('');
            } else {
                // Probe succeeded — close modal and show summary in status bar
                closeModal();
                const modelsCount = (probe.models || []).length;
                const pingEcho = probe.ping_echo ? ` | Ping reply: "${probe.ping_echo}"` : '';
                setStatus(
                    `Saved! ${modelsCount} model(s) found.${pingEcho}`,
                    'var(--success,#27ae60)'
                );
            }
        } catch (err) {
            setModalError('Error: ' + err.message);
            setStatus('');
        }
    }

    // -----------------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------------

    function initExternalEnginesTab() {
        const addBtn = document.getElementById('ext-ep-add-btn');
        if (addBtn) addBtn.addEventListener('click', () => openModal(null));

        const refreshBtn = document.getElementById('ext-ep-refresh-btn');
        if (refreshBtn) refreshBtn.addEventListener('click', loadEndpoints);

        const cancelBtn = document.getElementById('ext-ep-modal-cancel');
        if (cancelBtn) cancelBtn.addEventListener('click', closeModal);

        const form = document.getElementById('ext-ep-form');
        if (form) form.addEventListener('submit', handleFormSubmit);

        // Close modal when clicking the backdrop
        const modal = document.getElementById('ext-ep-modal');
        if (modal) {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) closeModal();
            });
        }

        loadEndpoints();
    }

    // Register with SynthWebUI — the init function name must match the pattern
    // 'init' + capitalize(section) + 'Tab' where section is the data-tab value.
    // For data-tab="external_engines" → initExternal_enginesTab
    window.SynthWebUI = window.SynthWebUI || {};
    window.SynthWebUI['initExternal_enginesTab'] = initExternalEnginesTab;

    // If the tab is already visible, initialize now
    (function () {
        try {
            const panel = document.querySelector('[data-tab="external_engines"]');
            if (panel && panel.classList.contains('active')) initExternalEnginesTab();
        } catch (e) { /* ignore */ }
    })();
})();
