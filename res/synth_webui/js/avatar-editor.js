/* Synth avatar editor (Settings → Synth Avatar).
 *
 * Pick an image, drag it and zoom to frame it, check the square and round
 * previews, then confirm: the framed 512x512 PNG is uploaded to
 * /api/synth_avatar and core pushes it to the interfaces that use it.
 * Vanilla canvas + pointer events, no dependencies. Exposes
 * window.createSynthAvatarEditor(): HTMLElement.
 */
(function () {
    'use strict';

    const VIEW = 320;      // editor viewport (CSS px)
    const OUT = 512;       // exported size
    const MAX_ZOOM = 4;
    const PREVIEW = 96;

    function apiBase() { return window.__getApiBase ? window.__getApiBase() : ''; }
    function toast(msg, isErr) { if (window.showToast) window.showToast(msg, !!isErr); }
    function el(tag, css, text) {
        const n = document.createElement(tag);
        if (css) n.style.cssText = css;
        if (text !== undefined) n.textContent = text;
        return n;
    }
    async function readError(r) {
        try { const j = await r.json(); return j.detail || ('HTTP ' + r.status); } catch (e) { return 'HTTP ' + r.status; }
    }

    window.createSynthAvatarEditor = function () {
        const state = { img: null, zoom: 1, cx: 0, cy: 0, dragging: null };

        const wrap = el('div', 'display:flex; flex-direction:column; gap:10px; max-width:760px; width:100%;');

        // --- current avatar + file picker ---------------------------------------
        const top = el('div', 'display:flex; align-items:center; gap:12px; flex-wrap:wrap;');
        const current = el('img', `width:${PREVIEW}px; height:${PREVIEW}px; object-fit:cover; border-radius:50%; background:#8883; display:none;`);
        current.alt = 'Current avatar';
        const currentLabel = el('span', '', 'No avatar set');
        currentLabel.className = 'meta';
        const picker = document.createElement('input');
        picker.type = 'file';
        picker.accept = 'image/png,image/jpeg,image/webp,image/gif';
        top.appendChild(current);
        top.appendChild(currentLabel);
        top.appendChild(picker);
        wrap.appendChild(top);

        // --- editor + previews -----------------------------------------------------
        const body = el('div', 'display:none; gap:16px; flex-wrap:wrap; align-items:flex-start;');
        const left = el('div', 'display:flex; flex-direction:column; gap:6px;');
        const view = document.createElement('canvas');
        view.width = VIEW; view.height = VIEW;
        view.style.cssText = `width:${VIEW}px; height:${VIEW}px; max-width:100%; border:1px solid var(--border,#444); border-radius:6px; background:#222; touch-action:none; cursor:grab;`;
        const zoom = document.createElement('input');
        zoom.type = 'range'; zoom.min = '1'; zoom.max = String(MAX_ZOOM); zoom.step = '0.01'; zoom.value = '1';
        zoom.style.width = VIEW + 'px';
        zoom.setAttribute('aria-label', 'Zoom');
        const hint = el('span', '', 'Drag to move · wheel or slider to zoom');
        hint.className = 'meta';
        left.appendChild(view); left.appendChild(zoom); left.appendChild(hint);

        const right = el('div', 'display:flex; flex-direction:column; gap:10px;');
        const mkPrev = (round, caption) => {
            const box = el('div', 'display:flex; align-items:center; gap:10px;');
            const c = document.createElement('canvas');
            c.width = PREVIEW * 2; c.height = PREVIEW * 2;
            c.style.cssText = `width:${PREVIEW}px; height:${PREVIEW}px; background:#8883; ${round ? 'border-radius:50%;' : 'border-radius:4px;'}`;
            const cap = el('span', '', caption);
            cap.className = 'meta';
            box.appendChild(c); box.appendChild(cap);
            right.appendChild(box);
            return c;
        };
        const prevSquare = mkPrev(false, 'Square');
        const prevRound = mkPrev(true, 'Round');
        body.appendChild(left); body.appendChild(right);
        wrap.appendChild(body);

        // --- actions -----------------------------------------------------------------
        const bar = el('div', 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;');
        const confirm = el('button', '', 'Confirm and apply');
        confirm.type = 'button'; confirm.disabled = true;
        const remove = el('button', '', 'Remove avatar');
        remove.type = 'button'; remove.className = 'btn-ghost'; remove.disabled = true;
        const status = el('span', ''); status.className = 'meta';
        bar.appendChild(confirm); bar.appendChild(remove); bar.appendChild(status);
        wrap.appendChild(bar);
        const ifaceList = el('div', 'display:flex; flex-direction:column; gap:2px;');
        ifaceList.className = 'meta';
        wrap.appendChild(ifaceList);

        // --- geometry ------------------------------------------------------------------
        function baseScale() { return VIEW / Math.min(state.img.naturalWidth, state.img.naturalHeight); }
        function scale() { return baseScale() * state.zoom; }
        function clamp() {
            const halfW = (state.img.naturalWidth * scale()) / 2;
            const halfH = (state.img.naturalHeight * scale()) / 2;
            const maxX = Math.max(halfW - VIEW / 2, 0);
            const maxY = Math.max(halfH - VIEW / 2, 0);
            state.cx = Math.min(Math.max(state.cx, -maxX), maxX);
            state.cy = Math.min(Math.max(state.cy, -maxY), maxY);
        }
        // Draw the framed square into ctx (size px), same mapping for editor, previews and export.
        function paint(ctx, size) {
            const k = size / VIEW;
            const s = scale() * k;
            const w = state.img.naturalWidth * s;
            const h = state.img.naturalHeight * s;
            ctx.clearRect(0, 0, size, size);
            ctx.imageSmoothingQuality = 'high';
            ctx.drawImage(state.img, size / 2 + state.cx * k - w / 2, size / 2 + state.cy * k - h / 2, w, h);
        }
        function render() {
            if (!state.img) return;
            clamp();
            const ctx = view.getContext('2d');
            paint(ctx, VIEW);
            // Dim everything outside the circle so the round crop is visible.
            ctx.save();
            ctx.fillStyle = 'rgba(0,0,0,0.45)';
            ctx.beginPath();
            ctx.rect(0, 0, VIEW, VIEW);
            ctx.arc(VIEW / 2, VIEW / 2, VIEW / 2, 0, Math.PI * 2, true);
            ctx.fill('evenodd');
            ctx.restore();
            [prevSquare, prevRound].forEach((c) => paint(c.getContext('2d'), c.width));
        }

        // --- interactions ------------------------------------------------------------------
        view.addEventListener('pointerdown', (e) => {
            if (!state.img) return;
            view.setPointerCapture(e.pointerId);
            state.dragging = { x: e.clientX, y: e.clientY, cx: state.cx, cy: state.cy };
            view.style.cursor = 'grabbing';
        });
        view.addEventListener('pointermove', (e) => {
            if (!state.dragging) return;
            const ratio = VIEW / view.getBoundingClientRect().width;
            state.cx = state.dragging.cx + (e.clientX - state.dragging.x) * ratio;
            state.cy = state.dragging.cy + (e.clientY - state.dragging.y) * ratio;
            render();
        });
        const endDrag = () => { state.dragging = null; view.style.cursor = 'grab'; };
        view.addEventListener('pointerup', endDrag);
        view.addEventListener('pointercancel', endDrag);
        function setZoom(z) {
            state.zoom = Math.min(Math.max(z, 1), MAX_ZOOM);
            zoom.value = String(state.zoom);
            render();
        }
        zoom.addEventListener('input', () => setZoom(parseFloat(zoom.value)));
        view.addEventListener('wheel', (e) => {
            if (!state.img) return;
            e.preventDefault();
            setZoom(state.zoom * (e.deltaY < 0 ? 1.08 : 1 / 1.08));
        }, { passive: false });

        picker.addEventListener('change', () => {
            const file = picker.files && picker.files[0];
            if (!file) return;
            if (!/^image\//.test(file.type)) { status.textContent = 'Choose an image file.'; return; }
            const url = URL.createObjectURL(file);
            const img = new Image();
            img.onload = () => {
                URL.revokeObjectURL(url);
                state.img = img; state.zoom = 1; state.cx = 0; state.cy = 0;
                zoom.value = '1';
                body.style.display = 'flex';
                confirm.disabled = false;
                status.textContent = '';
                render();
            };
            img.onerror = () => { URL.revokeObjectURL(url); status.textContent = 'Could not read that image.'; };
            img.src = url;
        });

        // --- server state ---------------------------------------------------------------------
        function showInfo(info) {
            if (info && info.exists) {
                current.src = `${apiBase()}/api/synth_avatar?v=${encodeURIComponent(info.version || '')}`;
                current.style.display = '';
                currentLabel.textContent = 'Current avatar';
                remove.disabled = false;
            } else {
                current.style.display = 'none';
                currentLabel.textContent = 'No avatar set';
                remove.disabled = true;
            }
            ifaceList.textContent = '';
            ((info && info.interfaces) || []).forEach((i) => {
                ifaceList.appendChild(el('span', '', `${i.name}: ${i.enabled ? 'uses the avatar' : 'not enabled (turn on in its settings)'}`));
            });
        }
        async function loadInfo() {
            try {
                const r = await fetch(apiBase() + '/api/synth_avatar/info');
                if (!r.ok) throw new Error(await readError(r));
                showInfo(await r.json());
            } catch (e) { status.textContent = 'Avatar info unavailable: ' + e.message; }
        }
        function summarize(results) {
            const entries = Object.entries(results || {});
            return entries.length ? ' — ' + entries.map(([n, s]) => `${n}: ${s}`).join('; ') : '';
        }

        confirm.addEventListener('click', async () => {
            if (!state.img) return;
            confirm.disabled = true; status.textContent = 'Uploading…';
            try {
                const out = document.createElement('canvas');
                out.width = OUT; out.height = OUT;
                paint(out.getContext('2d'), OUT);
                const blob = await new Promise((res) => out.toBlob(res, 'image/png'));
                if (!blob) throw new Error('Could not encode the image');
                const fd = new FormData();
                fd.append('file', blob, 'avatar.png');
                const r = await fetch(apiBase() + '/api/synth_avatar', { method: 'POST', body: fd });
                if (!r.ok) throw new Error(await readError(r));
                const res = await r.json();
                status.textContent = 'Saved' + summarize(res.interfaces);
                toast('Synth avatar updated', false);
                body.style.display = 'none'; state.img = null; picker.value = '';
                await loadInfo();
            } catch (e) {
                status.textContent = 'Not saved: ' + e.message;
                toast('Avatar upload failed: ' + e.message, true);
                confirm.disabled = false;
            }
        });

        remove.addEventListener('click', async () => {
            remove.disabled = true; status.textContent = 'Removing…';
            try {
                const r = await fetch(apiBase() + '/api/synth_avatar', { method: 'DELETE' });
                if (!r.ok) throw new Error(await readError(r));
                const res = await r.json();
                status.textContent = 'Removed' + summarize(res.interfaces);
                await loadInfo();
            } catch (e) {
                status.textContent = 'Not removed: ' + e.message;
                remove.disabled = false;
            }
        });

        loadInfo();
        return wrap;
    };
})();
