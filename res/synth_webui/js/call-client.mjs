// call-client.mjs — audio/video call client for the SyntH (WebUI).
//
// Thin wrapper over the server-side call service (core/call_manager.py,
// core/webui_call.py). The server owns the call; this module only:
//   * starts / ends the call (POST /api/call/start|end),
//   * streams screen-share frames (getDisplayMedia -> JPEG -> WS /api/call/screen).
//
// The SyntH's "webcam" in a video call is the Karada render already on screen,
// so there is nothing to capture here for it.

const SCREEN_FRAME_INTERVAL_MS = 2000;   // server also throttles (CALL_SCREEN_FRAME_MIN_INTERVAL_SEC)
const SCREEN_MAX_EDGE_PX = 1280;
const SCREEN_JPEG_QUALITY = 0.7;

function _wsBase(apiBase) {
    try {
        if (apiBase) return apiBase.replace(/^http/, 'ws');
        const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
        return `${proto}://${window.location.host}`;
    } catch (_) {
        return '';
    }
}

export function createCallClient({ apiBase = '', getSessionId } = {}) {
    let call = null;            // { call_id, kind, degraded, state }
    let screenStream = null;
    let screenWs = null;
    let screenTimer = null;
    let screenVideo = null;
    let screenCanvas = null;
    const listeners = new Set();

    const _emit = () => { listeners.forEach((fn) => { try { fn(api); } catch (_) { /* ignore */ } }); };

    async function _post(path, body) {
        const resp = await fetch(apiBase + path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const json = await resp.json().catch(() => null);
        if (!resp.ok) throw new Error((json && json.error) || `HTTP ${resp.status}`);
        return json;
    }

    async function start(kind = 'audio', languages = []) {
        if (call) return call;
        const sessionId = getSessionId && getSessionId();
        if (!sessionId) throw new Error('No WebUI session yet');
        call = await _post('/api/call/start', { session_id: sessionId, kind, languages });
        _emit();
        return call;
    }

    async function end() {
        stopScreenShare();
        const current = call;
        call = null;
        _emit();
        if (!current) return;
        try { await _post('/api/call/end', { call_id: current.call_id }); } catch (_) { /* already gone */ }
    }

    function _captureFrame() {
        if (!screenVideo || !screenWs || screenWs.readyState !== WebSocket.OPEN) return;
        const w = screenVideo.videoWidth;
        const h = screenVideo.videoHeight;
        if (!w || !h) return;
        const scale = Math.min(1, SCREEN_MAX_EDGE_PX / Math.max(w, h));
        screenCanvas.width = Math.round(w * scale);
        screenCanvas.height = Math.round(h * scale);
        screenCanvas.getContext('2d').drawImage(screenVideo, 0, 0, screenCanvas.width, screenCanvas.height);
        screenCanvas.toBlob(async (blob) => {
            if (!blob || !screenWs || screenWs.readyState !== WebSocket.OPEN) return;
            try { screenWs.send(await blob.arrayBuffer()); } catch (_) { /* ignore */ }
        }, 'image/jpeg', SCREEN_JPEG_QUALITY);
    }

    async function startScreenShare() {
        if (!call || screenStream) return false;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
            throw new Error('Screen sharing is not supported by this browser');
        }
        screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
        screenVideo = document.createElement('video');
        screenVideo.muted = true;
        screenVideo.srcObject = screenStream;
        await screenVideo.play();
        screenCanvas = document.createElement('canvas');
        screenWs = new WebSocket(`${_wsBase(apiBase)}/api/call/screen?call_id=${encodeURIComponent(call.call_id)}`);
        screenWs.binaryType = 'arraybuffer';
        screenWs.onopen = () => { _captureFrame(); };
        screenWs.onclose = () => { stopScreenShare(); };
        screenTimer = setInterval(_captureFrame, SCREEN_FRAME_INTERVAL_MS);
        // The user can stop sharing from the browser's own UI.
        const track = screenStream.getVideoTracks()[0];
        if (track) track.addEventListener('ended', () => stopScreenShare());
        _emit();
        return true;
    }

    function stopScreenShare() {
        clearInterval(screenTimer); screenTimer = null;
        try { if (screenStream) screenStream.getTracks().forEach((t) => t.stop()); } catch (_) { /* ignore */ }
        screenStream = null;
        try { if (screenWs) { screenWs.onclose = null; screenWs.close(); } } catch (_) { /* ignore */ }
        screenWs = null;
        screenVideo = null;
        screenCanvas = null;
        _emit();
    }

    const api = {
        start,
        end,
        startScreenShare,
        stopScreenShare,
        onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
        get active() { return !!call; },
        get callId() { return call ? call.call_id : null; },
        get kind() { return call ? call.kind : null; },
        get sharingScreen() { return !!screenStream; },
        get warnings() { return (call && call.warnings) || []; },
    };
    return api;
}
