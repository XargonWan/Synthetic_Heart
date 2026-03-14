(function(){
'use strict';

// History tab state and helpers (migrated from inline template)
const historyState = {
    _initialized: false,
    currentSubTab: 'interactions',
    interactions: { page: 1, per_page: 20, search: '', include_archived: false, sort: 'desc' },
    diary: { page: 1, per_page: 30, search: '', sort: 'desc' },
    grillo: { page: 1, per_page: 20, search: '', beat_type: '', sort: 'desc' },
    chat: { page: 1, per_page: 50, search: '', interface_path: '', sort: 'desc' }
};

function initializeHistoryTab() {
    if (historyState._initialized) return;
    console.log('[History] === INITIALIZING HISTORY TAB ===');

    const allSubTabs = document.querySelectorAll('.sub-tab-panel');
    console.log(`[History] Found ${allSubTabs.length} sub-tab panels`);
    if (!allSubTabs || allSubTabs.length === 0) {
        // If the template isn't loaded yet, retry a few times (idempotent).
        initializeHistoryTab._retry = (initializeHistoryTab._retry || 0) + 1;
        if (initializeHistoryTab._retry <= 10) {
            setTimeout(initializeHistoryTab, 200);
        }
        return;
    }
    allSubTabs.forEach((tab, idx) => { tab.classList.remove('active'); });

    const diaryPanel = document.getElementById('subtab-interactions');
    if (diaryPanel) {
        diaryPanel.classList.add('active');
        historyState.currentSubTab = 'interactions';
    }

    const subNavButtons = document.querySelectorAll('.sub-nav-btn[data-subtab]');
    if (!subNavButtons || subNavButtons.length === 0) {
        // Missing nav buttons: retry initialization (could be a script ordering race)
        initializeHistoryTab._retry = (initializeHistoryTab._retry || 0) + 1;
        if (initializeHistoryTab._retry <= 10) {
            setTimeout(initializeHistoryTab, 200);
        }
        return;
    }

    subNavButtons.forEach(button => {
        button.addEventListener('click', () => {
            console.log('[History] sub-nav clicked:', button.dataset.subtab);
            const subtabName = button.dataset.subtab;
            subNavButtons.forEach(btn => { btn.classList.remove('active'); btn.setAttribute('aria-selected', 'false'); });
            button.classList.add('active');
            button.setAttribute('aria-selected', 'true');
            const subPanels = document.querySelectorAll('.sub-tab-panel');
            subPanels.forEach(panel => {
                panel.classList.remove('active');
                try {
                    panel.style.display = 'none';
                    panel.style.visibility = 'hidden';
                    panel.style.opacity = '0';
                } catch (e) { /* ignore */ }
            });
            const targetPanel = document.querySelector(`#subtab-${subtabName}`);
            if (targetPanel) {
                // Ensure immediate visual feedback
                try {
                    targetPanel.classList.add('active');
                    targetPanel.style.display = 'flex';
                    targetPanel.style.visibility = 'visible';
                    targetPanel.style.opacity = '1';
                } catch (e) { /* ignore */ }
                try { targetPanel.style.zIndex = '46000'; setTimeout(() => { try { targetPanel.style.zIndex = ''; } catch (e) {} }, 800); } catch (e) {}
                historyState.currentSubTab = subtabName;
                if (subtabName === 'agent') {
                    try { if (window.SynthWebUI && typeof window.SynthWebUI.initAgentTab === 'function') window.SynthWebUI.initAgentTab(); } catch (e) { /* ignore */ }
                } else {
                    loadHistoryData(subtabName);
                }
            }
        });
    });

    // Controls
    historyState._initialized = true;
    console.log('[History] initialization complete');


    // Controls
    document.getElementById('history-interactions-search')?.addEventListener('input', SynthUtils.debounce(() => {
        historyState.interactions.search = document.getElementById('history-interactions-search').value;
        historyState.interactions.page = 1; loadHistoryInteractions();
    }, 500));

    document.getElementById('history-interactions-sort')?.addEventListener('change', () => { historyState.interactions.sort = document.getElementById('history-interactions-sort').value; historyState.interactions.page = 1; loadHistoryInteractions(); });
    document.getElementById('history-interactions-archived')?.addEventListener('change', () => { historyState.interactions.include_archived = document.getElementById('history-interactions-archived').checked; historyState.interactions.page = 1; loadHistoryInteractions(); });

    document.getElementById('history-diary-search')?.addEventListener('input', SynthUtils.debounce(() => {
        historyState.diary.search = document.getElementById('history-diary-search').value;
        historyState.diary.page = 1; loadHistoryDiary();
    }, 500));

    document.getElementById('history-diary-sort')?.addEventListener('change', () => { historyState.diary.sort = document.getElementById('history-diary-sort').value; historyState.diary.page = 1; loadHistoryDiary(); });

    document.getElementById('history-grillo-search')?.addEventListener('input', SynthUtils.debounce(() => { historyState.grillo.search = document.getElementById('history-grillo-search').value; historyState.grillo.page = 1; loadHistoryGrillo(); }, 500));
    document.getElementById('history-grillo-beat-type')?.addEventListener('change', () => { historyState.grillo.beat_type = document.getElementById('history-grillo-beat-type').value; historyState.grillo.page = 1; loadHistoryGrillo(); });
    document.getElementById('history-grillo-sort')?.addEventListener('change', () => { historyState.grillo.sort = document.getElementById('history-grillo-sort').value; historyState.grillo.page = 1; loadHistoryGrillo(); });

    document.getElementById('history-chat-interface')?.addEventListener('change', () => { historyState.chat.interface_path = document.getElementById('history-chat-interface').value; historyState.chat.page = 1; loadHistoryChat(); });
    document.getElementById('history-chat-search')?.addEventListener('input', SynthUtils.debounce(() => { historyState.chat.search = document.getElementById('history-chat-search').value; historyState.chat.page = 1; loadHistoryChat(); }, 500));
    document.getElementById('history-chat-sort')?.addEventListener('change', () => { historyState.chat.sort = document.getElementById('history-chat-sort').value; historyState.chat.page = 1; loadHistoryChat(); });

    // Load initial interactions data
    loadHistoryInteractions();
}

function loadHistoryData(subtab) {
    if (subtab === 'interactions') return loadHistoryInteractions();
    if (subtab === 'diary') return loadHistoryDiary();
    if (subtab === 'grillo') return loadHistoryGrillo();
    if (subtab === 'chat') return loadHistoryChat();
    if (subtab === 'agent') {
        try { if (window.SynthWebUI && typeof window.SynthWebUI.initAgentTab === 'function') window.SynthWebUI.initAgentTab(); } catch (e) { /* ignore */ }
        return;
    }
}

async function loadHistoryInteractions() {
    const content = document.getElementById('history-interactions-content'); if (!content) return;
    console.log('[History] loadHistoryInteractions called with state:', historyState.interactions);
    content.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Loading interactions...</p></div>';
    const params = new URLSearchParams({ page: historyState.interactions.page, per_page: historyState.interactions.per_page, search: historyState.interactions.search, include_archived: historyState.interactions.include_archived, sort: historyState.interactions.sort });
    try {
        const response = await fetch(`/api/history/interactions?${params}`);
        const data = await response.json();
        console.log('[History] interactions response:', data);
        if (data && data.success && Array.isArray(data.entries) && data.entries.length > 0) {
            content.innerHTML = data.entries.map(entry => renderInteractionEntry(entry)).join('');
            content.classList.add('history-populated');
            try { content.scrollTop = 0; content.tabIndex = -1; setTimeout(() => { try { content.focus(); } catch (e) {} }, 50); } catch (e) {}
            renderPagination('interactions', data.page, data.total_pages, data.total_count);
        } else if (data && data.success) {
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">🗂️</div><p>No interactions found</p></div>';
        } else {
            console.warn('[History] interactions response indicates failure or unexpected shape:', data);
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load interactions</p></div>';
        }
    } catch (error) {
        console.error('Failed to load interactions history:', error);
        content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load interactions</p></div>';
    }
}

async function loadHistoryDiary() {
    const content = document.getElementById('history-diary-content'); if (!content) return;
    console.log('[History] loadHistoryDiary called with state:', historyState.diary);
    content.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Loading diary...</p></div>';
    const params = new URLSearchParams({ page: historyState.diary.page, per_page: historyState.diary.per_page, search: historyState.diary.search, sort: historyState.diary.sort });
    try {
        const response = await fetch(`/api/history/diary?${params}`);
        const data = await response.json();
        console.log('[History] diary response:', data);
        if (data && data.success && Array.isArray(data.entries) && data.entries.length > 0) {
            content.innerHTML = data.entries.map(entry => renderDiaryDayEntry(entry)).join('');
            content.classList.add('history-populated');
            try { content.scrollTop = 0; content.tabIndex = -1; setTimeout(() => { try { content.focus(); } catch (e) {} }, 50); } catch (e) {}
            renderPagination('diary', data.page, data.total_pages, data.total_count);
        } else if (data && data.success) {
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">📔</div><p>No diary entries found</p></div>';
        } else {
            console.warn('[History] diary response indicates failure or unexpected shape:', data);
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load diary</p></div>';
        }
    } catch (error) {
        console.error('Failed to load diary:', error);
        content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load diary</p></div>';
    }
}

async function loadHistoryGrillo() {
    const content = document.getElementById('history-grillo-content'); if (!content) return;
    console.log('[History] loadHistoryGrillo called with state:', historyState.grillo);
    content.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Loading grillo activity...</p></div>';
    const params = new URLSearchParams({ page: historyState.grillo.page, per_page: historyState.grillo.per_page, search: historyState.grillo.search, beat_type: historyState.grillo.beat_type, sort: historyState.grillo.sort });
    try {
        const response = await fetch(`/api/history/grillo?${params}`);
        const data = await response.json();
        console.log('[History] grillo response:', data);
        if (data && data.success) {
            const beatSelect = document.getElementById('history-grillo-beat-type');
            if (beatSelect && Array.isArray(data.beat_types)) {
                // Replace existing options (preserve the default first option)
                const defaultOption = beatSelect.options && beatSelect.options[0] ? beatSelect.options[0] : null;
                beatSelect.innerHTML = '';
                if (defaultOption) beatSelect.appendChild(defaultOption);
                const existing = new Set();
                data.beat_types.forEach(bt => {
                    const trimmed = String(bt).trim();
                    if (!trimmed || existing.has(trimmed)) return;
                    const option = document.createElement('option');
                    option.value = trimmed;
                    option.textContent = trimmed.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                    beatSelect.appendChild(option);
                    existing.add(trimmed);
                });
            }
            if (Array.isArray(data.entries) && data.entries.length > 0) {
                content.innerHTML = data.entries.map(entry => renderGrilloEntry(entry)).join('');
                content.classList.add('history-populated');
                try { content.scrollTop = 0; content.tabIndex = -1; setTimeout(() => { try { content.focus(); } catch (e) {} }, 50); } catch (e) {}
                renderPagination('grillo', data.page, data.total_pages, data.total_count);
            } else {
                content.classList.remove('history-populated');
                content.innerHTML = '<div class="empty-state"><div class="icon">🦗</div><p>No grillo activity found</p></div>';
            }
        } else {
            console.warn('[History] grillo response indicates failure or unexpected shape:', data);
            content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load grillo activity</p></div>';
        }
    } catch (error) {
        console.error('Failed to load grillo history:', error);
        content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load grillo activity</p></div>';
    }
}

async function loadHistoryChat() {
    const content = document.getElementById('history-chat-content'); if (!content) return;
    console.log('[History] loadHistoryChat called with state:', historyState.chat);
    content.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Loading chat history...</p></div>';
    const params = new URLSearchParams({ page: historyState.chat.page, per_page: historyState.chat.per_page, search: historyState.chat.search, interface_path: historyState.chat.interface_path, sort: historyState.chat.sort });
    try {
        const response = await fetch(`/api/history/chat?${params}`);
        const data = await response.json();
        console.log('[History] chat response:', data);
        const interfaceSelect = document.getElementById('history-chat-interface');
        if (interfaceSelect && Array.isArray(data.interface_paths)) {
            // Replace options with fresh list (preserve default first 'All Chats')
            const defaultOpt = interfaceSelect.options && interfaceSelect.options[0] ? interfaceSelect.options[0] : null;
            interfaceSelect.innerHTML = '';
            if (defaultOpt) interfaceSelect.appendChild(defaultOpt);
            data.interface_paths.forEach(path => { const option = document.createElement('option'); option.value = path; option.textContent = path; interfaceSelect.appendChild(option); });
        }
        if (data && data.success && Array.isArray(data.messages) && data.messages.length > 0) {
            content.innerHTML = data.messages.map(msg => renderChatMessage(msg)).join('');
            content.classList.add('history-populated');
            try { content.scrollTop = 0; content.tabIndex = -1; setTimeout(() => { try { content.focus(); } catch (e) {} }, 50); } catch (e) {}
            renderPagination('chat', data.page, data.total_pages, data.total_count);
        } else if (data && data.success) {
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">💬</div><p>No chat messages found</p></div>';
        } else {
            console.warn('[History] chat response indicates failure or unexpected shape:', data);
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load chat history</p></div>';
        }
    } catch (error) {
        console.error('Failed to load chat history:', error);
        content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load chat history</p></div>';
    }
}

function renderInteractionEntry(entry) {
    const timestamp = formatTimestamp(entry.timestamp);
    const archived = entry.archived ? ' archived' : '';
    let safeContent = escapeHtml(entry.content || ''); safeContent = safeContent.replace(/^[\s\u00A0]+/, '').replace(/[\n\r]+$/, ''); const isLong = safeContent.split('\n').length > 6 || safeContent.length > 800; const longClass = isLong ? ' history-entry-content--limited' : '';
    let safeThought = escapeHtml(entry.personal_thought || ''); safeThought = safeThought.replace(/^[\s\u00A0]+/, '').replace(/[\n\r]+$/, '');

    return `
        <div class="history-entry${archived}">
            <div class="history-entry-header">
                <div class="history-entry-meta">
                    <div class="history-entry-date">📅 ${timestamp}</div>
                    ${entry.interaction_summary ? `<div class="history-entry-detail">${escapeHtml(entry.interaction_summary)}</div>` : ''}
                </div>
            </div>
            <div class="history-entry-content${longClass}">${safeContent}</div>
            ${entry.personal_thought ? `<div class="history-entry-detail"><strong>💭 Thought:</strong> ${safeThought}</div>` : ''}
            ${entry.primary_emotion ? `<div class="history-entry-detail"><strong>😊 Emotion:</strong> ${entry.primary_emotion}</div>` : ''}
            ${entry.user_count > 0 ? `<div class="history-entry-detail"><strong>👥 With:</strong> ${entry.user_count} user${entry.user_count > 1 ? 's' : ''}</div>` : ''}
        </div>
    `;
}

function renderDiaryDayEntry(entry) {
    const ts = entry.timestamp ? new Date(entry.timestamp) : null;
    const dayLabel = ts ? ts.toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' }) : '—';
    const rawContent = (entry.content || '').trim();

    // Split on '---' separators (written by _upsert_diary_impl before LLM consolidation,
    // or by GROUP_CONCAT when aggregating old multi-row days).
    const fragments = rawContent.split(/\n\n---\n\n|\n---\n/).map(f => f.trim()).filter(Boolean);
    let contentHtml;
    if (fragments.length > 1) {
        // Pre-consolidation: render each fragment as its own paragraph with a divider
        contentHtml = fragments
            .map(f => `<p class="diary-fragment">${escapeHtml(f).replace(/\n/g, '<br>')}</p>`)
            .join('<hr class="diary-separator">');
    } else {
        contentHtml = escapeHtml(rawContent).replace(/\n/g, '<br>');
    }

    let safeThought = entry.personal_thought ? escapeHtml(entry.personal_thought).replace(/^[\s\u00A0]+/, '').replace(/[\n\r]+$/, '') : '';

    return `
        <div class="diary-day-entry">
            <div class="diary-day-header">
                <span class="diary-day-icon">📔</span>
                <span class="diary-day-label">${dayLabel}</span>
            </div>
            <div class="diary-day-content">${contentHtml || '<em>No entry for this day</em>'}</div>
            ${safeThought ? `<div class="diary-day-thought"><strong>💭</strong> ${safeThought}</div>` : ''}
        </div>
    `;
}

function renderGrilloEntry(entry) {
    const timestamp = formatTimestamp(entry.executed_at);
    const beatTypeLabel = entry.beat_type.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
    const rawResponse = (entry.response_text || '') || '';
    const diaryContent = (entry.diary_content || '') || '';
    let displayResponse = rawResponse || diaryContent || '';
    let safePrompt = escapeHtml(entry.prompt_text || ''); safePrompt = safePrompt.replace(/^[\s\u00A0]+/, '').replace(/[\n\r]+$/, ''); const promptIsLong = safePrompt.split('\n').length > 6 || safePrompt.length > 600; const promptClass = promptIsLong ? ' grillo-prompt--limited' : '';
    let safeResponse = escapeHtml(displayResponse || ''); safeResponse = safeResponse.replace(/^[\s\u00A0]+/, '').replace(/[\n\r]+$/, ''); const responseIsLong = safeResponse.split('\n').length > 6 || safeResponse.length > 600; const responseClass = responseIsLong ? ' grillo-response--limited' : '';
    const actionsBlock = renderGrilloActions(entry.actions || []);

    return `
        <div class="history-entry">
            <div class="history-entry-header">
                <div class="history-entry-meta">
                    <div class="history-entry-date">🦗 ${timestamp}</div>
                    <span class="history-entry-type">${beatTypeLabel}</span>
                </div>
            </div>
            <div class="grillo-prompt${promptClass}">${safePrompt}</div>
            ${safeResponse ? `
            <div style="margin-top: 0.75rem;">
                <div style="font-size: 0.85rem; color: var(--text-soft); margin-bottom: 0.5rem; font-weight: 500;">Response:</div>
                <div class="grillo-response${responseClass}">${safeResponse}</div>
            </div>
            ` : ''}
            ${actionsBlock}
            ${entry.has_diary ? `
                <div class="history-entry-detail" style="margin-top: 1rem;">
                    <strong>📝 Reflection created</strong>
                    ${entry.diary_entry_id ? `<small> (ID: ${entry.diary_entry_id})</small>` : ''}
                </div>
            ` : '<div class="history-entry-detail" style="margin-top: 0.5rem; opacity: 0.6;"><em>No diary entry created yet</em></div>'}
        </div>
    `;
}

function renderGrilloActions(actions) {
    if (!Array.isArray(actions) || actions.length === 0) {
        return `
            <div class="grillo-actions">
                <div class="grillo-actions-header">
                    <span>Azioni Grillo</span>
                </div>
                <div class="history-entry-detail" style="margin: 0; opacity: 0.7;"><em>Nessuna azione proposta</em></div>
            </div>
        `;
    }

    const counts = actions.reduce((acc, action) => { const key = (action.status || 'pending').toLowerCase(); acc[key] = (acc[key] || 0) + 1; return acc; }, {});
    const countChips = ['pending', 'processed', 'failed'].filter(key => counts[key]).map(key => `<span>${key}: ${counts[key]}</span>`).join('');

    const items = actions.map(action => {
        const status = (action.status || 'pending').toLowerCase();
        const typeLabel = escapeHtml(action.action_type || 'unknown');
        const payload = formatJsonBlock(action.payload);
        const result = formatJsonBlock(action.result);
        const errorText = action.error_text ? escapeHtml(action.error_text) : '';
        const createdAt = action.created_at ? formatTimestamp(action.created_at) : '';
        return `
            <div class="grillo-action-item">
                <div class="grillo-action-header">
                    <span class="grillo-action-type">${typeLabel}</span>
                    <span class="grillo-action-status ${status}">${status}</span>
                    ${createdAt ? `<span style="font-size: 0.7rem; color: var(--text-soft);">${createdAt}</span>` : ''}
                </div>
                ${payload ? `<div class="grillo-action-detail"><strong>Payload:</strong>\n${payload}</div>` : ''}
                ${result ? `<div class="grillo-action-detail"><strong>Result:</strong>\n${result}</div>` : ''}
                ${errorText ? `<div class="grillo-action-detail"><strong>Error:</strong> ${errorText}</div>` : ''}
            </div>
        `;
    }).join('');

    return `
        <div class="grillo-actions">
            <div class="grillo-actions-header">
                <span>Azioni Grillo</span>
                ${countChips ? `<div class="grillo-actions-counts">${countChips}</div>` : ''}
            </div>
            <div class="grillo-actions-list">${items}</div>
        </div>
    `;
}

function formatJsonBlock(value) { return SynthUtils.formatJsonBlock(value); }

function renderChatMessage(msg) {
    const timestamp = formatTimestamp(msg.timestamp);
    let safeMessage = escapeHtml(msg.message_text || ''); safeMessage = safeMessage.replace(/^[\s\u00A0]+/, '').replace(/[\n\r]+$/, ''); const isLong = safeMessage.split('\n').length > 6 || safeMessage.length > 600; const longClass = isLong ? ' chat-text--limited' : '';
    return `
        <div class="chat-message">
            <div class="chat-sender">${escapeHtml(msg.sender_name || 'Unknown')}</div>
            <div class="chat-text${longClass}" title="${safeMessage}">
                ${safeMessage}
            </div>
            <div class="chat-time">${timestamp}</div>
        </div>
    `;
}

function renderPagination(type, currentPage, totalPages, totalCount) {
    const paginationDiv = document.getElementById(`history-${type}-pagination`);
    if (!paginationDiv) return;
    if (totalPages <= 1) { paginationDiv.innerHTML = ''; return; }
    paginationDiv.innerHTML = `
        <button class="pagination-btn" ${currentPage <= 1 ? 'disabled' : ''} onclick="changePage('${type}', ${currentPage - 1})">
            ← Previous
        </button>
        <span class="pagination-info">Page ${currentPage} of ${totalPages} (${totalCount} total)</span>
        <button class="pagination-btn" ${currentPage >= totalPages ? 'disabled' : ''} onclick="changePage('${type}', ${currentPage + 1})">
            Next →
        </button>
    `;
}

function changePage(type, newPage) { historyState[type].page = newPage; loadHistoryData(type); }

function debounce(func, wait) { let timeout; return function executedFunction(...args) { const later = () => { clearTimeout(timeout); func(...args); }; clearTimeout(timeout); timeout = setTimeout(later, wait); }; }

function escapeHtml(text) { return SynthUtils.escapeHtml(text); }

function formatTimestamp(isoString) { return SynthUtils.formatTimestamp(isoString); }

// Expose initializer for dynamic loader
window.SynthWebUI = window.SynthWebUI || {};
window.SynthWebUI.initHistoryTab = function() { initializeHistoryTab(); };

// Fallback: if the tab is active on DOMContentLoaded, initialize
document.addEventListener('DOMContentLoaded', () => {
    const historyTab = document.querySelector('[data-tab="history"]');
    if (historyTab && historyTab.children.length === 0 && window.SynthWebUI && typeof window.SynthWebUI.loadSection === 'function') {
        window.SynthWebUI.loadSection('history');
    }
    if (historyTab && historyTab.classList.contains('active')) {
        window.SynthWebUI.initHistoryTab();
    } else if (historyTab) {
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                if (mutation.type === 'attributes' && mutation.attributeName === 'class') {
                    if (historyTab.classList.contains('active')) {
                        window.SynthWebUI.initHistoryTab(); observer.disconnect();
                    }
                }
            });
        });
        observer.observe(historyTab, { attributes: true });
    }

    // Delegated click handler for sub-nav buttons to guard against DOM replacements
    if (!window.__synth_history_delegated) {
        window.addEventListener('click', (ev) => {
            try {
                const target = ev.target || ev.srcElement;
                if (!target) return;
                const btn = target.closest ? target.closest('.sub-nav-btn') : null;
                if (!btn) return;
                // If this button is within the History tab area, handle it here
                const parent = btn.closest('[data-tab="history"]');
                if (!parent) return; // not inside history

                console.debug('[History] delegated click for sub-nav:', btn.dataset && btn.dataset.subtab);
                // emulate the button's click handler: set active and load data
                const subNavButtons = Array.from(document.querySelectorAll('.sub-nav-btn[data-subtab]'));
                subNavButtons.forEach(b => { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
                btn.classList.add('active');
                btn.setAttribute('aria-selected', 'true');
                const subPanels = document.querySelectorAll('.sub-tab-panel');
                subPanels.forEach(panel => {
                    panel.classList.remove('active');
                    try {
                        panel.style.display = 'none';
                        panel.style.visibility = 'hidden';
                        panel.style.opacity = '0';
                    } catch (e) { /* ignore */ }
                });
                const subtabName = btn.dataset && btn.dataset.subtab;
                const targetPanel = document.querySelector(`#subtab-${subtabName}`);
                if (targetPanel) {
                    try {
                        targetPanel.classList.add('active');
                        targetPanel.style.display = 'flex';
                        targetPanel.style.visibility = 'visible';
                        targetPanel.style.opacity = '1';
                    } catch (e) { /* ignore */ }
                    historyState.currentSubTab = subtabName;
                    if (subtabName === 'agent') {
                        try { if (window.SynthWebUI && typeof window.SynthWebUI.initAgentTab === 'function') window.SynthWebUI.initAgentTab(); } catch (e) { /* ignore */ }
                    } else {
                        loadHistoryData(subtabName);
                    }
                }
            } catch (e) { /* ignore */ }
        }, true);
        window.__synth_history_delegated = true;
    }
});

})();
