(function(){
'use strict';

// Agent tab helpers (migrated from inline template)
async function fetchAgentTasks(){
  try{
    const resp = await fetch('/api/agent/tasks');
    const data = await resp.json();
    const list = data.tasks || [];
    const container = document.getElementById('agent-tasks-list');
    if(!container) return;
    if(!list.length){ container.innerHTML = '<div class="card">No agent tasks found.</div>'; return; }
    const html = list.map(t=>`<div class="card" style="margin-bottom:8px;"><div style="display:flex;justify-content:space-between;align-items:center;"><div><strong>#${t.id}</strong> <small>${t.engine}</small></div><div><small>${t.status}</small></div></div><div style="margin-top:6px;"><button onclick="loadAgentTask(${t.id})">Details</button></div></div>`).join('');
    container.innerHTML = html;
  }catch(e){ console.error(e); const el = document.getElementById('agent-tasks-list'); if (el) el.innerText = 'Failed to load agent tasks'; }
}

async function loadAgentTask(id){
  try{
    const resp = await fetch(`/api/agent/tasks/${id}`);
    if(!resp.ok){ document.getElementById('agent-task-detail').innerText = 'Failed to fetch task'; return; }
    const data = await resp.json();
    const detail = document.getElementById('agent-task-detail');
    const iterations = (data.iterations_meta||[]).map(it=>`<div style="border-top:1px solid var(--border);padding:6px;">Iteration ${it.iteration}: <pre style="white-space:pre-wrap;">${JSON.stringify(it.actions_result || it.result || {}, null, 2)}</pre></div>`).join('');
    detail.innerHTML = `<div class="card"><h3>Task #${data.id} — ${data.engine}</h3><div style="margin-bottom:8px;"><strong>Status:</strong> ${data.status}</div><div>${iterations}</div><div style="margin-top:8px;"><button onclick="pauseTask(${data.id})">Pause</button> <button onclick="resumeTask(${data.id})">Resume</button> <button onclick="cancelTask(${data.id})">Cancel</button></div></div>`;
  }catch(e){ console.error(e); }
}

async function pauseTask(id){ await fetch(`/api/agent/tasks/${id}/pause`, {method:'POST'}); await fetchAgentTasks(); loadAgentTask(id);} 
async function resumeTask(id){ await fetch(`/api/agent/tasks/${id}/resume`, {method:'POST'}); await fetchAgentTasks(); loadAgentTask(id);} 
async function cancelTask(id){ await fetch(`/api/agent/tasks/${id}/cancel`, {method:'POST'}); await fetchAgentTasks(); loadAgentTask(id);} 

async function fetchAgentProposals(){
  try{
    const resp = await fetch('/api/agent/proposals');
    const data = await resp.json();
    const list = data.proposals || [];
    const container = document.getElementById('agent-proposals-list');
    if(!container) return;
    if(!list.length){ container.innerHTML = '<div class="card">No pending proposals.</div>'; return; }
    const html = list.map(p=>`<div class="card" style="margin-bottom:8px;"><div style="display:flex;justify-content:space-between;align-items:center;"><div><strong>#${p.id}</strong> <small>${p.proposer||'system'}</small></div><div><small>${p.requested_at||''}</small></div></div><div style="margin-top:6px;">${p.command}</div><div style="margin-top:6px;"><button onclick="approveProposal(${p.id})">Approve</button></div></div>`).join('');
    container.innerHTML = html;
  }catch(e){ console.error(e); const el = document.getElementById('agent-proposals-list'); if(el) el.innerText = 'Failed to load proposals'; }
}

async function approveProposal(id){
  if(!confirm('Approve proposal #' + id + '?')) return;
  try{
    const resp = await fetch(`/api/agent/proposals/${id}/approve`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({trainer: 'webui'})});
    const data = await resp.json();
    alert('Approve result: ' + JSON.stringify(data));
    fetchAgentProposals(); fetchAgentTasks();
  }catch(e){ console.error(e); alert('Approval failed'); }
}

// Expose for loader
window.SynthWebUI = window.SynthWebUI || {};
window.SynthWebUI.initAgentTab = function(){
  // Initial load and periodic refreshes
  try{
    fetchAgentTasks(); fetchAgentProposals();
    window.__synth_agent_tasks_timer = setInterval(fetchAgentTasks, 5000);
    window.__synth_agent_proposals_timer = setInterval(fetchAgentProposals, 5000);
  }catch(e){ console.error('initAgentTab failed', e); }
};

})();
