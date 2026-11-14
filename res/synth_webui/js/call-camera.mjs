// call-camera.mjs — lends the SyntH's face to a call that happens outside the browser.
//
// The Karada render only exists in this page (the #vrm-canvas the pop-out webcam shows).
// When a call on another interface (e.g. the Telegram user account) wants to show the
// SyntH's face, the server (core/call_video.py) borrows this tab:
//
//   * the page keeps an idle WebSocket open on /api/call/camera while the avatar is on
//     screen — no frames, no cost — so the server knows a camera exists;
//   * the server sends {"type":"start", call_id, fps, width, height, quality} when a call
//     asks for video and {"type":"stop"} when it ends;
//   * between the two, every rendered frame of the canvas is (rate-limited) painted onto a
//     small offscreen canvas, encoded as JPEG and sent as one binary message.
//
// The canvas is read from the render loop itself (vrm-viewer.mjs calls
// window.__synthFrameTap right after renderer.render): the WebGL back buffer is not
// preserved between frames, so reading it later would give a blank image. The tap exists
// only while a call is capturing, so there is no per-frame cost otherwise.
//
// The tab must stay open with the avatar rendering for the whole call. A background tab
// has its animation frames throttled by the browser; the pop-out webcam window keeps
// rendering at full rate, so use it for long calls.

(function initCallCamera() {
    'use strict';

    const RECONNECT_MIN_MS = 2000;
    const RECONNECT_MAX_MS = 30000;
    // Do not queue frames behind a slow socket: drop instead (the server keeps the latest).
    const MAX_BUFFERED_BYTES = 512 * 1024;
    const FALLBACK_BG = '#0d0d16';

    let ws = null;
    let reconnectDelay = RECONNECT_MIN_MS;
    let reconnectTimer = null;
    let capture = null; // { callId, intervalMs, width, height, quality, canvas, ctx, lastAt, busy, bg }

    function wsUrl() {
        try {
            const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            return `${proto}://${window.location.host}/api/call/camera`;
        } catch (_) {
            return '';
        }
    }

    function pageBackground() {
        try {
            const color = window.getComputedStyle(document.body).backgroundColor;
            if (color && color !== 'rgba(0, 0, 0, 0)') return color;
        } catch (_) { /* ignore */ }
        return FALLBACK_BG;
    }

    // Paint the rendered canvas onto the offscreen one: page colour behind it (the WebGL
    // canvas is transparent), the avatar scaled to fit without cropping.
    function paint(state, source) {
        const { ctx, width, height } = state;
        ctx.fillStyle = state.bg;
        ctx.fillRect(0, 0, width, height);
        const sw = source.width;
        const sh = source.height;
        if (!sw || !sh) return false;
        const scale = Math.min(width / sw, height / sh);
        const dw = Math.round(sw * scale);
        const dh = Math.round(sh * scale);
        ctx.drawImage(source, Math.round((width - dw) / 2), Math.round((height - dh) / 2), dw, dh);
        return true;
    }

    function onRendered(source) {
        const state = capture;
        if (!state || state.busy || !ws || ws.readyState !== WebSocket.OPEN) return;
        const now = performance.now();
        if (now - state.lastAt < state.intervalMs) return;
        if (ws.bufferedAmount > MAX_BUFFERED_BYTES) return;
        try {
            if (!paint(state, source)) return;
        } catch (e) {
            console.warn('[call-camera] could not read the avatar canvas:', e);
            return;
        }
        state.lastAt = now;
        state.busy = true;
        state.canvas.toBlob(async (blob) => {
            try {
                if (blob && capture === state && ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(await blob.arrayBuffer());
                }
            } catch (_) { /* socket closed meanwhile */ } finally {
                state.busy = false;
            }
        }, 'image/jpeg', state.quality);
    }

    function startCapture(msg) {
        stopCapture();
        const width = Math.max(2, parseInt(msg.width, 10) || 854);
        const height = Math.max(2, parseInt(msg.height, 10) || 480);
        const fps = Math.min(30, Math.max(1, Number(msg.fps) || 12));
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        capture = {
            callId: msg.call_id || null,
            intervalMs: 1000 / fps - 2,
            width,
            height,
            quality: Math.min(1, Math.max(0.1, Number(msg.quality) || 0.7)),
            canvas,
            ctx: canvas.getContext('2d'),
            lastAt: 0,
            busy: false,
            bg: pageBackground(),
        };
        window.__synthFrameTap = onRendered;
        try { window.dispatchEvent(new CustomEvent('synth:call-camera', { detail: { on: true, call_id: capture.callId } })); } catch (_) { /* ignore */ }
    }

    function stopCapture(callId) {
        if (!capture) return;
        if (callId && capture.callId && callId !== capture.callId) return;
        window.__synthFrameTap = null;
        capture = null;
        try { window.dispatchEvent(new CustomEvent('synth:call-camera', { detail: { on: false } })); } catch (_) { /* ignore */ }
    }

    function scheduleReconnect() {
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(RECONNECT_MAX_MS, reconnectDelay * 2);
    }

    function connect() {
        // No avatar on this page: nothing to lend.
        if (!document.getElementById('vrm-canvas')) { scheduleReconnect(); return; }
        const url = wsUrl();
        if (!url) return;
        try {
            ws = new WebSocket(url);
        } catch (e) {
            scheduleReconnect();
            return;
        }
        ws.onopen = () => { reconnectDelay = RECONNECT_MIN_MS; };
        ws.onmessage = (ev) => {
            if (typeof ev.data !== 'string') return;
            let msg = null;
            try { msg = JSON.parse(ev.data); } catch (_) { return; }
            if (!msg) return;
            if (msg.type === 'start') startCapture(msg);
            else if (msg.type === 'stop') stopCapture(msg.call_id);
        };
        ws.onclose = () => {
            ws = null;
            stopCapture();
            scheduleReconnect();
        };
        ws.onerror = () => { /* onclose follows */ };
    }

    function ready(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn, { once: true });
        } else {
            fn();
        }
    }

    ready(connect);
})();
