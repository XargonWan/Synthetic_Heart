/**
 * engines.js — Manages the Engines section UI.
 *
 * Registered as window.SynthWebUI.initEnginesTab so the main
 * loadSection() helper invokes it automatically after the HTML is injected.
 */
(function () {
    'use strict';

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    let _endpoints = [];
    let _presets = [];          // loaded once, cached for the session
    let _presetsLoaded = false;

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

    // Icon text/emoji fallback for provider cards
    function providerIcon(icon) {
        const icons = {
            google: '🔵', anthropic: '🟠', openrouter: '⚡', github: '🐙',
            ollama: '🦙', openai: '🟢', selenium: '🤖', custom: '⚙️',
        };
        return icons[icon] || '🔌';
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

        // Model selector — dropdown (probed models) + custom text input fallback
        const modelDropdown = card.querySelector('.ext-ep-model-dropdown');
        const modelInput = card.querySelector('.ext-ep-model-select');
        const CUSTOM_VALUE = '__custom__';

        const models = Array.isArray(ep.available_models) ? ep.available_models : [];
        const current = ep.default_model || '';

        // Populate the dropdown: a leading placeholder, all probed models, then a
        // "custom" entry that reveals the free-text input.
        modelDropdown.innerHTML = '';
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = models.length === 0
            ? (ep.probe_status === 'never' ? '— probe first or enter custom —' : '— no models found, enter custom —')
            : '— select a model —';
        modelDropdown.appendChild(placeholder);

        for (const m of models) {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = m;
            modelDropdown.appendChild(opt);
        }

        const customOpt = document.createElement('option');
        customOpt.value = CUSTOM_VALUE;
        customOpt.textContent = '✏️ Custom…';
        modelDropdown.appendChild(customOpt);

        // Helper to read the effective model value (dropdown or custom input).
        const readModel = () =>
            modelDropdown.value === CUSTOM_VALUE ? modelInput.value.trim() : modelDropdown.value;

        // Initial selection: match the saved default against the known models.
        if (current && models.includes(current)) {
            modelDropdown.value = current;
            modelInput.style.display = 'none';
        } else if (current) {
            // Saved model not in the probed list → treat as custom.
            modelDropdown.value = CUSTOM_VALUE;
            modelInput.value = current;
            modelInput.style.display = '';
        } else {
            modelDropdown.value = '';
            modelInput.style.display = 'none';
        }

        modelDropdown.addEventListener('change', () => {
            if (modelDropdown.value === CUSTOM_VALUE) {
                modelInput.style.display = '';
                modelInput.focus();
                return; // wait for the user to type + blur before saving
            }
            modelInput.style.display = 'none';
            handleSetModel(ep.id, modelDropdown.value, card);
        });

        modelInput.addEventListener('change', () => {
            handleSetModel(ep.id, readModel(), card);
        });

        // Probe time
        if (ep.last_probe_at) {
            const probeEl = card.querySelector('.ext-ep-probe-time');
            if (probeEl) probeEl.textContent = 'Last probed: ' + ep.last_probe_at;
        }

        // Buttons
        card.querySelector('.ext-ep-edit-btn').addEventListener('click', () => openModalEdit(ep));
        const toggleBtn = card.querySelector('.ext-ep-toggle-btn');
        toggleBtn.textContent = ep.enabled ? 'Disable' : 'Enable';
        toggleBtn.style.background = ep.enabled ? 'var(--warning,#e67e22)' : 'var(--success,#27ae60)';
        toggleBtn.style.color = '#fff';
        toggleBtn.addEventListener('click', () => handleToggle(ep.id, !ep.enabled));

        card.querySelector('.ext-ep-probe-btn').addEventListener('click', () => handleProbe(ep.id, card));
        card.querySelector('.ext-ep-delete-btn').addEventListener('click', () => handleDelete(ep.id, ep.display_label || ep.name));
        card.querySelector('.ext-ep-model-test').addEventListener('click', () => {
            handleTestModel(ep.id, readModel(), card);
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
            window.SynthWebUI?.loadEnginesSummary?.();
        } catch (e) {
            setStatus('Error: ' + e.message, 'var(--danger,#c0392b)');
        }
    }

    async function handleProbe(id, card) {
        const echoEl = card ? card.querySelector('.ext-ep-probe-echo') : null;
        if (echoEl) { echoEl.textContent = 'Probing endpoint…'; echoEl.style.color = 'var(--muted)'; }
        try {
            const result = await apiFetch(`/api/external-endpoints/${id}/probe`, { method: 'POST' });
            const capStr = Object.entries(result.capabilities || {})
                .filter(([, v]) => v).map(([k]) => k).join(', ') || 'none';
            const msg = `Probe ${result.status}: capabilities=[${capStr}] models=${result.models?.length ?? 0}`;
            const color = result.status === 'success' ? 'var(--success,#27ae60)' : 'var(--danger,#c0392b)';
            await loadEndpoints();
            window.SynthWebUI?.loadEnginesSummary?.();
            // loadEndpoints() rebuilds all cards, so re-select the refreshed card by id.
            const newEcho = document.querySelector(`.ext-ep-card[data-id="${id}"] .ext-ep-probe-echo`);
            if (newEcho) {
                newEcho.textContent = msg;
                newEcho.style.color = color;
            }
        } catch (e) {
            if (echoEl) {
                echoEl.textContent = 'Probe error: ' + e.message;
                echoEl.style.color = 'var(--danger,#c0392b)';
            }
        }
    }

    async function handleDelete(id, name) {
        if (!confirm(`Delete external endpoint "${name}"?\n\nThis will also remove it from all subsystem registries.`)) return;
        try {
            setStatus('Deleting…', '');
            await apiFetch(`/api/external-endpoints/${id}`, { method: 'DELETE' });
            await loadEndpoints();
            window.SynthWebUI?.loadEnginesSummary?.();
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

    async function handleSetModel(id, model, card) {
        const echoEl = card ? card.querySelector('.ext-ep-model-test-echo') : null;
        if (echoEl) {
            echoEl.textContent = 'Saving…';
            echoEl.style.color = 'var(--muted)';
        }
        try {
            await apiFetch(`/api/external-endpoints/${id}/model`, {
                method: 'PUT',
                body: JSON.stringify({ model: model || null }),
            });
            const endpoint = _endpoints.find((e) => e.id === id);
            if (endpoint) {
                endpoint.default_model = model || null;
            }
            if (echoEl) {
                echoEl.textContent = model ? `✓ Saved "${model}"` : '✓ Saved';
                echoEl.style.color = 'var(--success,#27ae60)';
            }
            window.SynthWebUI?.loadEnginesSummary?.();
        } catch (e) {
            if (echoEl) {
                echoEl.textContent = `✗ ${e.message}`;
                echoEl.style.color = 'var(--danger,#c0392b)';
            }
            await loadEndpoints();
        }
    }

    // -----------------------------------------------------------------------
    // Add / Edit modal
    // -----------------------------------------------------------------------

    // -----------------------------------------------------------------------
    // Wizard — Step 1: provider picker
    // -----------------------------------------------------------------------

    async function ensurePresets() {
        if (_presetsLoaded) return;
        try {
            const data = await apiFetch('/api/external-endpoints/presets');
            _presets = data.presets || [];
            _presetsLoaded = true;
        } catch (e) {
            _presets = [];
            _presetsLoaded = true;
        }
    }

    function renderPresetGrid() {
        const grid = document.getElementById('ext-ep-preset-grid');
        const loading = document.getElementById('ext-ep-preset-loading');
        if (!grid) return;
        if (loading) loading.style.display = 'none';
        grid.innerHTML = '';

        const cardStyle = [
            'display:flex', 'flex-direction:column', 'align-items:center', 'justify-content:center',
            'gap:6px', 'padding:16px 10px 12px', 'border:1px solid var(--border,#444)',
            'border-radius:10px', 'cursor:pointer', 'text-align:center',
            'background:var(--background)', 'transition:border-color 0.15s,background 0.15s',
            'min-height:90px',
        ].join(';');

        for (const preset of _presets) {
            const card = document.createElement('div');
            card.style.cssText = cardStyle;
            card.innerHTML = `
                <span style="font-size:1.6rem; line-height:1;">${providerIcon(preset.icon)}</span>
                <span style="font-weight:700; font-size:0.9rem;">${preset.display_name}</span>
                <span style="font-size:0.75rem; color:var(--muted); line-height:1.3;">${preset.description}</span>
            `;
            card.addEventListener('mouseenter', () => {
                card.style.borderColor = 'var(--primary,#7b61ff)';
                card.style.background = 'var(--bg-card,#1a1a2e)';
            });
            card.addEventListener('mouseleave', () => {
                card.style.borderColor = 'var(--border,#444)';
                card.style.background = 'var(--background)';
            });
            card.addEventListener('click', () => selectPreset(preset));
            grid.appendChild(card);
        }

        if (_presets.length === 0) {
            grid.innerHTML = '<div style="color:var(--muted); font-size:0.9rem; grid-column:1/-1;">No providers found. You can still add a custom endpoint manually.</div>';
            selectPreset({
                provider_id: 'custom', display_name: 'Custom Endpoint', protocol: 'openai',
                base_url: 'http://localhost:8000', base_url_locked: false,
                requires_api_key: false, api_key_placeholder: '', api_key_hint: '',
                default_capabilities: { cortex: true, vox: false, auris: false, live: false },
                suggested_name: '', suggested_label: '',
            });
        }
    }

    function showStep(step) {
        const s1 = document.getElementById('ext-ep-step-template');
        const s2 = document.getElementById('ext-ep-step-form');
        if (s1) s1.style.display = step === 1 ? 'block' : 'none';
        if (s2) s2.style.display = step === 2 ? 'block' : 'none';
    }

    function selectPreset(preset) {
        setField('ext-ep-form-id', '');
        setField('ext-ep-form-name', preset.suggested_name || '');
        setField('ext-ep-form-label', preset.suggested_label || '');
        setField('ext-ep-form-protocol', preset.protocol || 'openai');
        setField('ext-ep-form-url', preset.base_url || '');
        setField('ext-ep-form-key', '');

        const urlInput = document.getElementById('ext-ep-form-url');
        const unlockBtn = document.getElementById('ext-ep-url-unlock-btn');
        if (urlInput && unlockBtn) {
            const locked = !!preset.base_url_locked;
            urlInput.readOnly = locked;
            urlInput.style.opacity = locked ? '0.7' : '1';
            unlockBtn.style.display = locked ? 'flex' : 'none';
        }

        const keyInput = document.getElementById('ext-ep-form-key');
        const keyHint = document.getElementById('ext-ep-form-key-hint');
        const keyBadge = document.getElementById('ext-ep-form-key-required-badge');
        if (keyInput) keyInput.placeholder = preset.api_key_placeholder || '';
        if (keyHint) {
            keyHint.innerHTML = preset.api_key_hint
                ? preset.api_key_hint.replace(/(https?:\/\/\S+)/g, '<a href="$1" target="_blank" rel="noopener" style="color:var(--primary);">$1</a>')
                : '';
        }
        if (keyBadge) keyBadge.textContent = preset.requires_api_key ? '(required)' : '';

        const caps = preset.default_capabilities || {};
        for (const k of ['cortex', 'vox', 'auris', 'vision', 'live']) {
            const cb = document.getElementById(`ext-ep-form-cap-${k}`);
            if (cb) cb.checked = !!caps[k];
        }

        const header = document.getElementById('ext-ep-form-provider-header');
        const provName = document.getElementById('ext-ep-form-provider-name');
        const backBtn = document.getElementById('ext-ep-back-btn');
        if (header) header.style.display = 'flex';
        if (provName) provName.textContent = preset.display_name || '';
        if (backBtn) backBtn.style.display = _presets.length > 0 ? 'inline-flex' : 'none';

        const nameInput = document.getElementById('ext-ep-form-name');
        if (nameInput) nameInput.disabled = false;

        const identityDetails = document.getElementById('ext-ep-identity-details');
        if (identityDetails) identityDetails.open = !preset.suggested_name;

        document.getElementById('ext-ep-modal-title').textContent = 'Add External Endpoint';
        showStep(2);
    }

    // -----------------------------------------------------------------------
    // Wizard — Step 2: edit existing endpoint
    // -----------------------------------------------------------------------

    function openModalEdit(ep) {
        setModalError('');
        document.getElementById('ext-ep-modal-title').textContent = 'Edit Endpoint';

        setField('ext-ep-form-id', ep.id);
        const nameInput = document.getElementById('ext-ep-form-name');
        if (nameInput) { nameInput.value = ep.name; nameInput.disabled = true; }
        setField('ext-ep-form-label', ep.display_label || '');
        setField('ext-ep-form-protocol', ep.protocol || 'openai');
        setField('ext-ep-form-url', ep.base_url || '');
        setField('ext-ep-form-key', '');

        const urlInput = document.getElementById('ext-ep-form-url');
        const unlockBtn = document.getElementById('ext-ep-url-unlock-btn');
        if (urlInput) { urlInput.readOnly = false; urlInput.style.opacity = '1'; }
        if (unlockBtn) unlockBtn.style.display = 'none';

        const keyInput = document.getElementById('ext-ep-form-key');
        if (keyInput) keyInput.placeholder = ep.has_api_key ? '(leave blank to keep existing)' : '(optional)';
        const keyHint = document.getElementById('ext-ep-form-key-hint');
        if (keyHint) keyHint.innerHTML = '';
        const keyBadge = document.getElementById('ext-ep-form-key-required-badge');
        if (keyBadge) keyBadge.textContent = '';

        const smap = ep.effective_subsystem_map || {};
        for (const k of ['cortex', 'vox', 'auris', 'vision', 'live']) {
            const cb = document.getElementById(`ext-ep-form-cap-${k}`);
            if (cb) cb.checked = !!smap[k];
        }

        const header = document.getElementById('ext-ep-form-provider-header');
        if (header) header.style.display = 'none';

        showStep(2);
        showModal();
    }

    // -----------------------------------------------------------------------
    // Modal open / close
    // -----------------------------------------------------------------------

    async function openModalAdd() {
        setModalError('');
        showStep(1);
        showModal();

        if (!_presetsLoaded) {
            const loading = document.getElementById('ext-ep-preset-loading');
            if (loading) loading.style.display = 'block';
            await ensurePresets();
        }
        renderPresetGrid();
    }

    function showModal() {
        const modal = document.getElementById('ext-ep-modal');
        if (modal) modal.style.display = 'flex';
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

    function setField(id, value) {
        const el = document.getElementById(id);
        if (el) el.value = value;
    }

    async function handleFormSubmit(e) {
        e.preventDefault();
        setModalError('');
        const id = document.getElementById('ext-ep-form-id').value;
        const nameEl = document.getElementById('ext-ep-form-name');
        const name = (nameEl ? nameEl.value : '').trim();
        const base_url = (document.getElementById('ext-ep-form-url').value || '').trim();
        const display_label = (document.getElementById('ext-ep-form-label').value || '').trim();
        const protocol = document.getElementById('ext-ep-form-protocol').value || 'openai';
        const key = (document.getElementById('ext-ep-form-key').value || '');

        // Client-side validation
        if (!id && !name) { setModalError('Name is required.'); return; }
        if (!base_url) { setModalError('Base URL is required.'); return; }

        // Collect capability checkboxes into subsystem_map
        const subsystem_map = {};
        for (const k of ['cortex', 'vox', 'auris', 'vision', 'live']) {
            const cb = document.getElementById(`ext-ep-form-cap-${k}`);
            if (cb) subsystem_map[k] = cb.checked;
        }

        const payload = { display_label, protocol, base_url, subsystem_map };
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
            window.SynthWebUI?.loadEnginesSummary?.();

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

    function initEnginesTab() {
        const addBtn = document.getElementById('ext-ep-add-btn');
        if (addBtn) addBtn.addEventListener('click', openModalAdd);

        const refreshBtn = document.getElementById('ext-ep-refresh-btn');
        if (refreshBtn) refreshBtn.addEventListener('click', loadEndpoints);

        // Step 1 cancel
        const cancelStep1 = document.getElementById('ext-ep-modal-cancel-step1');
        if (cancelStep1) cancelStep1.addEventListener('click', closeModal);

        // Step 2 back
        const backBtn = document.getElementById('ext-ep-back-btn');
        if (backBtn) backBtn.addEventListener('click', () => showStep(1));

        // Step 2 cancel
        const cancelBtn = document.getElementById('ext-ep-modal-cancel');
        if (cancelBtn) cancelBtn.addEventListener('click', closeModal);

        // URL unlock button
        const unlockBtn = document.getElementById('ext-ep-url-unlock-btn');
        const urlInput = document.getElementById('ext-ep-form-url');
        if (unlockBtn && urlInput) {
            unlockBtn.addEventListener('click', () => {
                urlInput.readOnly = false;
                urlInput.style.opacity = '1';
                unlockBtn.style.display = 'none';
                urlInput.focus();
            });
        }

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

        // Trigger engine configuration UI (cortex selectors, plugin list, etc.)
        if (window.SynthWebUI && typeof window.SynthWebUI.loadEnginesSummary === 'function') {
            window.SynthWebUI.loadEnginesSummary();
        }
    }

    // Register with SynthWebUI — the init function name must match the pattern
    // 'init' + capitalize(section) + 'Tab' where section is the data-tab value.
    // For data-tab="engines" → initEnginesTab (handles both endpoint CRUD and engine configuration)
    window.SynthWebUI = window.SynthWebUI || {};
    window.SynthWebUI['initEnginesTab'] = initEnginesTab;

    // If the tab is already visible, initialize now
    (function () {
        try {
            const panel = document.querySelector('[data-tab="engines"]');
            if (panel && panel.classList.contains('active')) initEnginesTab();
        } catch (e) { /* ignore */ }
    })();
})();
