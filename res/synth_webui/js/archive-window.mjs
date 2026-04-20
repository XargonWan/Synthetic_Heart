// archive-window.mjs — Archive modal extracted from vrm-viewer
export function createArchiveModal() {
    try {
        // Keep local state in module-scope
        if (window.__archive_modal_instance) {
            try {
                const existing = window.__archive_modal_instance;
                const editBtn = (existing.querySelector && existing.querySelector('#archive-edit')) ? existing.querySelector('#archive-edit') : null;
                const isVisible = (el) => {
                    try {
                        if (!el) return false;
                        const cs = (window.getComputedStyle && window.getComputedStyle(el)) || {};
                        return !(cs.display === 'none' || cs.visibility === 'hidden' || el.offsetParent === null);
                    } catch (e) { return false; }
                };
                if (!editBtn || !isVisible(editBtn)) {
                    // stale or hidden instance - remove and recreate to ensure visibility
                    try { existing.remove(); } catch (e) { /* ignore */ }
                    try { window.__archive_modal_winbox && window.__archive_modal_winbox.hide && window.__archive_modal_winbox.hide(); } catch (e) { /* ignore */ }
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

        // Prefer WinBox unconditionally when available. Fallback to legacy panel only when WinBox is not present.
        const canUseWinBox = window.SynthWindowManager && typeof window.SynthWindowManager.create === 'function' && typeof window.WinBox !== 'undefined';
        if (!canUseWinBox) {
            // Legacy panel fallback when WinBox is not available.
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
                    <button id="archive-refresh" class="pill secondary">Refresh</button>
                    <button id="archive-close" class="pill ghost" title="Close" style="margin-left:6px;font-weight:700;">✕</button>
                </div>
            </div>
            <div class="archive-list" id="archive-list" style="flex:1 1 auto;overflow:auto;padding:12px;">
                <div class="meta">Loading…</div>
            </div>
            <div class="archive-footer" style="padding:12px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center;justify-content:space-between;">
                <div>
                    <button id="archive-edit" class="pill secondary" type="button">Edit</button>
                    <button id="archive-delete-btn" class="pill secondary" type="button" style="display:none">Delete</button>
                    <button id="archive-restore-btn" class="pill" type="button" style="display:none">Restore</button>
                </div>
                <div id="archive-pagination" style="display:flex;gap:8px;align-items:center;">
                </div>
            </div>
        `;

        // Inject archive-specific styles into the document head (guarded to run once)
        try {
            if (!document.getElementById('archive-window-styles')) {
                const s = document.createElement('style');
                s.id = 'archive-window-styles';
                s.textContent = `
#archive-panel .archive-header { padding: 10px 14px; display: flex; align-items: center; justify-content: space-between; background: var(--surface-alt); border-bottom: 1px solid var(--border); }
#archive-panel .archive-title { font-weight: 600; letter-spacing: 0.02em; }
#archive-panel .archive-controls { display: flex; gap: 6px; align-items: center; }
#archive-panel .archive-list { flex: 1; overflow: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
#archive-panel .archive-footer { padding: 10px 12px; border-top: 1px solid var(--border); display: flex; gap: 8px; justify-content: flex-end; background: rgba(255, 255, 255, 0.02); }
#archive-panel .archive-row { background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border); padding: 8px 10px; }
#archive-panel .archive-row-inner { display:flex; gap:12px; align-items:flex-start; }
#archive-panel .archive-row .archive-check { display: none; accent-color: var(--accent); width:18px; height:18px; margin-top:2px; margin-right:8px; }
#archive-panel.archive-edit-mode .archive-row .archive-check { display: inline-block; }
#archive-panel.archive-edit-mode .archive-row { cursor: pointer; }
#archive-panel .archive-row:hover { border-color: var(--border-strong); }
#archive-panel .archive-row.selected { border-color: var(--accent); background: var(--accent-soft); }
#archive-panel .archive-row .archive-row-inner .meta { color: var(--text-soft); font-size:12px; margin-top:4px; }
#archive-panel .archive-row .archive-row-inner .archive-title { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
#archive-panel .archive-row input { background: rgba(255, 255, 255, 0.04); border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 6px 8px; }
#archive-panel .archive-row input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px rgba(255, 107, 214, 0.2); }
                `;
                document.head.appendChild(s);
            }
        } catch (e) { /* ignore style injection errors */ }

        // If a managed WinBox is available, create the WinBox instance and keep it hidden
        if (canUseWinBox && window.SynthWindowManager && typeof window.SynthWindowManager.create === 'function') {
            try {
                const opts = {
                    id: 'archives',
                    title: 'Archives',
                    mount: panel,
                    dockLabel: 'Archives',
                    dockClass: 'archive-toggle-btn',
                    // allow close button for the Archives window (no-full keeps fullscreen control hidden)
                    className: 'synth-winbox no-full'
                };
                opts.width = 720;
                opts.height = 520;
                opts.x = 'center';
                opts.y = 'center';
                archiveWinbox = window.SynthWindowManager.create(opts);
                // If created, hide initially to mimic modal behavior until restored.
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

                    // Attach Edit to the WinBox header so it's always visible and accessible.
                    try {
                        const renderHeaderTools = () => {
                            try {
                                const tools = [{
                                    label: archiveMultiSelect ? 'Done' : 'Edit',
                                    title: archiveMultiSelect ? 'Done' : 'Edit',
                                    className: 'archive-edit-btn',
                                    onClick: () => {
                                        try {
                                            archiveMultiSelect = !archiveMultiSelect;
                                            if (!archiveMultiSelect) archiveSelectedIds.clear();
                                            updateEditState();
                                            updateSelectedState();
                                            // re-render header tools to reflect label change
                                            setTimeout(renderHeaderTools, 0);
                                        } catch (e) { /* ignore */ }
                                    }
                                }];
                                try { /* header tool disabled: Edit is rendered in footer for consistent placement */ } catch (e) {}
                            } catch (e) { /* ignore */ }
                        };
                        renderHeaderTools();
                    } catch (e) { /* ignore */ }

                } catch (e) { /* ignore */ }
            } catch (e) { console.warn('[archive-window] WinBox creation failed', e); }
        }

        // Setup basic behaviors for now. The rest of the history rendering logic lives in history.js
        try {
            const listEl = panel.querySelector('#archive-list');
            const refreshBtn = panel.querySelector('#archive-refresh');
            const editBtn = panel.querySelector('#archive-edit');
            const deleteBtn = panel.querySelector('#archive-delete-btn');
            const closeBtn = panel.querySelector('#archive-close');

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
                    // Show/hide buttons based on selection — edit button remains always visible
                    if (deleteBtn) {
                        deleteBtn.style.display = hasSelection ? 'inline-flex' : 'none';
                    }
                    const restoreBtn = panel.querySelector('#archive-restore-btn');
                    if (restoreBtn) {
                        restoreBtn.style.display = hasSelection ? 'inline-flex' : 'none';
                    }
                } catch (e) { /* ignore */ }
            };

            const load = async () => {
                try {
                    if (!listEl) return;
                    console.debug('[archive-window] load() start');
                    listEl.innerHTML = '<div class="meta">Loading…</div>';
                    const res = await fetch((window.__getApiBase ? window.__getApiBase() : '') + '/api/chat/archives');
                    if (!res.ok) { listEl.innerHTML = '<div class="meta">Failed to load</div>'; console.warn('[archive-window] fetch returned non-ok', res.status); return; }
                    const data = await res.json();
                    const items = (data && data.success && Array.isArray(data.archives)) ? data.archives : [];
                    console.debug('[archive-window] load() got items:', (items && items.length) || 0, items);
                    listEl.innerHTML = '';
                    if (!items.length) {
                        listEl.innerHTML = '<div class="meta">No archived items</div>';
                        return;
                    }
                    items.forEach((it) => {
                        const row = document.createElement('div');
                        row.className = 'archive-row';
                        row.dataset.id = it.id;
                        const title = it.name || it.title || 'Chat';
                        // Format created_at to DD/MM/YYYY HH:MM for better readability
                        const formatDate = (s) => {
                            try {
                                if (!s) return '';
                                const d = new Date(s);
                                if (Number.isNaN(d.getTime())) return s;
                                const pad = (n) => (n < 10 ? '0' + n : n);
                                const day = pad(d.getDate());
                                const month = pad(d.getMonth() + 1);
                                const year = d.getFullYear();
                                const hours = pad(d.getHours());
                                const mins = pad(d.getMinutes());
                                return `${day}/${month}/${year} ${hours}:${mins}`;
                            } catch (e) { return s; }
                        };
                        const created = it.created_at ? `· ${formatDate(it.created_at)}` : '';
                        const count = (typeof it.message_count === 'number') ? `· ${it.message_count} msgs` : '';
                        row.innerHTML = `
                            <div class="archive-row-inner" style="display:flex;gap:12px;align-items:center;">
                                <div style="flex:0 0 auto; width:28px; display:flex; align-items:flex-start; justify-content:center;">
                                    <input class="archive-check" type="checkbox" />
                                </div>
                                <div style="flex:1 1 auto; min-width:0;">
                                    <div style="font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${title}</div>
                                    <div style="font-size:12px;color:var(--text-soft); margin-top:4px;">${created} ${count}</div>
                                </div>
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
                    console.error('[archive-window] load() error', e);
                    try { listEl.innerHTML = '<div class="meta">Failed to load</div>'; } catch (e) {}
                }
            };

            if (refreshBtn) refreshBtn.addEventListener('click', () => { load(); });
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
                        try { await fetch((window.__getApiBase ? window.__getApiBase() : '') + `/api/chat/archives/${archId}`, { method: 'DELETE' }); } catch (e) { /* ignore */ }
                    }
                    archiveSelectedIds.clear();
                    archiveMultiSelect = false;
                    updateEditState();
                    updateSelectedState();
                    await load();
                } catch (e) { /* ignore */ }
            });

            // Close/hide handler (works for both WinBox and legacy panel)
            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    try {
                        if (archiveWinbox && typeof archiveWinbox.close === 'function') {
                            archiveWinbox.close();
                        } else if (panel && panel.style) {
                            panel.style.display = 'none';
                        }
                        try { window.__archive_modal_instance = null; } catch (e) {}
                        try { window.__archive_modal_winbox = null; } catch (e) {}
                    } catch (e) { /* ignore */ }
                });
            }

            // Restore selected archive(s)
            const restoreBtn = panel.querySelector('#archive-restore-btn');
            if (restoreBtn) restoreBtn.addEventListener('click', async () => {
                try {
                    if (archiveSelectedIds.size === 0) return;
                    const count = archiveSelectedIds.size;
                    if (!confirm(`Restore ${count} archive${count === 1 ? '' : 's'}? This will archive the current chat (if non-empty) and replace it.`)) return;
                    const ids = Array.from(archiveSelectedIds);
                    let successCount = 0;
                    for (const archId of ids) {
                        try {
                            const res = await fetch((window.__getApiBase ? window.__getApiBase() : '') + '/api/chat/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archive_id: archId }) });
                            if (res && res.ok) {
                                try { const out = await res.json(); if (out && out.success) successCount++; } catch (e) { /* ignore */ }
                            }
                        } catch (e) { /* ignore */ }
                    }
                    // Clear selection and update UI
                    try { archiveSelectedIds.clear(); archiveMultiSelect = false; updateEditState(); updateSelectedState(); } catch (e) { /* ignore */ }
                    // Notify user about restore result
                    try {
                        if (typeof window.showToast === 'function') {
                            if (successCount > 0) window.showToast(`Restored ${successCount} archive${successCount === 1 ? '' : 's'}`, false);
                            else window.showToast('Restore completed (no messages restored)', true);
                        }
                    } catch (e) { /* ignore */ }
                    // Mark global timestamp and dispatch window event so all instances refresh
                    try { window.__archive_last_changed_ts = Date.now(); window.dispatchEvent(new CustomEvent('synth:archive-changed', { detail: { deleted_ids: ids, restored_count: successCount } })); } catch (e) { /* ignore */ }
                    // Reload the list to reflect server-side changes (deleted archives)
                    try { await load(); } catch (e) { /* ignore */ }
                    // Hide the panel after refresh to mimic previous behavior
                    try { if (archiveWinbox && typeof archiveWinbox.hide === 'function') archiveWinbox.hide(); else if (panel && panel.style) panel.style.display = 'none'; } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            });

            // Allow external refresh via CustomEvent 'archive:refresh'
            try { panel.addEventListener('archive:refresh', () => { try { load(); } catch (e) { /* ignore */ } }); } catch (e) {}
            // Also listen for global synth-level archive change events (useful if archive was changed while panel closed)
            try { window.addEventListener('synth:archive-changed', (ev) => { try { load(); } catch (e) { /* ignore */ } }); } catch (e) {}
            // On creation, if a recent change marker exists, trigger load() to ensure fresh list
            try { if (window.__archive_last_changed_ts) { try { load(); } catch (e) { /* ignore */ } } } catch (e) {}

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

