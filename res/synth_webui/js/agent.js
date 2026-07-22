(function(){
'use strict';

// compute base URL for API calls
const _apiBase = (window.__getApiBase && window.__getApiBase()) || '';
let _selectedAgentTaskId = null;

function _escapeHtml(value){
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Conversation stream builders ──────────────────────────────────────────

function _bubble(role, text){
  return `<div class="agent-msg ${role}">${_escapeHtml(text)}</div>`;
}

function _toolRow(item){
  const tool = _escapeHtml(item.tool || 'tool');
  const ok = !!item.ok && !item.error;
  const badge = ok
    ? '<span class="agent-tool-badge ok">ok</span>'
    : '<span class="agent-tool-badge err">error</span>';
  const resultText = item.result != null
    ? (typeof item.result === 'object' ? JSON.stringify(item.result, null, 2) : String(item.result))
    : '';
  const bodyPre = resultText ? `<pre>${_escapeHtml(resultText)}</pre>` : '';
  const errBody = item.error ? `<div class="agent-tool-err">${_escapeHtml(item.error)}</div>` : '';
  return `<details class="agent-tool">
    <summary><span class="agent-tool-name">${tool}</span>${badge}</summary>
    ${bodyPre}${errBody}
  </details>`;
}

// Renders a single iterations_meta entry as one or more stream elements.
function renderTimelineEntry(entry){
  if(!entry || typeof entry !== 'object') return '';
  const role = entry.role || 'observation';
  const raw = entry.result ?? entry.actions_result ?? entry.content ?? entry;

  // Explicit human intervention typed via the composer.
  if(role === 'user_message'){
    const txt = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2);
    return _bubble('user', txt);
  }

  // Tool result rows (array of {tool, ok, result, error}).
  if(Array.isArray(raw)){
    return raw.map(item=>{
      if(item && typeof item === 'object' && ('tool' in item || 'ok' in item || 'error' in item)){
        return _toolRow(item);
      }
      return `<div class="agent-msg assistant"><pre>${_escapeHtml(JSON.stringify(item, null, 2))}</pre></div>`;
    }).join('');
  }

  if(role === 'error'){
    const txt = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2);
    return _bubble('error', txt);
  }

  // Plain assistant reasoning / text.
  if(raw && typeof raw === 'object'){
    return `<div class="agent-msg assistant"><pre>${_escapeHtml(JSON.stringify(raw, null, 2))}</pre></div>`;
  }
  return _bubble('assistant', raw);
}

// Agent tab helpers (migrated from inline template)
async function fetchAgentTools(){
  try{
    const resp = await fetch(_apiBase + '/api/agent/tools');
    const data = await resp.json();
    const tools = data.tools || [];
    const container = document.getElementById('agent-tools-list');
    if(!container) return;
    if(!tools.length){
      container.innerHTML = '<div class="card">No tools available.</div>';
      return;
    }
    const html = tools.map(t=>{
      const params = (t.parameters || []).map(p=>`${p.name}:${p.type}${p.required ? ' (required)' : ''}`).join(', ') || 'no-params';
      const effects = (t.external_effects || []).join(', ') || '-';
      return `<div class="card" style="margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;">
          <div><strong>${t.name}</strong></div>
          <div><small>${t.source} | security:${t.security_level}</small></div>
        </div>
        <div style="margin-top:4px;"><small>${t.description || ''}</small></div>
        <div style="margin-top:6px;"><small><strong>params:</strong> ${params}</small></div>
        <div style="margin-top:4px;"><small><strong>effects:</strong> ${effects}</small></div>
      </div>`;
    }).join('');
    container.innerHTML = html;
  }catch(e){
    console.error(e);
    const el = document.getElementById('agent-tools-list');
    if(el) el.innerText = 'Failed to load tools';
  }
}

async function runAgenticTurn(){
  const goalEl = document.getElementById('agent-goal-input');
  const engineEl = document.getElementById('agent-engine-input');
  const maxIterEl = document.getElementById('agent-max-iter-input');
  const timeoutEl = document.getElementById('agent-timeout-input');
  const out = document.getElementById('agent-run-result');

  if(!goalEl || !out) return;
  const prompt = (goalEl.value || '').trim();
  if(!prompt){
    out.innerHTML = '<div class="card">Insert a goal first.</div>';
    return;
  }

  out.innerHTML = '<div class="card">Running agentic turn...</div>';
  try{
    const payload = {
      prompt,
      engine: (engineEl && engineEl.value || '').trim() || undefined,
      max_iterations: maxIterEl ? parseInt(maxIterEl.value || '5', 10) : 5,
      timeout_seconds: timeoutEl ? parseInt(timeoutEl.value || '120', 10) : 120,
    };
    const resp = await fetch(_apiBase + '/api/agent/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if(!resp.ok){
      out.innerHTML = `<div class="card">Run failed: ${data.detail || resp.statusText}</div>`;
      return;
    }

    const result = data.result || {};
    const observations = result.observations || [];
    const obsHtml = observations.map(o=>`<div style="border-top:1px solid var(--border);padding:6px 0;">
      <div><strong>Iter ${o.iteration || '-'} - ${o.role || 'observation'}</strong></div>
      <pre style="white-space:pre-wrap;overflow:auto;">${JSON.stringify(o.content, null, 2)}</pre>
    </div>`).join('');

    out.innerHTML = `<div class="card">
      <div><strong>Stop reason:</strong> ${result.stop_reason || '-'}</div>
      <div><strong>Iterations:</strong> ${result.iterations || 0}</div>
      <div style="margin-top:8px;"><strong>Final text:</strong></div>
      <pre style="white-space:pre-wrap;overflow:auto;">${result.final_text || ''}</pre>
      <div style="margin-top:8px;"><strong>Observations:</strong></div>
      ${obsHtml || '<div>No observations</div>'}
    </div>`;

    fetchAgentTasks();
  }catch(e){
    console.error(e);
    out.innerHTML = '<div class="card">Run failed due to network/runtime error.</div>';
  }
}

async function fetchAgentTasks(){
  try{
    const resp = await fetch(_apiBase + '/api/agent/tasks');
    const data = await resp.json();
    const list = data.tasks || [];
    const container = document.getElementById('agent-tasks-list');
    if(!container) return;
    if(!list.length){
      container.innerHTML = '<div class="card">No agent tasks found.</div>';
      const conv = document.getElementById('agent-conversation');
      if(conv) conv.innerHTML = '<div class="agent-empty">No task selected.</div>';
      const meta = document.getElementById('agent-task-meta');
      if(meta) meta.textContent = 'No task selected.';
      _selectedAgentTaskId = null;
      return;
    }

    const ids = new Set(list.map(t => String(t.id)));
    if(_selectedAgentTaskId == null || !ids.has(_selectedAgentTaskId)){
      _selectedAgentTaskId = String(list[0].id);
    }

    const html = list.map(t=>{
      const idStr = String(t.id);
      const isSelected = idStr === _selectedAgentTaskId;
      const border = isSelected ? '2px solid var(--primary, #4f8cff)' : '1px solid var(--border)';
      const bg = isSelected ? 'var(--panel, rgba(79,140,255,0.08))' : 'var(--surface, transparent)';
      const subtitle = t.created_at ? new Date(t.created_at).toLocaleString() : '';
      const label = t.name ? _escapeHtml(t.name) : `<small>${_escapeHtml(t.engine || 'default')}</small>`;
      const nameAttr = t.name ? _escapeHtml(t.name) : '';
      return `<div style="position:relative;margin-bottom:4px;">
        <button type="button" data-agent-task-id="${idStr}" class="card" style="width:100%;text-align:left;border:${border};background:${bg};cursor:pointer;border-radius:8px;padding:6px 66px 6px 10px;box-shadow:none;gap:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
            <div style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"><strong>#${t.id}</strong> ${label}</div>
            <div style="flex-shrink:0;"><small>${_escapeHtml(t.status || '-')}</small></div>
          </div>
          <div style="margin-top:2px;opacity:0.6;"><small>${_escapeHtml(subtitle)}</small></div>
        </button>
        <button type="button" class="history-delete-btn" data-agent-task-rename="${idStr}" data-agent-task-name="${nameAttr}" title="Rename this task" style="position:absolute;top:4px;right:34px;padding:0.15rem 0.35rem;">✏️</button>
        <button type="button" class="history-delete-btn" data-agent-task-delete="${idStr}" title="Delete this task" style="position:absolute;top:4px;right:4px;padding:0.15rem 0.35rem;">🗑</button>
      </div>`;
    }).join('');
    container.innerHTML = html;

    container.querySelectorAll('[data-agent-task-id]').forEach(btn=>{
      btn.addEventListener('click', ()=>{
        const id = parseInt(btn.getAttribute('data-agent-task-id') || '', 10);
        if(!Number.isFinite(id)) return;
        _selectedAgentTaskId = String(id);
        fetchAgentTasks();
        loadAgentTask(id);
      });
    });

    container.querySelectorAll('[data-agent-task-rename]').forEach(btn=>{
      btn.addEventListener('click', (ev)=>{
        ev.stopPropagation();
        const id = parseInt(btn.getAttribute('data-agent-task-rename') || '', 10);
        if(!Number.isFinite(id)) return;
        renameAgentTask(id, btn.getAttribute('data-agent-task-name') || '');
      });
    });

    container.querySelectorAll('[data-agent-task-delete]').forEach(btn=>{
      btn.addEventListener('click', (ev)=>{
        ev.stopPropagation();
        const id = parseInt(btn.getAttribute('data-agent-task-delete') || '', 10);
        if(!Number.isFinite(id)) return;
        deleteAgentTask(id);
      });
    });

    if(_selectedAgentTaskId != null){
      loadAgentTask(_selectedAgentTaskId);
    }
  }catch(e){ console.error(e); const el = document.getElementById('agent-tasks-list'); if (el) el.innerText = 'Failed to load agent tasks'; }
}

// ── Selected task: linear conversation stream ─────────────────────────────

// Distance from the bottom (px) within which we still consider the user
// "pinned" to the latest content and are allowed to auto-scroll.
const _AGENT_SCROLL_STICK_THRESHOLD = 60;

// True when the user is at (or very near) the bottom of the conversation.
// Used to decide whether a re-render should keep following new content or
// leave the scroll position alone so the user can read earlier messages.
function _isConversationNearBottom(conv){
  if(!conv) return true;
  const distance = conv.scrollHeight - conv.scrollTop - conv.clientHeight;
  return distance <= _AGENT_SCROLL_STICK_THRESHOLD;
}

// Auto-scroll to the bottom ONLY if the user was already near the bottom.
// While the agent runs, re-renders would otherwise keep yanking the view
// back down and prevent the user from scrolling up to read earlier steps.
function _scrollConversationToBottom(force){
  const conv = document.getElementById('agent-conversation');
  if(!conv) return;
  if(force || _isConversationNearBottom(conv)){
    conv.scrollTop = conv.scrollHeight;
  }
}

async function loadAgentTask(id){
  try{
    const resp = await fetch(_apiBase + `/api/agent/tasks/${id}`);
    const conv = document.getElementById('agent-conversation');
    const metaBar = document.getElementById('agent-task-meta');
    if(!conv) return;
    if(!resp.ok){
      conv.innerHTML = '<div class="agent-empty">Failed to fetch task.</div>';
      return;
    }
    const data = await resp.json();

    const iterationsMeta = Array.isArray(data.iterations_meta) ? data.iterations_meta : [];
    const output = data.output && typeof data.output === 'object' ? data.output : {};
    const finalText = output.final_text || '';
    const stopReason = output.stop_reason || '-';
    const status = data.status || '-';
    const engine = data.engine || 'default';
    const createdAt = data.created_at ? new Date(data.created_at).toLocaleString() : '-';
    const goal = (data.input && data.input.goal) || '';

    // Slim meta bar.
    if(metaBar){
      metaBar.innerHTML = `
        <span>Task <strong>#${_escapeHtml(String(data.id))}</strong></span>
        <span>engine <strong>${_escapeHtml(engine)}</strong></span>
        <span>status <strong>${_escapeHtml(status)}</strong></span>
        <span>stop <strong>${_escapeHtml(stopReason)}</strong></span>
        <span>${_escapeHtml(createdAt)}</span>`;
    }

    const parts = [];

    // Goal bubble — shown ONCE at the top of the stream, never repeated per step.
    if(goal){
      parts.push(`<div class="agent-msg user">${_escapeHtml(goal)}</div>`);
    }

    // Reasoning / tool timeline.
    for(const entry of iterationsMeta){
      const rendered = renderTimelineEntry(entry);
      if(rendered) parts.push(rendered);
    }

    // Final answer bubble.
    if(finalText){
      parts.push(`<div class="agent-msg assistant"><strong>Final answer</strong>\n${_escapeHtml(finalText)}</div>`);
    }

    if(!parts.length){
      parts.push('<div class="agent-empty">No steps captured for this task yet.</div>');
    }

    // Capture whether the user was pinned to the bottom BEFORE we replace the
    // HTML (which resets scrollTop). Only re-follow new content if they were.
    const wasNearBottom = _isConversationNearBottom(conv);
    conv.innerHTML = parts.join('');

    if(wasNearBottom) _scrollConversationToBottom(true);
  }catch(e){ console.error(e); }
}

async function deleteAgentTask(id){
  if(!window.confirm(`Delete agent task #${id}? This cannot be undone.`)) return;
  try{
    const resp = await fetch(_apiBase + `/api/agent/tasks/${id}`, {method:'DELETE'});
    if(!resp.ok){ console.error('Failed to delete agent task', id, resp.status); return; }
    if(String(_selectedAgentTaskId) === String(id)) _selectedAgentTaskId = null;
    await fetchAgentTasks();
  }catch(e){ console.error(e); }
}

async function renameAgentTask(id, currentName){
  const next = window.prompt(`Rename task #${id} (leave empty to clear):`, currentName || '');
  if(next === null) return; // cancelled
  try{
    const resp = await fetch(_apiBase + `/api/agent/tasks/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: next.trim() }),
    });
    if(!resp.ok){ console.error('Failed to rename agent task', id, resp.status); return; }
    await fetchAgentTasks();
  }catch(e){ console.error(e); }
}

// ── Composer: send a user message into the task timeline ──────────────────

function _autoGrow(el){
  if(!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

async function sendTaskMessage(){
  const input = document.getElementById('agent-msg-input');
  const btn = document.getElementById('agent-send-btn');
  if(!input) return;
  const text = (input.value || '').trim();
  if(!text) return;
  if(_selectedAgentTaskId == null){
    console.warn('No task selected; cannot send message.');
    return;
  }
  if(btn) btn.disabled = true;
  try{
    const resp = await fetch(_apiBase + `/api/agent/tasks/${_selectedAgentTaskId}/message`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}),
    });
    if(!resp.ok){
      const data = await resp.json().catch(()=>({}));
      console.error('Send failed', data.detail || resp.statusText);
      return;
    }
    input.value = '';
    _autoGrow(input);
    await loadAgentTask(_selectedAgentTaskId);
  }catch(e){
    console.error('Send failed', e);
  }finally{
    if(btn) btn.disabled = false;
  }
}

// Keep compatibility with inline onclick handlers rendered in template strings.
window.deleteAgentTask = deleteAgentTask;
window.renameAgentTask = renameAgentTask;
window.sendTaskMessage = sendTaskMessage;

// Expose for loader
window.SynthWebUI = window.SynthWebUI || {};
window.SynthWebUI.initAgentTab = function(){
  // Initial load and periodic refreshes
  try{
    fetchAgentTools();
    fetchAgentTasks();

    const layout = document.getElementById('agent-legacy-layout');
    if(layout){
      const applyLayout = ()=>{
        layout.style.gridTemplateColumns = window.innerWidth < 900 ? '1fr' : 'minmax(260px,34%) 1fr';
      };
      applyLayout();
      window.addEventListener('resize', applyLayout);
    }

    const runBtn = document.getElementById('agent-run-btn');
    if(runBtn && !runBtn.__agentBound){
      runBtn.addEventListener('click', runAgenticTurn);
      runBtn.__agentBound = true;
    }

    // Composer bindings: Send button + Enter-to-send (Shift+Enter = newline).
    const sendBtn = document.getElementById('agent-send-btn');
    if(sendBtn && !sendBtn.__agentBound){
      sendBtn.addEventListener('click', sendTaskMessage);
      sendBtn.__agentBound = true;
    }
    const msgInput = document.getElementById('agent-msg-input');
    if(msgInput && !msgInput.__agentBound){
      msgInput.addEventListener('keydown', (ev)=>{
        if(ev.key === 'Enter' && !ev.shiftKey){
          ev.preventDefault();
          sendTaskMessage();
        }
      });
      msgInput.addEventListener('input', ()=>_autoGrow(msgInput));
      msgInput.__agentBound = true;
    }

    window.__synth_agent_tasks_timer = setInterval(fetchAgentTasks, 5000);
    window.__synth_agent_tools_timer = setInterval(fetchAgentTools, 15000);
  }catch(e){ console.error('initAgentTab failed', e); }
};

})();
