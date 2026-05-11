/**
 * external-engines.js — Manages the External Engines section UI.
 *
 * Registered as window.SynthWebUI.initExternal_enginesTab so the main
 * loadSection() helper invokes it automatically after the HTML is injected.
 *
 * The add-endpoint flow is a guided 2-step wizard:
 *   Step 1 — Provider picker grid (loaded from /api/external-endpoints/presets)
 *   Step 2 — Pre-filled form (only essential fields front-and-center; advanced
 *             settings collapsed but always accessible)
 *
 * Edit mode skips Step 1 and opens directly in Step 2.
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
    // Render endpoint list
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

        // Subsystems — read-only badges
        const subsysEl = card.querySelector('.ext-ep-subsystems');
        const effectiveMap = ep.effective_subsystem_map || {};
        for (const key of ['cortex', 'vox', 'auris', 'vision', 'live']) {
            if (!(key in effectiveMap)) continue;
            const val = effectiveMap[key];
            const badge = document.createElement('span');
            badge.style.cssText = [
                'display:inline-flex', 'align-items:center', 'gap:4px',
                'font-size:0.8rem', 'padding:2px 9px', 'border-radius:12px',
                val ? 'background:var(--success-bg,#155724);color:#d4edda;' : 'background:var(--border,#444);color:var(--muted);',
            ].join(';');
            badge.textContent = subsystemLabel(key);
            subsysEl.appendChild(badge);
        }

        // Model selector
        const modelInput = card.querySelector('.ext-ep-model-select');
        const datalist = card.querySelector('.ext-ep-model-datalist');
        const listId = 'ext-ep-models-' + ep.id;
        datalist.id = listId;
        modelInput.setAttribute('list', listId);

        const models = Array.isArray(ep.available_models) ? ep.available_models : [];
        if (models.length === 0) {
            modelInput.placeholder = ep.probe_status === 'never'
                ? 'Type a model name or probe first...'
                : 'Type a model name or probe to refresh...';
            modelInput.disabled = false;
        } else {
            modelInput.placeholder = 'Type to search, select, or enter a model...';
            modelInput.disabled = false;
            for (const m of models) {
                const opt = document.createElement('option');
                opt.value = m;
                datalist.appendChild(opt);
            }
        }
        if (ep.default_model) {
            modelInput.value = ep.default_model;
        }

        modelInput.addEventListener('change', () => {
            handleSetModel(ep.id, modelInput.value, card);
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

        card.querySelector('.ext-ep-probe-btn').addEventListener('click', () => handleProbe(ep.id));
        card.querySelector('.ext-ep-delete-btn').addEventListener('click', () => handleDelete(ep.id, ep.display_label || ep.name));
        card.querySelector('.ext-ep-model-test').addEventListener('click', () => {
            const model = modelInput.value;
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
            window.SynthWebUI?.loadEnginesSummary?.();
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
            window.SynthWebUI?.loadEnginesSummary?.();
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
            window.SynthWebUI?.loadEnginesSummary?.();
            setStatus('');
        } catch (e) {
            setStatus('Error: ' + e.message, 'var(--danger,#c0392b)');
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
            // Auto-transition to form with empty "custom" preset
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
        // Pre-fill Step 2 form from preset data
        setField('ext-ep-form-id', '');
        setField('ext-ep-form-name', preset.suggested_name || '');
        setField('ext-ep-form-label', preset.suggested_label || '');
        setField('ext-ep-form-protocol', preset.protocol || 'openai');
        setField('ext-ep-form-url', preset.base_url || '');
        setField('ext-ep-form-key', '');

        // URL lock / unlock
        const urlInput = document.getElementById('ext-ep-form-url');
        const unlockBtn = document.getElementById('ext-ep-url-unlock-btn');
        if (urlInput && unlockBtn) {
            const locked = !!preset.base_url_locked;
            urlInput.readOnly = locked;
            urlInput.style.opacity = locked ? '0.7' : '1';
            unlockBtn.style.display = locked ? 'flex' : 'none';
            // Reset: unlock on new preset selection
            unlockBtn.dataset.unlocked = 'false';
            unlockBtn.title = 'Override URL';
            unlockBtn.textContent = '🔓';
        }

        // API key visibility + hint
        const keyRow = document.getElementById('ext-ep-form-key-row');
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
        if (keyRow) keyRow.style.display = 'block'; // always visible in step 2

        // Capabilities checkboxes
        const caps = preset.default_capabilities || {};
        for (const k of ['cortex', 'vox', 'auris', 'vision', 'live']) {
            const cb = document.getElementById(`ext-ep-form-cap-${k}`);
            if (cb) cb.checked = !!caps[k];
        }

        // Provider name header + back button visibility
        const header = document.getElementById('ext-ep-form-provider-header');
        const provName = document.getElementById('ext-ep-form-provider-name');
        const backBtn = document.getElementById('ext-ep-back-btn');
        if (header) header.style.display = 'flex';
        if (provName) provName.textContent = preset.display_name || '';
        if (backBtn) backBtn.style.display = _presets.length > 0 ? 'inline-flex' : 'none';

        // Name field: disable for edit (handled separately), enable for add
        const nameInput = document.getElementById('ext-ep-form-name');
        if (nameInput) nameInput.disabled = false;

        // Open identity details when suggested_name is empty (custom)
        const identityDetails = document.getElementById('ext-ep-identity-details');
        if (identityDetails) {
            identityDetails.open = !preset.suggested_name;
        }

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

        // URL always editable in edit mode
        const urlInput = document.getElementById('ext-ep-form-url');
        const unlockBtn = document.getElementById('ext-ep-url-unlock-btn');
        if (urlInput) { urlInput.readOnly = false; urlInput.style.opacity = '1'; }
        if (unlockBtn) unlockBtn.style.display = 'none';

        // API Key field
        const keyInput = document.getElementById('ext-ep-form-key');
        if (keyInput) keyInput.placeholder = ep.has_api_key ? '(leave blank to keep existing)' : '(optional)';
        const keyHint = document.getElementById('ext-ep-form-key-hint');
        if (keyHint) keyHint.innerHTML = '';
        const keyBadge = document.getElementById('ext-ep-form-key-required-badge');
        if (keyBadge) keyBadge.textContent = '';

        // Capabilities from effective_subsystem_map
        const smap = ep.effective_subsystem_map || {};
        for (const k of ['cortex', 'vox', 'auris', 'vision', 'live']) {
            const cb = document.getElementById(`ext-ep-form-cap-${k}`);
            if (cb) cb.checked = !!smap[k];
        }

        // Hide back button / provider name header in edit mode
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

    // -----------------------------------------------------------------------
    // Form submit
    // -----------------------------------------------------------------------

    async function handleFormSubmit(e) {
        e.preventDefault();
        setModalError('');
        const id = document.getElementById('ext-ep-form-id').value;
        const nameEl = document.getElementById('ext-ep-form-name');
        const name = (nameEl && !nameEl.disabled ? nameEl.value : nameEl?.value || '').trim();
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
                const probeErr = probe.error || 'unknown error';
                setModalError(
                    'Saved! But the probe failed: ' + probeErr +
                    '\nThe endpoint is saved but may not be reachable. ' +
                    'Check that the base URL is accessible from inside the container ' +
                    '(use host.docker.internal instead of localhost if needed).'
                );
                setStatus('');
            } else {
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
