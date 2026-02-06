// archive-window.mjs — Archive modal extracted from vrm-viewer
export function createArchiveModal() {
    try {
        // Keep local state in module-scope
        if (window.__archive_modal_instance) return window.__archive_modal_instance;

        let archiveModal = null;
        let archiveWinbox = null;
        let archiveMultiSelect = false;
        let archiveSelectedIds = new Set();

        const panel = document.createElement('div');
        panel.id = 'archive-panel';
        panel.className = 'synth-window-panel archive-panel';
        panel.style.display = 'flex';
        panel.style.flexDirection = 'column';
        panel.style.width = '100%';
        panel.style.height = '100%';

        const isMobileArchive = (typeof window !== 'undefined' && window.innerWidth && window.innerWidth <= 768);
        const canUseWinBox = !isMobileArchive && window.SynthWindowManager && typeof window.SynthWindowManager.create === 'function' && typeof window.WinBox !== 'undefined';
        if (isMobileArchive) {
            panel.style.cssText = `
                position: fixed;
                z-index: 10500;
                left: 0;
                top: 0;
                right: 0;
                bottom: 0;
                width: 100%;
                height: 100%;
                background: var(--panel-bg);
                color: var(--text);
                border: none;
                border-radius: 0;
                box-shadow: none;
                display: none; flex-direction: column; overflow: auto;
            `;
        } else if (!canUseWinBox) {
            panel.style.cssText = `
                position: fixed;
                z-index: 10080;
                right: 2rem;
                bottom: 4rem;
                width: 720px;
                height: 520px;
                background: var(--panel-bg);
                color: var(--text);
                border: 1px solid var(--border);
                border-radius: 18px;
                box-shadow: 0 40px 80px -40px rgba(0,0,0,0.95);
                display: none; flex-direction: column; overflow: hidden;
            `;
        }

        panel.innerHTML = `
            <div id="archive-header" class="archive-header">
                <div class="archive-title">Archives</div>
                <div class="archive-controls">
                    <label><input id="show-archived" type="checkbox" /> Show archived</label>
                    <button id="archive-refresh" class="pill secondary">Refresh</button>
                </div>
            </div>
            <div class="archive-list" id="archive-list" style="flex:1 1 auto;overflow:auto;padding:12px;">
                <div class="meta">Loading…</div>
            </div>
            <div class="archive-footer" style="padding:12px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center;justify-content:space-between;">
                <div>
                    <button id="archive-delete-btn" class="pill secondary" type="button">Delete</button>
                    <button id="archive-restore-btn" class="pill" type="button">Restore</button>
                </div>
                <div id="archive-pagination" style="display:flex;gap:8px;align-items:center;">
                </div>
            </div>
        `;

        // Setup basic behaviors for now. The rest of the history rendering logic lives in history.js
        try {
            const listEl = panel.querySelector('#archive-list');
            const showArchived = panel.querySelector('#show-archived');
            const refreshBtn = panel.querySelector('#archive-refresh');

            const load = async () => {
                try {
                    if (!listEl) return;
                    listEl.innerHTML = '<div class="meta">Loading…</div>';
                    const res = await fetch('/api/history?include_archived=1');
                    if (!res.ok) { listEl.innerHTML = '<div class="meta">Failed to load</div>'; return; }
                    const data = await res.json();
                    listEl.innerHTML = '';
                    const items = Array.isArray(data.items) ? data.items : [];
                    if (!items.length) {
                        listEl.innerHTML = '<div class="meta">No archived items</div>';
                        return;
                    }
                    items.forEach((it) => {
                        const row = document.createElement('div');
                        row.className = 'archive-row';
                        row.dataset.id = it.id;
                        row.innerHTML = `<div style="font-weight:600">${it.title || 'Entry'}</div><div style="font-size:12px;color:var(--text-soft)">${it.summary || ''}</div>`;
                        listEl.appendChild(row);
                    });
                } catch (e) {
                    try { listEl.innerHTML = '<div class="meta">Failed to load</div>'; } catch (e) {}
                }
            };

            if (refreshBtn) refreshBtn.addEventListener('click', () => { load(); });
            if (showArchived) showArchived.addEventListener('change', () => { load(); });
            // initial load
            load();
        } catch (e) { /* ignore */ }

        window.__archive_modal_instance = panel;
        // Register for backwards compatibility
        try { window.ArchiveWindow = window.ArchiveWindow || {}; window.ArchiveWindow.createArchiveModal = createArchiveModal; } catch (e) { /* ignore */ }
        return panel;
    } catch (err) {
        console.warn('[archive-window] createArchiveModal failed', err);
        return null;
    }
}

export default { createArchiveModal };
