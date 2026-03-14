// model-manager.mjs — Manage Models modal (WinBox-based)
// Reads from GET /api/models and drives download/delete via the model management API.

const _MM_POLL_INTERVAL_MS = 2000; // progress polling interval

// Human-readable language labels
const _LANG_LABELS = {
    en: '🇬🇧 English',
    it: '🇮🇹 Italiano',
    de: '🇩🇪 Deutsch',
    fr: '🇫🇷 Français',
    es: '🇪🇸 Español',
    ja: '🇯🇵 日本語',
    zh: '🇨🇳 中文',
    ko: '🇰🇷 한국어',
    pt: '🇵🇹 Português',
    ru: '🇷🇺 Русский',
};

/** Open (or focus) the Manage Models modal window. */
export function createModelManagerModal() {
    // Singleton guard
    if (window.__model_manager_instance) {
        try {
            const wb = window.__model_manager_winbox;
            if (wb && typeof wb.focus === 'function') {
                wb.focus();
                return window.__model_manager_instance;
            }
        } catch (_) {}
        try { window.__model_manager_instance.remove(); } catch (_) {}
        window.__model_manager_instance = null;
        window.__model_manager_winbox = null;
    }

    // ------------------------------------------------------------------ panel
    const panel = document.createElement('div');
    panel.id = 'model-manager-panel';
    panel.className = 'synth-window-panel';
    panel.style.cssText = 'display:flex;flex-direction:column;width:100%;height:100%;box-sizing:border-box;';

    panel.innerHTML = `
        <div id="mm-header" style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border,#444);">
            <span style="font-weight:700;font-size:1.05rem;letter-spacing:0.03em;">🗂 Manage Models</span>
            <div style="display:flex;gap:8px;align-items:center;">
                <button id="mm-refresh" class="pill secondary" style="padding:4px 12px;font-size:0.85rem;">Refresh</button>
                <button id="mm-close" class="pill ghost" title="Close" style="font-weight:700;padding:4px 12px;">✕</button>
            </div>
        </div>
        <div id="mm-body" style="flex:1;overflow-y:auto;padding:14px 16px;">
            <div class="meta" id="mm-loading">Loading model catalog…</div>
        </div>
        <div id="mm-footer" style="padding:10px 16px;border-top:1px solid var(--border,#444);font-size:0.8rem;color:var(--muted);">
            Models are stored in <code>SYNTH_MODELS_DIR</code> and are not bundled with the container image.
        </div>
    `;

    // ------------------------------------------------------------------ state
    let _pollTimer = null;
    let _activeDownloads = new Set();

    // ------------------------------------------------------------------ API helpers
    async function apiGet(url) {
        const r = await fetch(url);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }
    async function apiPost(url, body) {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || `HTTP ${r.status}`); }
        return r.json();
    }
    async function apiDelete(url) {
        const r = await fetch(url, { method: 'DELETE' });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || `HTTP ${r.status}`); }
        return r.json();
    }

    // ------------------------------------------------------------------ render helpers
    function _badge(text, color) {
        return `<span style="display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.75rem;font-weight:700;background:${color};color:#000;margin-left:6px;">${text}</span>`;
    }

    function _statusBadge(model) {
        if (model.downloading) {
            const pct = model.download_progress != null ? Math.round(model.download_progress * 100) : 0;
            return _badge(`⬇ ${pct}%`, '#ffd166');
        }
        if (model.downloaded) return _badge('✓ downloaded', '#18c98c');
        return _badge('not downloaded', '#555');
    }

    function _actionBtns(model) {
        const id = model.model_id;
        if (model.downloading) {
            return `<span class="meta" style="font-size:0.8rem;">Downloading…</span>`;
        }
        if (model.downloaded) {
            return `
                <button class="pill secondary mm-update" data-model="${id}" style="padding:3px 10px;font-size:0.8rem;">↻ Update</button>
                <button class="pill ghost mm-delete" data-model="${id}" style="padding:3px 10px;font-size:0.8rem;color:#ff6b6b;border-color:#ff6b6b;">✕ Delete</button>
            `.trim();
        }
        return `<button class="pill mm-download" data-model="${id}" style="padding:3px 10px;font-size:0.8rem;">⬇ Download</button>`;
    }

    /** Resolve the supported languages for a given voice inside a model.
     *
     *  voices_meta entries use ``"*"`` to mean "inherit from model.supported_languages".
     *  Explicit language codes override that.
     */
    function _voiceLangs(model, voiceName) {
        const modelLangs = model.supported_languages && model.supported_languages.length
            ? model.supported_languages
            : ['en'];
        if (!model.voices_meta || !model.voices_meta.length) return modelLangs;
        const meta = model.voices_meta.find(v => v.name === voiceName);
        if (!meta) return modelLangs;
        if (meta.languages.length === 1 && meta.languages[0] === '*') return modelLangs;
        return meta.languages;
    }

    /** Build the voice-picker section for a downloaded TTS model. */
    function _voicePickerHtml(model) {
        if (!model.downloaded || !model.voices || !model.voices.length) return '';

        const mid = model.model_id;
        const firstVoice = model.voices[0];
        const firstLangs = _voiceLangs(model, firstVoice);
        const firstLang = firstLangs[0] || 'en';

        // Voice options
        const voiceOpts = model.voices
            .map(v => `<option value="${v}">${v}</option>`)
            .join('');

        // Language options for first voice
        const langOpts = firstLangs
            .map(l => `<option value="${l}">${_LANG_LABELS[l] || l.toUpperCase()}</option>`)
            .join('');

        return `
            <div class="mm-voice-picker" data-model="${mid}"
                 style="margin-top:10px;padding:10px 12px;border:1px solid var(--border,#333);
                        border-radius:10px;background:rgba(255,255,255,0.03);">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <label style="font-size:0.8rem;color:var(--muted);white-space:nowrap;">🎙 Voice:</label>
                    <select class="mm-voice-select" data-model="${mid}"
                            style="font-size:0.82rem;padding:3px 8px;border-radius:6px;
                                   background:var(--input-bg,#222);color:var(--text,#eee);
                                   border:1px solid var(--border,#444);">
                        ${voiceOpts}
                    </select>
                    <label style="font-size:0.8rem;color:var(--muted);white-space:nowrap;">🌐 Language:</label>
                    <select class="mm-lang-select" data-model="${mid}"
                            style="font-size:0.82rem;padding:3px 8px;border-radius:6px;
                                   background:var(--input-bg,#222);color:var(--text,#eee);
                                   border:1px solid var(--border,#444);">
                        ${langOpts}
                    </select>
                    <button class="pill ghost mm-play-btn" data-model="${mid}"
                            data-voice="${firstVoice}" data-lang="${firstLang}"
                            style="padding:3px 12px;font-size:0.82rem;display:none;"
                            title="Play sample">▶ Play</button>
                    <button class="pill mm-gen-btn" data-model="${mid}"
                            data-voice="${firstVoice}" data-lang="${firstLang}"
                            style="padding:3px 12px;font-size:0.82rem;display:none;"
                            title="Generate sample">⚡ Generate</button>
                    <span class="mm-sample-spinner" data-model="${mid}"
                          style="display:none;font-size:0.8rem;color:var(--muted);">Generating…</span>
                </div>
            </div>`;
    }

    function renderCatalog(catalog) {
        const body = panel.querySelector('#mm-body');
        if (!catalog || !catalog.length) {
            body.innerHTML = '<div class="meta">No models registered. Enable a TTS/STT plugin to see available models.</div>';
            return;
        }

        // Group by plugin_id
        const groups = {};
        for (const m of catalog) {
            (groups[m.plugin_id] = groups[m.plugin_id] || []).push(m);
        }

        const _pluginLabel = {
            vox_kitten: 'Vox — KittenTTS (Text to Speech)',
            auris_vosk: 'Auris — Vosk (Speech to Text)',
        };

        let html = '';
        for (const [pluginId, models] of Object.entries(groups)) {
            const label = _pluginLabel[pluginId] || pluginId;
            html += `<div style="margin-bottom:24px;">
                <h4 style="margin:0 0 10px;font-size:0.9rem;text-transform:uppercase;letter-spacing:0.06em;color:var(--muted);">${label}</h4>
                <div style="display:flex;flex-direction:column;gap:8px;">`;

            for (const m of models) {
                const voiceList = m.voices && m.voices.length
                    ? `<span class="meta" style="font-size:0.78rem;">${m.voices.join(', ')}</span>`
                    : '';
                const langList = m.supported_languages && m.supported_languages.length
                    ? `<span class="meta" style="font-size:0.78rem;margin-right:6px;">🌐 ${m.supported_languages.map(l => l.toUpperCase()).join(', ')}</span>`
                    : (m.language ? `<span class="meta" style="font-size:0.78rem;margin-right:6px;">🌐 ${m.language.toUpperCase()}</span>` : '');
                const size = m.size_mb ? `<span class="meta" style="font-size:0.78rem;margin-right:6px;">~${m.size_mb} MB</span>` : '';

                html += `
                    <div class="card" style="padding:10px 14px;border:1px solid var(--border,#444);border-radius:10px;background:var(--background);" data-model-row="${m.model_id}">
                        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:4px;">
                            <strong style="font-size:0.95rem;">${m.display_name}</strong>
                            ${_statusBadge(m)}
                        </div>
                        <div style="margin-bottom:6px;">${langList}${size}${voiceList}</div>
                        <div class="meta" style="font-size:0.82rem;margin-bottom:8px;">${m.description}</div>
                        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;" class="mm-actions">
                            ${_actionBtns(m)}
                        </div>
                        <div class="mm-progress-wrap" style="${m.downloading ? '' : 'display:none;'}margin-top:6px;">
                            <progress max="100" value="${m.download_progress != null ? Math.round(m.download_progress * 100) : 0}" style="width:100%;height:6px;border-radius:3px;"></progress>
                        </div>
                        ${_voicePickerHtml(m)}
                    </div>
                `;
            }
            html += '</div></div>';
        }
        body.innerHTML = html;
        _bindEvents(body);

        // After render: check existence of default (first voice + first lang) for each picker
        body.querySelectorAll('.mm-voice-picker').forEach(picker => {
            const mid = picker.dataset.model;
            const voiceSel = picker.querySelector('.mm-voice-select');
            const langSel = picker.querySelector('.mm-lang-select');
            if (voiceSel && langSel) {
                _checkAndShowSampleBtn(picker, mid, voiceSel.value, langSel.value);
            }
        });
    }

    /** Check sample existence for (model, voice, lang) and toggle Play / Generate buttons. */
    async function _checkAndShowSampleBtn(picker, mid, voice, lang) {
        const playBtn = picker.querySelector('.mm-play-btn');
        const genBtn = picker.querySelector('.mm-gen-btn');
        if (!playBtn || !genBtn) return;

        // Hide both while checking
        playBtn.style.display = 'none';
        genBtn.style.display = 'none';

        try {
            const data = await apiGet(`/api/models/${encodeURIComponent(mid)}/voice/${encodeURIComponent(voice)}/exists?lang=${encodeURIComponent(lang)}`);
            playBtn.dataset.voice = voice;
            playBtn.dataset.lang = lang;
            genBtn.dataset.voice = voice;
            genBtn.dataset.lang = lang;
            if (data.exists) {
                playBtn.style.display = '';
                genBtn.style.display = 'none';
            } else {
                playBtn.style.display = 'none';
                genBtn.style.display = '';
            }
        } catch (_) {
            // On error assume sample not available → show Generate
            genBtn.dataset.voice = voice;
            genBtn.dataset.lang = lang;
            genBtn.style.display = '';
        }
    }

    function _bindEvents(root) {
        // Download
        root.querySelectorAll('.mm-download').forEach(btn => {
            btn.addEventListener('click', () => _confirmDownload(btn.dataset.model, false));
        });
        // Delete
        root.querySelectorAll('.mm-delete').forEach(btn => {
            btn.addEventListener('click', () => _confirmDelete(btn.dataset.model));
        });
        // Update (re-download)
        root.querySelectorAll('.mm-update').forEach(btn => {
            btn.addEventListener('click', () => _confirmDownload(btn.dataset.model, true));
        });

        // Voice / language select change → re-check existence
        root.querySelectorAll('.mm-voice-picker').forEach(picker => {
            const mid = picker.dataset.model;
            const voiceSel = picker.querySelector('.mm-voice-select');
            const langSel = picker.querySelector('.mm-lang-select');
            if (!voiceSel || !langSel) return;

            // Rebuild model catalog entry for this row (passed in as closure via the outer catalog array)
            const modelRow = picker.closest('[data-model-row]');
            const catalogModels = modelRow
                ? Array.from(root.querySelectorAll('[data-model-row]')).reduce((acc, el) => {
                    acc[el.dataset.modelRow] = el;
                    return acc;
                }, {})
                : {};

            function _onVoiceChange() {
                // Rebuild language options for the newly selected voice
                // Find the model's voices_meta from a data attribute we bake in
                const voice = voiceSel.value;
                // Try to get voices_meta from a data attr on the picker (populated below)
                let voicesMeta = [];
                try { voicesMeta = JSON.parse(picker.dataset.voicesMeta || '[]'); } catch (_) {}
                let modelLangs = [];
                try { modelLangs = JSON.parse(picker.dataset.supportedLangs || '["en"]'); } catch (_) {}

                const meta = voicesMeta.find(v => v.name === voice);
                let langs = modelLangs;
                if (meta && meta.languages && !(meta.languages.length === 1 && meta.languages[0] === '*')) {
                    langs = meta.languages;
                }

                // Rebuild lang selector
                langSel.innerHTML = langs
                    .map(l => `<option value="${l}">${_LANG_LABELS[l] || l.toUpperCase()}</option>`)
                    .join('');

                _checkAndShowSampleBtn(picker, mid, voice, langSel.value);
            }

            function _onLangChange() {
                _checkAndShowSampleBtn(picker, mid, voiceSel.value, langSel.value);
            }

            voiceSel.addEventListener('change', _onVoiceChange);
            langSel.addEventListener('change', _onLangChange);
        });

        // Store model metadata on each picker for select-change handlers
        root.querySelectorAll('.mm-voice-picker').forEach(picker => {
            const mid = picker.dataset.model;
            // Find catalog entry for this model
            // We re-load catalog from server lazily; for now, stash data-attrs via the render
        });

        // Play sample
        root.addEventListener('click', e => {
            const btn = e.target.closest('.mm-play-btn');
            if (!btn) return;
            const mid = btn.dataset.model;
            const voice = btn.dataset.voice;
            const lang = btn.dataset.lang || 'en';
            const url = `/api/models/${encodeURIComponent(mid)}/sample/${encodeURIComponent(voice)}?lang=${encodeURIComponent(lang)}`;
            const audio = new Audio(url);
            audio.play().catch(err => console.warn('[model-manager] sample play error', err));
        });

        // Generate sample
        root.addEventListener('click', async e => {
            const btn = e.target.closest('.mm-gen-btn');
            if (!btn) return;
            const mid = btn.dataset.model;
            const voice = btn.dataset.voice;
            const lang = btn.dataset.lang || 'en';
            const picker = btn.closest('.mm-voice-picker');
            const spinner = picker ? picker.querySelector('.mm-sample-spinner') : null;

            btn.disabled = true;
            if (spinner) spinner.style.display = '';
            try {
                const result = await apiPost(
                    `/api/models/${encodeURIComponent(mid)}/voice/${encodeURIComponent(voice)}/generate?lang=${encodeURIComponent(lang)}`
                );
                if (result.url) {
                    // Transform button into Play
                    const playBtn = picker ? picker.querySelector('.mm-play-btn') : null;
                    if (playBtn) {
                        playBtn.dataset.voice = voice;
                        playBtn.dataset.lang = lang;
                        playBtn.style.display = '';
                    }
                    btn.style.display = 'none';
                    // Auto-play the freshly generated sample
                    new Audio(result.url).play().catch(() => {});
                }
            } catch (err) {
                alert(`Failed to generate sample: ${err.message}`);
            } finally {
                btn.disabled = false;
                if (spinner) spinner.style.display = 'none';
            }
        });
    }

    // ------------------------------------------------------------------ actions
    async function _confirmDownload(modelId, isUpdate) {
        const spec = await apiGet(`/api/models/${encodeURIComponent(modelId)}`).catch(() => null);
        const label = spec ? spec.display_name : modelId;
        const size = spec ? ` (~${spec.size_mb} MB)` : '';
        const verb = isUpdate ? 'update' : 'download';
        if (!window.confirm(`${verb.charAt(0).toUpperCase() + verb.slice(1)} "${label}"${size}?\n\nThis will download the model to your server's SYNTH_MODELS_DIR.`)) return;

        try {
            if (isUpdate) {
                await apiDelete(`/api/models/${encodeURIComponent(modelId)}`).catch(() => null);
            }
            await apiPost(`/api/models/${encodeURIComponent(modelId)}/download`);
            _activeDownloads.add(modelId);
            _startPolling();
            _loadCatalog();
        } catch (e) {
            alert(`Failed to start download: ${e.message}`);
        }
    }

    async function _confirmDelete(modelId) {
        const spec = await apiGet(`/api/models/${encodeURIComponent(modelId)}`).catch(() => null);
        const label = spec ? spec.display_name : modelId;
        if (!window.confirm(`Delete "${label}" from disk?\n\nThis only removes cached files; you can re-download any time.`)) return;

        try {
            await apiDelete(`/api/models/${encodeURIComponent(modelId)}`);
            _activeDownloads.delete(modelId);
            _loadCatalog();
        } catch (e) {
            alert(`Failed to delete model: ${e.message}`);
        }
    }

    // ------------------------------------------------------------------ polling
    function _startPolling() {
        if (_pollTimer) return;
        _pollTimer = setInterval(_pollProgress, _MM_POLL_INTERVAL_MS);
    }
    function _stopPolling() {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    }

    async function _pollProgress() {
        if (!_activeDownloads.size) { _stopPolling(); return; }
        let anyActive = false;
        for (const modelId of [..._activeDownloads]) {
            try {
                const data = await apiGet(`/api/models/${encodeURIComponent(modelId)}/progress`);
                if (data.downloaded) {
                    _activeDownloads.delete(modelId);
                } else if (data.in_progress) {
                    anyActive = true;
                    const row = panel.querySelector(`[data-model-row="${modelId}"]`);
                    if (row) {
                        const prog = row.querySelector('progress');
                        if (prog && data.progress != null) prog.value = Math.round(data.progress * 100);
                        const wrap = row.querySelector('.mm-progress-wrap');
                        if (wrap) wrap.style.display = '';
                    }
                } else {
                    _activeDownloads.delete(modelId);
                }
            } catch (_) { /* ignore transient errors */ }
        }
        if (!anyActive && !_activeDownloads.size) {
            _stopPolling();
            _loadCatalog();
        }
    }

    // ------------------------------------------------------------------ load
    async function _loadCatalog() {
        try {
            const data = await apiGet('/api/models');
            const catalog = data.models || [];
            // Check if any downloads are in progress
            const inProgress = catalog.filter(m => m.downloading);
            for (const m of inProgress) _activeDownloads.add(m.model_id);
            if (_activeDownloads.size) _startPolling();
            renderCatalog(catalog);
            // After render: store voices_meta / supported_languages on each picker
            catalog.forEach(m => {
                const picker = panel.querySelector(`.mm-voice-picker[data-model="${m.model_id}"]`);
                if (!picker) return;
                picker.dataset.voicesMeta = JSON.stringify(m.voices_meta || []);
                picker.dataset.supportedLangs = JSON.stringify(m.supported_languages || ['en']);
            });
        } catch (e) {
            const body = panel.querySelector('#mm-body');
            body.innerHTML = `<div class="meta" style="color:#ff6b6b;">Failed to load model catalog: ${e.message}</div>`;
        }
    }

    // ------------------------------------------------------------------ header buttons
    panel.querySelector('#mm-refresh').addEventListener('click', _loadCatalog);
    panel.querySelector('#mm-close').addEventListener('click', () => {
        try {
            _stopPolling();
            const wb = window.__model_manager_winbox;
            if (wb && typeof wb.close === 'function') wb.close();
            else panel.remove();
        } catch (_) {}
        window.__model_manager_instance = null;
        window.__model_manager_winbox = null;
    });

    // ------------------------------------------------------------------ WinBox
    const canUseWinBox = window.SynthWindowManager && typeof window.SynthWindowManager.create === 'function' && typeof window.WinBox !== 'undefined';
    if (canUseWinBox) {
        const wb = window.SynthWindowManager.create({
            title: '🗂 Manage Models',
            width: '820px',
            height: '620px',
            x: 'center',
            y: 'center',
            mount: panel,
            onclose() {
                _stopPolling();
                window.__model_manager_instance = null;
                window.__model_manager_winbox = null;
            },
        });
        window.__model_manager_winbox = wb;
        try { panel.querySelector('#mm-close').style.display = 'none'; } catch (_) {}
    } else {
        panel.style.cssText = `
            position:fixed;z-index:10080;right:2rem;bottom:4rem;
            width:800px;height:600px;
            background:var(--panel-bg,#1a1a2e);color:var(--text,#eee);
            border:1px solid var(--border,#444);border-radius:18px;
            box-shadow:0 40px 80px -40px rgba(0,0,0,0.95);
            display:flex;flex-direction:column;overflow:hidden;
        `;
        document.body.appendChild(panel);
    }

    window.__model_manager_instance = panel;

    // Initial load
    _loadCatalog();

    return panel;
}


