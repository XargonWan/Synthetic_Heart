(function(){
'use strict';

// History tab state and helpers (migrated from inline template)
const historyState = {
    _initialized: false,
    currentSubTab: 'diary',
    diary: { page: 1, per_page: 30, search: '', sort: 'desc' },
    grillo: { page: 1, per_page: 20, search: '', beat_type: '', sort: 'desc' },
    dreams: { page: 1, per_page: 15, search: '', sort: 'desc' },
    chat: { page: 1, per_page: 50, search: '', interface_path: '', sort: 'desc' },
    calendar: { year: null, month: null, events: [], loaded: false },
    growth: { current: null, entries: [], currentLikes: [], currentDislikes: [], pendingProposal: null }
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

    const diaryPanel = document.getElementById('subtab-diary');
    if (diaryPanel) {
        diaryPanel.classList.add('active');
        historyState.currentSubTab = 'diary';
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
    document.getElementById('history-diary-search')?.addEventListener('input', SynthUtils.debounce(() => {
        historyState.diary.search = document.getElementById('history-diary-search').value;
        historyState.diary.page = 1; loadHistoryDiary();
    }, 500));

    document.getElementById('history-diary-sort')?.addEventListener('change', () => { historyState.diary.sort = document.getElementById('history-diary-sort').value; historyState.diary.page = 1; loadHistoryDiary(); });

    document.getElementById('history-grillo-search')?.addEventListener('input', SynthUtils.debounce(() => { historyState.grillo.search = document.getElementById('history-grillo-search').value; historyState.grillo.page = 1; loadHistoryGrillo(); }, 500));
    document.getElementById('history-grillo-beat-type')?.addEventListener('change', () => { historyState.grillo.beat_type = document.getElementById('history-grillo-beat-type').value; historyState.grillo.page = 1; loadHistoryGrillo(); });
    document.getElementById('history-grillo-sort')?.addEventListener('change', () => { historyState.grillo.sort = document.getElementById('history-grillo-sort').value; historyState.grillo.page = 1; loadHistoryGrillo(); });

    document.getElementById('history-dreams-search')?.addEventListener('input', SynthUtils.debounce(() => { historyState.dreams.search = document.getElementById('history-dreams-search').value; historyState.dreams.page = 1; loadHistoryDreams(); }, 500));
    document.getElementById('history-dreams-sort')?.addEventListener('change', () => { historyState.dreams.sort = document.getElementById('history-dreams-sort').value; historyState.dreams.page = 1; loadHistoryDreams(); });

    document.getElementById('history-chat-interface')?.addEventListener('change', () => { historyState.chat.interface_path = document.getElementById('history-chat-interface').value; historyState.chat.page = 1; loadHistoryChat(); });
    document.getElementById('history-chat-search')?.addEventListener('input', SynthUtils.debounce(() => { historyState.chat.search = document.getElementById('history-chat-search').value; historyState.chat.page = 1; loadHistoryChat(); }, 500));
    document.getElementById('history-chat-sort')?.addEventListener('change', () => { historyState.chat.sort = document.getElementById('history-chat-sort').value; historyState.chat.page = 1; loadHistoryChat(); });

    // Calendar controls
    document.getElementById('calendar-prev')?.addEventListener('click', () => shiftCalendarMonth(-1));
    document.getElementById('calendar-next')?.addEventListener('click', () => shiftCalendarMonth(1));
    document.getElementById('calendar-today')?.addEventListener('click', () => { const now = new Date(); historyState.calendar.year = now.getFullYear(); historyState.calendar.month = now.getMonth(); loadHistoryCalendar(); });
    document.getElementById('calendar-new-event')?.addEventListener('click', () => openCalendarEventModal(null));
    document.getElementById('calendar-subscribe')?.addEventListener('click', () => openCalendarSubscribeModal());

    // Load initial diary data
    loadHistoryDiary();
}

function shiftCalendarMonth(delta) {
    const cal = historyState.calendar;
    if (cal.year === null || cal.month === null) { const now = new Date(); cal.year = now.getFullYear(); cal.month = now.getMonth(); }
    let m = cal.month + delta;
    let y = cal.year;
    while (m < 0) { m += 12; y -= 1; }
    while (m > 11) { m -= 12; y += 1; }
    cal.year = y; cal.month = m;
    loadHistoryCalendar();
}

async function loadHistoryCalendar() {
    const content = document.getElementById('history-calendar-content');
    if (!content) return;
    const cal = historyState.calendar;
    if (cal.year === null || cal.month === null) { const now = new Date(); cal.year = now.getFullYear(); cal.month = now.getMonth(); }

    content.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Loading calendar...</p></div>';
    const params = new URLSearchParams({ year: cal.year, month: cal.month + 1 });
    try {
        const response = await fetch(`/api/history/calendar?${params}`);
        const data = await response.json();
        if (data && data.success && Array.isArray(data.events)) {
            cal.events = data.events;
            cal.loaded = true;
            renderCalendarGrid();
        } else {
            content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load calendar</p></div>';
        }
    } catch (error) {
        console.error('Failed to load calendar:', error);
        content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load calendar</p></div>';
    }
}

function renderCalendarGrid() {
    const content = document.getElementById('history-calendar-content');
    if (!content) return;
    const cal = historyState.calendar;
    const year = cal.year;
    const month = cal.month; // 0-based

    // Update the month title
    const titleEl = document.getElementById('calendar-title');
    if (titleEl) {
        try { titleEl.textContent = new Date(year, month, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' }); }
        catch (e) { titleEl.textContent = `${year}-${String(month + 1).padStart(2, '0')}`; }
    }

    // Group events by local YYYY-MM-DD (occurrence date). Backend returns
    // one object per occurrence within the requested month window.
    const byDay = {};
    (cal.events || []).forEach(ev => {
        const day = ev.date; // 'YYYY-MM-DD' local
        if (!day) return;
        if (!byDay[day]) byDay[day] = [];
        byDay[day].push(ev);
    });

    // Weekday headers (Mon-first display but Date-based, locale-neutral labels)
    const weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    let html = '<div class="calendar-grid">';
    weekdays.forEach(w => { html += `<div class="calendar-weekday">${w}</div>`; });

    const first = new Date(year, month, 1);
    // JS getDay(): 0=Sun..6=Sat. Convert to Mon-first index (0=Mon..6=Sun).
    let startOffset = (first.getDay() + 6) % 7;
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const today = new Date();
    const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

    // Leading days from previous month
    const prevMonthDays = new Date(year, month, 0).getDate();
    for (let i = startOffset - 1; i >= 0; i--) {
        const d = prevMonthDays - i;
        html += `<div class="calendar-day other-month"><span class="calendar-day-number">${d}</span></div>`;
    }

    // Days of the current month
    for (let d = 1; d <= daysInMonth; d++) {
        const dayStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        const isToday = dayStr === todayStr;
        const events = byDay[dayStr] || [];
        let eventsHtml = '';
        const maxShown = 3;
        events.slice(0, maxShown).forEach(ev => {
            const classes = ['calendar-event'];
            if (ev.recurring) classes.push('recurring');
            if (ev.source && String(ev.source).startsWith('external')) classes.push('external');
            const label = escapeHtml((ev.time ? ev.time + ' ' : '') + (ev.description || '(untitled)'));
            eventsHtml += `<div class="${classes.join(' ')}" title="${label}">${label}</div>`;
        });
        if (events.length > maxShown) {
            eventsHtml += `<div class="calendar-day-more">+${events.length - maxShown} more</div>`;
        }
        html += `<div class="calendar-day${isToday ? ' today' : ''}" data-day="${dayStr}"><span class="calendar-day-number">${d}</span>${eventsHtml}</div>`;
    }

    html += '</div>';
    content.innerHTML = html;

    // Click a day cell -> open the details/creation modal pre-filled with that date
    content.querySelectorAll('.calendar-day[data-day]').forEach(cell => {
        cell.addEventListener('click', () => {
            const dayStr = cell.dataset.day;
            const dayEvents = byDay[dayStr] || [];
            openCalendarDayModal(dayStr, dayEvents);
        });
    });
}

function openCalendarDayModal(dayStr, dayEvents) {
    let listHtml = '';
    if (dayEvents.length > 0) {
        listHtml = '<div class="calendar-day-events">' + dayEvents.map(ev => {
            const isExternal = ev.source && String(ev.source).startsWith('external');
            const badge = isExternal ? ' <small>(external)</small>' : '';
            const recur = ev.recurring ? ' 🔁' : '';
            const statusBadge = isExternal
                ? ''
                : (ev.delivered
                    ? '<br><small style="color:#7ec87e;" title="Already delivered / processed">✓ processed</small>'
                    : '<br><small style="opacity:0.6;" title="Not yet delivered">◷ pending</small>');
            const editBtn = isExternal ? '' : `<button type="button" class="calendar-edit-event" data-event-id="${ev.id}" title="Edit event">✏</button>`;
            const delBtn = isExternal ? '' : `<button type="button" class="calendar-del-event" data-event-id="${ev.id}" title="Delete event">🗑</button>`;
            const evJson = encodeURIComponent(JSON.stringify(ev));
            return `<div class="calendar-event-row" data-event="${evJson}" style="display:flex;justify-content:space-between;align-items:center;gap:0.5rem;padding:0.4rem 0;border-bottom:1px solid rgba(255,255,255,0.08);">
                <span>${escapeHtml((ev.time ? ev.time + ' — ' : '') + (ev.description || '(untitled)'))}${recur}${badge}${statusBadge}</span>
                <span style="display:flex;gap:0.25rem;flex-shrink:0;">${editBtn}${delBtn}</span>
            </div>`;
        }).join('') + '</div>';
    } else {
        listHtml = '<p style="opacity:0.6;">No events on this day.</p>';
    }

    const body = `
        <h3>📅 ${escapeHtml(dayStr)}</h3>
        ${listHtml}
        <div class="calendar-modal-actions">
            <button type="button" class="calendar-modal-close">Close</button>
            <button type="button" class="primary calendar-modal-add">＋ Add event</button>
        </div>
    `;
    const backdrop = showCalendarModal(body);
    backdrop.querySelector('.calendar-modal-close')?.addEventListener('click', () => backdrop.remove());
    backdrop.querySelector('.calendar-modal-add')?.addEventListener('click', () => { backdrop.remove(); openCalendarEventModal(dayStr); });
    backdrop.querySelectorAll('.calendar-edit-event').forEach(btn => {
        btn.addEventListener('click', () => {
            const row = btn.closest('.calendar-event-row');
            let ev = null;
            try { ev = JSON.parse(decodeURIComponent(row?.dataset.event || '') || 'null'); } catch (e) { ev = null; }
            if (!ev) return;
            backdrop.remove();
            openCalendarEventModal(dayStr, ev);
        });
    });
    backdrop.querySelectorAll('.calendar-del-event').forEach(btn => {
        btn.addEventListener('click', async () => {
            const eventId = btn.dataset.eventId;
            if (!eventId) return;
            if (!window.confirm('Delete this event?')) return;
            await deleteCalendarEvent(eventId);
            backdrop.remove();
            // Refresh the calendar grid in the background so it stays in sync.
            loadHistoryCalendar();
            // Keep the user on the day detail if there are still events left,
            // otherwise fall back to the calendar view.
            const remaining = dayEvents.filter(ev => String(ev.id) !== String(eventId));
            if (remaining.length > 0) {
                openCalendarDayModal(dayStr, remaining);
            }
        });
    });
}

function openCalendarEventModal(prefillDate, existingEvent) {
    const isEdit = !!(existingEvent && existingEvent.id);
    const dateVal = (existingEvent && existingEvent.date) || prefillDate || '';
    const timeVal = (existingEvent && existingEvent.time) || '09:00';
    const recurVal = (existingEvent && existingEvent.recurrence_type) || 'none';
    const descVal = (existingEvent && existingEvent.description) || '';
    const deliveredVal = !!(existingEvent && existingEvent.delivered);
    const recurOptions = ['none', 'daily', 'weekly', 'monthly'].map(function (r) {
        const label = r === 'none' ? 'Once' : (r.charAt(0).toUpperCase() + r.slice(1));
        const sel = r === recurVal ? ' selected' : '';
        return `<option value="${r}"${sel}>${label}</option>`;
    }).join('');
    const title = isEdit ? '✏ Edit event' : '＋ New event';
    const saveLabel = isEdit ? 'Save' : 'Create';
    const body = `
        <h3>${title}</h3>
        <p style="font-size:0.8rem;opacity:0.7;margin:0 0 0.5rem;">
            This is an internal reminder. When it fires, the Synth receives it as a private thought and decides on its own whether and how to reach you.
        </p>
        <label for="cal-ev-date">Date</label>
        <input type="date" id="cal-ev-date" value="${escapeHtml(dateVal)}">
        <label for="cal-ev-time">Time</label>
        <input type="time" id="cal-ev-time" value="${escapeHtml(timeVal)}">
        <label for="cal-ev-recurrence">Recurrence</label>
        <select id="cal-ev-recurrence">
            ${recurOptions}
        </select>
        <label for="cal-ev-description">Description</label>
        <textarea id="cal-ev-description" rows="3" placeholder="What should the Synth remember?">${escapeHtml(descVal)}</textarea>
        ${isEdit ? `
        <div style="display:flex;align-items:center;justify-content:space-between;gap:0.5rem;">
            <label for="cal-ev-delivered" style="display:flex;align-items:center;gap:0.5rem;cursor:pointer;margin:0;">
                <input type="checkbox" id="cal-ev-delivered" style="width:auto;margin:0;"${deliveredVal ? ' checked' : ''}>
                <span>Processed (already delivered)</span>
            </label>
            <button type="button" class="cal-ev-add-path" style="background:var(--accent);color:var(--accent-contrast, #fff);border:none;border-radius:6px;padding:0.35rem 0.7rem;font-size:0.8rem;cursor:pointer;white-space:nowrap;">Add Interface Path</button>
        </div>` : ''}
        <div class="calendar-modal-error" style="color:#ff8080;font-size:0.8rem;margin-top:0.5rem;display:none;"></div>
        <div class="calendar-modal-actions">
            <button type="button" class="calendar-modal-close">Cancel</button>
            <button type="button" class="primary calendar-modal-save">${saveLabel}</button>
        </div>
    `;
    const backdrop = showCalendarModal(body);
    backdrop.querySelector('.calendar-modal-close')?.addEventListener('click', () => backdrop.remove());
    backdrop.querySelector('.cal-ev-add-path')?.addEventListener('click', () => {
        openInterfacePathPicker((interfacePath, label) => {
            const textarea = backdrop.querySelector('#cal-ev-description');
            if (!textarea) return;
            const line = `Send your message on interface path: ${interfacePath} (${label})`;
            const current = textarea.value.replace(/\s+$/, '');
            textarea.value = current ? `${current}\n\n${line}` : line;
        });
    });
    backdrop.querySelector('.calendar-modal-save')?.addEventListener('click', async () => {
        const errEl = backdrop.querySelector('.calendar-modal-error');
        const date = backdrop.querySelector('#cal-ev-date').value;
        const time = backdrop.querySelector('#cal-ev-time').value || '09:00';
        const recurrence = backdrop.querySelector('#cal-ev-recurrence').value;
        const description = backdrop.querySelector('#cal-ev-description').value.trim();
        if (!date || !description) {
            if (errEl) { errEl.style.display = 'block'; errEl.textContent = 'Date and description are required.'; }
            return;
        }
        const url = isEdit ? `/api/history/calendar/${existingEvent.id}` : '/api/history/calendar';
        const method = isEdit ? 'PUT' : 'POST';
        const payload = { date, time, recurrence, description };
        if (isEdit) {
            payload.delivered = !!backdrop.querySelector('#cal-ev-delivered')?.checked;
        }
        try {
            const resp = await fetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await resp.json();
            if (data && data.success) {
                backdrop.remove();
                loadHistoryCalendar();
            } else if (errEl) {
                errEl.style.display = 'block';
                errEl.textContent = (data && data.error) || (isEdit ? 'Failed to update event.' : 'Failed to create event.');
            }
        } catch (e) {
            if (errEl) { errEl.style.display = 'block'; errEl.textContent = isEdit ? 'Failed to update event.' : 'Failed to create event.'; }
        }
    });
}

function openCalendarSubscribeModal() {
    const origin = window.location.origin;
    const httpsUrl = `${origin}/calendar.ics`;
    const webcalUrl = httpsUrl.replace(/^https?:/, 'webcal:');
    const body = `
        <h3>🔗 Subscribe & external calendars</h3>
        <p style="font-size:0.85rem;opacity:0.8;">
            Add this URL to Google Calendar, Apple Calendar, Thunderbird or any calendar app
            that supports iCalendar (ICS) subscriptions. It stays in sync automatically.
        </p>
        <label>Subscription URL (webcal)</label>
        <div class="calendar-subscribe-url">${escapeHtml(webcalUrl)}</div>
        <label>Plain HTTPS URL</label>
        <div class="calendar-subscribe-url">${escapeHtml(httpsUrl)}</div>
        <div class="calendar-modal-actions">
            <a href="${escapeHtml(webcalUrl)}" class="primary" style="text-decoration:none;padding:0.5rem 1rem;border-radius:6px;">Open in calendar app</a>
        </div>

        <hr style="margin:1.2rem 0;opacity:0.2;">

        <h3>📥 Subscribe SyntH to an external calendar</h3>
        <div class="calendar-privacy-warning" style="border:1px solid var(--accent);border-radius:6px;padding:0.6rem 0.8rem;margin:0.5rem 0;font-size:0.8rem;line-height:1.4;">
            <strong>⚠️ Privacy warning.</strong> Any event you subscribe SyntH to becomes part
            of what SyntH knows and may reason about. If "alert SyntH" is enabled, SyntH can
            proactively bring these events up and could <em>disclose their details to third
            parties</em> (people in chats, other interfaces). Only subscribe calendars whose
            contents you are comfortable SyntH seeing and potentially sharing.
        </div>
        <div id="external-cal-list" style="margin:0.5rem 0;">
            <div class="loading-state"><div class="loading-spinner"></div><p>Loading...</p></div>
        </div>
        <label>Calendar name</label>
        <input type="text" id="ext-cal-name" placeholder="e.g. Work calendar" style="width:100%;box-sizing:border-box;">
        <label>Type</label>
        <select id="ext-cal-type" style="width:100%;box-sizing:border-box;">
            <option value="ics">ICS (read-only URL)</option>
            <option value="caldav">CalDAV</option>
        </select>
        <label>URL</label>
        <input type="text" id="ext-cal-url" placeholder="https://... or webcal://..." style="width:100%;box-sizing:border-box;">
        <label>Username (optional)</label>
        <input type="text" id="ext-cal-user" placeholder="only for password-protected calendars" style="width:100%;box-sizing:border-box;">
        <label>Password (optional)</label>
        <input type="password" id="ext-cal-pass" placeholder="stored encrypted" style="width:100%;box-sizing:border-box;">
        <div class="calendar-modal-actions">
            <button type="button" class="calendar-modal-close">Close</button>
            <button type="button" id="ext-cal-add" class="primary">Add calendar</button>
        </div>
    `;
    const backdrop = showCalendarModal(body);
    backdrop.querySelector('.calendar-modal-close')?.addEventListener('click', () => backdrop.remove());

    const refresh = () => loadExternalCalendars(backdrop);
    refresh();

    backdrop.querySelector('#ext-cal-add')?.addEventListener('click', async () => {
        const name = backdrop.querySelector('#ext-cal-name')?.value?.trim();
        const url = backdrop.querySelector('#ext-cal-url')?.value?.trim();
        const cal_type = backdrop.querySelector('#ext-cal-type')?.value || 'ics';
        const username = backdrop.querySelector('#ext-cal-user')?.value?.trim() || null;
        const password = backdrop.querySelector('#ext-cal-pass')?.value || null;
        if (!name || !url) {
            alert('Name and URL are required.');
            return;
        }
        try {
            const resp = await fetch('/api/history/calendar/external', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, url, cal_type, username, password }),
            });
            const data = await resp.json();
            if (!resp.ok || data.error) {
                alert('Failed to add calendar: ' + (data.error || resp.status));
                return;
            }
            backdrop.querySelector('#ext-cal-name').value = '';
            backdrop.querySelector('#ext-cal-url').value = '';
            backdrop.querySelector('#ext-cal-user').value = '';
            backdrop.querySelector('#ext-cal-pass').value = '';
            refresh();
        } catch (e) {
            console.error('Failed to add external calendar:', e);
            alert('Failed to add calendar.');
        }
    });
}

async function loadExternalCalendars(backdrop) {
    const listEl = backdrop.querySelector('#external-cal-list');
    if (!listEl) return;
    try {
        const resp = await fetch('/api/history/calendar/external');
        const data = await resp.json();
        const cals = (data && data.calendars) || [];
        if (!cals.length) {
            listEl.innerHTML = '<p style="font-size:0.8rem;opacity:0.6;">No external calendars subscribed yet.</p>';
            return;
        }
        listEl.innerHTML = cals.map(c => `
            <div class="external-cal-item" style="display:flex;align-items:center;justify-content:space-between;padding:0.4rem 0;border-bottom:1px solid rgba(255,255,255,0.08);">
                <div>
                    <strong>${escapeHtml(c.name)}</strong>
                    <span style="font-size:0.75rem;opacity:0.6;"> (${escapeHtml(c.cal_type)})</span>
                    ${c.last_error ? `<div style="font-size:0.72rem;color:#e57373;">${escapeHtml(String(c.last_error))}</div>` : ''}
                </div>
                <button type="button" class="ext-cal-del" data-id="${c.id}" style="padding:0.25rem 0.5rem;">Remove</button>
            </div>
        `).join('');
        listEl.querySelectorAll('.ext-cal-del').forEach(btn => {
            btn.addEventListener('click', async () => {
                const id = btn.getAttribute('data-id');
                if (!confirm('Remove this external calendar and its imported events?')) return;
                try {
                    const r = await fetch(`/api/history/calendar/external/${encodeURIComponent(id)}`, { method: 'DELETE' });
                    if (!r.ok) {
                        const d = await r.json();
                        alert('Failed to remove: ' + (d.error || r.status));
                        return;
                    }
                    loadExternalCalendars(backdrop);
                } catch (e) {
                    console.error('Failed to delete external calendar:', e);
                }
            });
        });
    } catch (e) {
        console.error('Failed to load external calendars:', e);
        listEl.innerHTML = '<p style="font-size:0.8rem;color:#e57373;">Failed to load external calendars.</p>';
    }
}

async function deleteCalendarEvent(eventId) {
    try {
        const resp = await fetch(`/api/history/calendar/${encodeURIComponent(eventId)}`, { method: 'DELETE' });
        const data = await resp.json();
        if (!data || !data.success) {
            console.warn('[History] failed to delete calendar event', data);
        }
    } catch (e) {
        console.error('Failed to delete calendar event:', e);
    }
}

function showCalendarModal(innerHtml) {
    // Remove any existing modal first
    document.querySelectorAll('.calendar-modal-backdrop').forEach(el => el.remove());
    const backdrop = document.createElement('div');
    backdrop.className = 'calendar-modal-backdrop';
    backdrop.innerHTML = `<div class="calendar-modal">${innerHtml}</div>`;
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });
    document.body.appendChild(backdrop);
    return backdrop;
}

function openInterfacePathPicker(onSelect) {
    const backdrop = document.createElement('div');
    backdrop.className = 'calendar-modal-backdrop';
    backdrop.style.zIndex = '48000';
    backdrop.innerHTML = `<div class="calendar-modal">
        <h3>Add interface path</h3>
        <p style="font-size:0.8rem;opacity:0.7;margin:0 0 0.5rem;">
            Pick a known interface path or type one manually, then confirm.
        </p>
        <label for="cal-ev-path-input">Interface path</label>
        <input type="text" id="cal-ev-path-input" placeholder="Loading known paths..." autocomplete="off">
        <div class="cal-ev-path-list" style="max-height:220px;overflow-y:auto;margin-top:0.4rem;border:1px solid var(--border,#333);border-radius:6px;background:var(--panel-bg,#1a1a1a);"></div>
        <div class="calendar-modal-error" style="color:#ff8080;font-size:0.8rem;margin-top:0.5rem;display:none;"></div>
        <div class="calendar-modal-actions">
            <button type="button" class="cal-ev-path-cancel">Cancel</button>
            <button type="button" class="primary cal-ev-path-ok">OK</button>
        </div>
    </div>`;
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });
    document.body.appendChild(backdrop);

    const input = backdrop.querySelector('#cal-ev-path-input');
    const listEl = backdrop.querySelector('.cal-ev-path-list');
    const errEl = backdrop.querySelector('.calendar-modal-error');
    // path -> pretty label map (label without the "(path)" suffix)
    const labelByPath = {};
    let entries = [];

    const renderList = (filter) => {
        const q = (filter || '').toLowerCase();
        const matches = q
            ? entries.filter(e => e.label.toLowerCase().includes(q) || e.interface_path.toLowerCase().includes(q))
            : entries;
        const shown = matches.slice(0, 300);
        if (!shown.length) {
            listEl.innerHTML = '<div style="padding:0.5rem 0.7rem;font-size:0.8rem;opacity:0.6;">No matching paths</div>';
            return;
        }
        listEl.innerHTML = shown.map(e => {
            const pretty = escapeHtml(labelByPath[e.interface_path] || e.label);
            return `<div class="cal-ev-path-item" data-path="${escapeHtml(e.interface_path)}" title="${escapeHtml(e.interface_path)}" style="padding:0.4rem 0.7rem;font-size:0.85rem;cursor:pointer;border-bottom:1px solid var(--border,#2a2a2a);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${pretty}</div>`;
        }).join('');
        if (matches.length > shown.length) {
            listEl.innerHTML += `<div style="padding:0.4rem 0.7rem;font-size:0.75rem;opacity:0.6;">…and ${matches.length - shown.length} more, keep typing to filter</div>`;
        }
    };

    listEl.addEventListener('mouseover', (e) => {
        const item = e.target.closest('.cal-ev-path-item');
        if (item) { item.style.background = 'var(--accent)'; item.style.color = 'var(--accent-contrast, #fff)'; }
    });
    listEl.addEventListener('mouseout', (e) => {
        const item = e.target.closest('.cal-ev-path-item');
        if (item) { item.style.background = ''; item.style.color = ''; }
    });
    listEl.addEventListener('click', (e) => {
        const item = e.target.closest('.cal-ev-path-item');
        if (!item) return;
        input.value = item.getAttribute('data-path');
    });

    fetch('/api/history/interface-paths')
        .then(r => r.json())
        .then(data => {
            if (!data || !data.success || !Array.isArray(data.interface_paths)) {
                if (input) input.placeholder = 'telegram_bot/dm/12345';
                return;
            }
            entries = data.interface_paths;
            entries.forEach(entry => { labelByPath[entry.interface_path] = entry.label; });
            if (input) input.placeholder = 'telegram_bot/dm/12345';
            renderList('');
        })
        .catch(() => { if (input) input.placeholder = 'telegram_bot/dm/12345'; });

    input?.addEventListener('input', () => renderList(input.value));

    backdrop.querySelector('.cal-ev-path-cancel')?.addEventListener('click', () => backdrop.remove());
    backdrop.querySelector('.cal-ev-path-ok')?.addEventListener('click', () => {
        const path = (input?.value || '').trim();
        if (!path) {
            if (errEl) { errEl.style.display = 'block'; errEl.textContent = 'Please select or type an interface path.'; }
            return;
        }
        // Derive the pretty name from the known label "Pretty Name (path)".
        let pretty = path;
        const fullLabel = labelByPath[path];
        if (fullLabel) {
            const m = fullLabel.match(/^(.*)\s+\([^)]*\)\s*$/);
            pretty = m ? m[1] : fullLabel;
        }
        backdrop.remove();
        if (typeof onSelect === 'function') onSelect(path, pretty);
    });
}

function loadHistoryData(subtab) {
    if (subtab === 'diary') return loadHistoryDiary();
    if (subtab === 'grillo') return loadHistoryGrillo();
    if (subtab === 'calendar') return loadHistoryCalendar();
    if (subtab === 'dreams') return loadHistoryDreams();
    if (subtab === 'growth') return loadHistoryGrowth();
    if (subtab === 'chat') return loadHistoryChat();
    if (subtab === 'agent') {
        try { if (window.SynthWebUI && typeof window.SynthWebUI.initAgentTab === 'function') window.SynthWebUI.initAgentTab(); } catch (e) { /* ignore */ }
        return;
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

async function loadHistoryDreams() {
    const content = document.getElementById('history-dreams-content'); if (!content) return;
    console.log('[History] loadHistoryDreams called with state:', historyState.dreams);
    content.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Loading dreams...</p></div>';
    const params = new URLSearchParams({ page: historyState.dreams.page, per_page: historyState.dreams.per_page, search: historyState.dreams.search, sort: historyState.dreams.sort });
    try {
        const response = await fetch(`/api/history/dreams?${params}`);
        const data = await response.json();
        console.log('[History] dreams response:', data);
        if (data && data.success && Array.isArray(data.entries) && data.entries.length > 0) {
            content.innerHTML = data.entries.map(entry => renderDreamEntry(entry)).join('');
            content.classList.add('history-populated');
            try { content.scrollTop = 0; content.tabIndex = -1; setTimeout(() => { try { content.focus(); } catch (e) {} }, 50); } catch (e) {}
            renderPagination('dreams', data.page, data.total_pages, data.total_count);
        } else if (data && data.success) {
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">🌙</div><p>No dreams found</p></div>';
        } else {
            console.warn('[History] dreams response indicates failure or unexpected shape:', data);
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load dreams</p></div>';
        }
    } catch (error) {
        console.error('Failed to load dreams history:', error);
        content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load dreams</p></div>';
    }
}

function renderDreamEntry(entry) {
    const timestamp = formatTimestamp(entry.executed_at);
    let safeContent = escapeHtml(entry.content || '');
    safeContent = safeContent.replace(/^[\s\u00A0]+/, '').replace(/[\n\r]+$/, '');
    const contentIsLong = safeContent.split('\n').length > 8 || safeContent.length > 800;
    const contentClass = contentIsLong ? ' history-entry-content--limited' : '';

    return `
        <div class="history-entry">
            <div class="history-entry-header">
                <div class="history-entry-meta">
                    <div class="history-entry-date">🌙 ${timestamp}</div>
                </div>
                ${entry.id ? `<button type="button" class="history-delete-btn" title="Delete this dream" onclick="window.SynthWebUI.deleteDreamEntry(${entry.id})">🗑</button>` : ''}
            </div>
            <div class="history-entry-content${contentClass}">${safeContent || '<em style="opacity:0.5">No dream content</em>'}</div>
            ${entry.has_diary && entry.diary_entry_id ? `<div class="history-entry-detail" style="margin-top: 0.75rem; opacity: 0.7;"><small>📝 Diary entry ID: ${entry.diary_entry_id}</small></div>` : ''}
        </div>
    `;
}

async function loadHistoryGrowth() {
    const content = document.getElementById('history-growth-content'); if (!content) return;
    content.innerHTML = '<div class="loading-state"><div class="loading-spinner"></div><p>Loading self-growth...</p></div>';
    try {
        const response = await fetch('/api/history/growth');
        const data = await response.json();
        if (data && data.success) {
            historyState.growth.current = data.current || '';
            historyState.growth.entries = Array.isArray(data.entries) ? data.entries : [];
            historyState.growth.currentLikes = Array.isArray(data.current_likes) ? data.current_likes : [];
            historyState.growth.currentDislikes = Array.isArray(data.current_dislikes) ? data.current_dislikes : [];
            historyState.growth.pendingProposal = data.pending_proposal || null;
            renderGrowthPanel();
        } else {
            content.classList.remove('history-populated');
            content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load self-growth</p></div>';
        }
    } catch (error) {
        console.error('Failed to load self-growth:', error);
        content.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Failed to load self-growth</p></div>';
    }
}

function renderGrowthPanel() {
    const content = document.getElementById('history-growth-content'); if (!content) return;
    const current = historyState.growth.current || '';
    const entries = historyState.growth.entries || [];

    // Likes/dislikes block, rendered inline inside the editor card just above
    // the Save / Run now buttons.
    const proposalHtml = renderPendingProposalBlock(
        historyState.growth.pendingProposal,
        historyState.growth.currentLikes || [],
        historyState.growth.currentDislikes || []
    );

    const editor = `
        <div class="history-entry">
            <div class="history-entry-header">
                <div class="history-entry-meta">
                    <div class="history-entry-date">🌱 Current self-growth state</div>
                </div>
            </div>
            <textarea id="growth-current-editor" class="search-input" style="width:100%;min-height:200px;resize:vertical;font-family:inherit;line-height:1.5;">${escapeHtml(current)}</textarea>
            ${proposalHtml}
            <div style="margin-top:0.75rem;display:flex;gap:0.5rem;">
                <button type="button" class="calendar-action-btn" onclick="window.SynthWebUI.saveGrowthCurrent()">💾 Save as current</button>
                <button type="button" id="growth-run-now-btn" class="calendar-action-btn" onclick="window.SynthWebUI.runGrowthNow()">🌱 Run now</button>
            </div>
        </div>
    `;

    let historyHtml = '';
    if (entries.length === 0) {
        historyHtml = '<div class="empty-state"><div class="icon">🌱</div><p>No self-growth history yet</p></div>';
    } else {
        historyHtml = entries.map(e => renderGrowthHistoryEntry(e)).join('');
    }

    content.innerHTML = editor +
        '<h3 style="margin:1.25rem 0 0.5rem;font-size:0.95rem;opacity:0.85;">History (last 10)</h3>' +
        historyHtml;
    content.classList.add('history-populated');
}

// Render a list of requested (proposed) items, highlighting how they differ
// from the current list: additions in green, removals struck through in red,
// unchanged items neutral. Pure list-diff — no keyword/phrase matching.
function renderGrowthDiffList(proposed, current) {
    const currentSet = new Set(current);
    const proposedSet = new Set(proposed);
    const parts = [];
    proposed.forEach(item => {
        const cls = currentSet.has(item) ? '' : ' growth-item-added';
        parts.push(`<li class="growth-item${cls}">${escapeHtml(item)}</li>`);
    });
    current.forEach(item => {
        if (!proposedSet.has(item)) {
            parts.push(`<li class="growth-item growth-item-removed">${escapeHtml(item)}</li>`);
        }
    });
    if (parts.length === 0) {
        return '<li class="growth-item" style="opacity:0.6;">(empty)</li>';
    }
    return parts.join('');
}

function renderPendingProposalBlock(proposal, currentLikes, currentDislikes) {
    // Always show the likes/dislikes fields. When a proposal is pending, show
    // the requested values as a diff against the current ones (additions in
    // green, removals struck through). When nothing is pending, the requested
    // values simply are the current ones, so we diff each list against itself
    // (all neutral).
    const hasProposal = !!proposal;
    const likes = hasProposal && Array.isArray(proposal.likes) ? proposal.likes : currentLikes;
    const dislikes = hasProposal && Array.isArray(proposal.dislikes) ? proposal.dislikes : currentDislikes;
    const title = hasProposal
        ? '🌱 Requested likes / dislikes (pending approval)'
        : '🌱 Likes / dislikes (current)';
    const legend = hasProposal
        ? 'Green = added · struck through = removed vs current'
        : 'No pending proposal — showing current likes / dislikes';
    return `
        <div class="growth-proposal-inline" style="margin-top:0.75rem;">
            <div class="history-entry-date" style="margin-bottom:0.5rem;">${title}</div>
            <div class="growth-proposal-lists">
                <div class="growth-proposal-col">
                    <h4 style="margin:0 0 0.4rem;font-size:0.85rem;opacity:0.85;">👍 Likes</h4>
                    <ul class="growth-diff-list">${renderGrowthDiffList(likes, currentLikes)}</ul>
                </div>
                <div class="growth-proposal-col">
                    <h4 style="margin:0 0 0.4rem;font-size:0.85rem;opacity:0.85;">👎 Dislikes</h4>
                    <ul class="growth-diff-list">${renderGrowthDiffList(dislikes, currentDislikes)}</ul>
                </div>
            </div>
            <p style="margin:0.6rem 0 0;font-size:0.75rem;opacity:0.6;">${legend}</p>
        </div>
    `;
}

function renderGrowthEntryList(items) {
    const arr = Array.isArray(items) ? items : [];
    if (!arr.length) return '<li class="growth-item" style="opacity:0.5;">(empty)</li>';
    return arr.map(it => `<li class="growth-item">${escapeHtml(String(it))}</li>`).join('');
}

function renderGrowthEntryLists(entry) {
    const likes = Array.isArray(entry.likes) ? entry.likes : [];
    const dislikes = Array.isArray(entry.dislikes) ? entry.dislikes : [];
    // Nothing was recorded for this iteration (e.g. rows created before the
    // likes/dislikes columns existed) — omit the block entirely.
    if (!likes.length && !dislikes.length) return '';
    return `
        <div class="growth-proposal-inline" style="margin-top:0.6rem;">
            <div class="history-entry-date" style="font-size:0.85rem;">🌱 Likes / dislikes proposed at this iteration</div>
            <div class="growth-proposal-lists">
                <div class="growth-proposal-col">
                    <h4 style="margin:0 0 0.4rem;font-size:0.85rem;opacity:0.85;">👍 Likes</h4>
                    <ul class="growth-diff-list">${renderGrowthEntryList(likes)}</ul>
                </div>
                <div class="growth-proposal-col">
                    <h4 style="margin:0 0 0.4rem;font-size:0.85rem;opacity:0.85;">👎 Dislikes</h4>
                    <ul class="growth-diff-list">${renderGrowthEntryList(dislikes)}</ul>
                </div>
            </div>
        </div>
    `;
}

function renderGrowthHistoryEntry(entry) {
    const timestamp = formatTimestamp(entry.created_at);
    let safeContent = escapeHtml(entry.content || '');
    const contentIsLong = safeContent.split('\n').length > 8 || safeContent.length > 800;
    const contentClass = contentIsLong ? ' history-entry-content--limited' : '';
    const currentBadge = entry.is_current ? ' <span style="color:var(--accent, #6ec1e4);">● current</span>' : '';
    const meta = `${entry.source || 'weekly'} · ${entry.created_by || 'grillo_growth'}`;
    const listsHtml = renderGrowthEntryLists(entry);

    return `
        <div class="history-entry">
            <div class="history-entry-header">
                <div class="history-entry-meta">
                    <div class="history-entry-date">🌱 ${timestamp}${currentBadge}</div>
                    <div class="history-entry-detail" style="opacity:0.7;"><small>${escapeHtml(meta)}</small></div>
                </div>
                ${entry.is_current ? '' : `<div style="display:flex;gap:0.5rem;"><button type="button" class="calendar-action-btn" onclick="window.SynthWebUI.revertGrowthState(${entry.id})">↩ Revert to this</button><button type="button" class="history-delete-btn" title="Delete this entry" onclick="window.SynthWebUI.deleteGrowthState(${entry.id})">🗑</button></div>`}
            </div>
            <div class="history-entry-content${contentClass}">${safeContent || '<em style="opacity:0.5">No content</em>'}</div>
            ${listsHtml}
        </div>
    `;
}

async function saveGrowthCurrent() {
    const editor = document.getElementById('growth-current-editor'); if (!editor) return;
    const content = editor.value.trim();
    try {
        const response = await fetch('/api/growth/current', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content })
        });
        const data = await response.json();
        if (data && data.success) {
            loadHistoryGrowth();
        } else {
            alert('Failed to save: ' + ((data && data.error) || 'unknown error'));
        }
    } catch (error) {
        console.error('Failed to save self-growth:', error);
        alert('Failed to save self-growth.');
    }
}

async function runGrowthNow() {
    const btn = document.getElementById('growth-run-now-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Running...'; }
    try {
        const response = await fetch('/api/components/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: 'grillo_growth', action: 'run_now', payload: {} })
        });
        const data = await response.json();
        const result = data && data.result;
        if (result && result.status === 'done') {
            loadHistoryGrowth();
        } else {
            const msg = (result && result.message) || (data && data.error) || 'unknown error';
            alert('Self-growth run failed: ' + msg);
        }
    } catch (error) {
        console.error('Failed to run self-growth:', error);
        alert('Failed to run self-growth.');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🌱 Run now'; }
    }
}

async function revertGrowthState(id) {
    if (!confirm('Revert the current self-growth state to this history entry?')) return;
    try {
        const response = await fetch('/api/growth/revert', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        const data = await response.json();
        if (data && data.success) {
            loadHistoryGrowth();
        } else {
            alert('Failed to revert: ' + ((data && data.error) || 'unknown error'));
        }
    } catch (error) {
        console.error('Failed to revert self-growth:', error);
        alert('Failed to revert self-growth.');
    }
}

async function deleteHistoryItem(url, reload, confirmMsg) {
    if (!confirm(confirmMsg)) return;
    try {
        const response = await fetch(url, { method: 'DELETE' });
        const data = await response.json();
        if (data && data.success) {
            reload();
        } else {
            alert('Failed to delete: ' + ((data && data.error) || 'unknown error'));
        }
    } catch (error) {
        console.error('Failed to delete history item:', error);
        alert('Failed to delete.');
    }
}

function deleteDiaryDay(id) {
    deleteHistoryItem('/api/history/diary/' + id, loadHistoryDiary, 'Delete the entire diary entry for this day? This cannot be undone.');
}

function deleteGrilloEntry(id) {
    deleteHistoryItem('/api/history/grillo/' + id, loadHistoryGrillo, 'Delete this grillo activity entry? This cannot be undone.');
}

function deleteDreamEntry(id) {
    deleteHistoryItem('/api/history/dreams/' + id, loadHistoryDreams, 'Delete this dream entry? This cannot be undone.');
}

function deleteGrowthState(id) {
    deleteHistoryItem('/api/history/growth/' + id, loadHistoryGrowth, 'Delete this self-growth history entry? This cannot be undone.');
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
                ${entry.id ? `<button type="button" class="history-delete-btn" title="Delete this day" onclick="window.SynthWebUI.deleteDiaryDay(${entry.id})">🗑</button>` : ''}
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
                ${entry.id ? `<button type="button" class="history-delete-btn" title="Delete this entry" onclick="window.SynthWebUI.deleteGrilloEntry(${entry.id})">🗑</button>` : ''}
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
                    <span>Grillo Actions</span>
                </div>
                <div class="history-entry-detail" style="margin: 0; opacity: 0.7;"><em>No actions proposed</em></div>
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
                <span>Grillo Actions</span>
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
    let replyQuoteHtml = '';
    const replyTo = msg.metadata && msg.metadata.reply_to;
    if (replyTo) {
        const replySender = escapeHtml(replyTo.sender_name || 'Unknown');
        const replyText = escapeHtml(replyTo.text || '');
        replyQuoteHtml = `<div class="chat-reply-quote"><span class="chat-reply-sender">${replySender}</span><span class="chat-reply-text">${replyText}</span></div>`;
    }
    return `
        <div class="chat-message">
            <div class="chat-sender">${escapeHtml(msg.sender_name || 'Unknown')}</div>
            <div class="chat-body">
                ${replyQuoteHtml}<div class="chat-text${longClass}" title="${safeMessage}">
                ${safeMessage}
            </div>
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
// Expose to the global scope so the inline onclick="changePage(...)" handlers in
// renderPagination() can reach it (this file runs inside an IIFE).
window.changePage = changePage;

function debounce(func, wait) { let timeout; return function executedFunction(...args) { const later = () => { clearTimeout(timeout); func(...args); }; clearTimeout(timeout); timeout = setTimeout(later, wait); }; }

function escapeHtml(text) { return SynthUtils.escapeHtml(text); }

function formatTimestamp(isoString) { return SynthUtils.formatTimestamp(isoString); }

// Expose initializer for dynamic loader
window.SynthWebUI = window.SynthWebUI || {};
window.SynthWebUI.initHistoryTab = function() { initializeHistoryTab(); };
window.SynthWebUI.saveGrowthCurrent = saveGrowthCurrent;
window.SynthWebUI.runGrowthNow = runGrowthNow;
window.SynthWebUI.revertGrowthState = revertGrowthState;
window.SynthWebUI.deleteDiaryDay = deleteDiaryDay;
window.SynthWebUI.deleteGrilloEntry = deleteGrilloEntry;
window.SynthWebUI.deleteDreamEntry = deleteDreamEntry;
window.SynthWebUI.deleteGrowthState = deleteGrowthState;

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
