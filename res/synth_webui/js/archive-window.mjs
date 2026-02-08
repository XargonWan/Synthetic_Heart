// archive-window.mjs — Archive modal extracted from vrm-viewer
export function createArchiveModal() {
    try {
        // Keep local state in module-scope
        if (window.__archive_modal_instance) {
            try {
                if (window.__archive_modal_instance.querySelector && !window.__archive_modal_instance.querySelector('#archive-edit')) {
                    // stale instance (old version) - remove and recreate
                    try { window.__archive_modal_instance.remove(); } catch (e) { /* ignore */ }
                    window.__archive_modal_instance = null;
                } else {
                    return window.__archive_modal_instance;
                }
            } catch (e) { return window.__archive_modal_instance; }
        }

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
                    <button id="archive-edit" class="pill secondary">Edit</button>
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

        // If a managed WinBox is available, create the WinBox instance and keep it hidden
        if (canUseWinBox && window.SynthWindowManager && typeof window.SynthWindowManager.create === 'function') {
            try {
                archiveWinbox = window.SynthWindowManager.create({
                    id: 'archives',
                    title: 'Archives',
                    mount: panel,
                    width: 720,
                    height: 520,
                    x: 'center',
                    y: 'center',
                    dockLabel: 'Archives',
                    dockClass: 'archive-toggle-btn',
                    className: 'synth-winbox'
                });
                // If created, hide initially to mimic modal behavior until restored
                try { if (archiveWinbox && typeof archiveWinbox.hide === 'function') archiveWinbox.hide(); } catch (e) { /* ignore */ }
                try { window.__archive_modal_winbox = archiveWinbox; } catch (e) { /* ignore */ }
                try { 
                    console.debug('[archive-window] WinBox instance created for archives'); 
                    try {
                        if (archiveWinbox && typeof archiveWinbox.onclose === 'function') {
                            const prev = archiveWinbox.onclose;
                            archiveWinbox.onclose = function(ev) {
                                try { window.__archive_modal_instance = null; } catch (e) {}
                                try { window.__archive_modal_winbox = null; } catch (e) {}
                                try { if (typeof prev === 'function') prev.call(this, ev); } catch (e) {}
                            };
                        }
                    } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            } catch (e) { console.warn('[archive-window] WinBox creation failed', e); }
        }

        // Setup basic behaviors for now. The rest of the history rendering logic lives in history.js
        try {
            const listEl = panel.querySelector('#archive-list');
            const showArchived = panel.querySelector('#show-archived');
            const refreshBtn = panel.querySelector('#archive-refresh');
            const editBtn = panel.querySelector('#archive-edit');
            const deleteBtn = panel.querySelector('#archive-delete-btn');

            const updateEditState = () => {
                try {
                    if (!panel) return;
                    if (archiveMultiSelect) panel.classList.add('archive-edit-mode');
                    else panel.classList.remove('archive-edit-mode');
                    if (editBtn) editBtn.textContent = archiveMultiSelect ? 'Done' : 'Edit';
                } catch (e) { /* ignore */ }
            };

            const updateSelectedState = () => {
                try {
                    if (!panel) return;
                    const rows = panel.querySelectorAll('.archive-row');
                    rows.forEach((row) => {
                        const archId = row.dataset.id;
                        const selected = archId && archiveSelectedIds.has(archId);
                        row.classList.toggle('selected', !!selected);
                        const check = row.querySelector('input.archive-check');
                        if (check) check.checked = !!selected;
                    });
                    updateArchiveRestoreState();
                } catch (e) { /* ignore */ }
            };

            const updateArchiveRestoreState = () => {
                try {
                    const hasSelection = archiveSelectedIds.size > 0;
                    if (deleteBtn) deleteBtn.disabled = !hasSelection;
                    const restoreBtn = panel.querySelector('#archive-restore-btn');
                    if (restoreBtn) restoreBtn.disabled = !hasSelection;
                } catch (e) { /* ignore */ }
            };

            const load = async () => {
                try {
                    if (!listEl) return;
                    listEl.innerHTML = '<div class="meta">Loading…</div>';
                    const res = await fetch('/api/chat/archives');
                    if (!res.ok) { listEl.innerHTML = '<div class="meta">Failed to load</div>'; return; }
                    const data = await res.json();
                    listEl.innerHTML = '';
                    const items = (data && data.success && Array.isArray(data.archives)) ? data.archives : [];
                    if (!items.length) {
                        listEl.innerHTML = '<div class="meta">No archived items</div>';
                        return;
                    }
                    items.forEach((it) => {
                        const row = document.createElement('div');
                        row.className = 'archive-row';
                        row.dataset.id = it.id;
                        const title = it.name || it.title || 'Chat';
                        const created = it.created_at ? `· ${it.created_at}` : '';
                        const count = (typeof it.message_count === 'number') ? `· ${it.message_count} msgs` : '';
                        row.innerHTML = `
                            <input class="archive-check" type="checkbox" />
                            <div>
                                <div style="font-weight:600">${title}</div>
                                <div style="font-size:12px;color:var(--text-soft)">${created} ${count}</div>
                            </div>
                        `;
                        row.addEventListener('click', (ev) => {
                            try {
                                const target = ev?.target;
                                // Ignore clicks on the checkbox itself
                                if (target && target.classList && target.classList.contains('archive-check')) return;
                                const archId = row.dataset.id;
                                if (!archId) return;
                                if (archiveMultiSelect) {
                                    // toggle in multi-select mode
                                    if (archiveSelectedIds.has(archId)) archiveSelectedIds.delete(archId);
                                    else archiveSelectedIds.add(archId);
                                } else {
                                    // single-select: clear previous and select this one
                                    archiveSelectedIds.clear();
                                    archiveSelectedIds.add(archId);
                                }
                                updateSelectedState();
                            } catch (e) { /* ignore */ }
                        });
                        const check = row.querySelector('input.archive-check');
                        if (check) {
                            check.addEventListener('change', () => {
                                const archId = row.dataset.id;
                                if (!archId) return;
                                if (check.checked) archiveSelectedIds.add(archId);
                                else archiveSelectedIds.delete(archId);
                                updateSelectedState();
                            });
                        }
                        listEl.appendChild(row);
                    });
                    updateSelectedState();
                } catch (e) {
                    try { listEl.innerHTML = '<div class="meta">Failed to load</div>'; } catch (e) {}
                }
            };

            if (refreshBtn) refreshBtn.addEventListener('click', () => { load(); });
            if (showArchived) showArchived.addEventListener('change', () => { load(); });
            if (editBtn) editBtn.addEventListener('click', () => {
                archiveMultiSelect = !archiveMultiSelect;
                if (!archiveMultiSelect) archiveSelectedIds.clear();
                updateEditState();
                updateSelectedState();
            });
            if (deleteBtn) deleteBtn.addEventListener('click', async () => {
                try {
                    if (archiveSelectedIds.size === 0) return;
                    const count = archiveSelectedIds.size;
                    if (!confirm(`Delete ${count} archive${count === 1 ? '' : 's'}? This cannot be undone.`)) return;
                    const ids = Array.from(archiveSelectedIds);
                    for (const archId of ids) {
                        try { await fetch(`/api/chat/archives/${archId}`, { method: 'DELETE' }); } catch (e) { /* ignore */ }
                    }
                    archiveSelectedIds.clear();
                    archiveMultiSelect = false;
                    updateEditState();
                    updateSelectedState();
                    await load();
                } catch (e) { /* ignore */ }
            });

            // Restore selected archive(s)
            const restoreBtn = panel.querySelector('#archive-restore-btn');
            if (restoreBtn) restoreBtn.addEventListener('click', async () => {
                try {
                    if (archiveSelectedIds.size === 0) return;
                    const count = archiveSelectedIds.size;
                    if (!confirm(`Restore ${count} archive${count === 1 ? '' : 's'}? This will replace the current chat.`)) return;
                    const ids = Array.from(archiveSelectedIds);
                    for (const archId of ids) {
                        try {
                            await fetch('/api/chat/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archive_id: archId }) });
                        } catch (e) { /* ignore */ }
                    }
                    try { if (archiveWinbox && typeof archiveWinbox.hide === 'function') archiveWinbox.hide(); else if (panel && panel.style) panel.style.display = 'none'; } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            });
            // Ensure edit button visible (hot-reload support)
            try {
                const editBtn = panel.querySelector('#archive-edit');
                if (editBtn) {
                    const cs = (window.getComputedStyle && window.getComputedStyle(editBtn)) || {};
                    if (cs.display === 'none' || cs.visibility === 'hidden' || editBtn.offsetParent === null) {
                        editBtn.style.display = 'inline-flex';
                        editBtn.style.visibility = 'visible';
                        editBtn.style.opacity = '1';
                    }
                    const header = panel.querySelector('#archive-header');
                    if (header) header.style.display = 'flex';
                }
            } catch (e) { /* ignore */ }

            updateEditState();
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
