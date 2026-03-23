import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { VRM, VRMLoaderPlugin, VRMUtils } from 'https://cdn.jsdelivr.net/npm/@pixiv/three-vrm@3/lib/three-vrm.module.js';
import { loadMixamoAnimation } from '/js/loadMixamoAnimation.js';
import { mixamoVRMRigMap } from '/js/mixamoVRMRigMap.js';

// Module-scoped variables (initialized when the canvas is available)
let canvas = null;
let renderer = null;
let scene = null;
let camera = null;
let controls = null;
let __synthLookAtTarget = null;
let __synthDefaultLookAtTarget = null;
let __synthTmpAvatarPos = null;
let __synthTmpCamPos = null;
let __synthTmpForward = null;
let __synthTmpQuat = null;
let __synthTmpDesired = null;
let __synthTmpDesired2 = null;
let __synthTmpHeadPos = null;
let __synthTmpDir = null;
let __synthTmpUp = null;

let currentVRM = null;
let currentMixer = null;
let currentModel = null;
let loader = null;
let blobLoader = null;
let __synthKnockAudio = null; // legacy fallback
let __synthKnockSfx = { buffer: null, loading: null };
let __synthLastKnockAt = 0;
let __synthKnockLook = { activeUntil: 0, startedAt: 0, durationMs: 520, maxStrength: 0.32 };

const __synthNeutralGaze = { yawOffsetRad: 0.0, distance: 2.2 };

function showVrmFallback(err) {
    try {
        const parent = (canvas && canvas.parentElement) || document.querySelector('.home-vrm') || document.body;
        if (!parent) return;
        let banner = document.getElementById('vrm-fallback');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'vrm-fallback';
            banner.style.position = 'absolute';
            banner.style.inset = '0';
            banner.style.display = 'flex';
            banner.style.alignItems = 'center';
            banner.style.justifyContent = 'center';
            banner.style.textAlign = 'center';
            banner.style.padding = '1.5rem';
            banner.style.background = 'rgba(10, 10, 16, 0.6)';
            banner.style.color = 'var(--text)';
            banner.style.zIndex = '5';
            parent.style.position = parent.style.position || 'relative';
            parent.appendChild(banner);
        }
        const detail = err && err.message ? err.message : 'WebGL unavailable';
        banner.textContent = `3D view unavailable. ${detail}`;
    } catch (e) { /* ignore */ }
}

function initVRMViewer() {
    canvas = document.getElementById('vrm-canvas');
    if (!canvas) {
        console.warn('[synth_webui] VRM canvas not found; waiting for DOMContentLoaded...');
        document.addEventListener('DOMContentLoaded', initVRMViewer, { once: true });
        return;
    }

    try {
        renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
        renderer.outputEncoding = THREE.sRGBEncoding;
        renderer.setPixelRatio(window.devicePixelRatio);
        scene = new THREE.Scene();
        camera = new THREE.PerspectiveCamera(30, canvas.clientWidth / Math.max(1, canvas.clientHeight), 0.1, 20);
        camera.position.set(0, 1.4, 2.2);
    } catch (err) {
        console.error('[synth_webui] VRM renderer init failed:', err);
        try { window.__synth_vrm_init_failed = true; } catch (e) { /* ignore */ }
        showVrmFallback(err);
        return;
    }

    // LookAt targets (we use an explicit Object3D target instead of the camera object)
    // so we can blend the target position for a subtle “glance”.
    __synthLookAtTarget = new THREE.Object3D();
    __synthDefaultLookAtTarget = new THREE.Object3D();
    scene.add(__synthLookAtTarget);
    scene.add(__synthDefaultLookAtTarget);
    __synthLookAtTarget.position.set(0, 1.45, 1);
    __synthDefaultLookAtTarget.position.set(0, 1.45, 1);

    // Cached temps for render-loop math
    __synthTmpAvatarPos = new THREE.Vector3();
    __synthTmpCamPos = new THREE.Vector3();
    __synthTmpForward = new THREE.Vector3();
    __synthTmpQuat = new THREE.Quaternion();
    __synthTmpDesired = new THREE.Vector3();
    __synthTmpDesired2 = new THREE.Vector3();
    __synthTmpHeadPos = new THREE.Vector3();
    __synthTmpDir = new THREE.Vector3();
    __synthTmpUp = new THREE.Vector3(0, 1, 0);

    // Neutral gaze is avatar-forward (does not track the camera).
    // Knock temporarily blends gaze towards the camera.
    // (value already declared above as const __synthNeutralGaze)

    controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;
    // Notify any deferred init logic that VRM viewer is ready
    document.dispatchEvent(new Event('synth_vrm_initialized'));
}

// Initialize VRM viewer (will wait for DOM if canvas isn't present yet)
initVRMViewer();

// Only run the following initialization once the VRM viewer has been initialized
const runWhenInitialized = (fn) => {
    if (canvas && renderer && scene && camera && controls) {
        fn();
    } else {
        document.addEventListener('synth_vrm_initialized', () => fn(), { once: true });
    }
};

runWhenInitialized(() => {
    // Enable pan (Middle Mouse Button or Shift + Left Click)
    controls.enablePan = true;
    controls.panSpeed = 0.8;
    controls.screenSpacePanning = true; // Pan in screen space (more intuitive)

    // Mouse button configuration (inverted middle/right)
    controls.mouseButtons = {
        LEFT: THREE.MOUSE.ROTATE,      // Left click: rotate
        RIGHT: THREE.MOUSE.PAN,        // Right click: pan
        MIDDLE: THREE.MOUSE.DOLLY      // Middle click: zoom
    };

    // Enable zoom with mouse wheel
    controls.enableZoom = true;
    controls.zoomSpeed = 1.0;
    controls.minDistance = 0.5;  // Minimum zoom distance
    controls.maxDistance = 10;   // Maximum zoom distance

    // Camera change debounce logic: wait 10s after the last change to save camera state
    let cameraStateDebounce = null;
    controls.addEventListener('change', () => {
        const sessionId = getSessionId();
        if (!sessionId) return;
        try {
            const camState = {
                position: camera.position ? [camera.position.x, camera.position.y, camera.position.z] : null,
                rotation: camera.rotation ? [camera.rotation.x, camera.rotation.y, camera.rotation.z] : null,
                fov: camera.fov || null,
                zoom: camera.zoom || null
            };
            if (cameraStateDebounce) clearTimeout(cameraStateDebounce);
            cameraStateDebounce = setTimeout(async () => {
                try {
                    await apiPostJson('/api/chat/session_meta', { session_id: sessionId, meta: { camera: camState } });
                } catch (err) {
                    console.debug('[synth_webui] Failed to save camera state:', err);
                }
            }, 10000);
        } catch (err) { console.debug('[synth_webui] Camera change handler error:', err); }
    });

    controls.target.set(0, 1.2, 0);
    controls.update();

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.2);
    keyLight.position.set(1, 1.2, 1);
    scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight(0xffffff, 0.4);
    fillLight.position.set(-1, 1.2, -1);
    scene.add(fillLight);

    // Add floor/ground plane to always show 3D room
    const floorGeometry = new THREE.PlaneGeometry(10, 10);
    const floorMaterial = new THREE.MeshStandardMaterial({
        color: 0x2a2a2a,
        roughness: 0.8,
        metalness: 0.2
    });
    const floor = new THREE.Mesh(floorGeometry, floorMaterial);
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = 0;
    floor.receiveShadow = true;
    scene.add(floor);
    console.log('[synth_webui] Floor plane added to scene');

    // Add grid helper for better depth perception
    const gridHelper = new THREE.GridHelper(10, 20, 0x444444, 0x333333);
    gridHelper.position.y = 0.001; // Slightly above floor to avoid z-fighting
    scene.add(gridHelper);
    console.log('[synth_webui] Grid helper added to scene');

    loader = new GLTFLoader();
    loader.setCrossOrigin('anonymous');
    loader.setResourcePath('/skins/temp/');
    loader.register((parser) => new VRMLoaderPlugin(parser));
    try { window.loader = loader; } catch (e) { /* ignore */ }
    console.log('[synth_webui] VRM loader configured with resource path: /skins/temp/');

    blobLoader = new GLTFLoader();
    blobLoader.setCrossOrigin('anonymous');
    blobLoader.register((parser) => new VRMLoaderPlugin(parser));
    try { window.blobLoader = blobLoader; } catch (e) { /* ignore */ }
    console.log('[synth_webui] Blob loader configured');
});

// Animation mappings registry (GLOBAL, plugin/interface-extensible).
// Plugins/interfaces can populate this at runtime, including new states
// like GAMING without changing the WebUI code.
// Supported shapes:
//  - window.VRMAnimationMappings[state] = ["/skins/.../file.fbx", ...]
//  - window.VRMAnimationMappings[skinName][state] = ["/skins/.../file.fbx", ...]
window.VRMAnimationMappings = window.VRMAnimationMappings || {};
const animationMappings = window.VRMAnimationMappings;
const animationMappingsLoaded = window.__VRMAnimationMappingsLoaded || new Map(); // per-session cache
window.__VRMAnimationMappingsLoaded = animationMappingsLoaded;

function setStatus(message, level) {
    try {
        if (typeof window.SynthWebUISetStatus === 'function') {
            window.SynthWebUISetStatus(message, level);
        }
    } catch (e) { /* ignore */ }
}

// Animation handler
class AnimationHandler {
    _normalizeFaceKeyForLookup(k) {
        try {
            return String(k || '')
                .replace(/[\._\-\s]+/g, '')
                .toLowerCase();
        } catch (e) {
            return String(k || '').toLowerCase();
        }
    }

    _getFaceKeyLookup() {
        try {
            const caps = window.__synth_vrm_capabilities || null;
            const keys = (caps && Array.isArray(caps.expressionKeys)) ? caps.expressionKeys : null;
            const sig = keys ? `${keys.length}:${String(keys[0] || '')}:${String(keys[keys.length - 1] || '')}` : 'none';
            if (this._faceKeyLookup && this._faceKeyLookupSig === sig) return this._faceKeyLookup;
            const out = {};
            if (keys) {
                keys.forEach((kk) => {
                    try {
                        const s = String(kk);
                        const n = this._normalizeFaceKeyForLookup(s);
                        if (n && !out[n]) out[n] = s;
                    } catch (e) { /* ignore */ }
                });
            }
            this._faceKeyLookup = out;
            this._faceKeyLookupSig = sig;
            return out;
        } catch (e) {
            return this._faceKeyLookup || {};
        }
    }

    _resolveFaceKeys(key) {
        try {
            const raw = String(key || '');
            const norm = this._normalizeFaceKeyForLookup(raw);
            const lookup = this._getFaceKeyLookup();

            const snakeToCamel = (s) => {
                try { return String(s).replace(/[_\-]+([a-zA-Z0-9])/g, (_, c) => String(c).toUpperCase()); } catch (e) { return s; }
            };
            const camelToSnake = (s) => {
                try { return String(s).replace(/([A-Z])/g, '_$1').toLowerCase(); } catch (e) { return s; }
            };

            // Explicit aliases for common VRM expression preset names.
            const alias = {
                // Brows
                browdown: ['browDown', 'brow_down', 'BROW_DOWN'],
                browup: ['browUp', 'brow_up', 'BROW_UP'],
                // Blinks
                eyeblinkleft: ['blinkLeft', 'eyeBlinkLeft', 'eye_blink_left', 'BLINK_L'],
                eyeblinkright: ['blinkRight', 'eyeBlinkRight', 'eye_blink_right', 'BLINK_R'],
                blinkleft: ['blinkLeft', 'eyeBlinkLeft', 'eye_blink_left', 'BLINK_L'],
                blinkright: ['blinkRight', 'eyeBlinkRight', 'eye_blink_right', 'BLINK_R'],
                // Look directions
                eyelookleft: ['lookLeft', 'eyeLookLeft', 'eye_look_left', 'LOOKLEFT'],
                eyelookright: ['lookRight', 'eyeLookRight', 'eye_look_right', 'LOOKRIGHT'],
                eyelookup: ['lookUp', 'eyeLookUp', 'eye_look_up', 'LOOKUP'],
                eyelookdown: ['lookDown', 'eyeLookDown', 'eye_look_down', 'LOOKDOWN'],
                lookleft: ['lookLeft', 'eyeLookLeft', 'eye_look_left', 'LOOKLEFT'],
                lookright: ['lookRight', 'eyeLookRight', 'eye_look_right', 'LOOKRIGHT'],
                lookup: ['lookUp', 'eyeLookUp', 'eye_look_up', 'LOOKUP'],
                lookdown: ['lookDown', 'eyeLookDown', 'eye_look_down', 'LOOKDOWN'],
                // Eyes
                eyesclosed: ['eyesClosed', 'eyes_closed', 'BLINK'],
                eyessmile: ['eyesSmile', 'eyes_smile', 'JOY', 'happy'],
                eyeswide: ['eyesWide', 'eyes_wide', 'surprised'],
                // Mouth
                mouthopen: ['mouthOpen', 'mouth_open', 'A'],
                mouthsmile: ['mouthSmile', 'mouth_smile', 'JOY', 'happy'],
                mouthfrown: ['mouthFrown', 'mouth_frown', 'SORROW', 'sad'],
                moutho: ['mouthO', 'mouth_O', 'oh', 'O'],
                // Emotions (as granular morphs)
                sad: ['SORROW', 'sad'],
                happy: ['JOY', 'happy'],
                surprised: ['surprised'],
                relaxed: ['relaxed', 'FUN'],
                angry: ['ANGRY', 'angry'],
            };

            const candidates = [];
            const push = (v) => {
                const s = String(v || '');
                if (!s) return;
                if (!candidates.includes(s)) candidates.push(s);
            };

            push(raw);
            push(raw.replace(/\./g, '_'));
            push(raw.toLowerCase());
            push(camelToSnake(raw));
            push(snakeToCamel(raw));

            if (alias[norm]) alias[norm].forEach(push);

            // Map candidates through capabilities lookup (normalized match).
            const resolved = [];
            const pushResolved = (v) => { if (v && !resolved.includes(v)) resolved.push(v); };
            candidates.forEach((c) => {
                pushResolved(c);
                const mapped = lookup[this._normalizeFaceKeyForLookup(c)] || null;
                if (mapped) pushResolved(mapped);
            });

            return resolved.length ? resolved : candidates;
        } catch (e) {
            return [String(key || '')];
        }
    }

    _flushFaceNow() {
        // Apply expression weights to the mesh immediately.
        // This is important when WEB_DEBUG pause disables VRM.update().
        try {
            if (!this.vrm) return;
            // VRM0
            if (this.vrm.blendShapeProxy && typeof this.vrm.blendShapeProxy.update === 'function') {
                this.vrm.blendShapeProxy.update();
            }
            // VRM1
            const em = this.vrm.expressionManager || (this.vrm.userData && this.vrm.userData.vrmExpressionManager) || null;
            if (em && typeof em.update === 'function') {
                em.update();
            }
        } catch (e) { /* ignore */ }
    }

    _getFaceController() {
        try {
            if (!this.vrm) return null;
            if (this.vrm.blendShapeProxy && typeof this.vrm.blendShapeProxy.setValue === 'function') {
                return {
                    kind: 'vrm0',
                    setValue: (k, v) => this.vrm.blendShapeProxy.setValue(k, v),
                };
            }
            const em = this.vrm.expressionManager || (this.vrm.userData && this.vrm.userData.vrmExpressionManager) || null;
            if (em && typeof em.setValue === 'function') {
                return {
                    kind: 'vrm1',
                    setValue: (k, v) => em.setValue(k, v),
                };
            }
            return null;
        } catch (e) {
            return null;
        }
    }

    _setFaceValue(key, value) {
        try {
            const ctrl = this._getFaceController();
            console.debug('[AnimationHandler] _setFaceValue called', key, value, 'controller:', ctrl && ctrl.kind);
            if (!ctrl) return false;

            // Resolve the input key to one or more concrete VRM keys.
            const variants = this._resolveFaceKeys(key);

            let setAny = false;
            for (const kk of variants) {
                try {
                    ctrl.setValue(kk, value);
                    setAny = true;
                    // Cache last set value for the concrete key used
                    try {
                        if (!this._faceValueCache) this._faceValueCache = {};
                        this._faceValueCache[kk] = value;
                        // Also cache under the requested key so UI can read it back.
                        const req = String(key || '');
                        if (req) this._faceValueCache[req] = value;
                    } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            }

            return !!setAny;
        } catch (e) {
            return false;
        }
    }

    _getFaceValue(key) {
        try {
            if (!key) return 0;
            const k0 = String(key);
            if (this._faceValueCache && this._faceValueCache[k0] !== undefined) {
                const v = this._faceValueCache[k0];
                return (typeof v === 'number') ? v : 0;
            }
            // Try resolved keys (snake/camel/aliases)
            const variants = this._resolveFaceKeys(k0);
            for (const kk of variants) {
                if (this._faceValueCache && this._faceValueCache[kk] !== undefined) {
                    const v = this._faceValueCache[kk];
                    return (typeof v === 'number') ? v : 0;
                }
            }
            return 0;
        } catch (e) {
            return 0;
        }
    }

    // -----------------------------
    // WEB_DEBUG: manual overrides (session-local)
    // -----------------------------
    setDebugFaceOverride(key, value) {
        try {
            const k = (key !== undefined && key !== null) ? String(key) : '';
            console.debug('[AnimationHandler] setDebugFaceOverride called', k, value);
            if (!k) return;
            if (!this._debugFaceOverrides || typeof this._debugFaceOverrides !== 'object') this._debugFaceOverrides = {};
            if (value === null || value === undefined || value === '') {
                delete this._debugFaceOverrides[k];
                // Best-effort immediate apply (useful during pause).
                try {
                    if (!this._debugFaceDirty || typeof this._debugFaceDirty !== 'object') this._debugFaceDirty = {};
                    this._debugFaceDirty[k] = null;
                    if (!this._debugFaceApplyRaf) {
                        this._debugFaceApplyRaf = requestAnimationFrame(() => {
                            try {
                                const dirty = this._debugFaceDirty || {};
                                console.debug('[AnimationHandler] applying debug face dirty keys', dirty);
                                this._debugFaceDirty = {};
                                this._debugFaceApplyRaf = 0;
                                Object.keys(dirty).forEach((kk) => {
                                    try {
                                        const vv = dirty[kk];
                                        // Resolve persona mapping if available
                                        // Resolve effective persona with fallback to Rei
                                        const effectivePersona = this._getEffectivePersona();
                                        const blendMap = effectivePersona.blendshape_map;
                                        const nkey = String(kk || '');

                                        if (vv === null || vv === undefined) {
                                            // Attempt to clear mapped keys if any
                                            const flat = (blendMap && typeof blendMap[nkey] === 'string') ? blendMap[nkey] : null;
                                            if (flat) {
                                                try { if (this._expressionState) delete this._expressionState[flat]; } catch (e) { }
                                                this._setFaceValue(flat, 0);
                                            } else {
                                                try { if (this._expressionState) delete this._expressionState[nkey]; } catch (e) { }
                                                this._setFaceValue(nkey, 0);
                                                try { this._setFaceValue(nkey.replace(/\./g, '_'), 0); } catch (e) { }
                                            }
                                        } else {
                                            const val = Math.max(0, Math.min(1, Number(vv) || 0));
                                            // Flat mapping first
                                            const flat = (blendMap && typeof blendMap[nkey] === 'string') ? blendMap[nkey] : ((blendMap && typeof blendMap[nkey.replace(/\./g, '_')] === 'string') ? blendMap[nkey.replace(/\./g, '_')] : null);
                                            if (flat) {
                                                try { this._expressionState = this._expressionState || {}; this._expressionState[flat] = val; } catch (e) { }
                                                this._setFaceValue(flat, val);
                                            } else {
                                                // Try grouped mappings
                                                let applied = false;
                                                try {
                                                    const groups = ['emotions', 'visemes', 'expressions'];
                                                    for (let g of groups) {
                                                        const entry = (blendMap[g] && (blendMap[g][nkey] || blendMap[g][nkey.replace(/\./g, '_')])) ? (blendMap[g][nkey] || blendMap[g][nkey.replace(/\./g, '_')]) : null;
                                                        if (entry && entry.targets && typeof entry.targets === 'object') {
                                                            Object.keys(entry.targets).forEach(bs => {
                                                                try {
                                                                    const v2 = (entry.targets[bs] || 0) * val;
                                                                    this._expressionState = this._expressionState || {}; this._expressionState[bs] = v2;
                                                                    this._setFaceValue(bs, v2);
                                                                } catch (e) { /* ignore */ }
                                                            });
                                                            applied = true; break;
                                                        }
                                                    }
                                                } catch (e) { /* ignore */ }
                                                if (!applied) {
                                                    try { this._expressionState = this._expressionState || {}; this._expressionState[nkey] = val; } catch (e) { }
                                                    this._setFaceValue(nkey, val);
                                                }
                                            }
                                        }
                                    } catch (e) { /* ignore */ }
                                });
                                try { this._flushFaceNow(); } catch (e) { /* ignore */ }
                            } catch (e) { /* ignore */ }
                        });
                    }
                } catch (e) { /* ignore */ }
                return;
            }
            const v = Math.max(0, Math.min(1, Number(value) || 0));
            this._debugFaceOverrides[k] = v;

            // Throttle immediate face application to avoid stutter while dragging sliders.
            try {
                if (!this._debugFaceDirty || typeof this._debugFaceDirty !== 'object') this._debugFaceDirty = {};
                this._debugFaceDirty[k] = v;
                if (!this._debugFaceApplyRaf) {
                    this._debugFaceApplyRaf = requestAnimationFrame(() => {
                        try {
                            const dirty = this._debugFaceDirty || {};
                            this._debugFaceDirty = {};
                            this._debugFaceApplyRaf = 0;
                            Object.keys(dirty).forEach((kk) => {
                                try {
                                    const vv = dirty[kk];
                                    // persona mapping helper
                                    // Resolve effective persona with fallback to Rei
                                    const effectivePersona = this._getEffectivePersona();
                                    const blendMap = effectivePersona.blendshape_map;
                                    const nkey = String(kk || '');

                                    if (vv === null || vv === undefined) {
                                        const flat = (blendMap && typeof blendMap[nkey] === 'string') ? blendMap[nkey] : null;
                                        if (flat) {
                                            try { if (this._expressionState) delete this._expressionState[flat]; } catch (e) { }
                                            this._setFaceValue(flat, 0);
                                        } else {
                                            try { if (this._expressionState) delete this._expressionState[nkey]; } catch (e) { }
                                            this._setFaceValue(nkey, 0);
                                            try { this._setFaceValue(nkey.replace(/\./g, '_'), 0); } catch (e) { }
                                        }
                                    } else {
                                        const val = Math.max(0, Math.min(1, Number(vv) || 0));
                                        const flat = (blendMap && typeof blendMap[nkey] === 'string') ? blendMap[nkey] : ((blendMap && typeof blendMap[nkey.replace(/\./g, '_')] === 'string') ? blendMap[nkey.replace(/\./g, '_')] : null);
                                        if (flat) {
                                            try { this._expressionState = this._expressionState || {}; this._expressionState[flat] = val; } catch (e) { }
                                            this._setFaceValue(flat, val);
                                        } else {
                                            let applied = false;
                                            try {
                                                const groups = ['emotions', 'visemes', 'expressions'];
                                                for (let g of groups) {
                                                    const entry = (blendMap[g] && (blendMap[g][nkey] || blendMap[g][nkey.replace(/\./g, '_')])) ? (blendMap[g][nkey] || blendMap[g][nkey.replace(/\./g, '_')]) : null;
                                                    if (entry && entry.targets && typeof entry.targets === 'object') {
                                                        Object.keys(entry.targets).forEach(bs => {
                                                            try {
                                                                const v2 = (entry.targets[bs] || 0) * val;
                                                                this._expressionState = this._expressionState || {}; this._expressionState[bs] = v2;
                                                                this._setFaceValue(bs, v2);
                                                            } catch (e) { /* ignore */ }
                                                        });
                                                        applied = true; break;
                                                    }
                                                }
                                            } catch (e) { /* ignore */ }
                                            if (!applied) {
                                                try { this._expressionState = this._expressionState || {}; this._expressionState[nkey] = val; } catch (e) { }
                                                this._setFaceValue(nkey, val);
                                            }
                                        }
                                    }
                                } catch (e) { /* ignore */ }
                            });
                            try { this._flushFaceNow(); } catch (e) { /* ignore */ }
                        } catch (e) { /* ignore */ }
                    });
                }
            } catch (e) { /* ignore */ }
        } catch (e) { /* ignore */ }
    }

    clearDebugFaceOverrides() {
        try {
            const prev = (this._debugFaceOverrides && typeof this._debugFaceOverrides === 'object') ? this._debugFaceOverrides : {};
            try { console.debug('[AnimationHandler] clearDebugFaceOverrides called, prev keys:', Object.keys(prev || {})); } catch (e) { /* ignore */ }
            this._debugFaceOverrides = {};
            try { this._debugFaceDirty = {}; } catch (e) { /* ignore */ }
            try { if (this._debugFaceApplyRaf) cancelAnimationFrame(this._debugFaceApplyRaf); } catch (e) { /* ignore */ }
            try { this._debugFaceApplyRaf = 0; } catch (e) { /* ignore */ }

            // Immediately reset any keys that were forced by debug overrides.
            try {
                Object.keys(prev).forEach((k) => {
                    if (!k) return;
                    try { if (this._expressionState) delete this._expressionState[String(k)]; } catch (e) { /* ignore */ }
                    try { this._setFaceValue(String(k), 0); } catch (e) { /* ignore */ }
                    try { this._setFaceValue(String(k).replace(/\./g, '_'), 0); } catch (e) { /* ignore */ }
                });
            } catch (e) { /* ignore */ }
            try { this._flushFaceNow(); } catch (e) { /* ignore */ }
            try { console.debug('[AnimationHandler] clearDebugFaceOverrides done'); } catch (e) { /* ignore */ }
        } catch (e) { /* ignore */ }
    }

    getDebugFaceOverrides() {
        try {
            return (this._debugFaceOverrides && typeof this._debugFaceOverrides === 'object') ? this._debugFaceOverrides : {};
        } catch (e) {
            return {};
        }
    }

    setDebugEmotionOverride(name, intensity) {
        try {
            const k = (name !== undefined && name !== null) ? String(name) : '';
            if (!k) return;
            if (!this._debugEmotionOverrides || typeof this._debugEmotionOverrides !== 'object') this._debugEmotionOverrides = {};
            if (intensity === null || intensity === undefined || intensity === '') {
                delete this._debugEmotionOverrides[k];
                // Apply immediately (useful during pause or before any animation_state arrives).
                try {
                    if (!this._debugEmotionApplyRaf) {
                        this._debugEmotionApplyRaf = requestAnimationFrame(() => {
                            try {
                                this._debugEmotionApplyRaf = 0;
                                const base = this._lastAnimationState || {};
                                const st = Object.assign({ expressions: [] }, base);
                                if (!st.timing) st.timing = {};
                                if (!st.timing.started_at) st.timing.started_at = new Date().toISOString();
                                if (!st.clip) st.clip = { fps: 30 };
                                // Use a large dt for the one-shot apply so debug overrides feel instant 
                                // if the main loop is not already running.
                                try { this.applyExpressionsForFrame(st, 0.5); } catch (e) { /* ignore */ }
                                try { this._flushFaceNow(); } catch (e) { /* ignore */ }
                            } catch (e) { /* ignore */ }
                        });
                    }
                } catch (e) { /* ignore */ }
                return;
            }
            const v = Math.max(0, Math.min(1, Number(intensity) || 0));
            this._debugEmotionOverrides[k] = v;

            // Start expression ticking loop if not already running (ensures smooth transition and updates)
            if (!this._expressionsTicking) {
                try { this.applyAnimationState(this._lastAnimationState || { action: 'idle' }); } catch (e) { /* ignore */ }
            }

            // Apply immediately (useful during pause or before any animation_state arrives).
            try {
                if (!this._debugEmotionApplyRaf) {
                    this._debugEmotionApplyRaf = requestAnimationFrame(() => {
                        try {
                            this._debugEmotionApplyRaf = 0;
                            const base = this._lastAnimationState || {};
                            const st = Object.assign({ expressions: [] }, base);
                            if (!st.timing) st.timing = {};
                            if (!st.timing.started_at) st.timing.started_at = new Date().toISOString();
                            if (!st.clip) st.clip = { fps: 30 };
                            // Use a large dt for the one-shot apply so debug overrides feel instant
                            try { this.applyExpressionsForFrame(st, 0.5); } catch (e) { /* ignore */ }
                            try { this._flushFaceNow(); } catch (e) { /* ignore */ }
                        } catch (e) { /* ignore */ }
                    });
                }
            } catch (e) { /* ignore */ }
        } catch (e) { /* ignore */ }
    }

    clearDebugEmotionOverrides() {
        try {
            this._debugEmotionOverrides = {};
            // Apply immediately so previously-set face values don't linger.
            try {
                if (!this._debugEmotionApplyRaf) {
                    this._debugEmotionApplyRaf = requestAnimationFrame(() => {
                        try {
                            this._debugEmotionApplyRaf = 0;
                            const base = this._lastAnimationState || {};
                            const st = Object.assign({ expressions: [] }, base);
                            if (!st.timing) st.timing = {};
                            if (!st.timing.started_at) st.timing.started_at = new Date().toISOString();
                            if (!st.clip) st.clip = { fps: 30 };
                            try { this.applyExpressionsForFrame(st, 0.25); } catch (e) { /* ignore */ }
                            try { this._flushFaceNow(); } catch (e) { /* ignore */ }
                        } catch (e) { /* ignore */ }
                    });
                }
            } catch (e) { /* ignore */ }
        } catch (e) { /* ignore */ }
    }

    getDebugEmotionOverrides() {
        try {
            return (this._debugEmotionOverrides && typeof this._debugEmotionOverrides === 'object') ? this._debugEmotionOverrides : {};
        } catch (e) {
            return {};
        }
    }

    // Apply a rich animation state payload received from server
    applyAnimationState(state) {
        try {
            console.debug('[AnimationHandler] applyAnimationState START', state);
            // detect previous action so we can react to transitions (e.g., think -> write)
            const prevAction = (this._lastAnimationState && this._lastAnimationState.action) ? String(this._lastAnimationState.action).toLowerCase() : null;
            // store for reference
            this._lastAnimationState = state;
            this.currentActionName = state.action || this.currentActionName;
            this.currentActionPhase = state.phase || this.currentActionPhase;

            // Keep emotions/feelings snapshot for downstream consumers
            try { this._lastEmotions = (state && state.emotions) ? state.emotions : null; } catch (e) { this._lastEmotions = null; }
            try { this._lastFeelings = (state && state.feelings) ? state.feelings : null; } catch (e) { this._lastFeelings = null; }

            // Emotion overlay: after an action starts, apply the dominant emotion face for
            // a random duration that scales with intensity. This is action-agnostic because
            // plugins may override writing/phase semantics.
            try {
                const newAction = (state && state.action) ? String(state.action).toLowerCase() : null;
                let startedAtMs = NaN;
                try {
                    if (state && state.timing && state.timing.started_at) {
                        const t = Date.parse(state.timing.started_at);
                        if (Number.isFinite(t)) startedAtMs = t;
                    }
                } catch (e) { /* ignore */ }
                if (!Number.isFinite(startedAtMs)) startedAtMs = Date.now();

                const isNewStart = (!this._lastActionStartMs) || (Math.abs(startedAtMs - this._lastActionStartMs) > 10) || (prevAction !== newAction);
                if (isNewStart) {
                    this._lastActionStartMs = startedAtMs;
                    // Clear previous overlay on action change
                    this._emotionOverlay = null;

                    // On action change, perform a smooth eyes reset so that
                    // persistent eye-closed flags are removed in a non-abrupt way
                    try {
                        if (typeof this._resetEyesSmoothly === 'function') {
                            this._resetEyesSmoothly(220);
                        }
                    } catch (e) { /* ignore */ }

                    const emo = (state && state.emotions && state.emotions.values && typeof state.emotions.values === 'object') ? state.emotions.values : null;
                    if (emo) {
                        // pick max intensity; tie -> random
                        let maxVal = -Infinity;
                        Object.keys(emo).forEach(k => {
                            const v = Number(emo[k]);
                            if (Number.isFinite(v)) maxVal = Math.max(maxVal, v);
                        });
                        if (Number.isFinite(maxVal) && maxVal > 0) {
                            const candidates = Object.keys(emo).filter(k => {
                                const v = Number(emo[k]);
                                return Number.isFinite(v) && v === maxVal;
                            });
                            if (candidates.length > 0) {
                                const chosen = candidates[Math.floor(Math.random() * candidates.length)];

                                // normalize intensity to 0..1 (support 0..1 or 0..10-ish)
                                let norm = maxVal;
                                if (norm > 1) norm = norm / 10.0;
                                norm = Math.max(0, Math.min(1, norm));

                                const delayS = 0.2 + Math.random() * 0.9; // X seconds after action start
                                const baseMin = 0.8;
                                const baseMax = 3.5;
                                const durS = (baseMin + (baseMax - baseMin) * norm) * (0.8 + Math.random() * 0.45);

                                this._emotionOverlay = {
                                    action: newAction || 'unknown',
                                    emotion: String(chosen),
                                    intensity: norm,
                                    startsAtMs: startedAtMs + Math.round(delayS * 1000),
                                    endsAtMs: startedAtMs + Math.round((delayS + durS) * 1000),
                                    priority: 25,
                                };
                            }
                        }
                    }
                }
            } catch (e) { /* ignore */ }

            // store lipsync flag and emit event if changed
            const prev = this._lipsyncEnabled;
            this._lipsyncEnabled = !!state.lipsync;
            try { window.dispatchEvent(new CustomEvent('synth_animation_state_updated', { detail: state })); } catch (e) { }
            if (prev !== this._lipsyncEnabled) {
                try { window.dispatchEvent(new CustomEvent('synth_animation_lipsync_changed', { detail: { lipsync: this._lipsyncEnabled } })); } catch (e) { }
            }
            // manage blink/eye managers according to lipsync flag and persona defaults
            try {
                this._loadPersonaForSkin(window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei')
                    .then(persona => {
                        console.debug('[AnimationHandler] persona loaded', persona && persona.name ? persona.name : '(unknown)');
                        const effectivePersona = this._getEffectivePersona();
                        const pdefaults = (effectivePersona && effectivePersona.defaults) ? effectivePersona.defaults : {};

                        // Expose per-persona emotion presets and derive emotions list from mapping
                        try {
                            // New canonical format: effectivePersona.emotions is an object mapping
                            if (effectivePersona && effectivePersona.emotions && typeof effectivePersona.emotions === 'object' && !Array.isArray(effectivePersona.emotions)) {
                                window.__synth_emotion_face_presets = effectivePersona.emotions;
                                try { window.__synth_persona_emotions_list = Object.keys(effectivePersona.emotions); } catch (e) { window.__synth_persona_emotions_list = null; }
                            }
                        } catch (e) { /* ignore */ }

                        // Apply persona per-animation overrides on the client (hybrid design).
                        try {
                            const overrides = (effectivePersona && effectivePersona.animation_overrides && typeof effectivePersona.animation_overrides === 'object') ? effectivePersona.animation_overrides : null;
                            if (overrides) {
                                const actionKey = (state && state.action) ? String(state.action).toLowerCase() : '';
                                let animStem = '';
                                try {
                                    const a = state && state.animation ? String(state.animation) : '';
                                    animStem = (a.split('/').pop() || '').replace(/\.[^/.]+$/, '').toLowerCase();
                                } catch (e) { animStem = ''; }

                                let matched = null;
                                for (const k of Object.keys(overrides)) {
                                    const kk = String(k).toLowerCase();
                                    if ((actionKey && kk === actionKey) || (animStem && kk === animStem)) {
                                        matched = overrides[k];
                                        break;
                                    }
                                }

                                if (matched && typeof matched === 'object') {
                                    if (!Array.isArray(state.expressions)) state.expressions = [];

                                    if (matched.blendshape_presets && typeof matched.blendshape_presets === 'object') {
                                        state.expressions.push({
                                            start_frame: 0,
                                            end_frame: 1000000000,
                                            targets: matched.blendshape_presets,
                                            priority: (matched.priority !== undefined) ? matched.priority : 90,
                                            source: 'persona_override'
                                        });
                                    }
                                    if (Array.isArray(matched.expressions)) {
                                        matched.expressions.forEach((ex) => {
                                            const e = Object.assign({}, ex || {});
                                            if (e.start_frame === undefined) e.start_frame = 0;
                                            if (e.end_frame === undefined) e.end_frame = 1000000000;
                                            if (!e.source) e.source = 'persona_override';
                                            if (e.priority === undefined) e.priority = 90;
                                            state.expressions.push(e);
                                        });
                                    }
                                    this._lastAnimationState = state;
                                }
                            }
                        } catch (e) { /* ignore */ }

                        // Defaults tuned for human-like blink timing: ~15–20 blinks/min => ~3–4s interval.
                        const blinkCfg = state.blink || pdefaults.blink || { auto: true, rate_s: 3.5, intensity: 0.6, close_ms: 60, hold_ms: 100, open_ms: 60 };
                        const eyeCfg = state.eye_movement || pdefaults.eye_movement || { auto: true, saccade_rate_s: 2 };

                        this._blinkAutoEnabled = (!!blinkCfg.auto) && !this._lipsyncEnabled;
                        this._blinkRateS = blinkCfg.rate_s || 3.5;
                        this._blinkIntensity = blinkCfg.intensity || 0.6;
                        this._blinkCloseMs = (typeof blinkCfg.close_ms === 'number') ? blinkCfg.close_ms : 60;
                        this._blinkHoldMs = (typeof blinkCfg.hold_ms === 'number') ? blinkCfg.hold_ms : 120;
                        this._blinkOpenMs = (typeof blinkCfg.open_ms === 'number') ? blinkCfg.open_ms : 60;

                        // Special-case tweaks: during 'think' we prefer faster, shallower blinks
                        try {
                            const actionKey = (state && state.action) ? String(state.action).toLowerCase() : '';
                            if (actionKey === 'think') {
                                // Keep frequency within ~15–20/min; only adjust shape/timing.
                                this._blinkRateS = (blinkCfg.rate_s && blinkCfg.rate_s > 0) ? Math.max(3.0, blinkCfg.rate_s) : 3.5;
                                this._blinkCloseMs = Math.max(30, Math.round((blinkCfg.close_ms || 60) * 0.6));
                                this._blinkHoldMs = Math.max(60, Math.round((blinkCfg.hold_ms || 100) * 0.7));
                                this._blinkOpenMs = Math.max(30, Math.round((blinkCfg.open_ms || 60) * 0.6));
                                this._blinkIntensity = Math.max(0.35, Math.min(0.85, (blinkCfg.intensity || 0.6) * 0.75));
                            }
                        } catch (e) { /* ignore */ }

                        this._eyeAutoEnabled = (!!eyeCfg.auto) && !this._lipsyncEnabled;
                        this._saccadeRateS = eyeCfg.saccade_rate_s || 2;

                        if (this._blinkAutoEnabled) this._startBlinkLoop(); else this._stopBlinkLoop();
                        if (this._eyeAutoEnabled) this._startEyeMovement(); else this._stopEyeMovement();
                        // If we transitioned out of 'think' and persona overrides applied
                        // after the async persona load, ensure eyes are open now to avoid
                        // persona_override re-closing them after a force-open earlier.
                        try {
                            if (prevAction === 'think' && newAction !== 'think') {
                                console.debug('[AnimationHandler] Transitioned out of think after persona overrides; forcing eyes open');
                                try { this._forceOpenEyes(); } catch (e) { /* ignore */ }
                            }
                        } catch (e) { /* ignore */ }
                    })
                    .catch(() => { });
            } catch (e) { }

            // If we transitioned out of 'think', ensure eyes are open immediately
            try {
                const newAction = (state && state.action) ? String(state.action).toLowerCase() : null;
                if (prevAction === 'think' && newAction !== 'think') {
                    try { this._forceOpenEyes(); } catch (e) { }
                }
            } catch (e) { }
            // attempt to ensure persona mapping for current skin is loaded
            try {
                const skin = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
                if (!this._personaCache) this._personaCache = {};
                if (!this._personaCache[skin]) this._loadPersonaForSkin(skin).catch(() => { });
                // Always pre-load Rei persona as a fallback source if we are on a different skin
                if (skin !== 'Rei' && !this._personaCache['Rei']) {
                    this._loadPersonaForSkin('Rei').catch(() => { });
                }
            } catch (e) { }

            // start ticking expressions if needed
            if (!this._expressionsTicking) {
                this._lastTickTime = performance.now();
                this._expressionsTicking = true;
                const tick = (now) => {
                    try {
                        const paused = !!(window.__synth_web_debug_enabled && window.__synth_debug_pause_all);
                        const hasDebugEmotionOverrides = !!(this._debugEmotionOverrides && typeof this._debugEmotionOverrides === 'object' && Object.keys(this._debugEmotionOverrides).length);
                        const hasDebugFaceOverrides = !!(this._debugFaceOverrides && typeof this._debugFaceOverrides === 'object' && Object.keys(this._debugFaceOverrides).length);
                        const hasDebugLoopOverride = !!(this._debugOverride && this._debugOverride.action);
                        const allowOverridesWhilePaused = hasDebugLoopOverride || hasDebugEmotionOverrides || hasDebugFaceOverrides;

                        if (paused && !allowOverridesWhilePaused) {
                            // Freeze expression ticking during pause (standard animations only).
                            this._lastTickTime = now;
                        } else {
                            const dt = Math.min(0.1, (now - (this._lastTickTime || now)) / 1000);
                            this._lastTickTime = now;
                            try { this.applyExpressionsForFrame(this._lastAnimationState, dt); } catch (e) { }
                        }
                    } catch (e) { }
                    if (this._expressionsTicking) requestAnimationFrame(tick);
                };
                requestAnimationFrame(tick);
            }
            console.debug('[AnimationHandler] applyAnimationState END');
        } catch (e) {
            console.warn('[AnimationHandler] applyAnimationState failed', e);
        }
    }

    // allow external code to push/remove expression sources
    addExpressionSource(source) {
        if (!this._expressionSources) this._expressionSources = [];
        this._expressionSources.push(source);
    }
    removeExpressionSourcesByTag(tag) {
        if (!this._expressionSources) return;
        this._expressionSources = this._expressionSources.filter(
            s => s && s.source !== tag
        );
    }
    clearExpressionSources() {
        this._expressionSources = [];
    }

    // Placeholder: compute expressions for current frame and apply via blendShapeProxy
    // Apply expressions for the current frame with smoothing
    applyExpressionsForFrame(state, dt = 0.033) {
        try {
            // Throttle debug logging to avoid flooding the console during animation ticks.
            try {
                if (!this._lastExpressionLogTime) this._lastExpressionLogTime = 0;
                const nowTs = Date.now();
                const exprCount = (state && Array.isArray(state.expressions)) ? state.expressions.length : 0;
                // Only log if there are expressions active or if >2s have passed since last log
                if (exprCount > 0 || (nowTs - this._lastExpressionLogTime) > 2000) {
                    this._lastExpressionLogTime = nowTs;
                    console.debug('[AnimationHandler] applyExpressionsForFrame START', { dt: dt, expressions: exprCount });
                }
            } catch (e) { /* ignore logging errors */ }
            if (!state) return;
            if (!this.vrm) return;
            if (!this._getFaceController()) return;

            // Ensure expression state bookkeeping
            if (!this._expressionState) this._expressionState = {};

            // Resolve effective persona with fallback to Rei
            const effectivePersona = this._getEffectivePersona();
            const blendMap = effectivePersona.blendshape_map;
            const emotions = effectivePersona.emotions;
            const emotionSpeed = effectivePersona.emotion_speed;

            // Determine current time_in_clip (seconds) or frame.
            // The server typically sends started_at + fps; the client derives a moving frame/time.
            const fps = (state.clip && typeof state.clip.fps === 'number' && state.clip.fps > 0) ? state.clip.fps : 30;
            let timeInClip = (state.timing && typeof state.timing.time_in_clip === 'number') ? state.timing.time_in_clip : null;
            let currentFrame = (state.timing && typeof state.timing.current_frame === 'number') ? state.timing.current_frame : null;

            try {
                const startedAt = (state.timing && state.timing.started_at) ? Date.parse(state.timing.started_at) : NaN;
                if (Number.isFinite(startedAt)) {
                    const elapsed = Math.max(0, (Date.now() - startedAt) / 1000);
                    timeInClip = elapsed;

                    const desc = (state.descriptor && typeof state.descriptor === 'object') ? state.descriptor : null;
                    const phase = (state.phase && typeof state.phase === 'string') ? state.phase : 'loop';
                    const sectionKey = (phase === 'clip') ? 'loop' : phase;
                    const section = (desc && desc[sectionKey] && typeof desc[sectionKey] === 'object') ? desc[sectionKey] : null;
                    if (section && typeof section.start_frame === 'number' && typeof section.end_frame === 'number') {
                        const start = section.start_frame;
                        const end = section.end_frame;
                        // Descriptors use inclusive frame indices.
                        const span = Math.max(1, (end - start) + 1);
                        const tFrames = Math.floor(elapsed * fps);
                        if (phase === 'loop') {
                            currentFrame = start + (tFrames % span);
                        } else {
                            currentFrame = start + Math.min(span - 1, tFrames);
                        }
                    } else {
                        currentFrame = Math.floor(elapsed * fps);
                    }
                }
            } catch (e) { /* ignore */ }

            // Evaluate each expression and update target blendshape values
            const desired = {};
            const evaluateFrame = (expr) => {
                try {
                    // Expressions without explicit bounds are treated as always-on.
                    // Allow the descriptor to define only start_frame (meaning from that frame to the end)
                    if (expr.start_frame === undefined && expr.end_frame === undefined) return true;
                    // Only a start_frame: treat as [start_frame .. +inf]
                    if (expr.start_frame !== undefined && expr.end_frame === undefined) {
                        if (currentFrame !== null && currentFrame !== undefined) {
                            return currentFrame >= expr.start_frame;
                        }
                        // Fall back to time_in_clip using clip fps if provided
                        if (timeInClip !== null && state.clip && state.clip.fps) {
                            const sf = expr.start_frame / state.clip.fps;
                            return (sf <= timeInClip);
                        }
                        return true;
                    }
                    // Only an end_frame: treat as [-inf .. end_frame]
                    if (expr.start_frame === undefined && expr.end_frame !== undefined) {
                        if (currentFrame !== null && currentFrame !== undefined) {
                            return currentFrame <= expr.end_frame;
                        }
                        if (timeInClip !== null && state.clip && state.clip.fps) {
                            const ef = expr.end_frame / state.clip.fps;
                            return (timeInClip <= ef);
                        }
                        return true;
                    }
                    // Prefer frame indices if both bounds are present
                    if (currentFrame !== null && currentFrame !== undefined) {
                        if (expr.start_frame <= currentFrame && currentFrame <= expr.end_frame) return true;
                        return false;
                    }
                    // Fall back to time_in_clip using clip fps if provided
                    if (timeInClip !== null && expr.start_frame !== undefined && expr.end_frame !== undefined && state.clip && state.clip.fps) {
                        const sf = expr.start_frame / state.clip.fps;
                        const ef = expr.end_frame / state.clip.fps;
                        return (sf <= timeInClip && timeInClip <= ef);
                    }
                    return false;
                } catch (e) { return false; }
            };

            // Respect expression priority: higher priority wins on conflicts
            const baseExpressions = Array.isArray(state.expressions) ? state.expressions : [];
            const exprs = (baseExpressions || []).slice().map(e => Object.assign({ priority: 0 }, e));
            // include any externally pushed facial_expression sources (priority 25)
            if (Array.isArray(this._expressionSources) && this._expressionSources.length) {
                exprs.push(...this._expressionSources.map(e => Object.assign({priority:0}, e)));
            }

            // Emotion/Feeling state injection: treat state.emotions/state.feelings as synthetic expressions keyed by name.
            // The client will map these names to blendshape targets using the per-persona
            // `persona.emotions` mapping exposed by the backend (no legacy fallback required).
            try {
                const collectValues = (srcObj) => {
                    if (!srcObj) return null;
                    const valuesObj = (srcObj.values && typeof srcObj.values === 'object') ? srcObj.values
                        : ((typeof srcObj === 'object') ? srcObj : null);
                    const t = {};
                    if (Array.isArray(valuesObj)) {
                        valuesObj.forEach((it) => {
                            try {
                                const name = it && (it.type || it.name) ? String(it.type || it.name) : '';
                                if (!name) return;
                                const raw = Number(it.intensity !== undefined ? it.intensity : it.value);
                                if (!Number.isFinite(raw)) return;
                                const v01 = (raw > 1) ? (raw / 10.0) : raw;
                                t[name] = Math.max(t[name] || 0, Math.max(0, Math.min(1, v01)));
                            } catch (e) { /* ignore */ }
                        });
                    } else if (valuesObj && typeof valuesObj === 'object') {
                        Object.keys(valuesObj).forEach((k) => {
                            if (!k || /^\d+$/.test(String(k))) return;
                            const raw = Number(valuesObj[k]);
                            if (!Number.isFinite(raw)) return;
                            const v01 = (raw > 1) ? (raw / 10.0) : raw;
                            t[String(k)] = Math.max(0, Math.min(1, v01));
                        });
                    }
                    return Object.keys(t).length > 0 ? t : null;
                };

                const emTargets = collectValues(state && state.emotions);
                if (emTargets) {
                    exprs.push({ targets: emTargets, priority: 15, source: 'emotions_state' });
                }

                const fTargets = collectValues(state && state.feelings);
                if (fTargets) {
                    exprs.push({ targets: fTargets, priority: 14, source: 'feelings_state' });
                }
            } catch (e) { /* ignore */ }

            // WEB_DEBUG feelings override: inject persistent targets keyed by emotion name.
            // Persona can map these emotion keys to actual blendshape presets.
            try {
                const dbg = (this._debugEmotionOverrides && typeof this._debugEmotionOverrides === 'object') ? this._debugEmotionOverrides : null;
                if (dbg) {
                    const t = {};
                    Object.keys(dbg).forEach((k) => {
                        const v = Number(dbg[k]);
                        if (!k || !Number.isFinite(v)) return;
                        t[String(k)] = Math.max(0, Math.min(1, v));
                    });
                    if (Object.keys(t).length > 0) {
                        exprs.push({ targets: t, priority: 100, source: 'debug_emotion_override' });
                    }
                }
            } catch (e) { /* ignore */ }

            // Derive expressions from high-level dimensions (valence/arousal/stress/calm/etc)
            // consolidated across all active expressions (respecting priorities).
            try {
                const consolidatedFeelings = {};
                const sortedExprs = exprs.slice().sort((a, b) => (a.priority || 0) - (b.priority || 0));
                sortedExprs.forEach(ex => {
                    if (!evaluateFrame(ex)) return;
                    if (ex.targets) Object.keys(ex.targets).forEach(k => {
                        consolidatedFeelings[k] = Math.max(0, Math.min(1, Number(ex.targets[k]) || 0));
                    });
                });

                const derived = {};
                const get = (k) => (consolidatedFeelings[k] !== undefined) ? Number(consolidatedFeelings[k]) : null;

                // 1. Valence-based (happy/sad)
                const valence = get('valence');
                if (Number.isFinite(valence)) {
                    const v = Math.max(0, Math.min(1, valence));
                    const pos = Math.max(0, (v - 0.5) * 2);
                    const neg = Math.max(0, (0.5 - v) * 2);
                    if (pos > 0.02) derived['happy'] = Math.max(derived['happy'] || 0, pos);
                    if (neg > 0.02) derived['sad'] = Math.max(derived['sad'] || 0, neg);
                }

                // 2. Arousal-based (surprised)
                const arousal = get('arousal');
                if (Number.isFinite(arousal)) {
                    const a = Math.max(0, Math.min(1, arousal));
                    if (a > 0.25) derived['surprised'] = Math.max(derived['surprised'] || 0, (a - 0.25) / 0.75);
                }

                // 3. Stress/Anger-based
                const stress = get('stress');
                const angryF = get('angry'); // 'angry' as feeling dimension
                const sVal = Math.max(Number.isFinite(stress) ? stress : 0, Number.isFinite(angryF) ? angryF : 0);
                if (sVal > 0.05) {
                    derived['scared'] = Math.max(derived['scared'] || 0, sVal * 0.4);
                    derived['angry'] = Math.max(derived['angry'] || 0, sVal);
                }

                // 4. Calmness/Relaxed
                const calm = get('calm');
                if (Number.isFinite(calm)) {
                    const c = Math.max(0, Math.min(1, calm));
                    if (c > 0.65) derived['relaxed'] = Math.max(derived['relaxed'] || 0, (c - 0.65) / 0.35);
                }

                if (Object.keys(derived).length) {
                    // Derived expressions have slightly higher priority than base feelings
                    // so they can override lower-priority presets if needed.
                    exprs.push({ targets: derived, priority: 14.5, source: 'feelings_derived' });
                }
            } catch (e) { console.warn('[AnimationHandler] derivation failed', e); }

            // Emotion overlay injection (client-side): during the scheduled window,
            // inject a synthetic expression keyed by emotion name; the client will map
            // it to blendshape targets using the per-persona `persona.emotions` mapping.
            try {
                const ov = this._emotionOverlay || null;
                const actionKey = (state && state.action) ? String(state.action).toLowerCase() : null;
                const now = Date.now();
                if (ov && ov.emotion && Number.isFinite(ov.startsAtMs) && Number.isFinite(ov.endsAtMs)) {
                    if (now > ov.endsAtMs) {
                        this._emotionOverlay = null;
                    } else if (now >= ov.startsAtMs && now <= ov.endsAtMs) {
                        if (!ov.action || !actionKey || ov.action === actionKey) {
                            const t = {};
                            t[String(ov.emotion)] = Math.max(0, Math.min(1, Number(ov.intensity) || 0));
                            exprs.push({ targets: t, priority: (ov.priority || 25), source: 'emotion_overlay' });
                        }
                    }
                }
            } catch (e) { /* ignore */ }

            // WEB_DEBUG feelings override: inject persistent targets keyed by emotion name.
            // Persona can map these emotion keys to actual blendshape presets.
            try {
                const dbg = (this._debugEmotionOverrides && typeof this._debugEmotionOverrides === 'object') ? this._debugEmotionOverrides : null;
                if (dbg) {
                    const t = {};
                    Object.keys(dbg).forEach((k) => {
                        const v = Number(dbg[k]);
                        if (!k) return;
                        if (!Number.isFinite(v)) return;
                        t[String(k)] = Math.max(0, Math.min(1, v));
                    });
                    if (Object.keys(t).length > 0) {
                        exprs.push({ targets: t, priority: 100, source: 'debug_emotion_override' });
                    }
                }
            } catch (e) { /* ignore */ }

            exprs.sort((a, b) => (b.priority || 0) - (a.priority || 0));
            const assignedPriority = {};
            const normalizeKey = (k) => {
                try {
                    if (!k || typeof k !== 'string') return k;
                    return k.replace(/\./g, '_');
                } catch (e) {
                    return k;
                }
            };

            const addBlinkAliases = (intensity) => {
                // VRM0 common
                desired['eye_blink_left'] = Math.max(desired['eye_blink_left'] || 0, intensity);
                desired['eye_blink_right'] = Math.max(desired['eye_blink_right'] || 0, intensity);
                // VRM1 presets (three-vrm)
                desired['blink'] = Math.max(desired['blink'] || 0, intensity);
                desired['blinkLeft'] = Math.max(desired['blinkLeft'] || 0, intensity);
                desired['blinkRight'] = Math.max(desired['blinkRight'] || 0, intensity);
            };

            const isEyesClosedLogicalKey = (k) => {
                try {
                    const s = String(k || '');
                    // Match underscore/dash-separated or camelCase variants (no dot variant)
                    return /(^|[_-])eyes[_-]?closed($|[_-])|^eyesClosed$/i.test(s) || /^eyes_closed$/i.test(s);
                } catch (e) { return false; }
            };

            const isBlinkLogicalKey = (k) => {
                try {
                    return /blink/i.test(String(k || ''));
                } catch (e) { return false; }
            };

            const addVisemeAliases = (key, intensity) => {
                try {
                    const k = String(key || '').toLowerCase();
                    // Heuristic mapping to VRM1 preset visemes
                    if (k.includes('mouth_a') || /mouth.*\ba\b/.test(k)) desired['aa'] = Math.max(desired['aa'] || 0, intensity);
                    if (k.includes('mouth_i') || /mouth.*\bi\b/.test(k)) desired['ih'] = Math.max(desired['ih'] || 0, intensity);
                    if (k.includes('mouth_u') || /mouth.*\bu\b/.test(k)) desired['ou'] = Math.max(desired['ou'] || 0, intensity);
                    if (k.includes('mouth_e') || /mouth.*\be\b/.test(k)) desired['ee'] = Math.max(desired['ee'] || 0, intensity);
                    if (k.includes('mouth_o') || /mouth.*\bo\b/.test(k)) desired['oh'] = Math.max(desired['oh'] || 0, intensity);
                } catch (e) { /* ignore */ }
            };

            // Track whether eyes are being intentionally held closed by expressions.
            // If so, we suppress blink targets *except* those used as the actual
            // eyelid-closure mapping for eyes_closed (some models only have blink morphs).
            // Also maintain a richer eyesState so we can distinguish persistent
            // closures (persona/animation) from transient blinks (autoblink).
            let eyesClosedRequestedMax = 0;
            const eyesClosedResolvedTargets = new Set();

            exprs.forEach(expr => {
                if (!evaluateFrame(expr)) return;
                const targets = expr.targets || {};
                const p = expr.priority || 0;
                Object.keys(targets).forEach(key => {
                    const intensity = Math.max(0, Math.min(1, targets[key]));
                    const nkey = normalizeKey(key);
                    if (isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(nkey)) {
                        eyesClosedRequestedMax = Math.max(eyesClosedRequestedMax, intensity);
                    }
                    // Resolve mapping: support flat maps or grouped maps (emotions, visemes, expressions)
                    const flat = (blendMap && typeof blendMap[key] === 'string') ? blendMap[key]
                        : ((blendMap && typeof blendMap[nkey] === 'string') ? blendMap[nkey] : null);
                    if (flat) {
                        // If eyes_closed resolves to a blink morph, keep it (clamped) and
                        // remember it so we don't suppress it later.
                        let resolvedIntensity = intensity;
                        if ((isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(nkey)) && isBlinkLogicalKey(flat)) {
                            resolvedIntensity = Math.min(0.85, resolvedIntensity);
                            try { eyesClosedResolvedTargets.add(flat); } catch (e) { /* ignore */ }
                        }
                        if ((assignedPriority[flat] === undefined) || (p > assignedPriority[flat])) {
                            desired[flat] = resolvedIntensity;
                            assignedPriority[flat] = p;
                            // Helpful fallbacks for common logical keys (eyes/mouth)
                            // Important: do NOT map eyes_closed -> blink aliases, otherwise
                            // the avatar appears to keep blinking while eyes are intentionally closed.
                            if ((isBlinkLogicalKey(key) || isBlinkLogicalKey(nkey)) && !(isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(nkey))) {
                                addBlinkAliases(intensity);
                            }
                            if (/mouth/i.test(key) || /mouth/i.test(nkey)) {
                                const alt = key.replace(/[\.]/g, '_');
                                desired[alt] = Math.max(desired[alt] || 0, intensity);
                                addVisemeAliases(key, intensity);
                            }
                        }
                        return;
                    }

                    const grouped = (blendMap && typeof blendMap === 'object') ? blendMap : null;
                    if (grouped) {
                        const groups = ['emotions', 'visemes', 'expressions'];
                        for (let g of groups) {
                            let entry = (grouped[g] && grouped[g][key]) ? grouped[g][key]
                                : ((grouped[g] && grouped[g][nkey]) ? grouped[g][nkey] : null);
                            // Fallback: if persona declares emotion presets, allow those to act like grouped.emotions
                            if (!entry && g === 'emotions' && window.__synth_emotion_face_presets && typeof window.__synth_emotion_face_presets === 'object') {
                                entry = window.__synth_emotion_face_presets[key] || window.__synth_emotion_face_presets[nkey] || null;
                            }
                            if (entry) {
                                if (entry.targets && typeof entry.targets === 'object') {
                                    Object.keys(entry.targets).forEach(bs => {
                                        try {
                                            if ((isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(nkey)) && isBlinkLogicalKey(bs)) {
                                                eyesClosedResolvedTargets.add(bs);
                                            }
                                        } catch (e) { /* ignore */ }
                                        if ((assignedPriority[bs] === undefined) || (p > assignedPriority[bs])) {
                                            desired[bs] = (entry.targets[bs] || 0) * intensity;
                                            assignedPriority[bs] = p;
                                        }
                                    });
                                } else if (typeof entry === 'object') {
                                    Object.keys(entry).forEach(bs => {
                                        try {
                                            if ((isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(nkey)) && isBlinkLogicalKey(bs)) {
                                                eyesClosedResolvedTargets.add(bs);
                                            }
                                        } catch (e) { /* ignore */ }
                                        if ((assignedPriority[bs] === undefined) || (p > assignedPriority[bs])) {
                                            desired[bs] = (entry[bs] || 0) * intensity;
                                            assignedPriority[bs] = p;
                                        }
                                    });
                                }
                                if (/eyes?|blink/i.test(key) || /eyes?|blink/i.test(nkey)) {
                                    if ((isBlinkLogicalKey(key) || isBlinkLogicalKey(nkey)) && !(isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(nkey))) {
                                        addBlinkAliases(intensity);
                                    }
                                }
                                if (/mouth/i.test(key) || /mouth/i.test(nkey)) {
                                    addVisemeAliases(key, intensity);
                                }
                                return;
                            }
                        }
                    }

                    // No legacy fallback mapping: the client will not synthesize viseme presets
                    // automatically when a persona does not declare `persona.emotions`.
                    try { /* intentionally no-op */ } catch (e) { /* ignore */ }

                    // Fallback: use key as blendshape name
                    const outKey = nkey || key;
                    try {
                        if ((isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(outKey)) && isBlinkLogicalKey(outKey)) {
                            eyesClosedResolvedTargets.add(outKey);
                        }
                    } catch (e) { /* ignore */ }
                    if ((assignedPriority[outKey] === undefined) || (p > assignedPriority[outKey])) {
                        desired[outKey] = intensity;
                        assignedPriority[outKey] = p;
                        // fallback keys
                        if ((isBlinkLogicalKey(key) || isBlinkLogicalKey(outKey)) && !(isEyesClosedLogicalKey(key) || isEyesClosedLogicalKey(outKey))) {
                            addBlinkAliases(intensity);
                        }
                        if (/mouth/i.test(key) || /mouth/i.test(outKey)) {
                            const alt = outKey.replace(/[\.]/g, '_');
                            desired[alt] = Math.max(desired[alt] || 0, intensity);
                            addVisemeAliases(key, intensity);
                        }
                    }
                });
            });

            // If eyes are intentionally held closed, suppress any blink targets that are NOT
            // being used as the actual eyelid closure mapping for eyes_closed.
            // This prevents the blink loop (or unrelated expression aliases) from fighting the pose.
            if (eyesClosedRequestedMax > 0.5) {
                ['eye_blink_left', 'eye_blink_right', 'blink', 'blinkLeft', 'blinkRight', 'eyeBlinkLeft', 'eyeBlinkRight', 'Blink', 'BlinkLeft', 'BlinkRight'].forEach(k => {
                    if (!eyesClosedResolvedTargets.has(k)) desired[k] = 0;
                });
            }

            // Suspend/resume blink + eye movement based on *requested* eyes_closed.
            // This works even on models that don't expose a readable eyes_closed morph.
            try {
                const wasEyesClosed = !!(this._eyesState && this._eyesState.locked && this._eyesState.value > 0.5);
                const nowEyesClosed = eyesClosedRequestedMax > 0.5;
                // If an expression is requesting eyes closed, set a persistent eyesState
                if (nowEyesClosed) {
                    // source 'expression' indicates a persistent request coming from expressions/persona
                    this._setEyesState({ value: eyesClosedRequestedMax, source: 'expression' });
                } else {
                    // clear expression-based eyes state if present
                    if (this._eyesState && this._eyesState.source === 'expression') this._clearEyesState();
                }

                // If a new *locked* persistent closure is in effect, suspend blink/eye movement;
                // if a previously locked closure was cleared, resume according to config.
                try {
                    const isLockedNow = !!(this._eyesState && this._eyesState.locked && this._eyesState.value > 0.5);
                    if (!wasEyesClosed && isLockedNow) {
                        if (this._blinkLoopRunning) {
                            console.debug(`[AnimationHandler] Eyes locked closed by expression/source (${eyesClosedRequestedMax.toFixed(2)}), suspending blink`);
                            this._stopBlinkLoop();
                        }
                        if (this._eyeLoopRunning) {
                            console.debug(`[AnimationHandler] Eyes locked closed by expression/source (${eyesClosedRequestedMax.toFixed(2)}), suspending eye movement`);
                            this._stopEyeMovement();
                        }
                    } else if (wasEyesClosed && !isLockedNow) {
                        if (this._blinkAutoEnabled && !this._blinkLoopRunning) {
                            console.debug(`[AnimationHandler] Locked eyes reopened (${eyesClosedRequestedMax.toFixed(2)}), resuming blink`);
                            this._startBlinkLoop();
                        }
                        if (this._eyeAutoEnabled && !this._eyeLoopRunning) {
                            console.debug(`[AnimationHandler] Locked eyes reopened (${eyesClosedRequestedMax.toFixed(2)}), resuming eye movement`);
                            this._startEyeMovement();
                        }
                    }
                } catch (e) { /* ignore */ }
            } catch (e) { /* ignore */ }

            // WEB_DEBUG facial override: force desired values (wins over descriptors).
            // Try to resolve logical keys via persona mapping when available so
            // debug keys like 'eyesClosed' or 'mouth_O' map to real blendshape names.
            try {
                const dbg = (this._debugFaceOverrides && typeof this._debugFaceOverrides === 'object') ? this._debugFaceOverrides : null;
                if (dbg) {
                    Object.keys(dbg).forEach((k) => {
                        try {
                            if (!k) return;
                            const rawVal = Number(dbg[k]);
                            if (!Number.isFinite(rawVal)) return;
                            const v = Math.max(0, Math.min(1, rawVal));

                            const nkey = normalizeKey(String(k));
                            // Try flat mapping from persona blendshape_map
                            const flat = (blendMap && typeof blendMap[k] === 'string') ? blendMap[k]
                                : ((blendMap && typeof blendMap[nkey] === 'string') ? blendMap[nkey] : null);
                            if (flat) {
                                desired[flat] = v;
                                assignedPriority[flat] = Math.max(assignedPriority[flat] || 0, 100);
                                return;
                            }

                            // Try grouped mappings (emotions/visemes/expressions)
                            const grouped = (blendMap && typeof blendMap === 'object') ? blendMap : null;
                            if (grouped) {
                                const groups = ['emotions', 'visemes', 'expressions'];
                                for (let g of groups) {
                                    let entry = (grouped[g] && grouped[g][k]) ? grouped[g][k]
                                        : ((grouped[g] && grouped[g][nkey]) ? grouped[g][nkey] : null);
                                    // Fallback: check persona emotion presets if present
                                    if (!entry && g === 'emotions' && window.__synth_emotion_face_presets && typeof window.__synth_emotion_face_presets === 'object') {
                                        entry = window.__synth_emotion_face_presets[k] || window.__synth_emotion_face_presets[nkey] || null;
                                    }
                                    if (entry) {
                                        if (entry.targets && typeof entry.targets === 'object') {
                                            Object.keys(entry.targets).forEach(bs => {
                                                try {
                                                    const val = (entry.targets[bs] || 0) * v;
                                                    desired[bs] = Math.max(desired[bs] || 0, val);
                                                    assignedPriority[bs] = Math.max(assignedPriority[bs] || 0, 100);
                                                } catch (e) { /* ignore */ }
                                            });
                                            return;
                                        } else if (typeof entry === 'object') {
                                            Object.keys(entry).forEach(bs => {
                                                try {
                                                    const val = (entry[bs] || 0) * v;
                                                    desired[bs] = Math.max(desired[bs] || 0, val);
                                                    assignedPriority[bs] = Math.max(assignedPriority[bs] || 0, 100);
                                                } catch (e) { /* ignore */ }
                                            });
                                            return;
                                        }
                                    }
                                }
                            }

                            // Fallback: try to set normalized key directly (existing behavior)
                            desired[nkey] = Math.max(0, Math.min(1, v));
                            assignedPriority[nkey] = Math.max(assignedPriority[nkey] || 0, 100);
                        } catch (e) { /* ignore per-key errors */ }
                    });
                }
            } catch (e) { /* ignore */ }

            // Apply smoothing/interpolation towards desired values
            const speed = (effectivePersona && effectivePersona.emotion_speed && effectivePersona.emotion_speed.default) ? effectivePersona.emotion_speed.default : 6.0; // units/sec

            // Collect blendshape keys driven by _directApply sources (e.g. lipsync).
            // These should snap to target instantly — the source handles its own
            // per-frame smoothing and the expression interpolation is too slow.
            const directApplyKeys = new Set();
            try {
                if (Array.isArray(this._expressionSources)) {
                    this._expressionSources.forEach(s => {
                        if (s && s._directApply && s.targets) {
                            Object.keys(s.targets).forEach(tk => directApplyKeys.add(tk));
                        }
                    });
                }
            } catch (_e) { /* ignore */ }

            Object.keys(desired).forEach(k => {
                const cur = this._expressionState[k] || 0;
                const tgt = desired[k];

                let next;
                if (directApplyKeys.has(k)) {
                    // Lipsync (or other direct sources): snap to target, no slow lerp
                    next = tgt;
                } else {
                    // Normal interpolation for expressions/emotions
                    const step = Math.min(1, speed * dt);
                    next = cur + (tgt - cur) * step;
                }

                // Limit eyes_closed to avoid eyelid/cheek clipping when fully closed.
                // 0.85 is intentionally conservative; adjust in persona if needed.
                if ((k === 'eyes_closed' || k === 'eyesClosed') && next > 0.85) {
                    next = 0.85;
                }

                this._expressionState[k] = next;
                const ok = this._setFaceValue(k, next);
                if (ok) console.debug('[AnimationHandler] setValue', k, next);
            });

            // Decay any blendshapes that are no longer targeted
            Object.keys(this._expressionState).forEach(k => {
                if (desired[k] === undefined) {
                    const cur = this._expressionState[k] || 0;
                    const next = Math.max(0, cur - Math.min(1, ((effectivePersona && effectivePersona.emotion_speed && effectivePersona.emotion_speed.decay) ? effectivePersona.emotion_speed.decay : 4.0) * dt));
                    if (Math.abs(next - cur) > 1e-4) {
                        this._expressionState[k] = next;
                        const ok = this._setFaceValue(k, next);
                        if (ok) console.debug('[AnimationHandler] decay setValue', k, next);
                    } else {
                        // negligible - clear
                        delete this._expressionState[k];
                        const ok = this._setFaceValue(k, 0);
                        if (ok) console.debug('[AnimationHandler] cleared', k);
                    }
                }
            });

            // End log (throttled to the same policy)
            try {
                const nowTs2 = Date.now();
                if ((nowTs2 - (this._lastExpressionLogTime || 0)) < 2500) {
                    console.debug('[AnimationHandler] applyExpressionsForFrame END');
                }
            } catch (e) { /* ignore */ }

        } catch (e) {
            console.warn('[AnimationHandler] applyExpressionsForFrame failed', e);
        }
    }

    // Manage an eyes state object to distinguish persistent (persona/animation)
    // closures from transient autoblinks. Value in range [0,1].
    _setEyesState({ value = 0, source = null, duration = null } = {}) {
        try {
            if (!this._eyesState) this._eyesState = { value: 0, source: null, since: null, duration: null, locked: false };
            const now = Date.now();
            this._eyesState.value = Math.max(0, Math.min(1, value || 0));
            this._eyesState.source = source || null;
            this._eyesState.since = now;
            this._eyesState.duration = (typeof duration === 'number' && duration > 0) ? duration : null;
            // Lock state for persistent sources
            // Only treat the state as "locked" (preventing autoblinks) when:
            // - source is present and not an autoblink, AND
            // - eyes are substantially closed (>0.5), AND
            // - either the request is not from a transient 'expression' OR a duration
            //   was explicitly provided (longer closures).
            try {
                const isLong = (typeof duration === 'number' && duration > 300);
                const willLock = (source && source !== 'autoblink' && this._eyesState.value > 0.5 && (source !== 'expression' || isLong));
                this._eyesState.locked = willLock;
                console.debug('[AnimationHandler] _setEyesState: locked=', willLock, 'source=', source, 'value=', this._eyesState.value, 'duration=', duration);
            } catch (e) {
                this._eyesState.locked = (source && source !== 'autoblink' && this._eyesState.value > 0.5);
            }
            try { window.dispatchEvent(new CustomEvent('synth_eyes_state_changed', { detail: { value: this._eyesState.value, source: this._eyesState.source } })); } catch (e) { }

            // Safety: if persistent close lasts too long, force reopen after timeout (30s)
            if (this._eyesState.locked) {
                const timeoutMs = 30000;
                if (this._eyesStateTimeout) { try { clearTimeout(this._eyesStateTimeout); } catch (e) { } }
                this._eyesStateTimeout = setTimeout(() => {
                    try {
                        // If still locked by the same source, force open and log
                        if (this._eyesState && this._eyesState.locked && Date.now() - this._eyesState.since >= timeoutMs) {
                            console.warn('[AnimationHandler] eyesState locked too long, forcing reopen');
                            this._forceOpenEyes();
                            this._clearEyesState();
                        }
                    } catch (e) { }
                }, timeoutMs);
            } else {
                if (this._eyesStateTimeout) { try { clearTimeout(this._eyesStateTimeout); } catch (e) { } this._eyesStateTimeout = null; }
            }
        } catch (e) { /* ignore */ }
    }

    _clearEyesState() {
        try {
            if (!this._eyesState) return;
            this._eyesState = { value: 0, source: null, since: null, duration: null, locked: false };
            try { window.dispatchEvent(new CustomEvent('synth_eyes_state_changed', { detail: { value: 0, source: null } })); } catch (e) { }
            if (this._eyesStateTimeout) { try { clearTimeout(this._eyesStateTimeout); } catch (e) { } this._eyesStateTimeout = null; }
        } catch (e) { /* ignore */ }
    }


    _getEffectivePersona() {
        const skin = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
        const persona = (this._personaCache && this._personaCache[skin]) || null;
        const reiPersona = (this._personaCache && this._personaCache['Rei']) || null;

        // Return a merged object where current persona takes precedence, but falls back to Rei for top-level keys
        return {
            ...(reiPersona || {}),
            ...(persona || {}),
            blendshape_map: {
                ...(reiPersona && reiPersona.blendshape_map ? reiPersona.blendshape_map : {}),
                ...(persona && persona.blendshape_map ? persona.blendshape_map : {})
            },
            emotions: {
                ...(reiPersona && reiPersona.emotions ? reiPersona.emotions : {}),
                ...(persona && persona.emotions ? persona.emotions : {})
            },
            emotion_speed: {
                ...(reiPersona && reiPersona.emotion_speed ? reiPersona.emotion_speed : {}),
                ...(persona && persona.emotion_speed ? persona.emotion_speed : {})
            }
        };
    }

    // Load persona.json for a skin and cache it
    async _loadPersonaForSkin(skin) {
        try {
            if (!this._personaCache) this._personaCache = {};
            if (this._personaCache[skin]) return this._personaCache[skin];
            const res = await fetch(`/skins/${encodeURIComponent(skin)}/persona.json`);
            if (!res.ok) { this._personaCache[skin] = null; return null; }
            const json = await res.json();
            this._personaCache[skin] = json;
            return json;
        } catch (e) {
            this._personaCache[skin] = null;
            return null;
        }
    }

    // Blink manager: schedules automatic blinks with jitter
    _startBlinkLoop() {
        try {
            if (this._blinkLoopRunning) return;
            console.debug('[AnimationHandler] _startBlinkLoop START', { rate_s: this._blinkRateS, intensity: this._blinkIntensity });
            this._blinkLoopRunning = true;
            const scheduleNext = () => {
                if (!this._blinkLoopRunning) return;
                const jitter = (Math.random() - 0.5) * 0.3; // +/-15%
                const interval = Math.max(0.4, (this._blinkRateS || 3.0) * (1 + jitter));
                this._blinkTimer = setTimeout(() => {
                    try { this._performBlink(); } catch (e) { }
                    scheduleNext();
                }, interval * 1000);
            };
            scheduleNext();
        } catch (e) { console.warn('[AnimationHandler] _startBlinkLoop failed', e); }
    }

    _stopBlinkLoop() {
        try {
            console.debug('[AnimationHandler] _stopBlinkLoop');
            this._blinkLoopRunning = false;
            if (this._blinkTimer) { clearTimeout(this._blinkTimer); this._blinkTimer = null; }
            // clear any phase timers used during blink (close/hold/open)
            if (this._blinkPhaseTimers && Array.isArray(this._blinkPhaseTimers)) {
                this._blinkPhaseTimers.forEach(t => { try { clearTimeout(t); } catch (e) { } });
                this._blinkPhaseTimers = [];
            }
            this._blinkInProgress = false;
            this._blinkState = 'open';
        } catch (e) { /* ignore */ }
    }

    // Force eyes open immediately: cancel blink timers and reset eye blendshapes
    _forceOpenEyes() {
        try {
            const wasLoopRunning = !!this._blinkLoopRunning;
            // Cancel any in-flight blink animation loop
            try { this._blinkToken = (this._blinkToken || 0) + 1; } catch (e) { }
            // clear phase timers
            if (this._blinkPhaseTimers && Array.isArray(this._blinkPhaseTimers)) {
                this._blinkPhaseTimers.forEach(t => { try { clearTimeout(t); } catch (e) { } });
                this._blinkPhaseTimers = [];
            }
            this._blinkInProgress = false;
            this._blinkState = 'open';

            // Ensure all candidate blink blendshapes are set to 0 and removed from expressionState
            try {
                const effectivePersona = this._getEffectivePersona();
                const blendMap = effectivePersona.blendshape_map;
                const candidates = [];
                ['blink', 'eyes_closed', 'eyes.close'].forEach(k => { if (blendMap && blendMap[k]) candidates.push(blendMap[k]); });
                candidates.push(
                    'eyes_closed', 'eyesClosed', 'EyesClosed',
                    'blink', 'Blink', 'blinkLeft', 'blinkRight', 'BlinkLeft', 'BlinkRight',
                    'eye_blink_left', 'eye_blink_right', 'eyeBlinkLeft', 'eyeBlinkRight'
                );
                candidates.forEach(k => { try { this._setFaceValue(k, 0); if (this._expressionState) delete this._expressionState[k]; } catch (e) { } });
            } catch (e) { /* ignore */ }

            // If the blink loop was running, ensure it's still scheduled.
            // (We intentionally do NOT clear _blinkTimer here, otherwise the loop can stop permanently.)
            try {
                if (wasLoopRunning && !this._blinkTimer) {
                    // Restart scheduling only; _startBlinkLoop() is idempotent.
                    this._startBlinkLoop();
                }
            } catch (e) { /* ignore */ }
            // Clear any persistent eyesState when forcing open.
            try { if (this._eyesState) this._clearEyesState(); } catch (e) { }
        } catch (e) { /* ignore */ }
    }

    // Smoothly reset eyes over a short duration (ms). This will interpolate
    // eye-related blendshapes down to 0 instead of snapping them, then
    // clear the persistent eyes state and ensure blink is running.
    _resetEyesSmoothly(totalMs = 220) {
        try {
            const candidates = [];
            try {
                const effectivePersona = this._getEffectivePersona();
                const blendMap = effectivePersona.blendshape_map;
                ['blink', 'eyes_closed', 'eyes.close'].forEach(k => { if (blendMap && blendMap[k]) candidates.push(blendMap[k]); });
            } catch (e) { /* ignore */ }

            // Add fallback candidates
            candidates.push(
                'eyes_closed', 'eyesClosed', 'EyesClosed',
                'blink', 'Blink', 'blinkLeft', 'blinkRight', 'BlinkLeft', 'BlinkRight',
                'eye_blink_left', 'eye_blink_right', 'eyeBlinkLeft', 'eyeBlinkRight'
            );

            // Determine current values
            const cur = {};
            candidates.forEach(k => { try { cur[k] = Math.max(0, Math.min(1, this._getFaceValue(k) || 0)); } catch (e) { cur[k] = 0; } });

            const steps = Math.max(3, Math.round(totalMs / 20));
            const stepDelay = Math.max(10, Math.round(totalMs / steps));

            let step = 0;
            const stepFn = () => {
                try {
                    step++;
                    const t = step / steps;
                    candidates.forEach(k => {
                        try {
                            const start = cur[k] || 0;
                            const next = Math.max(0, start * (1 - t));
                            this._setFaceValue(k, next);
                        } catch (e) { /* ignore */ }
                    });
                    if (step < steps) {
                        setTimeout(stepFn, stepDelay);
                    } else {
                        // Finalize: ensure zeros, clear expression state, clear persistent eyes state
                        candidates.forEach(k => { try { this._setFaceValue(k, 0); if (this._expressionState) delete this._expressionState[k]; } catch (e) { } });
                        try { if (this._eyesState) this._clearEyesState(); } catch (e) { }
                        // Restart blink loop if it was running or if we want it active
                        try { if (!this._blinkLoopRunning) this._startBlinkLoop(); } catch (e) { }
                    }
                } catch (e) { /* ignore */ }
            };

            // Kick off smoothing
            setTimeout(stepFn, stepDelay);
        } catch (e) { /* ignore */ }
    }

    _performBlink() {
        try {
            if (!this.vrm || !this._getFaceController()) {
                try { window.dispatchEvent(new CustomEvent('synth_animation_blink', { detail: { ok: false } })); } catch (e) { }
                return;
            }

            // If eyes are intentionally held closed by expressions, skip blink.
            // Don't use raw face values here: they may be cached or driven by an animation
            // track and would incorrectly suppress blinking forever in idle.
            try {
                if (this._eyesState && this._eyesState.locked && this._eyesState.value > 0.5) {
                    console.debug('[AnimationHandler] Skipping blink: eyes held closed by state', this._eyesState);
                    return;
                }
            } catch (e) { /* ignore */ }

            if (this._blinkInProgress) return;
            this._blinkInProgress = true;
            this._blinkState = 'closing';
            const token = (this._blinkToken || 0) + 1;
            this._blinkToken = token;

            // resolve blink blendshape keys from persona or fallback heuristics
            const effectivePersona = this._getEffectivePersona();
            const blendMap = effectivePersona.blendshape_map;

            // Candidate keys (try mapped names first)
            const candidates = [];
            // check grouped mapping for a 'blink' or 'eyes_closed' mapping
            ['blink', 'eyes_closed', 'eyes.close'].forEach(k => { if (blendMap && blendMap[k]) candidates.push(blendMap[k]); });
            // fallback common names
            candidates.push(
                'eyes_closed', 'blink', 'Blink',
                'blinkLeft', 'blinkRight', 'BlinkLeft', 'BlinkRight',
                'eye_blink_left', 'eye_blink_right', 'eyeBlinkLeft', 'eyeBlinkRight'
            );

            const intensity = Math.max(0, Math.min(1, (this._blinkIntensity || 0.6)));
            const closeMs = Math.max(20, Math.round(this._blinkCloseMs || 60));
            const holdMs = Math.max(0, Math.round(this._blinkHoldMs || 120));
            const openMs = Math.max(20, Math.round(this._blinkOpenMs || 60));
            const totalMs = closeMs + holdMs + openMs;

            const setAll = (v) => {
                const vv = Math.max(0, Math.min(1, v));
                // Re-check if eyes are closed JUST BEFORE applying blink
                // Prefer authoritative eyesState if present to avoid racing with face cache.
                try {
                    if (this._eyesState && this._eyesState.locked && this._eyesState.value > 0.5) {
                        console.debug(`[AnimationHandler] Aborting blink mid-execution: eyes closed by state (${this._eyesState.source}=${this._eyesState.value.toFixed(2)})`);
                        return;
                    }
                    const eyesClosedCandidates = ['eyes_closed', 'eyesClosed', 'EyesClosed'];
                    for (const candidate of eyesClosedCandidates) {
                        try {
                            const eyeValue = this._getFaceValue(candidate);
                            if (typeof eyeValue === 'number' && eyeValue > 0.5) {
                                // Allow the current autoblink to proceed even if face value
                                // is already > 0.5 (since it's the blink itself applying those values).
                                const isAutoblinkActive = !!(this._eyesState && this._eyesState.source === 'autoblink' && this._blinkInProgress);
                                if (!isAutoblinkActive) {
                                    console.debug(`[AnimationHandler] Aborting blink mid-execution: eyes closed (${candidate}=${eyeValue.toFixed(2)})`, 'blinkInProgress=', !!this._blinkInProgress, 'eyesState=', this._eyesState && this._eyesState.source);
                                    // Ensure we don't leave the face in a partially-closed state
                                    try { candidates.forEach(k => { try { this._setFaceValue(k, 0); } catch (e) { } }); } catch (e) { }
                                    try { if (this._eyesState && this._eyesState.source === 'autoblink') this._clearEyesState(); } catch (e) { }
                                    this._blinkInProgress = false;
                                    this._blinkState = 'open';
                                    return; // Don't apply blink if eyes are closed by external reason
                                }
                            }
                        } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }
                candidates.forEach(k => { try { this._setFaceValue(k, vv); } catch (e) { } });
            };

            // Mark transient autoblink state so other logic can distinguish it
            try { this._setEyesState({ value: intensity, source: 'autoblink', duration: totalMs }); } catch (e) { }
            try { window.dispatchEvent(new CustomEvent('synth_animation_blink', { detail: { intensity, closeMs, holdMs, openMs, totalMs } })); } catch (e) { }

            const t0 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
            const step = (now) => {
                try {
                    if ((this._blinkToken || 0) !== token) return;
                    const t = now - t0;

                    if (t < closeMs) {
                        this._blinkState = 'closing';
                        setAll(intensity * (t / closeMs));
                    } else if (t < closeMs + holdMs) {
                        this._blinkState = 'closed';
                        setAll(intensity);
                    } else if (t < closeMs + holdMs + openMs) {
                        this._blinkState = 'opening';
                        const tt = (t - closeMs - holdMs) / openMs;
                        setAll(intensity * (1 - tt));
                    } else {
                        this._blinkState = 'open';
                        this._blinkInProgress = false;
                        setAll(0);
                        try { if (this._eyesState && this._eyesState.source === 'autoblink') this._clearEyesState(); } catch (e) { }
                        return;
                    }

                    requestAnimationFrame(step);
                } catch (e) {
                    try { setAll(0); } catch (ee) { }
                    this._blinkState = 'open';
                    this._blinkInProgress = false;
                }
            };

            try {
                requestAnimationFrame(step);
            } catch (e) {
                // RAF unavailable: coarse fallback
                setAll(intensity);
                const t1 = setTimeout(() => { try { setAll(0); } catch (e) { } this._blinkInProgress = false; this._blinkState = 'open'; try { if (this._eyesState && this._eyesState.source === 'autoblink') this._clearEyesState(); } catch (e) { } }, totalMs);
                this._blinkPhaseTimers.push(t1);
            }

        } catch (e) { console.warn('[AnimationHandler] _performBlink failed', e); }
    }

    // Eye movement (saccades): simple emit + optional blendshape nudges
    _startEyeMovement() {
        try {
            if (this._eyeLoopRunning) return;
            this._eyeLoopRunning = true;
            const schedule = () => {
                if (!this._eyeLoopRunning) return;
                const jitter = (Math.random() - 0.5) * 0.5;
                const interval = Math.max(0.2, (this._saccadeRateS || 2) * (1 + jitter));
                this._eyeTimer = setTimeout(() => {
                    try { this._performSaccade(); } catch (e) { }
                    schedule();
                }, interval * 1000);
            };
            schedule();
        } catch (e) { console.warn('[AnimationHandler] _startEyeMovement failed', e); }
    }

    _stopEyeMovement() {
        try { this._eyeLoopRunning = false; if (this._eyeTimer) { clearTimeout(this._eyeTimer); this._eyeTimer = null; } } catch (e) { }
    }

    _performSaccade() {
        try {
            // If eyes are intentionally held closed by expressions, skip saccade.
            try {
                if (this._eyesState && this._eyesState.locked && this._eyesState.value > 0.5) {
                    console.debug('[AnimationHandler] Skipping saccade: eyes held closed by state', this._eyesState);
                    return;
                }
            } catch (e) { /* ignore */ }

            // small random notch values
            const x = (Math.random() - 0.5) * 0.2;
            const y = (Math.random() - 0.5) * 0.2;
            try { window.dispatchEvent(new CustomEvent('synth_animation_saccade', { detail: { x, y } })); } catch (e) { }

            // Optionally nudge blendshapes if mapped
            // Resolve effective persona with fallback to Rei
            const effectivePersona = this._getEffectivePersona();
            const blendMap = effectivePersona.blendshape_map;
            const lookMap = (blendMap && blendMap.expressions && blendMap.expressions['look']) ? blendMap.expressions['look'] : null;
            // try to set tiny values to candidate look blendshapes (if any)
            if (this.vrm && this._getFaceController() && lookMap && lookMap.targets) {
                Object.keys(lookMap.targets).forEach(k => {
                    try { this._setFaceValue(k, Math.max(0, Math.min(1, (lookMap.targets[k] || 0) * Math.abs(x)))); } catch (e) { }
                });
            }
        } catch (e) { console.warn('[AnimationHandler] _performSaccade failed', e); }
    }
    constructor(mixer, vrm) {
        this.mixer = mixer;
        this.vrm = vrm;
        this.actions = {};
        this.currentAction = null;
        this.currentActionName = null; // track which action (think, write, etc.) is currently playing
        this.currentActionPhase = null; // track 'intro', 'loop', or 'outro'
        this.currentStructuredAction = null; // reference to the structured action object
        this.loadedAnimations = {};
        this.loadedDescriptors = {}; // cache descriptor JSON (including null for 404)
        // Track in-flight preloads so startAction can wait for readiness
        // before fading out the current action (prevents transient T-pose).
        this._preloadPromises = {};
        // Keep a low-weight idle baseline running so bones that are not keyed
        // by the active action don't get stuck in previous poses.
        this._baseIdleAction = null;
        this._baseIdleKey = null;
        this._playOnceTimers = {}; // safety timers for play-once actions
        this._globalMixerFinishBound = false; // ensure we bind a single finished handler
        this._mixerEventBound = false; // ensure structured action mixer event is bound once

        // If a structured action ends and we briefly have no active action,
        // schedule a conservative fallback to idle to avoid lingering T-pose.
        this._postOutroIdleTimer = null;
        this._postOutroIdleToken = 0;
        this._pendingRequestedAction = null;
        this._lateRecoveryTokens = {};

        // Serialize animation switches to avoid concurrent startAction() calls
        // which can momentarily stop the current action and cause a visible T-pose.
        this._startActionChain = Promise.resolve();
        // All overlay (non-idle) actions currently running (or fading out).
        // Used by _stopAllOverlays() to guarantee no orphaned clips remain.
        this._activeActions = new Set();
        // Blink state tracking to prevent overlapping blinks and control timings
        this._blinkInProgress = false;
        this._blinkState = 'open'; // open|closing|closed|opening
        this._blinkPhaseTimers = [];
        this._blinkToken = 0;
    }

    async preloadAnimation(animationFile, descriptor) {
        /**
         * Pre-load animation data to cache before playback starts.
         * This prevents T-pose by ensuring FBX/descriptor are ready when play_animation arrives.
         * 
         * Args:
         *   animationFile: Animation filename (e.g. 'Thinking.fbx') or full path
         *   descriptor: Optional descriptor JSON with frame ranges and structure
         */
        try {
            const normalizedKey = this._normalizeAnimationKey(animationFile);
            console.log('[AnimationHandler] preloadAnimation:', normalizedKey, 'descriptor:', !!descriptor);

            // Try to infer actionName (state folder) from a full URL/path like:
            //   /skins/<Skin>/animations/<state>/<File>.fbx
            // so we can call loadAnimation(state, <fullpath>) rather than loadAction(<filename>).
            let inferredAction = null;
            try {
                if (typeof animationFile === 'string' && animationFile.includes('/animations/')) {
                    const parts = animationFile.split('/animations/');
                    const rest = (parts && parts[1]) ? parts[1] : '';
                    const segs = rest.split('/').filter(Boolean);
                    if (segs && segs.length >= 2) {
                        inferredAction = String(segs[0] || '').toLowerCase();
                    }
                }
            } catch (e) { /* ignore */ }

            // Cache the descriptor immediately if provided
            if (descriptor && typeof descriptor === 'object') {
                this.loadedDescriptors[normalizedKey] = descriptor;
                console.log('[AnimationHandler] Cached descriptor for:', normalizedKey);

                // Also cache under the canonical descriptor URL key used by loadDescriptor().
                // loadDescriptor() constructs the path as /api/skins/<skin>/animations/<state>/<file>.json
                // so we must cache under BOTH the bare path and the /api/-prefixed path to avoid
                // a cache miss that would trigger a redundant HTTP fetch.
                try {
                    if (typeof animationFile === 'string' && animationFile.includes('/')) {
                        const cleanAnim = animationFile.split('?')[0].split('#')[0];
                        // Bare path: /skins/Rei/animations/think/Thinking.fbx.json
                        this.loadedDescriptors[`${cleanAnim}.json`] = descriptor;
                        // API-prefixed path: /api/skins/Rei/animations/think/Thinking.fbx.json
                        if (cleanAnim.includes('/skins/')) {
                            this.loadedDescriptors[`/api${cleanAnim}.json`] = descriptor;
                        }
                    }
                } catch (e) { /* ignore */ }
            }

            // Attempt to load the animation asynchronously
            try {
                if (!this.vrm || !this.mixer) {
                    console.warn('[AnimationHandler] preloadAnimation skipped (VRM/mixer not ready yet):', normalizedKey);
                    return;
                }

                // Prefer preloading the specific file (full path), not a logical action list.
                // Also track readiness so startAction can wait for it.
                if (!this._preloadPromises) this._preloadPromises = {};
                const existing = this._preloadPromises[normalizedKey] || (animationFile && this._preloadPromises[animationFile]) || null;
                if (existing) return;

                const actionName = inferredAction || 'idle';
                const p = (async () => {
                    const clip = await this.loadAnimation(actionName, animationFile);
                    if (clip) {
                        console.log('[AnimationHandler] Successfully preloaded animation clip:', normalizedKey);
                    } else {
                        console.warn('[AnimationHandler] Failed to preload animation clip:', normalizedKey);
                    }
                    return clip;
                })();

                this._preloadPromises[normalizedKey] = p;
                try {
                    if (animationFile && typeof animationFile === 'string' && animationFile !== normalizedKey) {
                        this._preloadPromises[animationFile] = p;
                    }
                } catch (e) { /* ignore */ }

                // Do NOT delete the promise from _preloadPromises after it resolves.
                // _preloadPromises stores in-flight AND completed promises so that
                // _awaitAnimationReady never re-triggers a redundant fetch.
                // The clip itself is persisted in this.loadedAnimations (the real cache).
            } catch (e) {
                console.warn('[AnimationHandler] Error preloading animation:', normalizedKey, e);
            }
        } catch (e) {
            console.warn('[AnimationHandler] preloadAnimation caught exception:', e);
        }
    }

    _normalizeAnimationKey(animationFile) {
        try {
            if (!animationFile || typeof animationFile !== 'string') return animationFile;
            const clean = animationFile.split('?')[0].split('#')[0];
            // If backend sends a full URL/path, normalize to the filename.
            // This matches the filenames returned by /api/animations and used by preloadAllAnimations().
            if (clean.includes('/')) {
                const last = clean.split('/').pop() || clean;
                return decodeURIComponent(last);
            }
            return clean;
        } catch (e) {
            return animationFile;
        }
    }

    async _awaitAnimationReady(actionName, animationFile, timeoutMs = 30000) {
        // HARD RULE: never start playback unless the animation is loaded.
        // Keep the current animation playing while we load; do not "fail open".
        try {
            if (!animationFile) return null;
            if (!this.vrm || !this.mixer) return null;
            if (!this._preloadPromises) this._preloadPromises = {};
            const normalizedKey = this._normalizeAnimationKey(animationFile);

            // Fast path: clip already in the persistent cache — no need to wait for anything.
            const cachedClip = (this.loadedAnimations && (this.loadedAnimations[normalizedKey] || this.loadedAnimations[animationFile])) || null;
            if (cachedClip) {
                console.log(`[AnimationHandler] _awaitAnimationReady fast-path (already cached): ${normalizedKey}`);
                return cachedClip;
            }

            let p = this._preloadPromises[normalizedKey] || this._preloadPromises[animationFile] || null;
            if (!p) {
                // Ensure the clip is loaded while the current action still plays.
                p = (async () => await this.loadAnimation(actionName, animationFile))();
                this._preloadPromises[normalizedKey] = p;
                try {
                    if (animationFile !== normalizedKey) this._preloadPromises[animationFile] = p;
                } catch (e) { /* ignore */ }
            }

            // Hard timeout: abort the wait (not the download) if the clip takes too long.
            // The download continues in the background via _preloadPromises, so the next
            // call will resolve immediately once it completes.
            const hardMs = Math.max(500, Math.min(60000, Number(timeoutMs) || 30000));
            const softWarnMs = Math.round(hardMs * 0.5);
            let warnTimer = null;
            const timeoutP = new Promise((resolve) => {
                setTimeout(() => {
                    try { console.warn('[AnimationHandler] Animation preload timed out after', hardMs, 'ms:', actionName, animationFile); } catch (e) { /* ignore */ }
                    resolve(null);
                }, hardMs);
            });
            try {
                warnTimer = setTimeout(() => {
                    try { console.warn('[AnimationHandler] Still preloading (holding previous animation):', actionName, animationFile); } catch (e) { /* ignore */ }
                }, softWarnMs);
            } catch (e) { /* ignore */ }

            const clip = await Promise.race([p, timeoutP]);
            try { if (warnTimer) clearTimeout(warnTimer); } catch (e) { /* ignore */ }
            return clip || null;
        } catch (e) {
            return null;
        }
    }

    _safeFadeStop(action, fadeSec = 0.2) {
        try {
            if (!action) return;
            try { this._activeActions && this._activeActions.delete(action); } catch (e) { /* ignore */ }
            // Only re-enable the action if it is currently running (or paused
            // mid-play).  Re-enabling a finished LoopOnce action can re-trigger
            // playback from t=0 on the next mixer update, causing a spurious
            // finished event and potential T-pose flicker.
            try {
                const isRunning = (typeof action.isRunning === 'function')
                    ? action.isRunning()
                    : (action.enabled && !action.paused);
                if (isRunning || action.paused) {
                    action.enabled = true;
                }
            } catch (e) { /* ignore */ }
            try { action.fadeOut(fadeSec); } catch (e) { /* ignore */ }
            setTimeout(() => {
                try {
                    action.stop();
                    action.reset();
                    try { action.enabled = false; } catch (e) { /* ignore */ }
                } catch (e) { /* ignore */ }
            }, Math.round(fadeSec * 1000) + 60);
        } catch (e) {
            /* ignore */
        }
    }

    /**
     * Stop every overlay action (everything except _baseIdleAction) with a smooth
     * fade-out.  Uses the mixer's internal _actions array to catch orphaned clips
     * that are no longer referenced by this.currentAction / currentStructuredAction.
     * Call this right before starting a new animation so no old pose can bleed through.
     */
    _stopAllOverlays(fadeSec = 0.3) {
        // Make sure baseIdle is at least minimally active before we fade others out.
        try {
            if (this._baseIdleAction) {
                this._baseIdleAction.enabled = true;
                if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                    // don't reduce existing weight; just enforce a floor
                    const cur = this._baseIdleAction.getEffectiveWeight
                        ? this._baseIdleAction.getEffectiveWeight()
                        : 0;
                    this._baseIdleAction.setEffectiveWeight(Math.max(cur, 0.15));
                }
            }
        } catch (e) { /* ignore */ }

        try {
            const baseIdle = this._baseIdleAction;
            const seen = new Set();

            // 1) Iterate mixer's own internal action list — catches orphaned clips.
            const mixerActions = (this.mixer && Array.isArray(this.mixer._actions))
                ? this.mixer._actions : [];
            for (const a of mixerActions) {
                try {
                    if (!a || a === baseIdle) continue;
                    seen.add(a);
                    this._safeFadeStop(a, fadeSec);
                } catch (e) { /* ignore */ }
            }

            // 2) Also flush the _activeActions Set (may include actions already fading).
            if (this._activeActions) {
                for (const a of this._activeActions) {
                    try {
                        if (!a || a === baseIdle || seen.has(a)) continue;
                        this._safeFadeStop(a, fadeSec);
                    } catch (e) { /* ignore */ }
                }
                this._activeActions.clear();
            }
        } catch (e) { /* ignore */ }
    }

    async _ensureBaseIdle(minWeight = 0.15, forceReload = false) {
        try {
            if (this._baseIdleAction && !forceReload) {
                try {
                    this._baseIdleAction.enabled = true;
                    this._baseIdleAction.setLoop(THREE.LoopRepeat);
                    this._baseIdleAction.clampWhenFinished = false;
                    if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                        // Apply weight immediately — DO NOT call fadeIn() here.
                        // fadeIn(t) resets the weight to 0 internally and ramps to 1
                        // over t seconds, which would leave the skeleton partially
                        // un-driven during that window and cause a visible T-pose.
                        this._baseIdleAction.setEffectiveWeight(minWeight);
                    }
                    this._baseIdleAction.play();
                } catch (e) { /* ignore */ }
                return;
            }

            // Replace base idle with a freshly chosen idle variant.
            // IMPORTANT: keep the previous base idle playing until the new one
            // is loaded and started (prevents transient T-pose/gaps between idles).
            // Do NOT clear this._baseIdleAction here! A concurrent #finished event might need it.
            const prevBaseIdle = this._baseIdleAction;

            const idleActionOrStructured = await this.loadAction('idle');
            if (!idleActionOrStructured) {
                // Since we didn't clear this._baseIdleAction, just verify it hasn't changed
                if (prevBaseIdle && this._baseIdleAction === prevBaseIdle) {
                    this._baseIdleAction = prevBaseIdle;
                    this._baseIdleKey = 'idle';
                    try {
                        prevBaseIdle.enabled = true;
                        prevBaseIdle.setLoop(THREE.LoopRepeat);
                        prevBaseIdle.clampWhenFinished = false;
                        if (typeof prevBaseIdle.setEffectiveWeight === 'function') {
                            prevBaseIdle.setEffectiveWeight(minWeight);
                        }
                        prevBaseIdle.fadeIn(0.2);
                        prevBaseIdle.play();
                    } catch (e) { /* ignore */ }
                }
                return;
            }

            let idleAction = idleActionOrStructured;
            if (idleActionOrStructured && idleActionOrStructured.intro && idleActionOrStructured.outro) {
                idleAction = idleActionOrStructured.loop || idleActionOrStructured.intro;
            }
            if (!idleAction) return;

            this._baseIdleAction = idleAction;
            this._baseIdleKey = 'idle';

            try {
                idleAction.enabled = true;
                idleAction.setLoop(THREE.LoopRepeat);
                idleAction.clampWhenFinished = false;
                idleAction.reset();
                if (typeof idleAction.setEffectiveWeight === 'function') {
                    idleAction.setEffectiveWeight(minWeight);
                }
                // Longer fade-in (0.35s) for smoother transitions to base idle
                idleAction.fadeIn(0.35).play();
            } catch (e) { /* ignore */ }

            // Only after the new base idle is in play, fade out the previous base.
            if (prevBaseIdle && prevBaseIdle !== idleAction) {
                this._safeFadeStop(prevBaseIdle, 0.35);
            }
        } catch (e) {
            console.warn('[AnimationHandler] Failed to ensure base idle:', e);
        }
    }

    async getAnimationsForType(actionName) {
        const skin = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
        const cacheKey = `${skin}:${actionName}`;

        const resolveCurrentList = () => {
            try {
                const reg = window.VRMAnimationMappings || {};
                const perSkin = (reg && typeof reg[skin] === 'object' && reg[skin] !== null) ? reg[skin] : null;
                const list = (perSkin && Array.isArray(perSkin[actionName])) ? perSkin[actionName]
                    : (Array.isArray(reg[actionName]) ? reg[actionName] : null);
                return Array.isArray(list) ? list : [];
            } catch (e) {
                return [];
            }
        };

        // 1) Prefer global registry overrides (skin-aware).
        try {
            const list = resolveCurrentList();
            if (Array.isArray(list) && list.length > 0) {
                animationMappingsLoaded.set(cacheKey, true);
                return list;
            }
        } catch (e) {
            // ignore registry lookup errors
        }

        // Return cached mappings if available
        if (animationMappingsLoaded.has(cacheKey)) {
            return resolveCurrentList();
        }

        try {
            console.log(`[AnimationHandler] Fetching animations for ${skin}/${actionName}`);
            const response = await fetch(`/api/animations/${skin}/${actionName}`);
            if (response.ok) {
                const data = await response.json();
                // Store in global registry (skin-aware) so plugins can read/extend.
                try {
                    window.VRMAnimationMappings = window.VRMAnimationMappings || {};
                    if (!window.VRMAnimationMappings[skin] || typeof window.VRMAnimationMappings[skin] !== 'object') {
                        window.VRMAnimationMappings[skin] = {};
                    }
                    window.VRMAnimationMappings[skin][actionName] = data.animations || [];
                } catch (e) {
                    // Fallback to local mapping object
                    animationMappings[actionName] = data.animations || [];
                }
                animationMappingsLoaded.set(cacheKey, true);
                const arr = resolveCurrentList();
                console.log(`[AnimationHandler] Loaded ${arr.length} animations for ${actionName}:`, arr);
                return arr;
            } else {
                console.warn(`[AnimationHandler] Failed to load animations: HTTP ${response.status}`);
                try {
                    window.VRMAnimationMappings = window.VRMAnimationMappings || {};
                    if (!window.VRMAnimationMappings[skin] || typeof window.VRMAnimationMappings[skin] !== 'object') {
                        window.VRMAnimationMappings[skin] = {};
                    }
                    window.VRMAnimationMappings[skin][actionName] = [];
                } catch (e) {
                    animationMappings[actionName] = [];
                }
            }
        } catch (error) {
            console.error(`[AnimationHandler] Error fetching animations:`, error);
            try {
                window.VRMAnimationMappings = window.VRMAnimationMappings || {};
                if (!window.VRMAnimationMappings[skin] || typeof window.VRMAnimationMappings[skin] !== 'object') {
                    window.VRMAnimationMappings[skin] = {};
                }
                window.VRMAnimationMappings[skin][actionName] = [];
            } catch (e) {
                animationMappings[actionName] = [];
            }
        }

        return resolveCurrentList();
    }

    async loadAnimation(actionName, animationFile) {
        console.log(`[AnimationHandler] loadAnimation called for ${actionName} with file ${animationFile}`);
        const normalizedFile = this._normalizeAnimationKey(animationFile);
        // Include actionName in the cache key so the same filename in different
        // state directories (e.g. write/Texting.fbx vs idle/Texting.fbx) doesn't
        // collide.  Fall back to filename-only lookup for backward compatibility.
        const cacheKey = actionName ? `${actionName}:${normalizedFile}` : normalizedFile;
        if (this.loadedAnimations[cacheKey]) {
            console.log(`[AnimationHandler] Using cached animation for ${cacheKey}`);
            return this.loadedAnimations[cacheKey];
        }
        // Also allow lookup by normalized filename for backward compatibility
        // (clips loaded before this fix used filename-only keys).
        if (this.loadedAnimations[normalizedFile]) {
            console.log(`[AnimationHandler] Using cached animation for ${normalizedFile}`);
            return this.loadedAnimations[normalizedFile];
        }
        // Also allow lookup by the original string for backward compatibility.
        if (animationFile && this.loadedAnimations[animationFile]) {
            console.log(`[AnimationHandler] Using cached animation for ${animationFile}`);
            return this.loadedAnimations[animationFile];
        }
        try {
            // Accept either a plain filename (resolved under /skins/<skin>/animations/<state>/)
            // or a full URL/path provided by the backend.
            let animPath = null;
            if (typeof animationFile === 'string' && (animationFile.includes('/') || animationFile.startsWith('http'))) {
                animPath = animationFile;
            } else {
                // Build path with action type subdirectory: /skins/{skin}/animations/{actionType}/{file}
                const skinName = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
                const encodedFile = encodeURIComponent(animationFile);
                animPath = `/skins/${skinName}/animations/${actionName}/${encodedFile}`;
            }
            console.log(`[AnimationHandler] Calling loadMixamoAnimation for ${animPath}`);
            console.log(`[AnimationHandler] loadMixamoAnimation function exists:`, typeof loadMixamoAnimation);
            // Load animation
            const clip = await loadMixamoAnimation(animPath, this.vrm);
            console.log(`[AnimationHandler] loadMixamoAnimation returned clip:`, !!clip);
            if (clip) {
                // Cache by state-scoped key, normalized filename, and original string.
                this.loadedAnimations[cacheKey] = clip;
                if (normalizedFile !== cacheKey) {
                    this.loadedAnimations[normalizedFile] = clip;
                }
                if (animationFile && animationFile !== cacheKey && animationFile !== normalizedFile) {
                    this.loadedAnimations[animationFile] = clip;
                }
                console.log(`[AnimationHandler] Animation ${cacheKey} cached successfully`);
            }
            return clip;
        } catch (error) {
            console.error(`[AnimationHandler] Failed to load ${animationFile}:`, error);
            return null;
        }
    }

    async loadDescriptor(actionName, animationFile) {
        console.log(`[AnimationHandler] loadDescriptor called for ${actionName}/${animationFile}`);
        try {
            const cleanAnim = (typeof animationFile === 'string') ? animationFile.split('?')[0].split('#')[0] : animationFile;
            // Accept either filename (resolved under /skins/<skin>/animations/<state>/)
            // or full URL/path (descriptor expected at `<anim>.json`).
            let descriptorPath = null;
            if (typeof cleanAnim === 'string' && (cleanAnim.includes('/skins/') || cleanAnim.startsWith('/skins/'))) {
                const skinName = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
                const fileName = cleanAnim.split('/').pop();
                const encodedFile = encodeURIComponent(String(fileName || '') + '.json');
                descriptorPath = `/api/skins/${skinName}/animations/${actionName}/${encodedFile}`;
            } else if (typeof cleanAnim === 'string' && (cleanAnim.includes('/') || cleanAnim.startsWith('http'))) {
                descriptorPath = `${cleanAnim}.json`;
            } else {
                // Prefer API endpoint for descriptors. The API will return
                // the on-disk descriptor if present or an implicit descriptor
                // when the .json file is missing (avoids client-side 404s).
                const skinName = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
                const encodedFile = encodeURIComponent(String(cleanAnim) + '.json');
                descriptorPath = `/api/skins/${skinName}/animations/${actionName}/${encodedFile}`;
            }
            // Cache descriptors (including null when missing) to avoid repeated 404 fetches.
            if (descriptorPath && Object.prototype.hasOwnProperty.call(this.loadedDescriptors, descriptorPath)) {
                return this.loadedDescriptors[descriptorPath];
            }

            console.log(`[AnimationHandler] Fetching descriptor from ${descriptorPath}`);
            const response = await fetch(descriptorPath);
            if (!response.ok) {
                // Missing descriptor: return null so the caller's playOnce/loop parameter is
                // the sole authority. An implicit play_once:true would override the server's
                // loop:true for write/talk and cause unwanted clamping.
                this.loadedDescriptors[descriptorPath] = null;
                return null;
            }

            try {
                const descriptor = await response.json();
                this.loadedDescriptors[descriptorPath] = descriptor;
                console.log(`[AnimationHandler] Loaded descriptor for ${animationFile}:`, descriptor);
                return descriptor;
            } catch (err) {
                // Malformed JSON: log, cache null and continue
                console.warn(`[AnimationHandler] Descriptor JSON malformed for ${animationFile}:`, err);
                this.loadedDescriptors[descriptorPath] = null;
                return null;
            }
        } catch (error) {
            console.warn(`[AnimationHandler] Failed to load descriptor for ${animationFile}:`, error);
            // On network errors, do not cache so the next request retries.
            return null;
        }
    }

    async loadAction(actionName) {
        let files = await this.getAnimationsForType(actionName);
        if (!files || files.length === 0) {
            // Safety net: if mapping/cache is stale or a race cleared entries,
            // force-refresh from API and repopulate mappings before giving up.
            try {
                const primarySkin = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
                const candidateSkins = Array.from(new Set([primarySkin, 'Rei'].filter(Boolean)));
                for (const skinName of candidateSkins) {
                    try {
                        const resp = await fetch(`/api/animations/${encodeURIComponent(skinName)}/${encodeURIComponent(actionName)}`);
                        if (!resp.ok) continue;
                        const payload = await resp.json();
                        const forced = (payload && Array.isArray(payload.animations)) ? payload.animations : [];
                        if (forced.length > 0) {
                            window.VRMAnimationMappings = window.VRMAnimationMappings || {};
                            if (!window.VRMAnimationMappings[skinName] || typeof window.VRMAnimationMappings[skinName] !== 'object') {
                                window.VRMAnimationMappings[skinName] = {};
                            }
                            window.VRMAnimationMappings[skinName][actionName] = forced;
                            files = forced;
                            console.log(`[AnimationHandler] Forced refresh recovered ${forced.length} animations for ${actionName} from ${skinName}`);
                            break;
                        }
                    } catch (e) { /* ignore */ }
                }
            } catch (e) { /* ignore */ }
        }

        if (!files || files.length === 0) {
            console.log(`[AnimationHandler] No animations found for ${actionName}`);
            return null;
        }

        // IDLE is the baseline layer. To avoid any uncovered window (and resulting T-pose),
        // we randomize IDLE animations in blocks of two:
        // - current: used now
        // - next: preloaded in background for the next refresh/switch
        // This way, switching IDLE variants does not depend on immediate IO.
        if (actionName === 'idle') {
            try {
                if (!this._idleQueue) this._idleQueue = { currentFile: null, nextFile: null };
                const pickRandom = (arr, avoid) => {
                    if (!Array.isArray(arr) || arr.length === 0) return null;
                    if (arr.length === 1) return arr[0];
                    let tries = 0;
                    while (tries++ < 10) {
                        const f = arr[Math.floor(Math.random() * arr.length)];
                        if (f && f !== avoid) return f;
                    }
                    // fallback
                    return arr[0] !== avoid ? arr[0] : arr[1];
                };

                // Initialize queue
                if (!this._idleQueue.currentFile) {
                    this._idleQueue.currentFile = pickRandom(files, null);
                }
                if (!this._idleQueue.nextFile) {
                    this._idleQueue.nextFile = pickRandom(files, this._idleQueue.currentFile);
                }

                const selectedFile = this._idleQueue.currentFile;
                const nextFile = this._idleQueue.nextFile;
                console.log(`[AnimationHandler] IDLE queue: current=${selectedFile}, next=${nextFile}`);

                // Preload NEXT in background (clip + descriptor) so the next swap is instant.
                try {
                    if (nextFile) {
                        this._awaitAnimationReady('idle', nextFile, 20000);
                        // Descriptor fetch is also prewarmed; never awaited here.
                        this.loadDescriptor('idle', nextFile).catch(() => null);
                    }
                } catch (e) { /* ignore */ }

                // Load ONLY the selected (current) clip, not every idle clip.
                const clip = await this.loadAnimation('idle', selectedFile);
                if (!clip) return null;

                // Also prefetch descriptor for CURRENT (non-blocking refinement happens below).
                let descriptor = null;
                let descriptorPromise = null;
                try {
                    if (selectedFile) {
                        descriptorPromise = this.loadDescriptor('idle', selectedFile)
                            .then((d) => {
                                console.log(`[AnimationHandler] After loading descriptor for ${selectedFile}, descriptor is:`, d);
                                return d;
                            })
                            .catch((err) => {
                                console.warn(`[AnimationHandler] Could not load descriptor for ${selectedFile}:`, err);
                                return null;
                            });
                    }
                } catch (e) {
                    descriptorPromise = null;
                }

                // (The existing IDLE branch below will create an immediate loop and refine from descriptor)
                // Reuse variables expected by the remainder of this function.
                // NOTE: keep names consistent with the existing code.
                const clips = [clip];
                const fileMap = [selectedFile];
                // Fall through into the existing logic by shadowing the variables it uses.
                // eslint-disable-next-line no-unused-vars
                const clipIndex = 0;
                // eslint-disable-next-line no-unused-vars
                const selectedFileShadow = selectedFile;
                // Provide variables for the downstream code without rewriting large sections.
                // We'll return early by jumping into the existing IDLE creation block below.

                // Shift queue for next time *after* we have a playable current.
                try {
                    this._idleQueue.currentFile = nextFile || selectedFile;
                    this._idleQueue.nextFile = pickRandom(files, this._idleQueue.currentFile);
                } catch (e) { /* ignore */ }

                // The code below expects `clip` and `selectedFile` vars.
                // We can't literally jump, but we can set them in-place.
                // eslint-disable-next-line no-var
                var __idle_clip = clip;
                // eslint-disable-next-line no-var
                var __idle_selectedFile = selectedFile;
                // eslint-disable-next-line no-var
                var __idle_descriptorPromise = descriptorPromise;

                // Create the immediate IDLE action + background refinement (inline minimal duplication)
                // IDLE is the baseline animation: it should start immediately.
                // We always loop, but we allow skins to ship a descriptor with an explicit loop-range.
                // IMPORTANT: do NOT await descriptor here; create a safe full-clip loop first.
                let idleAction = null;
                const storageKey = __idle_selectedFile ? `idle:${__idle_selectedFile}` : 'idle';
                try {
                    idleAction = this.mixer.clipAction(__idle_clip);
                    idleAction.setLoop(THREE.LoopRepeat);
                    idleAction.clampWhenFinished = false;
                    // Ensure the underlying clip has a useful name
                    try {
                        const clipName = __idle_selectedFile || (__idle_clip && __idle_clip.name) || `idle`;
                        const c = idleAction.getClip ? idleAction.getClip() : __idle_clip;
                        if (c && c.name !== clipName) {
                            try { c.name = clipName; } catch (e) { /* ignore */ }
                        }
                    } catch (e) { /* ignore */ }

                    this.actions[storageKey] = idleAction;
                    this.actions['idle'] = idleAction;
                    console.log(`[AnimationHandler] Stored IDLE action (immediate loop) with keys: ${storageKey} AND idle`);
                } catch (e) {
                    console.warn('[AnimationHandler] Failed to create immediate IDLE action:', e);
                    return null;
                }

                // Background refinement: if descriptor defines a loop-range, rebuild the idle clip and swap caches.
                try {
                    const currentToken = (this._idleRefineToken = (this._idleRefineToken || 0) + 1);
                    if (__idle_descriptorPromise) {
                        __idle_descriptorPromise.then((d) => {
                            try {
                                if (this._idleRefineToken !== currentToken) return;
                                descriptor = d;
                                if (!descriptor || !descriptor.loop) return;
                                if (!__idle_clip || !__idle_clip.duration || !THREE.AnimationUtils || typeof THREE.AnimationUtils.subclip !== 'function') return;

                                const fps = (d && typeof d.fps === 'number' && d.fps > 0) ? d.fps : 30;
                                const totalFrames = Math.max(2, Math.round(__idle_clip.duration * fps));
                                const clampInt = (v, lo, hi) => {
                                    const n = Math.floor(Number(v));
                                    if (!Number.isFinite(n)) return lo;
                                    return Math.max(lo, Math.min(hi, n));
                                };
                                const normalizeRange = (start, end, label) => {
                                    const s = clampInt(start, 0, totalFrames);
                                    const e = clampInt(end, 0, totalFrames + 1);
                                    if (e <= s + 1) {
                                        throw new Error(`[AnimationHandler] Invalid ${label} range: ${start}-${end} (normalized ${s}-${e}) totalFrames=${totalFrames}`);
                                    }
                                    return { start: s, end: e };
                                };

                                const loopStart = descriptor.loop?.start_frame ?? 0;
                                // Descriptors use inclusive end_frame; subclip() expects exclusive. Add +1.
                                const loopEnd = (descriptor.loop?.end_frame ?? (totalFrames - 1)) + 1;
                                const loopR = normalizeRange(loopStart, loopEnd, 'idle.loop');
                                const loopClip = THREE.AnimationUtils.subclip(__idle_clip, `${storageKey}_idle_loop`, loopR.start, loopR.end, fps);
                                loopClip.loop = THREE.LoopRepeat;
                                const refined = this.mixer.clipAction(loopClip);
                                refined.setLoop(THREE.LoopRepeat);
                                refined.clampWhenFinished = false;

                                this.actions[storageKey] = refined;
                                this.actions['idle'] = refined;
                                console.log(`[AnimationHandler] Refined IDLE loop from descriptor for ${storageKey}: frames ${loopR.start}-${loopR.end}`);

                                try {
                                    if (this._baseIdleAction && this._baseIdleAction === idleAction) {
                                        const w = (typeof idleAction.getEffectiveWeight === 'function') ? idleAction.getEffectiveWeight() : 1.0;
                                        refined.enabled = true;
                                        refined.reset();
                                        if (typeof refined.setEffectiveWeight === 'function' && Number.isFinite(w)) refined.setEffectiveWeight(w);
                                        refined.fadeIn(0.25).play();
                                        this._safeFadeStop(idleAction, 0.25);
                                        this._baseIdleAction = refined;
                                    }
                                } catch (e) { /* ignore */ }
                            } catch (e) {
                                console.warn('[AnimationHandler] Failed to refine IDLE from descriptor:', e);
                            }
                        });
                    }
                } catch (e) { /* ignore */ }

                return idleAction;
            } catch (e) {
                console.warn('[AnimationHandler] IDLE queue logic failed, falling back to legacy:', e);
                // Fall through to legacy behavior below.
            }
        }

        const clips = [];
        const fileMap = []; // Track which file corresponds to which clip
        for (const file of files) {
            const clip = await this.loadAnimation(actionName, file);
            if (clip) {
                clips.push(clip);
                fileMap.push(file);
            }
        }

        if (clips.length === 0) return null;

        // Create action from random clip if multiple
        const clipIndex = Math.floor(Math.random() * clips.length);
        const clip = clips[clipIndex];
        const selectedFile = fileMap[clipIndex]; // Get the correct file for the selected clip

        // Descriptor loading can be slow (network/file IO). For IDLE we must not block playback,
        // otherwise the avatar can briefly fall back to rest pose (T-pose) while no clip is active.
        // Strategy:
        // - Start IDLE immediately with a safe full-clip loop.
        // - Load descriptor in background; if it defines a loop-range, refine/swap the cached idle.
        let descriptor = null;
        let descriptorPromise = null;
        try {
            if (selectedFile) {
                descriptorPromise = this.loadDescriptor(actionName, selectedFile)
                    .then((d) => {
                        console.log(`[AnimationHandler] After loading descriptor for ${selectedFile}, descriptor is:`, d);
                        return d;
                    })
                    .catch((err) => {
                        console.warn(`[AnimationHandler] Could not load descriptor for ${selectedFile}:`, err);
                        return null;
                    });
            }
        } catch (e) {
            descriptorPromise = null;
        }

        // IDLE is the baseline animation: it should start immediately.
        // We always loop, but we allow skins to ship a descriptor with an explicit loop-range.
        // IMPORTANT: do NOT await descriptor here; create a safe full-clip loop first.
        if (actionName === 'idle') {
            let idleAction = null;
            const storageKey = selectedFile ? `${actionName}:${selectedFile}` : actionName;
            try {
                idleAction = this.mixer.clipAction(clip);
                idleAction.setLoop(THREE.LoopRepeat);
                idleAction.clampWhenFinished = false;
                // Ensure the underlying clip has a useful name
                try {
                    const clipName = selectedFile || (clip && clip.name) || `${actionName}`;
                    const c = idleAction.getClip ? idleAction.getClip() : clip;
                    if (c && c.name !== clipName) {
                        try { c.name = clipName; } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }

                this.actions[storageKey] = idleAction;
                this.actions[actionName] = idleAction;
                console.log(`[AnimationHandler] Stored IDLE action (immediate loop) with keys: ${storageKey} AND ${actionName}`);
            } catch (e) {
                console.warn('[AnimationHandler] Failed to create immediate IDLE action:', e);
                return null;
            }

            // Background refinement: if descriptor defines a loop-range, rebuild the idle clip and swap caches.
            try {
                const currentToken = (this._idleRefineToken = (this._idleRefineToken || 0) + 1);
                if (descriptorPromise) {
                    descriptorPromise.then((d) => {
                        try {
                            // Only refine if we're still on the same idle load cycle
                            if (this._idleRefineToken !== currentToken) return;
                            descriptor = d;
                            if (!descriptor || !descriptor.loop) return;
                            if (!clip || !clip.duration || !THREE.AnimationUtils || typeof THREE.AnimationUtils.subclip !== 'function') return;

                            const fps = (descriptor && typeof descriptor.fps === 'number' && descriptor.fps > 0) ? descriptor.fps : 30;
                            const totalFrames = Math.max(2, Math.round(clip.duration * fps));
                            const clampInt = (v, lo, hi) => {
                                const n = Math.floor(Number(v));
                                if (!Number.isFinite(n)) return lo;
                                return Math.max(lo, Math.min(hi, n));
                            };
                            const normalizeRange = (start, end, label) => {
                                const s = clampInt(start, 0, totalFrames);
                                const e = clampInt(end, 0, totalFrames + 1);
                                if (e <= s + 1) {
                                    throw new Error(`[AnimationHandler] Invalid ${label} range: ${start}-${end} (normalized ${s}-${e}) totalFrames=${totalFrames}`);
                                }
                                return { start: s, end: e };
                            };

                            const loopStart = descriptor.loop?.start_frame ?? 0;
                            // Descriptors use inclusive end_frame; subclip() expects exclusive. Add +1.
                            const loopEnd = (descriptor.loop?.end_frame ?? (totalFrames - 1)) + 1;
                            const loopR = normalizeRange(loopStart, loopEnd, 'idle.loop');
                            const loopClip = THREE.AnimationUtils.subclip(clip, `${storageKey}_idle_loop`, loopR.start, loopR.end, fps);
                            loopClip.loop = THREE.LoopRepeat;
                            const refined = this.mixer.clipAction(loopClip);
                            refined.setLoop(THREE.LoopRepeat);
                            refined.clampWhenFinished = false;

                            // Swap caches
                            this.actions[storageKey] = refined;
                            this.actions[actionName] = refined;
                            console.log(`[AnimationHandler] Refined IDLE loop from descriptor for ${storageKey}: frames ${loopR.start}-${loopR.end}`);

                            // If the base idle is currently this action, crossfade to refined.
                            try {
                                if (this._baseIdleAction && this._baseIdleAction === idleAction) {
                                    const w = (typeof idleAction.getEffectiveWeight === 'function') ? idleAction.getEffectiveWeight() : 1.0;
                                    refined.enabled = true;
                                    refined.reset();
                                    if (typeof refined.setEffectiveWeight === 'function' && Number.isFinite(w)) refined.setEffectiveWeight(w);
                                    refined.fadeIn(0.25).play();
                                    this._safeFadeStop(idleAction, 0.25);
                                    this._baseIdleAction = refined;
                                }
                            } catch (e) { /* ignore */ }
                        } catch (e) {
                            console.warn('[AnimationHandler] Failed to refine IDLE from descriptor:', e);
                        }
                    });
                }
            } catch (e) { /* ignore */ }

            return idleAction;
        }

        // For non-idle states, await the descriptor now (FBX is already loaded so this is fast).
        // Without this, descriptor is always null at the hasStructuredDescriptor check below,
        // meaning write/talk/etc with intro/loop/outro descriptors never get their structure built.
        if (descriptorPromise) {
            try { descriptor = await descriptorPromise; } catch (e) { descriptor = null; }
        }

        // Check if we should create structured animations (intro/loop/outro)
        // This can be for 'think' state or for any animation with intro/outro in descriptor
        const hasStructuredDescriptor = descriptor && descriptor.intro && descriptor.outro;
        console.log(`[AnimationHandler] For ${actionName}/${selectedFile}: hasStructuredDescriptor=${hasStructuredDescriptor}, actionName==='think' is ${actionName === 'think'}, descriptor=${descriptor ? JSON.stringify(descriptor) : 'null'}`);
        if ((actionName === 'think' || hasStructuredDescriptor) && clip && clip.duration && THREE.AnimationUtils && typeof THREE.AnimationUtils.subclip === 'function') {
            try {
                const fps = (descriptor && typeof descriptor.fps === 'number' && descriptor.fps > 0) ? descriptor.fps : 30;
                const totalFrames = Math.max(2, Math.round(clip.duration * fps));

                const clampInt = (v, lo, hi) => {
                    const n = Math.floor(Number(v));
                    if (!Number.isFinite(n)) return lo;
                    return Math.max(lo, Math.min(hi, n));
                };

                const normalizeRange = (start, end, label) => {
                    const s = clampInt(start, 0, totalFrames);
                    // Allow end up to totalFrames + 1 for exclusive endpoint (inclusive + 1)
                    const e = clampInt(end, 0, totalFrames + 1);
                    // subclip expects end > start; require at least 2 frames to avoid instant-finish loops
                    if (e <= s + 1) {
                        throw new Error(`[AnimationHandler] Invalid ${label} range: ${start}-${end} (normalized ${s}-${e}) totalFrames=${totalFrames}`);
                    }
                    return { start: s, end: e };
                };

                // Check if descriptor has a loop section defined
                const hasLoopSection = descriptor && descriptor.loop;

                // Extract frames from descriptor or use defaults
                let introStart, introEnd, loopStart, loopEnd, outroStart, outroEnd;

                if (hasStructuredDescriptor) {
                    // Use descriptor-defined frames.
                    // Descriptors use inclusive end_frame; subclip() expects exclusive endFrame.
                    // Convert: (inclusive ?? fallback_inclusive) + 1  →  exclusive.
                    introStart = descriptor.intro?.start_frame ?? 0;
                    introEnd = (descriptor.intro?.end_frame ?? (totalFrames - 1)) + 1;

                    if (hasLoopSection) {
                        // Animation has intro/loop/outro structure
                        loopStart = descriptor.loop?.start_frame ?? introEnd;
                        loopEnd = (descriptor.loop?.end_frame ?? (totalFrames - 1)) + 1;
                        outroStart = descriptor.outro?.start_frame ?? loopEnd;
                        outroEnd = (descriptor.outro?.end_frame ?? (totalFrames - 1)) + 1;
                    } else {
                        // Animation has only intro/outro structure (play_once animation)
                        // No loop section - outro starts right after intro
                        loopStart = null;
                        loopEnd = null;
                        outroStart = descriptor.outro?.start_frame ?? introEnd;
                        outroEnd = (descriptor.outro?.end_frame ?? (totalFrames - 1)) + 1;
                    }
                } else {
                    // Default split for 'think' state (always has loop)
                    introStart = 0;
                    introEnd = Math.max(1, Math.round(totalFrames * 0.20));
                    loopStart = introEnd;
                    loopEnd = Math.max(1, Math.round(totalFrames * 0.80));
                    outroStart = loopEnd;
                    outroEnd = totalFrames;
                }

                // Validate & clamp ranges; if invalid, fall back to full clip.
                // End values are now exclusive (inclusive + 1); normalizeRange allows up to totalFrames + 1.
                const introR = normalizeRange(introStart, introEnd, 'intro');
                const outroR = normalizeRange(outroStart, outroEnd, 'outro');
                introStart = introR.start;
                introEnd = introR.end;
                outroStart = outroR.start;
                outroEnd = outroR.end;
                if (loopStart !== null && loopEnd !== null) {
                    const loopR = normalizeRange(loopStart, loopEnd, 'loop');
                    loopStart = loopR.start;
                    loopEnd = loopR.end;
                }

                // Use selectedFile in clip names so they match the storage key in mixer finished handler
                const clipKeyBase = selectedFile ? `${actionName}:${selectedFile}` : actionName;
                const introClip = THREE.AnimationUtils.subclip(clip, `${clipKeyBase}_intro`, introStart, introEnd, fps);
                const outroClip = THREE.AnimationUtils.subclip(clip, `${clipKeyBase}_outro`, outroStart, outroEnd, fps);

                const introAction = this.mixer.clipAction(introClip);
                const outroAction = this.mixer.clipAction(outroClip);

                introAction.setLoop(THREE.LoopOnce, 0);
                introAction.clampWhenFinished = true;
                outroAction.setLoop(THREE.LoopOnce, 0);
                // clampWhenFinished = true keeps the outro's last-frame pose
                // until we manually fade it out, preventing the T-pose gap that
                // occurs when Three.js zeroes the weight before our finished handler fires.
                outroAction.clampWhenFinished = true;

                // Create structured action object
                const structuredAction = {
                    intro: introAction,
                    outro: outroAction,
                    _meta: { source: clip.name || selectedFile || 'structured_clip', descriptor: descriptor }
                };

                // Only create loop section if it exists in the descriptor or is default 'think'
                if (loopStart !== null && loopEnd !== null) {
                    const loopClip = THREE.AnimationUtils.subclip(clip, `${clipKeyBase}_loop`, loopStart, loopEnd, fps);
                    // Attach loop frame metadata so we can verify during playback
                    try {
                        loopClip._meta = loopClip._meta || {};
                        loopClip._meta.loopFrames = { startFrame: loopStart, endFrame: loopEnd, fps };
                    } catch (e) { /* ignore metadata attach errors */ }
                    const loopAction = this.mixer.clipAction(loopClip);
                    loopAction.setLoop(THREE.LoopRepeat);
                    loopAction.clampWhenFinished = false;
                    structuredAction.loop = loopAction;
                    console.log(`[AnimationHandler] Created loop section for ${clipKeyBase}: frames ${loopStart}-${loopEnd}, time ${loopStart / fps}s-${loopEnd / fps}s, setLoop(LoopRepeat)`);
                } else {
                    // Mark this as a play_once only animation (no loop section)
                    structuredAction.loop = null;
                    structuredAction._playOnceOnly = true;
                }

                // Store with file-specific key if we know the file
                const storageKey = selectedFile ? `${actionName}:${selectedFile}` : actionName;
                this.actions[storageKey] = structuredAction;
                // Avoid caching a play_once-only idle as the GENERIC 'idle' key: that would cause
                // the UI to reuse the play_once animation as the default and potentially loop it.
                if (selectedFile && actionName !== 'think') {
                    if (actionName === 'idle' && structuredAction._playOnceOnly) {
                        console.log(`[AnimationHandler] Not caching playOnce-only idle as generic '${actionName}': ${storageKey}`);
                    } else {
                        // For idle/talk/etc with specific file, also cache the generic key
                        this.actions[actionName] = structuredAction;
                        console.log(`[AnimationHandler] Stored structured action with keys: ${storageKey} AND ${actionName}, playOnceOnly: ${structuredAction._playOnceOnly}`);
                    }
                } else {
                    console.log(`[AnimationHandler] Stored structured action with key: ${storageKey}, playOnceOnly: ${structuredAction._playOnceOnly}`);
                }
                return structuredAction;
            } catch (err) {
                console.warn(`[AnimationHandler] Failed to create structured subclips, falling back to full clip: ${err}`);
                // Fall through to create single action below
            }
        }

        // Default: create a single looping action
        const action = this.mixer.clipAction(clip);
        action.setLoop(THREE.LoopRepeat);
        action.clampWhenFinished = false;
        // Ensure the underlying clip has a useful name (selectedFile may be null)
        try {
            const clipName = selectedFile || (clip && clip.name) || `${actionName}`;
            const c = action.getClip ? action.getClip() : clip;
            if (c && c.name !== clipName) {
                try { c.name = clipName; } catch (e) { /* ignore */ }
            }
        } catch (e) { /* ignore */ }
        const storageKey = selectedFile ? `${actionName}:${selectedFile}` : actionName;
        this.actions[storageKey] = action;
        console.log(`[AnimationHandler] Stored simple action with key: ${storageKey}`);
        return action;
    }

    // Preload all animations across common action types (idle, think, talk, write, touch)
    // Loads each animation clip and caches it in this.loadedAnimations to avoid
    // re-fetching and momentary T-pose when switching clips.
    // NOTE: Only preloads clips, NOT actions. Actions are created by startAction() with proper structure logic.
    async preloadAllAnimations() {
        try {
            // Preload only states we know about from the global registry.
            // Always include 'idle' as the only guaranteed built-in.
            const reg = window.VRMAnimationMappings || {};
            const skin = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
            const perSkin = (reg && typeof reg[skin] === 'object' && reg[skin] !== null) ? reg[skin] : null;
            const keys = perSkin ? Object.keys(perSkin) : Object.keys(reg);
            const actionTypes = Array.from(new Set(['idle', ...keys]));
            for (const at of actionTypes) {
                try {
                    const files = await this.getAnimationsForType(at);
                    if (!files || files.length === 0) continue;
                    console.log(`[AnimationHandler] Preloading ${files.length} animation clips for ${at}`);
                    for (const f of files) {
                        try {
                            if (!this.loadedAnimations[f]) {
                                const clip = await this.loadAnimation(at, f);
                                if (clip) {
                                    console.log(`[AnimationHandler] Preloaded clip for ${at}/${f}`);
                                    // Just load the clip, don't create actions yet
                                    // Actions will be created by startAction() which handles structure properly
                                }
                            }
                        } catch (e) {
                            console.warn(`[AnimationHandler] Failed to preload ${f} for ${at}:`, e);
                        }
                    }
                } catch (e) {
                    console.warn(`[AnimationHandler] Error listing animations for ${at}:`, e);
                }
            }
        } catch (err) {
            console.warn('[AnimationHandler] preloadAllAnimations top-level error:', err);
        }
    }

    startAction(actionName, animationFile = null, playOnce = false, playSection = null, descriptorOverride = null) {
        // Queue to prevent concurrent state switches (fixes transient T-pose and out-of-order visual transitions).
        this._startActionChain = (this._startActionChain || Promise.resolve())
            .then(() => this._startActionInternal(actionName, animationFile, playOnce, playSection, descriptorOverride))
            .catch((err) => {
                console.warn('[AnimationHandler] startAction chain error:', err);
            });
        return this._startActionChain;
    }

    async _startActionInternal(actionName, animationFile = null, playOnce = false, playSection = null, descriptorOverride = null) {
        console.log(`[AnimationHandler] startAction called with actionName: ${actionName}, animationFile: ${animationFile}, playOnce: ${playOnce}, playSection: ${playSection}`);
        console.log(`[AnimationHandler] this.mixer exists:`, !!this.mixer);
        console.log(`[AnimationHandler] this.vrm exists:`, !!this.vrm);
        this._pendingRequestedAction = { actionName, animationFile, playOnce, playSection, descriptorOverride };

        // If we got a descriptorOverride but no explicit rich animation_state, apply a minimal
        // state so expression/blink configs are consistent across transitions.
        try {
            const desc = descriptorOverride || null;
            const hasRichFromDesc = !!(desc && (desc.expressions || desc.blink || desc.eye_movement || (typeof desc.lipsync === 'boolean')));
            if (hasRichFromDesc && typeof this.applyAnimationState === 'function') {
                const phase = (playSection != null) ? playSection : (playOnce ? 'clip' : 'loop');
                const fps = (desc && (typeof desc.fps === 'number' || typeof desc.fps === 'string')) ? Number(desc.fps) : 30;
                const st = {
                    action: (actionName || '').toString().toLowerCase(),
                    phase,
                    animation: animationFile || null,
                    descriptor: desc,
                    clip: { fps: (Number.isFinite(fps) && fps > 0) ? fps : 30 },
                    timing: { started_at: new Date().toISOString(), time_in_clip: 0.0, current_frame: 0 },
                    expressions: Array.isArray(desc.expressions) ? desc.expressions : null,
                    blink: (desc && typeof desc.blink === 'object') ? desc.blink : null,
                    eye_movement: (desc && typeof desc.eye_movement === 'object') ? desc.eye_movement : null,
                    lipsync: !!(desc && desc.lipsync),
                    source: 'startAction_descriptor'
                };
                this.applyAnimationState(st);
            }
        } catch (e) { /* ignore */ }

        // Cancel any pending post-outro idle fallback as a new action is starting.
        try {
            if (this._postOutroIdleTimer) {
                clearTimeout(this._postOutroIdleTimer);
                this._postOutroIdleTimer = null;
            }
            this._postOutroIdleToken = (this._postOutroIdleToken || 0) + 1;
        } catch (e) { /* ignore */ }

        // GUARANTEE: make sure the base-idle layer is active and strong **before**
        // we begin any loading or tearing down of the previous action.  This is the
        // last line of defence against T-pose; even if the current clip ends while
        // we're waiting for a new one, the skeleton will still be driven by idle.
        try {
            await this._ensureBaseIdle(1.0, false);
        } catch (e) { /* ignore */ }

        // Ensure eyes are open when starting a new non-think action to avoid lingering closed lids
        try {
            const a = (actionName || '').toString().toLowerCase();
            if (a !== 'think') {
                try { this._forceOpenEyes(); } catch (e) { }
            }
        } catch (e) { }

        // Reset expression state to clean up residuals from previous animation
        // This ensures blendshapes from the old animation don't linger during transitions
        try {
            if (!this._expressionState) this._expressionState = {};
            // Decay all current expression values to zero over next frame
            // Rather than instant reset, gradual decay prevents abrupt visual pops
            Object.keys(this._expressionState).forEach(k => {
                if (this._expressionState[k] > 0.01) {
                    this._expressionState[k] *= 0.1; // Quick decay to near-zero
                } else {
                    this._expressionState[k] = 0;
                }
            });
        } catch (e) { /* ignore */ }

        // Missing tracks are common in Mixamo clips; keep a low-weight idle baseline
        // so unkeyed bones don't stay in previous poses.
        // When entering IDLE (no specific file), refresh the baseline and boost it to full weight.
        if (actionName === 'idle' && !playSection && !animationFile) {
            // Clear expressions when returning to idle to prevent the previous state's
            // face pose (e.g., THINK eyes_closed) from persisting.
            try {
                this._clearEyesState();
                if (this._expressionState) this._expressionState = {};
                if (typeof this._forceOpenEyes === 'function') this._forceOpenEyes();
                // Ensure autonomous blink/eye movement can resume in idle.
                if (this._blinkAutoEnabled && !this._blinkLoopRunning) { try { this._startBlinkLoop(); } catch (e) { } }
                if (this._eyeAutoEnabled && !this._eyeLoopRunning) { try { this._startEyeMovement(); } catch (e) { } }
                // Replace last rich state with an idle-ish empty expression state.
                this._lastAnimationState = { action: 'idle', phase: 'loop', expressions: [] };
            } catch (e) { /* ignore */ }
            await this._ensureBaseIdle(1.0, true);
            // Stop ALL overlay actions (including any orphaned clips) with a smooth fade.
            this._stopAllOverlays(0.35);
            this.currentAction = null;
            this.currentActionName = 'idle';
            this.currentActionPhase = null;
            this.currentStructuredAction = null;
            this._currentAnimationFile = null;
            return;
        }

        // If we're attempting to start a new action while THINK was very recently started,
        // enforce a minimum visible duration for THINK to prevent it being immediately preempted.
        try {
            const now = Date.now();
            const minUntil = this._minActionVisibleUntil || 0;
            if ((this.currentActionName === 'think' || String(actionName || '').toLowerCase() !== 'think') && now < minUntil && String(actionName || '').toLowerCase() !== 'think') {
                const waitMs = Math.max(0, minUntil - now);
                console.debug('[AnimationHandler] Delaying startAction due to think minimum visibility, waiting', waitMs, 'ms');
                await new Promise((res) => setTimeout(res, waitMs));
            }
        } catch (e) { /* ignore */ }

        if (actionName !== 'idle') {
            // Keep base idle at low weight (0.05 = 5%) as fallback skeleton during non-idle animations.
            // This prevents T-pose gaps if the main animation fails or when transitioning.
            await this._ensureBaseIdle(0.12, false);
        }

        // If a specific file was requested, wait for its preload/load BEFORE
        // we start fading out the current action. This eliminates transient
        // "no clip driving bones" gaps and reduces skipped WRITE.
        // During this entire period the base-idle has been boosted to full weight
        // above (see guarantee comment), so even if the previous action finishes
        // the avatar will never fall back to a visible T-pose.
        try {
            if (animationFile) {
                const clipReady = await this._awaitAnimationReady(actionName, animationFile, 8000);
                if (!clipReady) {
                    console.warn('[AnimationHandler] Cannot play yet (preload still pending or failed). Keeping current animation and scheduling late recovery:', actionName, animationFile);
                    // If nothing is currently driving the skeleton, boost base idle as a safe fallback.
                    try {
                        if (!this.currentAction && !this.currentStructuredAction && this._baseIdleAction) {
                            this._baseIdleAction.enabled = true;
                            this._baseIdleAction.setLoop(THREE.LoopRepeat);
                            this._baseIdleAction.clampWhenFinished = false;
                            if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                this._baseIdleAction.setEffectiveWeight(1.0);
                            }
                            this._baseIdleAction.fadeIn(0.2);
                            this._baseIdleAction.play();
                        }
                    } catch (_e) { /* ignore */ }

                    // Keep the previous animation alive and auto-retry this action once the clip finishes loading.
                    try {
                        const normalizedKey = this._normalizeAnimationKey(animationFile);
                        const pending = this._preloadPromises[normalizedKey] || this._preloadPromises[animationFile] || null;
                        if (pending && !this._lateRecoveryTokens[normalizedKey]) {
                            this._lateRecoveryTokens[normalizedKey] = true;
                            pending.then((lateClip) => {
                                try {
                                    delete this._lateRecoveryTokens[normalizedKey];
                                    if (!lateClip) return;
                                    const pendingRequest = this._pendingRequestedAction || {};
                                    const stillRequested = pendingRequest.actionName === actionName && pendingRequest.animationFile === animationFile;
                                    if (!stillRequested) return;
                                    console.warn('[AnimationHandler] Late animation preload recovered; replaying requested action:', actionName, animationFile);
                                    this.startAction(actionName, animationFile, playOnce, playSection, descriptorOverride);
                                } catch (lateErr) {
                                    console.warn('[AnimationHandler] Late animation recovery failed:', lateErr);
                                }
                            }).catch(() => {
                                try { delete this._lateRecoveryTokens[normalizedKey]; } catch (e) { /* ignore */ }
                            });
                        }
                    } catch (e) { /* ignore */ }
                    return;
                }
            }
        } catch (e) {
            console.warn('[AnimationHandler] Cannot play (preload error). Keeping previous animation:', actionName, animationFile, e);
            // Boost base idle so skeleton is fully driven even when the clip errored out (prevents T-pose).
            try {
                if (this._baseIdleAction) {
                    this._baseIdleAction.enabled = true;
                    this._baseIdleAction.setLoop(THREE.LoopRepeat);
                    this._baseIdleAction.clampWhenFinished = false;
                    if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                        this._baseIdleAction.setEffectiveWeight(1.0);
                    }
                    this._baseIdleAction.fadeIn(0.2);
                    this._baseIdleAction.play();
                }
            } catch (_e) { /* ignore */ }
            return;
        }

        this._pendingRequestedAction = null;

        // Guard: if the requested logical action is already active with the same (or any)
        // animation file, avoid restarting it to prevent rapid transitions / T-pose gaps.
        // The debug-window resyncs every 2 s and would otherwise call reset().play() on an
        // already-running action, preventing it from ever reaching its natural end.
        try {
            if (this.currentActionName === actionName && this.currentActionPhase && this.currentActionPhase !== 'outro' &&
                (!animationFile || animationFile === this._currentAnimationFile)) {
                console.log(`[AnimationHandler] startAction: ${actionName} (${animationFile || 'any'}) already active (phase=${this.currentActionPhase}) - no-op`);
                return;
            }
        } catch (e) { /* ignore */ }

        // ── KEY FIX: stop ALL active overlays before starting the new action. ──────────
        // Any orphaned intro/loop/outro from a previous state that didn't get cleaned up
        // will be faded out here, preventing residual poses from mixing with the new clip
        // (which is what causes the "T-pose idle" appearance).
        // _baseIdleAction is deliberately skipped so the skeleton stays covered during the
        // transition; we also re-affirm its presence immediately after to guard against
        // any accidental weight reduction.
        this._stopAllOverlays(0.3);
        try { await this._ensureBaseIdle(1.0, false); } catch(e) { /* ignore */ }

        let action = this.actions[actionName];

        // If starting THINK, ensure a small minimum visible window so persona overrides and preload finish
        try {
            if (String(actionName || '').toLowerCase() === 'think') {
                const minMs = 300; // minimum time to show THINK before it can be preempted
                this._minActionVisibleUntil = Date.now() + minMs;
            }
        } catch (e) { /* ignore */ }

        // If specific animation file is requested
        if (animationFile) {
            const normalizedFile = this._normalizeAnimationKey(animationFile);
            const specificKey = `${actionName}:${normalizedFile}`;
            if (this.actions[specificKey]) {
                action = this.actions[specificKey];
                console.log(`[AnimationHandler] Using cached specific animation: ${specificKey}`);
            } else {
                console.log(`[AnimationHandler] Loading specific animation: ${animationFile} for ${actionName}`);
                // Load the specific animation file
                const clip = await this.loadAnimation(actionName, animationFile);
                if (clip) {
                    // Load descriptor to check for structured animation (intro/loop/outro)
                    let descriptor = null;
                    try {
                        descriptor = descriptorOverride || await this.loadDescriptor(actionName, animationFile);
                        console.log(`[AnimationHandler] Loaded descriptor for ${animationFile}:`, descriptor);
                    } catch (err) {
                        console.debug(`[AnimationHandler] Could not load descriptor for ${animationFile}`);
                    }

                    // IDLE must always be a stable looping animation.
                    // Descriptors are allowed for IDLE, but only to define a safe loop range.
                    // We intentionally ignore play_once/outro to prevent clamped poses.
                    if (actionName === 'idle') {
                        playOnce = false;
                        if (descriptor) {
                            try {
                                descriptor = { ...descriptor };
                                delete descriptor.play_once;
                                delete descriptor.outro;
                            } catch (e) {
                                // ignore
                            }
                        }
                    }

                    // Check if this should be a structured animation
                    // Require both intro and outro to have numeric start/end frame bounds
                    const hasStructuredDescriptor = (actionName !== 'idle') && descriptor &&
                        descriptor.intro && descriptor.outro &&
                        (typeof descriptor.intro.start_frame === 'number') && (typeof descriptor.intro.end_frame === 'number') &&
                        (typeof descriptor.outro.start_frame === 'number') && (typeof descriptor.outro.end_frame === 'number');
                    console.log(`[AnimationHandler] hasStructuredDescriptor: ${hasStructuredDescriptor}, hasLoopSection: ${descriptor && descriptor.loop ? 'yes' : 'no'}`);
                    if (hasStructuredDescriptor && clip && clip.duration && THREE.AnimationUtils && typeof THREE.AnimationUtils.subclip === 'function') {
                        // Create structured animation (intro/loop/outro)
                        try {
                            const fps = (descriptor && typeof descriptor.fps === 'number' && descriptor.fps > 0) ? descriptor.fps : 30;
                            const totalFrames = Math.max(2, Math.round(clip.duration * fps));
                            const hasLoopSection = descriptor && descriptor.loop;

                            const clampInt = (v, lo, hi) => {
                                const n = Math.floor(Number(v));
                                if (!Number.isFinite(n)) return lo;
                                return Math.max(lo, Math.min(hi, n));
                            };

                            const normalizeRange = (start, end, label) => {
                                const s = clampInt(start, 0, totalFrames);
                                const e = clampInt(end, 0, totalFrames + 1);
                                if (e <= s + 1) {
                                    throw new Error(`[AnimationHandler] Invalid ${label} range: ${start}-${end} (normalized ${s}-${e}) totalFrames=${totalFrames}`);
                                }
                                return { start: s, end: e };
                            };

                            // Descriptors use inclusive end_frame; subclip() expects exclusive. Add +1.
                            let introStart = descriptor.intro?.start_frame ?? 0;
                            let introEnd = (descriptor.intro?.end_frame ?? (totalFrames - 1)) + 1;
                            let loopStart, loopEnd, outroStart, outroEnd;

                            if (hasLoopSection) {
                                loopStart = descriptor.loop?.start_frame ?? introEnd;
                                loopEnd = (descriptor.loop?.end_frame ?? (totalFrames - 1)) + 1;
                                outroStart = descriptor.outro?.start_frame ?? loopEnd;
                                outroEnd = (descriptor.outro?.end_frame ?? (totalFrames - 1)) + 1;
                            } else {
                                // No loop section - intro goes directly to outro
                                loopStart = null;
                                loopEnd = null;
                                outroStart = descriptor.outro?.start_frame ?? introEnd;
                                outroEnd = (descriptor.outro?.end_frame ?? (totalFrames - 1)) + 1;
                            }

                            // Validate & clamp ranges; if invalid, fall back to simple action.
                            const introR = normalizeRange(introStart, introEnd, 'intro');
                            const outroR = normalizeRange(outroStart, outroEnd, 'outro');
                            introStart = introR.start;
                            introEnd = introR.end;
                            outroStart = outroR.start;
                            outroEnd = outroR.end;
                            if (loopStart !== null && loopEnd !== null) {
                                const loopR = normalizeRange(loopStart, loopEnd, 'loop');
                                loopStart = loopR.start;
                                loopEnd = loopR.end;
                            }

                            const introClip = THREE.AnimationUtils.subclip(clip, `${specificKey}_intro`, introStart, introEnd, fps);
                            const outroClip = THREE.AnimationUtils.subclip(clip, `${specificKey}_outro`, outroStart, outroEnd, fps);

                            const introAction = this.mixer.clipAction(introClip);
                            const outroAction = this.mixer.clipAction(outroClip);

                            introAction.setLoop(THREE.LoopOnce, 0);
                            introAction.clampWhenFinished = true;
                            outroAction.setLoop(THREE.LoopOnce, 0);
                            // clampWhenFinished = true keeps the outro's last-frame pose
                            // until we manually fade it out, preventing T-pose gap.
                            outroAction.clampWhenFinished = true;

                            const structuredAction = {
                                intro: introAction,
                                outro: outroAction,
                                _meta: { source: clip.name || animationFile, descriptor: descriptor }
                            };

                            if (loopStart !== null && loopEnd !== null) {
                                const loopClip = THREE.AnimationUtils.subclip(clip, `${specificKey}_loop`, loopStart, loopEnd, fps);
                                // Attach loop frame metadata so we can verify during playback
                                try {
                                    loopClip._meta = loopClip._meta || {};
                                    loopClip._meta.loopFrames = { startFrame: loopStart, endFrame: loopEnd, fps };
                                } catch (e) { /* ignore */ }
                                // CRITICAL: Set loop directly on the clip BEFORE creating action
                                loopClip.loop = THREE.LoopRepeat;
                                const loopAction = this.mixer.clipAction(loopClip);
                                loopAction.setLoop(THREE.LoopRepeat);
                                loopAction.clampWhenFinished = false;
                                structuredAction.loop = loopAction;
                                console.log(`[AnimationHandler] Created loop section: frames ${loopStart}-${loopEnd - 1}, setLoop(LoopRepeat), clip.loop=${loopClip.loop}, clamped=false`);
                            } else {
                                structuredAction.loop = null;
                                structuredAction._playOnceOnly = true;
                            }

                            this.actions[specificKey] = structuredAction;
                            action = structuredAction;
                            console.log(`[AnimationHandler] Created structured animation for ${animationFile}, playOnceOnly: ${structuredAction._playOnceOnly}`);
                        } catch (err) {
                            console.warn(`[AnimationHandler] Failed to create structured animation for ${animationFile}, using simple action:`, err);
                            // Fall back to simple action
                            action = this.mixer.clipAction(clip);
                            if (playOnce || (descriptor && descriptor.play_once)) {
                                action.setLoop(THREE.LoopOnce, 0);
                                action.clampWhenFinished = (actionName === 'idle');
                                try { action._synthPlayOnce = true; action._synthLogical = actionName; } catch (e) { /* ignore */ }
                            } else {
                                action.setLoop(THREE.LoopRepeat);
                                action.clampWhenFinished = false;
                            }
                            this.actions[specificKey] = action;
                        }
                    } else {
                        // Simple animation without intro/outro structure.
                        // For IDLE, if a loop section is provided, subclip to that range and loop it.
                        if (actionName === 'idle' && descriptor && descriptor.loop && clip && clip.duration && THREE.AnimationUtils && typeof THREE.AnimationUtils.subclip === 'function') {
                            try {
                                const fps = (descriptor && typeof descriptor.fps === 'number' && descriptor.fps > 0) ? descriptor.fps : 30;
                                const totalFrames = Math.max(2, Math.round(clip.duration * fps));

                                const clampInt = (v, lo, hi) => {
                                    const n = Math.floor(Number(v));
                                    if (!Number.isFinite(n)) return lo;
                                    return Math.max(lo, Math.min(hi, n));
                                };
                                const normalizeRange = (start, end, label) => {
                                    const s = clampInt(start, 0, totalFrames);
                                    const e = clampInt(end, 0, totalFrames + 1);
                                    if (e <= s + 1) {
                                        throw new Error(`[AnimationHandler] Invalid ${label} range: ${start}-${end} (normalized ${s}-${e}) totalFrames=${totalFrames}`);
                                    }
                                    return { start: s, end: e };
                                };

                                const loopStart = descriptor.loop?.start_frame ?? 0;
                                // Descriptors use inclusive end_frame; subclip() expects exclusive. Add +1.
                                const loopEnd = (descriptor.loop?.end_frame ?? (totalFrames - 1)) + 1;
                                const loopR = normalizeRange(loopStart, loopEnd, 'loop');
                                const loopClip = THREE.AnimationUtils.subclip(clip, `${specificKey}_idle_loop`, loopR.start, loopR.end, fps);
                                loopClip.loop = THREE.LoopRepeat;
                                action = this.mixer.clipAction(loopClip);
                                action.setLoop(THREE.LoopRepeat);
                                action.clampWhenFinished = false;
                                this.actions[specificKey] = action;
                                console.log(`[AnimationHandler] Created IDLE loop subclip: frames ${loopR.start}-${loopR.end - 1}, LoopRepeat, no clamp`);
                            } catch (err) {
                                console.warn(`[AnimationHandler] Failed to apply IDLE loop descriptor for ${animationFile}, using full clip:`, err);
                                action = this.mixer.clipAction(clip);
                                action.setLoop(THREE.LoopRepeat);
                                action.clampWhenFinished = false;
                                this.actions[specificKey] = action;
                            }
                        } else {
                            action = this.mixer.clipAction(clip);
                            if (actionName !== 'idle' && (playOnce || (descriptor && descriptor.play_once))) {
                                action.setLoop(THREE.LoopOnce, 0);
                                action.clampWhenFinished = false;
                                try { action._synthPlayOnce = true; action._synthLogical = actionName; } catch (e) { /* ignore */ }
                            } else {
                                action.setLoop(THREE.LoopRepeat);
                                action.clampWhenFinished = false;
                            }
                            this.actions[specificKey] = action;
                            console.log(`[AnimationHandler] Created simple animation for ${animationFile}, playOnce: ${playOnce || (descriptor && descriptor.play_once)}`);
                        }
                    }
                } else {
                    console.log(`[AnimationHandler] Failed to load specific animation ${animationFile}, falling back to random from ${actionName}`);
                    // Fall back to random animation from the action group
                }
            }
        }

        if (!action) {
            console.log(`[AnimationHandler] Loading action ${actionName}...`);
            action = await this.loadAction(actionName);
            console.log(`[AnimationHandler] Action loaded:`, !!action);
        } else if (!animationFile && actionName === 'idle') {
            // For idle without a specific file, always load a new random animation
            // instead of reusing the cached 'idle' action
            console.log(`[AnimationHandler] Loading new random idle animation...`);
            action = await this.loadAction(actionName);
            console.log(`[AnimationHandler] New random idle animation loaded:`, !!action);
        } else {
            console.log(`[AnimationHandler] Using existing action for ${actionName}`);
        }

        if (!action) {
            console.error(`[AnimationHandler] No action available for ${actionName}`);
            return;
        }

        // If we explicitly entered IDLE with a specific animation, make it the baseline.
        if (actionName === 'idle' && animationFile && !playSection && !playOnce) {
            try {
                // Replace base idle with this action (simple or structured loop).
                const prevBaseIdle = this._baseIdleAction;
                let base = action;
                if (action && action.intro && action.outro) {
                    base = action.loop || action.intro;
                }
                // Guard: if the exact same THREE.js action is already running as base idle,
                // skip the restart entirely. Periodic re-syncs (e.g. debug-window every 2 s
                // calls startAction via resyncFromBackend) would otherwise reset the
                // animation to t=0 with no visible benefit, causing the "resets every second"
                // artefact the user sees.
                try {
                    if (base && prevBaseIdle === base) {
                        const running = typeof base.isRunning === 'function'
                            ? base.isRunning()
                            : (base.enabled && !base.paused);
                        if (running) {
                            console.log(`[AnimationHandler] startAction: IDLE '${animationFile}' already running as base idle - no-op`);
                            return;
                        }
                    }
                } catch (e) { /* ignore */ }
                this._baseIdleAction = base;
                this._baseIdleKey = `idle:${animationFile}`;
                try {
                    base.enabled = true;
                    base.setLoop(THREE.LoopRepeat);
                    base.clampWhenFinished = false;
                    base.reset();
                    if (typeof base.setEffectiveWeight === 'function') base.setEffectiveWeight(1.0);
                    base.fadeIn(0.2).play();
                } catch (e) { /* ignore */ }

                // Only after the new base idle is running, fade out the previous.
                if (prevBaseIdle && prevBaseIdle !== base) {
                    this._safeFadeStop(prevBaseIdle, 0.25);
                }

                // Fade out overlays and return.
                if (this.currentStructuredAction) {
                    this._safeFadeStop(this.currentStructuredAction.intro, 0.25);
                    this._safeFadeStop(this.currentStructuredAction.loop, 0.25);
                    this._safeFadeStop(this.currentStructuredAction.outro, 0.25);
                }
                this._safeFadeStop(this.currentAction, 0.25);
                this.currentAction = null;
                this.currentActionName = 'idle';
                this.currentActionPhase = null;
                this.currentStructuredAction = null;
                // Same as generic idle: clear lingering expressions so eyes don't stay closed.
                try {
                    this._clearEyesState();
                    if (this._expressionState) this._expressionState = {};
                    if (typeof this._forceOpenEyes === 'function') this._forceOpenEyes();
                    if (this._blinkAutoEnabled && !this._blinkLoopRunning) { try { this._startBlinkLoop(); } catch (e) { } }
                    if (this._eyeAutoEnabled && !this._eyeLoopRunning) { try { this._startEyeMovement(); } catch (e) { } }
                    this._lastAnimationState = { action: 'idle', phase: 'loop', expressions: [] };
                } catch (e) { /* ignore */ }
                return;
            } catch (e) {
                // If anything goes wrong, fall through to legacy playback.
            }
        }

        // If action is structured (intro/outro with optional loop), handle it differently
        // Check for intro and outro (loop is optional for play_once animations)
        if (action && action.intro && action.outro) {
            const structured = action;
            const isPlayOnceOnly = structured._playOnceOnly || !structured.loop;

            // Check if we need to interrupt a currently playing structured action
            // and transition to its outro before starting the new one
            if (this.currentActionName && this.currentActionName !== actionName && this.currentStructuredAction) {
                console.log(`[AnimationHandler] Structured action change detected: ${this.currentActionName} -> ${actionName}`);

                // If we're in intro or loop, transition to outro
                if (this.currentActionPhase === 'intro' || this.currentActionPhase === 'loop') {
                    console.log(`[AnimationHandler] Currently in ${this.currentActionPhase} phase, transitioning to outro...`);
                    try {
                        // Stop the current phase (fade) and then ensure it's stopped to avoid overlay
                        const fadeDuration = 150; // ms
                        if (this.currentActionPhase === 'intro' && this.currentStructuredAction.intro) {
                            this.currentStructuredAction.intro.fadeOut(fadeDuration / 1000);
                        } else if (this.currentActionPhase === 'loop' && this.currentStructuredAction.loop) {
                            // Fade out the loop gently
                            this.currentStructuredAction.loop.fadeOut(fadeDuration / 1000);
                        }

                        // After fade completes, explicitly stop (to avoid residual state) then start outro
                        setTimeout(() => {
                            try {
                                // stop previous phase to make way for outro
                                try {
                                    if (this.currentActionPhase === 'intro' && this.currentStructuredAction.intro) {
                                        this.currentStructuredAction.intro.stop();
                                    } else if (this.currentActionPhase === 'loop' && this.currentStructuredAction.loop) {
                                        this.currentStructuredAction.loop.stop();
                                    }
                                } catch (e) { /* ignore */ }

                                // Start outro if available
                                if (this.currentStructuredAction.outro) {
                                    // Proactively boost base idle so the skeleton
                                    // is fully covered when the outro finishes.
                                    try {
                                        if (this._baseIdleAction) {
                                            this._baseIdleAction.enabled = true;
                                            if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                                this._baseIdleAction.setEffectiveWeight(1.0);
                                            }
                                            this._baseIdleAction.play();
                                        }
                                    } catch (_e) { /* ignore */ }
                                    // Ensure outro starts at time 0 and plays once
                                    try {
                                        const outroAction = this.currentStructuredAction.outro;
                                        outroAction.reset();
                                        outroAction.setLoop(THREE.LoopOnce, 0);
                                        outroAction.clampWhenFinished = true;
                                        outroAction.enabled = true;
                                        outroAction.paused = false;
                                        outroAction.fadeIn(0.1).play();
                                    } catch (e) {
                                        // fallback using existing code if above fails
                                        this.currentStructuredAction.outro.reset().fadeIn(0.1).play();
                                    }

                                    this.currentAction = this.currentStructuredAction.outro;
                                    this.currentActionPhase = 'outro';
                                    console.log(`[AnimationHandler] Started outro for ${this.currentActionName} (after fade stop of prior phase)`);

                                    // Determine duration reliably, fallback to 1s
                                    const outroClip = this.currentStructuredAction.outro.getClip();
                                    const outroDuration = (outroClip && Number.isFinite(outroClip.duration) ? outroClip.duration : 1) * 1000;

                                    // Schedule the next action after outro completes
                                    setTimeout(() => {
                                        console.log(`[AnimationHandler] Outro completed for ${this.currentActionName}, now starting ${actionName}`);
                                        try { this.startAction(actionName, animationFile, playOnce, playSection); } catch (e) { console.warn('[AnimationHandler] Failed to start next action after outro:', e); }
                                    }, Math.round(outroDuration) + 100);
                                    return;
                                }
                            } catch (errInner) {
                                console.warn('[AnimationHandler] Error while handling fade->stop->outro transition:', errInner);
                            }
                        }, fadeDuration + 10);
                    } catch (err) {
                        console.warn('[AnimationHandler] Error during transition to outro:', err);
                    }

                    // We've scheduled the outro and the next action to start after it — don't continue
                    // starting the new action now.
                    return;
                }
            }

            // If playSection is specified (intro, loop, or outro), play only that section
            if (playSection === 'intro') {
                console.log(`[AnimationHandler] Playing only intro section for ${actionName}`);
                if (this.currentAction && this.currentAction !== structured.intro) {
                    this._safeFadeStop(this.currentAction, 0.25);
                }
                structured.intro.setLoop(THREE.LoopOnce, 0);
                structured.intro.clampWhenFinished = true;
                structured.intro.reset().fadeIn(0.15).play();
                this.currentAction = structured.intro;
                this.currentActionName = actionName;
                this.currentActionPhase = 'intro';
                this.currentStructuredAction = structured;
                return;
            } else if (playSection === 'loop') {
                if (!structured.loop) {
                    console.warn(`[AnimationHandler] Loop section requested but not available for ${actionName}, ignoring`);
                    return;
                }
                console.log(`[AnimationHandler] Playing only loop section for ${actionName}`);
                if (this.currentAction && this.currentAction !== structured.loop) {
                    this._safeFadeStop(this.currentAction, 0.25);
                }
                structured.loop.setLoop(THREE.LoopRepeat);
                structured.loop.clampWhenFinished = false;
                structured.loop.reset().fadeIn(0.15).play();
                this.currentAction = structured.loop;
                this.currentActionName = actionName;
                this.currentActionPhase = 'loop';
                this.currentStructuredAction = structured;
                return;
            } else if (playSection === 'outro') {
                console.log(`[AnimationHandler] Playing only outro section for ${actionName}`);
                // Boost base idle BEFORE playing outro so the skeleton is always
                // covered when the outro finishes (prevents T-pose gap).
                try {
                    if (this._baseIdleAction) {
                        this._baseIdleAction.enabled = true;
                        if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                            this._baseIdleAction.setEffectiveWeight(1.0);
                        }
                        this._baseIdleAction.play();
                    }
                } catch (_e) { /* ignore */ }
                if (this.currentAction && this.currentAction !== structured.outro) {
                    this._safeFadeStop(this.currentAction, 0.25);
                }
                structured.outro.setLoop(THREE.LoopOnce, 0);
                structured.outro.clampWhenFinished = true;
                structured.outro.reset().fadeIn(0.15).play();
                this.currentAction = structured.outro;
                this.currentActionName = actionName;
                this.currentActionPhase = 'outro';
                this.currentStructuredAction = structured;
                return;
            }

            // If playOnce requested, make the loop action play only once so that
            // the mixer 'finished' event will transition to the outro.
            if (playOnce && structured.loop) {
                try {
                    structured.loop.setLoop(THREE.LoopOnce, 0);
                    structured.loop.clampWhenFinished = false;
                    structured.loop._playOnce = true;  // Track for mixer event handler
                } catch (err) {
                    console.warn('[AnimationHandler] Failed to set structured loop to playOnce:', err);
                }
            }

            // Cross-fade: start the new structured intro/loop first, then fade out the previous action
            // This prevents a momentary gap where no action is active (T-pose) while loading/preparing clips.
            let prevStructuredActionToStop = null;
            if (this.currentAction && this.currentAction !== structured.loop && this.currentAction !== structured.intro) {
                prevStructuredActionToStop = this.currentAction;
            }

            // Start the intro immediately, then gently fade out any previous action to avoid T-pose gaps.
            try {
                structured.intro.reset().fadeIn(0.15).play();
                this.currentAction = structured.intro;
                this.currentActionName = actionName;
                this.currentActionPhase = 'intro';
                this.currentStructuredAction = structured;
                this._currentAnimationFile = animationFile || null;
                console.log(`[AnimationHandler] Structured action started (intro playing)`);
            } catch (e) {
                console.warn('[AnimationHandler] Failed to start structured intro immediately:', e);
            }

            // If there was a previous action to stop, fade it out after new action started
            if (prevStructuredActionToStop) {
                try {
                    // Shortly delay to ensure the intro has applied transforms
                    setTimeout(() => { try { this._safeFadeStop(prevStructuredActionToStop, 0.25); } catch (e) { /* ignore */ } }, 20);
                } catch (e) { /* ignore */ }
            }

            // Ensure we have a mixer finished handler to move intro -> loop and outro -> cleanup
            if (!this._mixerEventBound) {
                this._mixerEventBound = true;
                this.mixer.addEventListener('finished', (evt) => {
                    try {
                        const finishedAction = evt.action;
                        const finishedClip = finishedAction && finishedAction.getClip ? finishedAction.getClip() : null;
                        const finishedClipName = finishedClip?.name;

                        console.log(`[AnimationHandler] 🎬 Finished event: clip=${finishedClipName}, loopMode=${finishedClip?.loop}, action.looping=${finishedAction?.looping}`);

                        // Iterate structured actions to find matching intro/loop/outro
                        for (const key in this.actions) {
                            const candidate = this.actions[key];
                            if (!candidate || !candidate.intro || !candidate.outro) continue;
                            const introName = `${key}_intro`;
                            const loopName = `${key}_loop`;
                            const outroName = `${key}_outro`;

                            if (finishedClipName === introName) {
                                // Intro finished: either start loop or go directly to outro for playOnce animations
                                try {
                                    const logicalName = String(key || '').split(':')[0] || String(key || '');
                                    if (candidate._playOnceOnly || !candidate.loop) {
                                        console.log(`[AnimationHandler] intro finished for play_once animation ${key} -> starting outro`);
                                        // Proactively boost base idle so skeleton is covered when outro finishes.
                                        try {
                                            if (this._baseIdleAction) {
                                                this._baseIdleAction.enabled = true;
                                                if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                                    this._baseIdleAction.setEffectiveWeight(1.0);
                                                }
                                                this._baseIdleAction.play();
                                            }
                                        } catch (_e) { /* ignore */ }
                                        candidate.outro.reset().fadeIn(0.15).play();
                                        // Fade out intro so it doesn't keep driving bones at its
                                        // clamped last-frame pose while outro plays.
                                        try { this._safeFadeStop(candidate.intro, 0.15); } catch (e) { }
                                        this.currentAction = candidate.outro;
                                        this.currentActionName = logicalName;
                                        this.currentActionKey = key;
                                        this.currentActionPhase = 'outro';
                                        this.currentStructuredAction = candidate;
                                    } else {
                                        // Ensure loop is set to LoopRepeat on clip and action, then start it
                                        const loopClip = candidate.loop.getClip ? candidate.loop.getClip() : null;
                                        if (loopClip) loopClip.loop = THREE.LoopRepeat;
                                        try { candidate.loop.setLoop(THREE.LoopRepeat); } catch (e) { }
                                        try { candidate.loop.clampWhenFinished = false; } catch (e) { }
                                        try { candidate.loop.reset().fadeIn(0.15).play(); } catch (e) { }
                                        // Fade out intro so it doesn't keep clamping at its last frame.
                                        try { this._safeFadeStop(candidate.intro, 0.15); } catch (e) { }
                                        this.currentAction = candidate.loop;
                                        this.currentActionName = logicalName;
                                        this.currentActionKey = key;
                                        this.currentActionPhase = 'loop';
                                        this.currentStructuredAction = candidate;
                                    }
                                } catch (e) { /* ignore */ }
                                break;
                            }

                            if (finishedClipName === loopName && candidate.loop) {
                                // Loop finished - this should NOT happen if LoopRepeat is working!
                                console.warn(`[AnimationHandler] ⚠️ UNEXPECTED: Loop finished event for ${key}, loopMode:${candidate.loop.getClip?.()?.loop}, _playOnce:${candidate.loop._playOnce}`);
                                // Don't restart here; log and break
                                break;
                            }

                            if (finishedClipName === outroName) {
                                console.log(`[AnimationHandler] outro finished for ${key} -> advancing to next animation`);

                                // ── CRITICAL: boost base-idle to full weight FIRST ──────────────────
                                // With clampWhenFinished=true, the outro holds its last frame.
                                // Boost base idle to full weight before fading out the clamped
                                // outro so the skeleton seamlessly transitions to idle.
                                try {
                                    if (this._baseIdleAction) {
                                        this._baseIdleAction.enabled = true;
                                        this._baseIdleAction.setLoop(THREE.LoopRepeat);
                                        this._baseIdleAction.clampWhenFinished = false;
                                        if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                            this._baseIdleAction.setEffectiveWeight(1.0);
                                        }
                                        this._baseIdleAction.play();
                                    }
                                } catch (e) { /* ignore */ }

                                // Now clean up the finished structured clips (outro already at w=0).
                                try {
                                    this._safeFadeStop(candidate.intro, 0.05);
                                    this._safeFadeStop(candidate.loop, 0.05);
                                    this._safeFadeStop(candidate.outro, 0.05);
                                } catch (e) { /* ignore */ }
                                if (this.currentAction === candidate.outro) this.currentAction = null;
                                this.currentActionPhase = null;
                                this.currentActionName = null;
                                this.currentActionKey = null;
                                this.currentStructuredAction = null;
                                this._currentAnimationFile = null;

                                // Fallback: if no new action arrives immediately after a structured outro,
                                // force a return to idle to avoid a visible T-pose window.
                                try {
                                    const logical = String(key || '').split(':')[0];
                                    if (logical && logical !== 'idle') {
                                        const token = (this._postOutroIdleToken || 0) + 1;
                                        this._postOutroIdleToken = token;
                                        if (this._postOutroIdleTimer) {
                                            clearTimeout(this._postOutroIdleTimer);
                                        }
                                        this._postOutroIdleTimer = setTimeout(() => {
                                            try {
                                                if (this._postOutroIdleToken !== token) return;
                                                if (!this.currentAction && !this.currentStructuredAction) {
                                                    // Boost base idle to full weight immediately
                                                    // to avoid T-pose gap while startAction loads.
                                                    if (this._baseIdleAction) {
                                                        try {
                                                            this._baseIdleAction.enabled = true;
                                                            if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                                                this._baseIdleAction.setEffectiveWeight(1.0);
                                                            }
                                                        } catch (_e) { /* ignore */ }
                                                    }
                                                    this.startAction('idle');
                                                }
                                            } catch (e) { /* ignore */ }
                                        }, 50);
                                    }
                                } catch (e) { /* ignore */ }

                                // Signal to the page that a structured outro finished for this key
                                try {
                                    // Avoid dispatching duplicate outro completed events
                                    // in quick succession (can happen after VRM reloads or
                                    // if multiple listeners/handlers are present).
                                    this._lastOutroDispatched = this._lastOutroDispatched || {};
                                    const now = Date.now();
                                    if (!this._lastOutroDispatched[key] || (now - this._lastOutroDispatched[key] > 300)) {
                                        this._lastOutroDispatched[key] = now;
                                        window.dispatchEvent(new CustomEvent('synth_animation_outro_completed', { detail: { key } }));
                                    } else {
                                        console.debug('[AnimationHandler] Skipping duplicate outro dispatch for', key);
                                    }
                                } catch (e) { /* ignore non-browser env */ }

                                // Clear any persistent eyes state and ensure avatar eyes are open
                                try {
                                    console.debug('[AnimationHandler] Clearing eyesState and forcing eyes open after outro for', key);
                                    try { this._clearEyesState(); } catch (e) { }
                                    try { this._forceOpenEyes(); } catch (e) { }
                                    try { if (this._blinkAutoEnabled && !this._blinkLoopRunning) this._startBlinkLoop(); } catch (e) { }
                                    try { if (this._eyeAutoEnabled && !this._eyeLoopRunning) this._startEyeMovement(); } catch (e) { }
                                    try { this._minActionVisibleUntil = 0; } catch (e) { }
                                } catch (e) { /* ignore */ }

                                // If this was an idle animation, advance to next random idle
                                if (key.startsWith('idle:') || key === 'idle') {
                                    console.log(`[AnimationHandler] Outro finished for idle animation -> starting next idle`);
                                    setTimeout(() => {
                                        try {
                                            this.startAction('idle');
                                        } catch (e) {
                                            console.warn('[AnimationHandler] Failed to start next idle after outro:', e);
                                        }
                                    }, 100);
                                }
                                break;
                            }
                        }
                    } catch (err) {
                        console.warn('[AnimationHandler] mixer finished handler error:', err);
                    }
                });
            }

            // Log only — intro was already started in the cross-fade block above.
            // Do NOT call reset() again here: that was the cause of the visible
            // "jerk" (the animation restarted from t=0 a few ms after beginning).
            if (isPlayOnceOnly) {
                console.log(`[AnimationHandler] Starting play_once structured action (intro -> outro)`);
            } else {
                console.log(`[AnimationHandler] Starting structured action (intro -> loop -> outro)`);
            }
            // Ensure _currentAnimationFile is set even if the cross-fade block threw.
            if (!this._currentAnimationFile) this._currentAnimationFile = animationFile || null;
            console.log(`[AnimationHandler] Structured action confirmed started (no double-reset)`);

            // If playOnce requested for structured action (or if it's a play_once only animation),
            // schedule a safety fallback after the total duration
            try {
                if (playOnce || isPlayOnceOnly) {
                    const introDur = structured.intro.getClip ? (structured.intro.getClip().duration || 0) : 0;
                    const loopDur = structured.loop && structured.loop.getClip ? (structured.loop.getClip().duration || 0) : 0;
                    const outroDur = structured.outro.getClip ? (structured.outro.getClip().duration || 0) : 0;
                    const totalMs = Math.round((introDur + loopDur + outroDur) * 1000) + 300;
                    const key = `${actionName}:structured`;
                    if (this._playOnceTimers[key]) { clearTimeout(this._playOnceTimers[key]); delete this._playOnceTimers[key]; }
                    this._playOnceTimers[key] = setTimeout(() => {
                        try {
                            console.log(`[AnimationHandler] playOnce structured fallback for ${key} -> advancing to next animation`);
                            // Fade out whatever is playing
                            if (structured.loop) {
                                try { structured.loop.fadeOut(0.2); } catch (e) { /* ignore */ }
                            }
                            try { structured.outro.fadeOut(0.2); } catch (e) { /* ignore */ }

                            // If this was an idle animation, start next random idle
                            if (actionName.startsWith('idle') || actionName === 'idle') {
                                setTimeout(() => { try { this.startAction('idle'); } catch (e) { /* ignore */ } }, 200);
                            } else {
                                // Otherwise transition to idle
                                setTimeout(() => { try { this.startAction('idle'); } catch (e) { /* ignore */ } }, 200);
                            }
                        } catch (err) { console.warn('[AnimationHandler] structured fallback error:', err); }
                    }, totalMs);
                }
            } catch (err) {
                console.warn('[AnimationHandler] Failed to schedule structured playOnce fallback:', err);
            }
            return;
        }

        // Bind a global mixer finished handler once to detect when
        // single-clip playOnce actions finish so we can advance idle loops.
        try {
            if (this.mixer && !this._globalMixerFinishBound) {
                this._globalMixerFinishBound = true;
                this.mixer.addEventListener('finished', async (evt) => {
                    try {
                        const finishedAction = evt.action;
                        if (!finishedAction) return;

                        // Treat as playOnce candidate if explicitly marked or clamped.
                        const isPlayOnceCandidate = !!finishedAction.clampWhenFinished || !!finishedAction._synthPlayOnce;
                        if (!isPlayOnceCandidate) return;

                        // Find the key (actionName[:file]) corresponding to this action
                        let matchedKey = null;
                        for (const k of Object.keys(this.actions)) {
                            if (this.actions[k] === finishedAction) {
                                matchedKey = k;
                                break;
                            }
                        }

                        // If we couldn't map it, bail out
                        if (!matchedKey) return;

                        // If it was a specific-file action like 'idle:Look Around.fbx'
                        const parts = matchedKey.split(':');
                        const keyActionName = parts[0];
                        const keyFile = parts.length > 1 ? parts.slice(1).join(':') : null;

                        // For non-idle playOnce actions, recover to idle immediately.
                        if (keyActionName !== 'idle') {
                            // Guard: only act if this action is still the active one,
                            // OR if currentAction is already null (cleared by a prior transition)
                            // and no structured action is running — both indicate we need idle.
                            const isStillActive = (this.currentAction === finishedAction);
                            const isAbandoned = (!this.currentAction && !this.currentStructuredAction);
                            if (!isStillActive && !isAbandoned) return;
                            // Boost base idle to full weight FIRST, to cover any gap while transitioning.
                            const hasBaseIdle = !!this._baseIdleAction;
                            try {
                                if (this._baseIdleAction) {
                                    this._baseIdleAction.enabled = true;
                                    this._baseIdleAction.setLoop(THREE.LoopRepeat);
                                    this._baseIdleAction.clampWhenFinished = false;
                                    if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                        this._baseIdleAction.setEffectiveWeight(1.0);
                                    }
                                    this._baseIdleAction.play();
                                }
                                if (isStillActive) this._safeFadeStop(finishedAction, 0.12);
                            } catch (e) { /* ignore */ }
                            // Before handing back to idle, ensure eyes are opened and blinking resumes
                            try {
                                console.debug('[AnimationHandler] Clearing eyesState and forcing eyes open after playOnce finish for', matchedKey);
                                try { this._clearEyesState(); } catch (e) { }
                                try { this._forceOpenEyes(); } catch (e) { }
                                try { if (this._blinkAutoEnabled && !this._blinkLoopRunning) this._startBlinkLoop(); } catch (e) { }
                                try { if (this._eyeAutoEnabled && !this._eyeLoopRunning) this._startEyeMovement(); } catch (e) { }
                                try { this._minActionVisibleUntil = 0; } catch (e) { }
                            } catch (e) { }
                            // If no base idle is available, start idle immediately (no safe gap to wait).
                            // Otherwise wait 140ms so the base idle at weight=1.0 fully covers bones
                            // before the new idle clip fades in.
                            const idleDelay = hasBaseIdle ? 140 : 0;
                            setTimeout(() => { try { this.startAction('idle'); } catch (e) { /* ignore */ } }, idleDelay);

                            // Clear any playOnce timers for this non-idle action before returning.
                            try {
                                const tkey = keyFile ? `${keyActionName}:${keyFile}` : keyActionName;
                                if (this._playOnceTimers && this._playOnceTimers[tkey]) {
                                    clearTimeout(this._playOnceTimers[tkey]);
                                    delete this._playOnceTimers[tkey];
                                }
                            } catch (e) { /* ignore */ }
                            // Non-idle recovery is fully handled above; do NOT fall through
                            // to the idle-cycling code below (which would fire a second
                            // startAction('idle') and load descriptors for the wrong state).
                            return;
                        }

                        // Clear any playOnce timers for this action
                        try {
                            const tkey = keyFile ? `${keyActionName}:${keyFile}` : keyActionName;
                            if (this._playOnceTimers && this._playOnceTimers[tkey]) {
                                clearTimeout(this._playOnceTimers[tkey]);
                                delete this._playOnceTimers[tkey];
                            }
                        } catch (e) { /* ignore */ }

                        // Decide the next idle animation to play
                        try {
                            const files = await this.getAnimationsForType('idle');
                            if (!files || files.length === 0) return;

                            let descriptor = null;
                            let isPlayOnce = false;
                            if (keyFile) {
                                try {
                                    descriptor = await this.loadDescriptor('idle', keyFile);
                                    isPlayOnce = !!(descriptor && descriptor.play_once);
                                } catch (err) {
                                    console.debug('[AnimationHandler] Unable to load descriptor for finished idle animation:', err);
                                }
                            }

                            if (isPlayOnce) {
                                // play_once animations should finish and then hand back control to the default idle cycle
                                console.log('[AnimationHandler] Finished play_once idle animation -> resuming default idle cycle');
                                try {
                                    await this.startAction('idle');
                                } catch (e) {
                                    console.warn('[AnimationHandler] Failed to resume default idle after play_once:', e);
                                }
                                return;
                            }

                            // Pick a different idle animation (prefer next random different)
                            let nextFile = keyFile;
                            try {
                                const candidates = keyFile ? files.filter(f => f !== keyFile) : files;
                                if (candidates.length > 0) {
                                    nextFile = candidates[Math.floor(Math.random() * candidates.length)];
                                } else if (files.length > 0) {
                                    nextFile = files[Math.floor(Math.random() * files.length)];
                                } else {
                                    nextFile = null;
                                }
                            } catch (err) {
                                nextFile = (files && files.length > 0) ? files[Math.floor(Math.random() * files.length)] : null;
                            }

                            if (nextFile) {
                                console.log('[AnimationHandler] Finished idle animation -> starting next:', nextFile);
                                try {
                                    // Ensure base idle is at full weight to cover the gap between old and new idle.
                                    if (this._baseIdleAction) {
                                        try {
                                            this._baseIdleAction.enabled = true;
                                            this._baseIdleAction.setLoop(THREE.LoopRepeat);
                                            this._baseIdleAction.clampWhenFinished = false;
                                            if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                                this._baseIdleAction.setEffectiveWeight(1.0);
                                            }
                                            this._baseIdleAction.play();
                                        } catch (e) { /* ignore */ }
                                    }
                                    await this.startAction('idle', nextFile, false);
                                } catch (e) {
                                    console.warn('[AnimationHandler] Failed to start next idle action:', e);
                                }
                            }
                        } catch (e) {
                            console.warn('[AnimationHandler] Error advancing idle after playOnce:', e);
                        }
                    } catch (err) {
                        console.warn('[AnimationHandler] global mixer finished handler error:', err);
                    }
                });
            }
        } catch (err) {
            console.warn('[AnimationHandler] Failed to bind global mixer finished handler:', err);
        }

        // Start new action (single clip) and cross-fade previous action after ensuring
        // the new action is playing to avoid a momentary T-pose gap.
        console.log(`[AnimationHandler] Starting new action for ${actionName} (playOnce=${playOnce})`);
        try {
            if (playOnce) {
                // Ensure action is configured to play once
                action.setLoop(THREE.LoopOnce, 0);
                // Do not clamp non-idle: some exports end in bind pose (T-pose)
                action.clampWhenFinished = (actionName === 'idle');
                try { action._synthPlayOnce = true; action._synthLogical = actionName; } catch (e) { /* ignore */ }
            }
        } catch (err) {
            console.warn('[AnimationHandler] Failed to set action playOnce:', err);
        }

        // If a previous action exists, start the new action first (fade in) then fade out the previous.
        if (this.currentAction && this.currentAction !== action) {
            const prevAction = this.currentAction;
            try {
                action.enabled = true;
                action.reset().fadeIn(0.5).play();
                this.currentAction = action;
                this.currentActionName = actionName;
                this._currentAnimationFile = animationFile || null;
                console.log(`[AnimationHandler] New action started (cross-fade in)`);
            } catch (e) {
                console.warn('[AnimationHandler] Failed to start new action for cross-fade:', e);
                // Fallback: start normally
                try { action.reset().fadeIn(0.5).play(); this.currentAction = action; } catch (ee) { /* ignore */ }
            }

            // Fade out previous action using safe helper which resets and disables
            // after the fade completes.  This ensures no residual pose remains and
            // weight is cleanly removed (the old manual stop sometimes left traces).
            try {
                this._safeFadeStop(prevAction, 0.5);
            } catch (e) { /* ignore */ }
        } else {
            // No previous action, start normally
            try {
                action.reset().fadeIn(0.5).play();
                this.currentAction = action;
                this.currentActionName = actionName;
                this._currentAnimationFile = animationFile || null;
                console.log(`[AnimationHandler] Action started successfully`);
            } catch (e) {
                console.warn('[AnimationHandler] Failed to start action:', e);
            }
        }

        // Safety fallback: if playOnce requested, schedule a timer to
        // advance to next animation after the clip duration + buffer
        try {
            // clear previous timer for this actionName if present
            const key = animationFile ? `${actionName}:${animationFile}` : actionName;
            if (this._playOnceTimers[key]) {
                clearTimeout(this._playOnceTimers[key]);
                delete this._playOnceTimers[key];
            }
            if (playOnce) {
                const clip = action.getClip ? action.getClip() : null;
                const dur = clip && clip.duration ? (clip.duration * 1000) : 3000; // default 3s
                this._playOnceTimers[key] = setTimeout(() => {
                    try {
                        console.log(`[AnimationHandler] playOnce fallback timer fired for ${key} -> advancing to next animation`);
                        // For playOnce idle animations, advance to next random idle animation
                        if (actionName === 'idle' && animationFile) {
                            try { action.fadeOut(0.2); } catch (err) { /* ignore */ }
                            setTimeout(() => { try { this.startAction('idle'); } catch (e) { /* ignore */ } }, 200);
                        } else {
                            // For non-idle playOnce or fallback, transition to idle
                            try { action.fadeOut(0.2); } catch (err) { /* ignore */ }
                            setTimeout(() => { try { this.startAction('idle'); } catch (e) { /* ignore */ } }, 200);
                        }
                    } catch (err) {
                        console.warn('[AnimationHandler] playOnce fallback handler error:', err);
                    }
                }, dur + 200);
            }
        } catch (err) {
            console.warn('[AnimationHandler] Failed to schedule playOnce fallback timer:', err);
        }
    }

    stopAction(actionName) {
        const action = this.actions[actionName];
        // If structured think action, play outro when stopping
        if (action && action.loop && action.intro && action.outro) {
            try {
                // If currently looping, fade it out and play outro
                if (this.currentAction === action.loop) {
                    console.log('[AnimationHandler] Stopping think loop -> playing outro');
                    // Proactively boost base idle so the skeleton is fully covered
                    // when the outro finishes and we transition back.
                    try {
                        if (this._baseIdleAction) {
                            this._baseIdleAction.enabled = true;
                            if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                this._baseIdleAction.setEffectiveWeight(1.0);
                            }
                            this._baseIdleAction.play();
                        }
                    } catch (_e) { /* ignore */ }
                    action.loop.fadeOut(0.2);
                    action.outro.reset().fadeIn(0.15).play();
                    this.currentAction = action.outro;
                    this.currentActionPhase = 'outro';
                    return;
                }
                // If intro still playing, fade it out and play outro
                if (this.currentAction === action.intro) {
                    console.log('[AnimationHandler] Intro still playing -> switching to outro');
                    try {
                        if (this._baseIdleAction) {
                            this._baseIdleAction.enabled = true;
                            if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                                this._baseIdleAction.setEffectiveWeight(1.0);
                            }
                            this._baseIdleAction.play();
                        }
                    } catch (_e) { /* ignore */ }
                    action.intro.fadeOut(0.1);
                    action.outro.reset().fadeIn(0.15).play();
                    this.currentAction = action.outro;
                    this.currentActionPhase = 'outro';
                    return;
                }
            } catch (err) {
                console.warn('[AnimationHandler] Error while stopping structured think action:', err);
            }
        }

        if (action && typeof action === 'object' && action.stop) {
            // Boost base idle to full weight before fading out the current action
            // so bones that would otherwise lose coverage don't snap to T-pose
            // during the 500ms fade-out window.
            if (this._baseIdleAction) {
                try {
                    this._baseIdleAction.enabled = true;
                    if (typeof this._baseIdleAction.setEffectiveWeight === 'function') {
                        this._baseIdleAction.setEffectiveWeight(1.0);
                    }
                } catch (_e) { /* ignore */ }
            }
            action.fadeOut(0.5);
            if (this.currentAction === action) {
                this.currentAction = null;
            }
        }
    }

    stopAll() {
        for (const action of Object.values(this.actions)) {
            action.stop();
        }
        this.currentAction = null;
        // clear any playOnce fallback timers
        try {
            for (const k of Object.keys(this._playOnceTimers || {})) {
                clearTimeout(this._playOnceTimers[k]);
                delete this._playOnceTimers[k];
            }
        } catch (err) {
            console.warn('[AnimationHandler] Failed to clear playOnce timers on stopAll:', err);
        }
    }

    // -----------------------------
    // Debug: temporary loop override (WEB_DEBUG only, session-local)
    // -----------------------------
    async startTemporaryLoop(actionName, animationFile, startFrame, endFrame, fps = 30) {
        // Create an ephemeral looping action that will override the current animation until cleared
        try {
            console.log(`[AnimationHandler] startTemporaryLoop called: ${actionName}, ${animationFile}, frames ${startFrame}-${endFrame} (inclusive)`);

            // Clear existing debug override if present
            if (this._debugOverride && this._debugOverride.action) {
                try { this.clearTemporaryOverride(); } catch (e) { /* ignore */ }
            }

            let clip = null;
            try { console.debug('[AnimationHandler] resolving clip', { animationFile, loadedAnimationsKeys: Object.keys(this.loadedAnimations || {}).slice(0, 50) }); } catch (e) { }
            if (animationFile) {
                // ensure it's loaded
                try {
                    clip = this.loadedAnimations[animationFile] || await this.loadAnimation(actionName, animationFile);
                    try { console.debug('[AnimationHandler] load attempt result for animationFile', { animationFile, clip: clip && (clip.name || clip._clipName) }); } catch (e) { }
                } catch (errLoad) {
                    console.warn('[AnimationHandler] loadAnimation threw for animationFile', animationFile, errLoad);
                    clip = null;
                }
            } else {
                // no file provided: try to pick a default for the action
                try {
                    const files = await this.getAnimationsForType(actionName);
                    const pick = (files && files.length) ? files[0] : null;
                    try { console.debug('[AnimationHandler] picked file for action', { actionName, pick, filesCount: files && files.length }); } catch (e) { }
                    try {
                        clip = pick ? (this.loadedAnimations[pick] || await this.loadAnimation(actionName, pick)) : null;
                        try { console.debug('[AnimationHandler] load attempt result for pick', { pick, clip: clip && (clip.name || clip._clipName) }); } catch (e) { }
                    } catch (errPick) {
                        console.warn('[AnimationHandler] loadAnimation threw for pick', pick, errPick);
                        clip = null;
                    }
                } catch (e) { console.warn('[AnimationHandler] getAnimationsForType failed:', e); }
            }

            if (!clip) {
                console.warn('[AnimationHandler] No clip available to create temporary loop', { actionName, animationFile, pick: clip });
                return null;
            }
            try { console.debug('[AnimationHandler] startTemporaryLoop using clip', { name: clip && (clip.name || clip._clipName || '(unknown)'), duration: clip && clip.duration, frames: Math.round((clip && clip.duration || 0) * tfps) }); } catch (e) { }

            // Create subclip using frame indices.
            // Note: THREE.AnimationUtils.subclip expects an *exclusive* end frame.
            // Our UI + descriptors use inclusive end_frame, so we add +1.
            // Use descriptor FPS when available to avoid "range looks ignored" due to FPS mismatches.
            let tfps = (Number.isFinite(Number(fps)) && Number(fps) > 0) ? Number(fps) : NaN;
            try {
                const norm = (typeof this._normalizeAnimationKey === 'function') ? this._normalizeAnimationKey(animationFile) : String(animationFile || '');
                const desc = (this.loadedDescriptors && norm) ? (this.loadedDescriptors[norm] || this.loadedDescriptors[animationFile]) : null;
                const dfps = desc && Number.isFinite(Number(desc.fps)) ? Number(desc.fps) : NaN;
                if (!Number.isFinite(tfps) && Number.isFinite(dfps) && dfps > 0) tfps = dfps;
            } catch (e) { /* ignore */ }
            if (!Number.isFinite(tfps) || tfps <= 0) tfps = 30;
            const subName = `debug:${actionName}:${animationFile || clip.name}:${startFrame}-${endFrame}`;
            let sInc = Math.max(0, Math.floor(Number(startFrame) || 0));
            let eInc = Math.max(0, Math.floor(Number(endFrame) || 0));
            if (eInc < sInc) { const tmp = sInc; sInc = eInc; eInc = tmp; }
            // Clamp to clip bounds (inclusive frame index).
            try {
                const maxFrame = Math.max(0, Math.round(Number(clip.duration || 0) * tfps) - 1);
                sInc = Math.max(0, Math.min(maxFrame, sInc));
                eInc = Math.max(0, Math.min(maxFrame, eInc));
            } catch (e) { /* ignore */ }
            const eExc = Math.max(sInc + 1, eInc + 1);
            const loopClip = THREE.AnimationUtils.subclip(clip, subName, sInc, eExc, tfps);
            try { loopClip._meta = loopClip._meta || {}; loopClip._meta.loopFrames = { startFrame: sInc, endFrame: eInc, fps: tfps }; } catch (e) { /* ignore */ }

            // Ensure the clip intends to repeat
            loopClip.loop = THREE.LoopRepeat;
            const loopAction = this.mixer.clipAction(loopClip);
            loopAction.setLoop(THREE.LoopRepeat);
            loopAction.clampWhenFinished = false;

            // Save the previous current action to restore later
            const prevAction = this.currentAction;
            const prevPhase = this.currentActionPhase;
            const prevStructured = this.currentStructuredAction;

            // DEBUG OVERRIDE: hard cleanup to avoid hybrid blends.
            try {
                // Stop anything currently playing in the mixer.
                if (this.mixer && typeof this.mixer.stopAllAction === 'function') this.mixer.stopAllAction();
                // Also stop/disable named actions (structured entries etc.)
                try {
                    const vals = Object.values(this.actions || {});
                    vals.forEach((a) => {
                        try {
                            if (!a) return;
                            if (a.intro || a.loop || a.outro) {
                                [a.intro, a.loop, a.outro].forEach((x) => {
                                    try { if (x && typeof x.stop === 'function') x.stop(); } catch (e) { /* ignore */ }
                                    try { if (x) { x.enabled = false; x.paused = true; } } catch (e) { /* ignore */ }
                                    try { if (x && typeof x.setEffectiveWeight === 'function') x.setEffectiveWeight(0); } catch (e) { /* ignore */ }
                                });
                            } else {
                                try { if (typeof a.stop === 'function') a.stop(); } catch (e) { /* ignore */ }
                                try { a.enabled = false; a.paused = true; } catch (e) { /* ignore */ }
                                try { if (typeof a.setEffectiveWeight === 'function') a.setEffectiveWeight(0); } catch (e) { /* ignore */ }
                            }
                        } catch (e) { /* ignore */ }
                    });
                } catch (e) { /* ignore */ }
                // Clear any blend leftover on currentAction.
                try {
                    if (this.currentAction) {
                        try { if (typeof this.currentAction.stop === 'function') this.currentAction.stop(); } catch (e) { /* ignore */ }
                        try { this.currentAction.enabled = false; this.currentAction.paused = true; } catch (e) { /* ignore */ }
                        try { if (typeof this.currentAction.setEffectiveWeight === 'function') this.currentAction.setEffectiveWeight(0); } catch (e) { /* ignore */ }
                    }
                } catch (e) { /* ignore */ }
                // Reset mixer time so the override starts deterministic.
                try { if (this.mixer && typeof this.mixer.setTime === 'function') this.mixer.setTime(0); } catch (e) { /* ignore */ }
            } catch (e) { /* ignore */ }

            // Play debug loop with no crossfade.
            loopAction.reset();
            loopAction.enabled = true;
            loopAction.paused = false;
            try { if (typeof loopAction.setEffectiveWeight === 'function') loopAction.setEffectiveWeight(1); } catch (e) { /* ignore */ }
            try { if (typeof loopAction.setEffectiveTimeScale === 'function') loopAction.setEffectiveTimeScale(1); } catch (e) { /* ignore */ }
            loopAction.play();

            this._debugOverride = { action: loopAction, clip: loopClip, previous: { action: prevAction, phase: prevPhase, structured: prevStructured }, params: { actionName, animationFile, startFrame, endFrame, fps: tfps } };

            // Make sure we mark the current state so UI can reflect it
            this.currentAction = loopAction;
            this.currentActionName = actionName;
            this.currentActionPhase = 'debug_loop';
            this.currentStructuredAction = null;

            console.log('[AnimationHandler] Temporary debug loop started', this._debugOverride);
            return this._debugOverride;
        } catch (err) {
            console.warn('[AnimationHandler] startTemporaryLoop failed:', err);
            return null;
        }
    }

    clearTemporaryOverride() {
        try {
            if (!this._debugOverride) return;
            const debug = this._debugOverride;
            console.log('[AnimationHandler] Clearing temporary debug override:', debug);

            // Stop everything hard so we don't keep blended weights.
            try { if (this.mixer && typeof this.mixer.stopAllAction === 'function') this.mixer.stopAllAction(); } catch (e) { /* ignore */ }
            try {
                if (debug.action) {
                    try { if (typeof debug.action.stop === 'function') debug.action.stop(); } catch (e) { /* ignore */ }
                    try { debug.action.enabled = false; debug.action.paused = true; } catch (e) { /* ignore */ }
                    try { if (typeof debug.action.setEffectiveWeight === 'function') debug.action.setEffectiveWeight(0); } catch (e) { /* ignore */ }
                }
            } catch (e) { /* ignore */ }

            // Try to restore prior action
            const prev = debug.previous || {};
            setTimeout(() => {
                try {
                    // Ensure the mixer is clean before restoring.
                    try { if (this.mixer && typeof this.mixer.stopAllAction === 'function') this.mixer.stopAllAction(); } catch (e) { /* ignore */ }
                    try { if (this.mixer && typeof this.mixer.setTime === 'function') this.mixer.setTime(0); } catch (e) { /* ignore */ }

                    if (prev.action) {
                        try {
                            prev.action.reset();
                            prev.action.enabled = true;
                            prev.action.paused = false;
                            try { if (typeof prev.action.setEffectiveWeight === 'function') prev.action.setEffectiveWeight(1); } catch (e) { /* ignore */ }
                            prev.action.play();
                        } catch (e) { /* ignore */ }
                        this.currentAction = prev.action;
                        this.currentActionPhase = prev.phase || null;
                        this.currentStructuredAction = prev.structured || null;
                    } else {
                        // If nothing to restore, default to idle
                        try { this.startAction('idle'); } catch (e) { /* ignore */ }
                    }
                } catch (errInner) {
                    console.warn('[AnimationHandler] Error restoring previous action after clearing debug override:', errInner);
                }
            }, 30);

            // Remove debug override reference
            delete this._debugOverride;
        } catch (err) {
            console.warn('[AnimationHandler] clearTemporaryOverride failed:', err);
        }
    }
}

let isWaitingForFirstResponse = false;
let idleTimeout = null;

const clock = new THREE.Clock();

function resizeRenderer() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight || 1;
    if (canvas.width !== width || canvas.height !== height) {
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
    }
}
window.addEventListener('resize', resizeRenderer);
window.resizeVRMRenderer = resizeRenderer; // Expose globally for tab switching
resizeRenderer();

function clearVRM() {
    console.log('[synth_webui] Clearing current VRM from scene');
    if (currentVRM) {
        scene.remove(currentVRM.scene);
        currentVRM = null;
        console.log('[synth_webui] VRM cleared successfully');
    }
}

async function loadVRM(url, name, { isObjectUrl = false } = {}) {
    console.log(`[synth_webui] ========== LOAD VRM START ==========`);
    console.log(`[synth_webui] loadVRM called - url: ${url}, name: ${name}, isObjectUrl: ${isObjectUrl}`);
    console.log(`[synth_webui] typeof url: ${typeof url}`);
    console.log(`[synth_webui] url length: ${url ? url.length : 'N/A'}`);
    console.log(`[synth_webui] Current location: ${window.location.href}`);

    if (!url) {
        console.warn('[synth_webui] ⚠️ No URL provided for VRM, clearing');
        clearVRM();
        currentModel = null;
        setStatus('No active avatar', 'warn');
        console.log(`[synth_webui] ========== LOAD VRM END (no url) ==========`);
        return;
    }

    try {
        const targetLoader = isObjectUrl ? blobLoader : loader;
        console.log(`[synth_webui] Using loader: ${isObjectUrl ? 'blobLoader' : 'loader'}`);
        console.log(`[synth_webui] Loader object:`, targetLoader);

        const cacheBusted = isObjectUrl ? url : (url.includes('?') ? `${url}&t=${Date.now()}` : `${url}?t=${Date.now()}`);
        console.log(`[synth_webui] Original URL: ${url}`);
        console.log(`[synth_webui] Cache-busted URL: ${cacheBusted}`);
        console.log(`[synth_webui] Attempting to fetch VRM from: ${cacheBusted}`);

        // Test fetch before loading
        if (!isObjectUrl) {
            console.log(`[synth_webui] Pre-testing fetch to verify URL accessibility...`);
            try {
                const testResponse = await fetch(cacheBusted, { method: 'HEAD' });
                console.log(`[synth_webui] ✓ HEAD request status: ${testResponse.status}`);
                console.log(`[synth_webui] HEAD response headers:`, [...testResponse.headers.entries()]);
                console.log(`[synth_webui] Content-Type: ${testResponse.headers.get('content-type')}`);
                console.log(`[synth_webui] Content-Length: ${testResponse.headers.get('content-length')}`);
            } catch (fetchErr) {
                console.error(`[synth_webui] ⚠️ HEAD request failed:`, fetchErr);
                console.error(`[synth_webui] This indicates the URL is not accessible`);
            }
        }

        setStatus(`Loading avatar ${name}…`, 'info');
        console.log(`[synth_webui] Calling targetLoader.loadAsync()...`);

        const gltf = await targetLoader.loadAsync(cacheBusted);

        console.log('[synth_webui] ✓ GLTF loaded successfully');
        console.log('[synth_webui] GLTF object:', gltf);
        console.log('[synth_webui] GLTF.userData:', gltf.userData);
        console.log('[synth_webui] GLTF.scene:', gltf.scene);
        console.log('[synth_webui] GLTF.userData.vrm:', gltf.userData.vrm);

        const vrm = gltf.userData.vrm || await VRM.from(gltf);
        console.log('[synth_webui] ✓ VRM object created');
        console.log('[synth_webui] VRM object:', vrm);
        console.log('[synth_webui] VRM.scene:', vrm?.scene);
        console.log('[synth_webui] VRM.meta:', vrm?.meta);

        if (vrm && vrm.scene) {
            console.log('[synth_webui] Processing VRM scene...');
            console.log('[synth_webui] Scene children count:', vrm.scene.children.length);

            console.log('[synth_webui] Combining and optimizing skeleton...');
            VRMUtils.combineSkeletons(vrm.scene);
            console.log('[synth_webui] ✓ Skeleton combined and optimized');

            console.log('[synth_webui] Removing unnecessary vertices...');
            VRMUtils.removeUnnecessaryVertices(vrm.scene);
            console.log('[synth_webui] ✓ Unnecessary vertices removed');

            if (vrm.meta?.metaVersion === '0') {
                console.log('[synth_webui] Rotating VRM0 model (metaVersion=0)');
                VRMUtils.rotateVRM0(vrm);
                console.log('[synth_webui] ✓ VRM0 rotation applied');
            } else {
                console.log('[synth_webui] No VRM0 rotation needed (metaVersion:', vrm.meta?.metaVersion, ')');
            }

            console.log('[synth_webui] Setting scene rotation and position...');
            vrm.scene.rotation.y = Math.PI;
            vrm.scene.position.set(0, 0, 0);
            console.log('[synth_webui] Scene rotation:', vrm.scene.rotation);
            console.log('[synth_webui] Scene position:', vrm.scene.position);

            console.log('[synth_webui] Disabling frustum culling for all objects...');
            let objectCount = 0;
            vrm.scene.traverse((obj) => {
                obj.frustumCulled = false;
                objectCount++;
            });
            console.log('[synth_webui] ✓ Frustum culling disabled for', objectCount, 'objects');

            console.log('[synth_webui] ✓ VRM scene processed successfully');
        } else {
            console.error('[synth_webui] ⚠️ VRM or VRM.scene is missing!');
        }

        console.log('[synth_webui] Preparing VRM before adding to scene...');

        // Hide the VRM initially to avoid visible T-pose while animations
        // are being prepared. We'll unhide after animations are ready
        // (or on error to avoid leaving the scene invisible forever).
        try {
            if (vrm.scene) vrm.scene.visible = false;

        } catch (visErr) {
            console.warn('[synth_webui] Failed to set VRM invisible before preload:', visErr);
        }

        // Initialize animation mixer BEFORE adding the VRM to the scene
        // so animations can be loaded and started and the model will be
        // added already animating (reduces visible T-pose on load).
        try {
            console.log('[synth_webui] Creating AnimationMixer (pre-add)...');
            currentMixer = new THREE.AnimationMixer(vrm.scene);
            window.vrmMixer = currentMixer; // Make mixer available globally for AnimationHandler
            console.log('[synth_webui] AnimationMixer created and set globally (pre-add)');

            console.log('[synth_webui] Loading default animations (pre-add)...');
            // Load and start idle/talk/think/write actions before adding to scene
            await loadDefaultAnimations(vrm);
            console.log('[synth_webui] Default animations loaded (pre-add)');
            // If animations loaded successfully, unhide the VRM so it
            // will be displayed already animated when added to the scene.
            try {
                if (vrm.scene) vrm.scene.visible = true;
            } catch (unvisErr) {
                console.warn('[synth_webui] Failed to unhide VRM after preload:', unvisErr);
            }
        } catch (animErr) {
            console.warn('[synth_webui] Warning: failed to preload animations before adding VRM:', animErr);
            // Ensure we unhide even on error to avoid invisible models
            try {
                if (vrm.scene) vrm.scene.visible = true;
            } catch (_e) {
                /* ignore */
            }
        }

        console.log('[synth_webui] Clearing existing VRM from scene...');
        clearVRM();
        console.log('[synth_webui] ✓ Previous VRM cleared');

        console.log('[synth_webui] Adding new VRM to scene (already animated)...');
        console.log('[synth_webui] Scene before add - children count:', scene.children.length);
        scene.add(vrm.scene);
        console.log('[synth_webui] ✓ VRM added to scene');
        console.log('[synth_webui] Scene after add - children count:', scene.children.length);

        currentVRM = vrm;
        currentModel = name;
        console.log('[synth_webui] currentVRM set:', currentVRM);

        // Build raycast target list once for this model (meshes only)
        try {
            window.__synthRaycastTargets = [];
            if (vrm && vrm.scene) {
                vrm.scene.traverse((obj) => {
                    try {
                        if (!obj) return;
                        const isMesh = !!(obj.isMesh || obj.isSkinnedMesh);
                        if (!isMesh) return;
                        if (!obj.geometry) return;
                        window.__synthRaycastTargets.push(obj);
                    } catch (_e) { /* ignore */ }
                });
            }
            try { console.log('[synth_webui] Raycast targets:', window.__synthRaycastTargets.length); } catch (e) { }
        } catch (e) {
            console.warn('[synth_webui] Failed to build raycast targets:', e);
            window.__synthRaycastTargets = [];
        }

        // Detect face/expression capabilities (VRM0 vs VRM1) for case-by-case debugging.
        try {
            const hasBlendShapeProxy = !!(vrm && vrm.blendShapeProxy && typeof vrm.blendShapeProxy.setValue === 'function');
            const hasExpressionManager = !!(vrm && vrm.expressionManager && typeof vrm.expressionManager.setValue === 'function');
            const metaVersion = (vrm && vrm.meta && typeof vrm.meta.metaVersion !== 'undefined') ? String(vrm.meta.metaVersion) : null;

            // Best-effort enumeration of available expression keys (varies by three-vrm version).
            const expressionKeys = [];
            try {
                const em = vrm && (vrm.expressionManager || (vrm.userData && vrm.userData.vrmExpressionManager)) ? (vrm.expressionManager || vrm.userData.vrmExpressionManager) : null;
                if (em) {
                    // Common internal shapes in different builds: expressions, _expressions, expressionMap, _expressionMap
                    const candidates = [
                        em.expressions,
                        em._expressions,
                        em.expressionMap,
                        em._expressionMap,
                        em._map,
                    ].filter(Boolean);
                    for (const c of candidates) {
                        try {
                            if (c instanceof Map) {
                                for (const k of c.keys()) expressionKeys.push(String(k));
                            } else if (typeof c === 'object') {
                                for (const k of Object.keys(c)) expressionKeys.push(String(k));
                            }
                        } catch (_e) { /* ignore */ }
                    }
                }
            } catch (_e) { /* ignore */ }

            const uniq = Array.from(new Set(expressionKeys)).sort();
            window.__synth_vrm_capabilities = {
                model: name,
                metaVersion,
                hasBlendShapeProxy,
                hasExpressionManager,
                expressionKeys: uniq,
            };
            console.log('[synth_webui] VRM capabilities:', window.__synth_vrm_capabilities);
        } catch (e) {
            console.warn('[synth_webui] VRM capability probe failed:', e);
        }

        // Configure VRM LookAt to face the camera
        if (vrm.lookAt) {
            // Do not hard-bind LookAt to the camera here. The render loop owns LookAt
            // and applies a neutral forward gaze + temporary knock glance.
            try { vrm.lookAt.target = __synthLookAtTarget; } catch (_e) { /* ignore */ }
            console.log('[synth_webui] ✓ VRM LookAt enabled (render-loop controlled)');
        } else {
            console.log('[synth_webui] ⚠️ VRM LookAt not available');
        }
        console.log('[synth_webui] currentModel set:', currentModel);

        setStatus(`Avatar ${name} loaded`, 'success');
        console.log(`[synth_webui] ✓✓✓ VRM ${name} loaded and rendered successfully ✓✓✓`);

        // Dispatch event to notify animation system
        window.dispatchEvent(new Event('vrmLoaded'));

        console.log(`[synth_webui] Dispatched 'vrmLoaded' event`);

        console.log(`[synth_webui] ========== LOAD VRM END (success) ==========`);

    } catch (error) {
        console.error('[synth_webui] ========== LOAD VRM ERROR ==========');
        console.error('[synth_webui] ⚠️ Failed to load VRM');
        console.error('[synth_webui] Error object:', error);
        console.error('[synth_webui] Error message:', error.message);
        console.error('[synth_webui] Error name:', error.name);
        console.error('[synth_webui] Error stack:', error.stack);

        if (error.response) {
            console.error('[synth_webui] HTTP Response:', error.response);
        }

        setStatus('Unable to load the VRM model', 'error');
        console.error('[synth_webui] ========== LOAD VRM END (error) ==========');
    }
}

// Animation loading function
async function loadDefaultAnimations(vrm) {
    try {
        console.log('[synth_webui] Initializing AnimationHandler...');
        animationHandler = new AnimationHandler(currentMixer, vrm);
        // Immediately expose the real instance globally so WS message handlers
        // (e.g. in chat-window.mjs) that check window.VRMAnimations / window.animationHandler
        // always resolve to the live object, not the null stub set at module-load time.
        window.animationHandler = animationHandler;

        // Flush queued preloads captured before the handler existed.
        try {
            const q = window.__synth_pending_preloads || {};
            const keys = Object.keys(q);
            if (keys.length > 0 && typeof animationHandler.preloadAnimation === 'function') {
                console.log('[synth_webui] Flushing pending preloads at AnimationHandler init:', keys.length);
                for (const k of keys) {
                    try { await animationHandler.preloadAnimation(k, q[k] || null); } catch (e) { /* ignore */ }
                }
                window.__synth_pending_preloads = {};
            }
        } catch (e) { /* ignore */ }

        // Debug helpers to inspect VRM capabilities case-by-case (VRM0 vs VRM1).
        // Usage in DevTools console: `DEBUG_VRM_HELPERS.dump()`
        try {
            window.DEBUG_VRM_HELPERS = window.DEBUG_VRM_HELPERS || {};
            window.DEBUG_VRM_HELPERS.dump = function () {
                try {
                    const v = window.currentVRM || (window.animationHandler && window.animationHandler.vrm) || null;
                    const caps = window.__synth_vrm_capabilities || null;
                    console.log('[DEBUG_VRM_HELPERS] __synth_vrm_capabilities:', caps);
                    if (!v) {
                        console.warn('[DEBUG_VRM_HELPERS] No VRM loaded');
                        return caps;
                    }

                    const hasBlendShapeProxy = !!(v.blendShapeProxy && typeof v.blendShapeProxy.setValue === 'function');
                    const hasExpressionManager = !!(v.expressionManager && typeof v.expressionManager.setValue === 'function');
                    const metaVersion = (v.meta && typeof v.meta.metaVersion !== 'undefined') ? String(v.meta.metaVersion) : null;

                    const out = { metaVersion, hasBlendShapeProxy, hasExpressionManager };
                    try {
                        const em = v.expressionManager || (v.userData && v.userData.vrmExpressionManager) || null;
                        if (em) {
                            out.expressionManagerKeys = Object.keys(em);
                            const keys = [];
                            const candidates = [em.expressions, em._expressions, em.expressionMap, em._expressionMap, em._map].filter(Boolean);
                            for (const c of candidates) {
                                if (c instanceof Map) {
                                    for (const k of c.keys()) keys.push(String(k));
                                } else if (typeof c === 'object') {
                                    keys.push(...Object.keys(c).map(String));
                                }
                            }
                            out.expressionKeys = Array.from(new Set(keys)).sort();
                        }
                    } catch (e) {
                        out.expressionProbeError = String(e);
                    }

                    console.log('[DEBUG_VRM_HELPERS] live probe:', out);
                    return out;
                } catch (e) {
                    console.warn('[DEBUG_VRM_HELPERS] dump failed:', e);
                    return null;
                }
            };
            console.log('[synth_webui] DEBUG_VRM_HELPERS.dump available');
        } catch (e) { /* ignore */ }

        // Preload all animations for the current skin and action types to
        // reduce T-pose flashes when switching animations at runtime.
        // This will populate animationHandler.loadedAnimations cache.
        try {
            console.log('[synth_webui] Preloading all animations (may take a moment)...');
            if (typeof animationHandler.preloadAllAnimations === 'function') {
                await animationHandler.preloadAllAnimations();
                console.log('[synth_webui] ✓ Preloaded all animations');
            } else {
                console.log('[synth_webui] preloadAllAnimations not available');
            }
        } catch (err) {
            console.warn('[synth_webui] Preload all animations failed:', err);
        }

        console.log('[synth_webui] Loading base actions...');
        try {
            // Bootstrap the base idle immediately so _baseIdleAction is always
            // non-null before any other startAction runs. Using weight=1.0 means
            // the skeleton is fully driven from this point forward. Any animation
            // that follows will either succeed (fading idle to 0.12) or fail and
            // boost idle back to 1.0 — but T-pose is never reachable.
            await animationHandler._ensureBaseIdle(1.0, true);
            console.log('[synth_webui] ✓ Base idle bootstrapped (weight=1.0, _baseIdleAction set)');
        } catch (e) {
            console.error('[synth_webui] ✗ Base idle bootstrap failed:', e);
        }

        console.log('[synth_webui] Server-driven mode: skipping eager THINK preload');

        // Debug helper for manual testing of face expressions/blink
        try {
            window.DEBUG_ANIM_HELPERS = window.DEBUG_ANIM_HELPERS || {};
            window.DEBUG_ANIM_HELPERS.triggerThink = function () {
                try {
                    const state = {
                        action: 'think',
                        phase: 'loop',
                        timing: { time_in_clip: 0, current_frame: 0 },
                        expressions: [{ start_frame: 0, end_frame: 15, targets: { 'eyes_closed': 1.0 }, priority: 90, source: 'persona_override' }],
                        blink: { auto: false },
                        lipsync: false
                    };
                    if (window.animationHandler && typeof window.animationHandler.applyAnimationState === 'function') {
                        window.animationHandler.applyAnimationState(state);
                        console.info('[DEBUG_ANIM_HELPERS] triggerThink: applied state');
                    } else {
                        console.warn('[DEBUG_ANIM_HELPERS] animationHandler not ready');
                    }
                } catch (e) { console.warn('[DEBUG_ANIM_HELPERS] triggerThink failed', e); }
            };
            console.debug('[synth_webui] DEBUG_ANIM_HELPERS.triggerThink available');
        } catch (e) { /* ignore */ }

        console.log('[synth_webui] Server-driven mode: skipping eager WRITE/TALK preload');

        // Process any pending animation commands that arrived while loading
        // Attempt to sync with backend animation state so page reloads pick
        // up the current persona state instead of defaulting to idle.
        let desiredState = null;
        let desiredAnimation = null;
        let desiredDescriptor = null;
        try {
            if (window.__synth_current_animation_state && window.__synth_current_animation_state.state) {
                desiredState = window.__synth_current_animation_state.state;
                desiredAnimation = window.__synth_current_animation_state.animation || null;
                desiredDescriptor = window.__synth_current_animation_state.descriptor || null;
                console.log('[synth_webui] Using cached animation state for VRM reload:', desiredState, desiredAnimation);
            }
        } catch (err) {
            // ignore
        }

        if (!desiredState) {
            try {
                console.log('[synth_webui] Querying backend for current animation state...');
                const resp = await fetch('/api/animation_state');
                if (resp && resp.ok) {
                    const summary = await resp.json();
                    console.log('[synth_webui] Backend animation state:', summary);
                    if (summary && summary.state) {
                        desiredState = summary.state;
                        desiredAnimation = summary.animation || null;
                        desiredDescriptor = summary.descriptor || null;
                        window.__synth_current_animation_state = { state: desiredState, animation: desiredAnimation, descriptor: desiredDescriptor || null };
                    }
                } else {
                    console.warn('[synth_webui] Failed to fetch backend animation state, resp ok=', !!resp?.ok);
                }
            } catch (err) {
                console.warn('[synth_webui] Error while querying backend animation state:', err);
            }
        }

        // Start the desired state (avoids a visible reset to idle during skin/model reload).
        try {
            const stateToStart = desiredState || 'idle';
            const playOnce = !!(desiredDescriptor && desiredDescriptor.play_once);

            // If WEB_DEBUG pause is active, do not mutate playback; keep state for resync.
            if (window.__synth_web_debug_enabled && window.__synth_debug_pause_all) {
                try {
                    window.__synth_debug_last_remote.animation_state = {
                        type: 'animation_state',
                        state: stateToStart,
                        animation: desiredAnimation || null,
                        descriptor: desiredDescriptor || null,
                        animation_state: window.__synth_last_rich_animation_state || null,
                    };
                    window.__synth_debug_last_remote_at.animation_state = Date.now();
                } catch (e) { /* ignore */ }
            } else {
                await animationHandler.startAction(stateToStart, desiredAnimation || null, playOnce, null, desiredDescriptor || null);
                console.log('[synth_webui] Started initial animation state:', stateToStart, desiredAnimation || null);
            }
        } catch (e) {
            console.warn('[synth_webui] Failed to start initial animation state, falling back to idle:', e);
            try {
                if (!(window.__synth_web_debug_enabled && window.__synth_debug_pause_all)) {
                    await animationHandler.startAction('idle');
                }
            } catch (_e) {
                // ignore
            }
        }

        // Process any pending animation commands that arrived while loading.
        // Only apply the last (authoritative) command to avoid replay storms and T-pose flashes.
        const _pendingArray = (typeof pendingAnimationCommands !== 'undefined' && Array.isArray(pendingAnimationCommands)) ? pendingAnimationCommands : (window.pendingAnimationCommands && Array.isArray(window.pendingAnimationCommands) ? window.pendingAnimationCommands : []);
        if (_pendingArray.length > 0) {
            const last = _pendingArray[_pendingArray.length - 1];
            const count = _pendingArray.length;
            // Clear the authoritative storage we found so we don't reprocess
            if (typeof pendingAnimationCommands !== 'undefined' && Array.isArray(pendingAnimationCommands)) pendingAnimationCommands.length = 0; else if (window.pendingAnimationCommands && Array.isArray(window.pendingAnimationCommands)) window.pendingAnimationCommands.length = 0;
            console.log('[synth_webui] Processing last pending animation command (dropped', Math.max(0, count - 1), '):', last?.state, last?.animation);
            if (last && last.state && animationHandler) {
                // If WEB_DEBUG pause is active, keep last remote payload for resync but do not apply.
                try {
                    if (window.__synth_web_debug_enabled && window.__synth_debug_pause_all) {
                        window.__synth_debug_last_remote.animation = last;
                        window.__synth_debug_last_remote_at.animation = Date.now();
                        return;
                    }
                } catch (e) { /* ignore */ }

                const animationFileOrUrl = last.animation || null;
                const lastPlayOnce = (last.descriptor && last.descriptor.play_once) || (last.loop === false);
                const initialPlayOnce = !!(desiredDescriptor && desiredDescriptor.play_once);

                // Skip if it matches what we just started as initial state.
                try {
                    const startedKey = `${(desiredState || 'idle') || ''}|${desiredAnimation || ''}|${initialPlayOnce ? '1' : '0'}`;
                    const lastKey = `${last.state || ''}|${last.animation || ''}|${lastPlayOnce ? '1' : '0'}`;
                    if (startedKey !== lastKey) {
                        animationHandler.startAction(last.state, animationFileOrUrl, !!lastPlayOnce, last.play_section || null, last.descriptor || null);
                    } else {
                        console.log('[synth_webui] Pending command matches started state; skipping');
                        // Even when skipping (because the action is already playing),
                        // apply any rich animation_state so blink/eye/lipsync managers are configured.
                        try {
                            const st = last.animation_state || window.__synth_last_rich_animation_state || null;
                            const looksRich = !!(st && (st.blink || st.eye_movement || st.expressions || st.emotions || (typeof st.lipsync === 'boolean')));
                            if (looksRich && typeof animationHandler.applyAnimationState === 'function') {
                                animationHandler.applyAnimationState(st);
                            }
                        } catch (e) { /* ignore */ }
                    }
                } catch (e) {
                    animationHandler.startAction(last.state, animationFileOrUrl, !!lastPlayOnce, last.play_section || null, last.descriptor || null);
                }
            }
        }

    } catch (error) {
        console.error('[synth_webui] Failed to load animations:', error);
    }
}

// Function to refresh the active VRM model in the 3D viewer (called from skins UI)
async function refreshModels() {
    try {
        console.log('[synth_webui] refreshModels called - fetching active VRM');
        const response = await fetch('/api/vrm/active');
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        const activeModel = await response.json();
        console.log('[synth_webui] Active VRM response:', activeModel);

        if (activeModel.name && activeModel.url) {
            console.log(`[synth_webui] Loading active VRM: ${activeModel.name} from ${activeModel.url}`);
            await loadVRM(activeModel.url, activeModel.name);
        } else {
            console.log('[synth_webui] No active VRM - clearing scene');
            clearVRM();
            currentModel = null;
        }
    } catch (error) {
        console.error('[synth_webui] Failed to refresh models:', error);
        setStatus('Failed to refresh VRM model', 'error');
    }
}

// Make refreshModels available globally for skins UI
window.refreshModels = refreshModels;

// Load the active VRM model on page load
refreshModels().catch((error) => {
    console.error('[synth_webui] Failed to load VRM on page load:', error);
    setStatus('Failed to load VRM model', 'error');
});

// Start thinking animation
function startThinking() {
    if (!animationHandler || isProcessing) return;
    isProcessing = true;
    console.log('[synth_webui] 🤔 Starting thinking animation');
    animationHandler.startAction('think');
}

// Stop thinking animation
function stopThinking() {
    if (!animationHandler || !isProcessing) return;
    isProcessing = false;
    console.log('[synth_webui] ✓ Thinking state ended');
    // Will transition back to idle automatically if no other action
}

// Start talking animation with estimated duration
function startTalking(text) {
    if (!animationHandler) return;

    // Estimate speech duration (approximately 150 words per minute)
    const wordCount = text.trim().split(/\s+/).length;
    const estimatedDuration = (wordCount / 150) * 60; // seconds

    console.log(`[synth_webui] 💬 Starting talking animation for ${wordCount} words (~${estimatedDuration.toFixed(1)}s)`);

    isSpeaking = true;

    // Start talking animation
    animationHandler.startAction('talk');

    // Stop talking after estimated duration
    setTimeout(() => {
        stopTalking();
    }, estimatedDuration * 1000);
}

// Stop talking animation
function stopTalking() {
    if (!animationHandler || !isSpeaking) return;

    console.log('[synth_webui] ✓ Stopping talking animation');
    isSpeaking = false;

    // Transition back to idle
    animationHandler.startAction('idle');
}

function render() {
    requestAnimationFrame(render);
    const delta = clock.getDelta();
    if (currentVRM) {
        if (window.__synthIsLipSyncing && window.__synthLipSyncAnalyser && currentVRM.expressionManager) {
            try {
                const analyser = window.__synthLipSyncAnalyser;
                if (!window.__synthLipSyncData) window.__synthLipSyncData = new Uint8Array(analyser.frequencyBinCount);
                analyser.getByteFrequencyData(window.__synthLipSyncData);
                const data = window.__synthLipSyncData;

                // Focus on voice-frequency bins (~85-4000 Hz) instead of
                // averaging the entire spectrum.  With fftSize=256 and a
                // typical 44100/48000 Hz sample rate each bin spans ~172-188 Hz.
                // Bins 1-23 cover roughly 170-4300 Hz — the vocal range.
                const binCount = data.length; // frequencyBinCount = fftSize/2
                const lo = 1;
                const hi = Math.min(Math.floor(binCount * 0.18), binCount - 1); // ~18% of bins ≈ voice band
                let voiceSum = 0;
                for (let i = lo; i <= hi; i++) voiceSum += data[i];
                const voiceVolume = voiceSum / ((hi - lo + 1) * 255.0);

                // Aggressive gain curve: low threshold + strong multiplier
                // so even soft speech opens the mouth visibly.
                const rawMouth = Math.max(0, (voiceVolume - 0.01) * 5.0);
                const mouthOpen = Math.max(0, Math.min(1, rawMouth));

                // Apply lipsync visemes directly to the VRM model, bypassing
                // the expression source interpolation which is too slow for
                // frame-by-frame mouth movement.  A lightweight per-frame
                // smoothing (lerp α ≈ 0.35) keeps the motion natural.
                if (!window.__synthLipSyncPrev) window.__synthLipSyncPrev = 0;
                const alpha = 0.35;  // 0 = no change, 1 = instant snap
                const smoothed = window.__synthLipSyncPrev + (mouthOpen - window.__synthLipSyncPrev) * alpha;
                window.__synthLipSyncPrev = smoothed;

                // Register as high-priority expression source so the priority
                // system prevents facial_expression (priority 25) from
                // overriding mouth visemes during speech.
                if (animationHandler && typeof animationHandler.removeExpressionSourcesByTag === 'function') {
                    animationHandler.removeExpressionSourcesByTag('lipsync');
                    animationHandler.addExpressionSource({
                        targets: { aa: smoothed, ih: 0, ou: 0, ee: 0, oh: 0 },
                        priority: 30,
                        source: 'lipsync',
                        _directApply: true  // flag: bypass applyExpressionsForFrame interpolation
                    });
                }

                // Also apply directly for instant responsiveness (the
                // expression source above is still needed for priority gating).
                try {
                    const em = currentVRM.expressionManager;
                    em.setValue('aa', smoothed);
                    // Reset other visemes to 0 so they don't linger
                    em.setValue('ih', 0);
                    em.setValue('ou', 0);
                    em.setValue('ee', 0);
                    em.setValue('oh', 0);
                } catch (_e) { /* ignore */ }
            } catch (e) {
                // suppress to avoid render-loop spam
            }
        } else if (animationHandler && typeof animationHandler.removeExpressionSourcesByTag === 'function') {
            // Clean up lipsync source when lipsync stops
            try { animationHandler.removeExpressionSourcesByTag('lipsync'); } catch (e) { /* ignore */ }
            window.__synthLipSyncPrev = 0;
        }
        // Update VRM lookAt target.
        // Default: look forward (not directly at the camera).
        // On knock: briefly blend the target slightly towards the camera for a low-key “notice”.
        try {
            if (currentVRM.lookAt && currentVRM.scene) {
                // Camera world position
                camera.getWorldPosition(__synthTmpCamPos);

                // Compute head/world position (prefer VRM humanoid head bone if available)
                let haveHead = false;
                try {
                    const humanoid = currentVRM.humanoid;
                    const headNode = humanoid && typeof humanoid.getNormalizedBoneNode === 'function'
                        ? (humanoid.getNormalizedBoneNode('head') || humanoid.getNormalizedBoneNode('Head'))
                        : null;
                    if (headNode && typeof headNode.getWorldPosition === 'function') {
                        headNode.getWorldPosition(__synthTmpHeadPos);
                        haveHead = true;
                    }
                } catch (e) { /* ignore */ }
                if (!haveHead) {
                    currentVRM.scene.getWorldPosition(__synthTmpAvatarPos);
                    __synthTmpHeadPos.copy(__synthTmpAvatarPos);
                    __synthTmpHeadPos.y += 1.45;
                }

                // Compute avatar-forward using hips bone (more reliable than scene orientation)
                let haveHips = false;
                try {
                    const humanoid = currentVRM.humanoid;
                    const hipsNode = humanoid && typeof humanoid.getNormalizedBoneNode === 'function'
                        ? (humanoid.getNormalizedBoneNode('hips') || humanoid.getNormalizedBoneNode('Hips'))
                        : null;
                    if (hipsNode && typeof hipsNode.getWorldQuaternion === 'function') {
                        hipsNode.getWorldQuaternion(__synthTmpQuat);
                        haveHips = true;
                    }
                } catch (e) { /* ignore */ }
                if (!haveHips) {
                    try { currentVRM.scene.getWorldQuaternion(__synthTmpQuat); } catch (e) { /* ignore */ }
                }

                __synthTmpForward.set(0, 0, -1).applyQuaternion(__synthTmpQuat).normalize();
                // slight "avoid eye contact" offset (secondary mood)
                __synthTmpForward.applyAxisAngle(__synthTmpUp, __synthNeutralGaze.yawOffsetRad);

                const headY = __synthTmpHeadPos.y;
                const defaultTarget = __synthDefaultLookAtTarget.position;
                defaultTarget.copy(__synthTmpHeadPos).add(__synthTmpForward.multiplyScalar(__synthNeutralGaze.distance));
                defaultTarget.y = headY;

                // Knock glance strength: eased 0..1..0 over the window
                const now = Date.now();
                let strength = 0;
                if (__synthKnockLook && __synthKnockLook.activeUntil && now < __synthKnockLook.activeUntil) {
                    const start = __synthKnockLook.startedAt || (now - 1);
                    const dur = Math.max(120, Number(__synthKnockLook.durationMs) || 520);
                    const p = Math.max(0, Math.min(1, (now - start) / dur));
                    const ease = Math.sin(Math.PI * p);
                    strength = (Number(__synthKnockLook.maxStrength) || 0.32) * ease;
                }

                const desiredTarget = __synthTmpDesired2.copy(defaultTarget);
                if (strength > 0) {
                    // Compute a camera-facing target at the same distance as neutral gaze.
                    __synthTmpDir.copy(__synthTmpCamPos).sub(__synthTmpHeadPos);
                    __synthTmpDir.y *= 0.35;
                    if (__synthTmpDir.lengthSq() < 1e-6) __synthTmpDir.set(0, 0, -1);
                    __synthTmpDir.normalize();
                    __synthTmpDesired.copy(__synthTmpHeadPos).add(__synthTmpDir.multiplyScalar(__synthNeutralGaze.distance));
                    __synthTmpDesired.y = headY;
                    desiredTarget.lerp(__synthTmpDesired, strength);
                }

                // Smoothly move our explicit target to avoid snapping.
                const dt = Number.isFinite(delta) ? delta : 0.016;
                const alpha = 1 - Math.exp(-10 * dt);
                __synthLookAtTarget.position.lerp(desiredTarget, alpha);
                currentVRM.lookAt.target = __synthLookAtTarget;
            }
        } catch (e) { /* ignore */ }

        try {
            const paused = !!(window.__synth_web_debug_enabled && window.__synth_debug_pause_all);
            const allowWhilePaused = !!(window.__synth_web_debug_enabled && paused && animationHandler && animationHandler._debugOverride && animationHandler._debugOverride.action);
            if (!paused || allowWhilePaused) currentVRM.update(delta);
        } catch (e) {
            currentVRM.update(delta);
        }
    }
    if (currentMixer) {
        try {
            const paused = !!(window.__synth_web_debug_enabled && window.__synth_debug_pause_all);
            const allowWhilePaused = !!(window.__synth_web_debug_enabled && paused && animationHandler && animationHandler._debugOverride && animationHandler._debugOverride.action);
            if (!paused || allowWhilePaused) currentMixer.update(delta);
        } catch (e) {
            currentMixer.update(delta);
        }

        // Monitor loop status and keep it alive
        if (animationHandler && animationHandler.currentActionPhase === 'loop') {
            const action = animationHandler.currentAction;
            if (action) {
                const clip = action.getClip?.();

                // Solo monitoraggio: se il loop è paused, logga ma non forzare restart
                if (action.paused) {
                    console.warn(`[render] ❌ LOOP PAUSED! (no force-restart)`);
                }

                if (!animationHandler._loopMonitorFrame) animationHandler._loopMonitorFrame = 0;
                animationHandler._loopMonitorFrame++;

                // Fix suspicious NaN times in render loop when seen — try to recover safely
                try {
                    if (!Number.isFinite(action.time) || Number.isNaN(action.time)) {
                        console.warn('[render] ⚠️ Detected invalid action.time (NaN). Resetting to 0 to recover.');
                        action.time = 0;
                        try { action.enabled = true; action.paused = false; } catch (e) { /* ignore */ }
                    }
                } catch (e) {
                    /* ignore */
                }

                if (animationHandler._loopMonitorFrame % 60 === 0) {
                    const loopMode = clip?.loop;
                    const loopModeStr = loopMode === THREE.LoopRepeat ? '∞' : loopMode === THREE.LoopOnce ? '1x' : '?';
                    // If clip has loop frame metadata, compute current frame (relative to source)
                    const loopMeta = clip?._meta?.loopFrames;
                    if (loopMeta && loopMeta.fps) {
                        const currentFrame = Math.round(loopMeta.startFrame + (Number.isFinite(action.time) ? action.time * loopMeta.fps : 0));
                        const inRange = currentFrame >= loopMeta.startFrame && currentFrame <= loopMeta.endFrame;
                        console.log(`[render] 🔄 LOOP STATUS: paused=${action.paused}, time=${action.time?.toFixed(2)}s, clip=${clip?.name}, loopMode=${loopModeStr}, currentFrame=${currentFrame} (range ${loopMeta.startFrame}-${loopMeta.endFrame})`);
                        if (!inRange) {
                            console.warn(`[render] ⚠️ LOOP FRAME OUT OF RANGE: ${currentFrame} not in ${loopMeta.startFrame}-${loopMeta.endFrame}`);
                        }
                    } else {
                        console.log(`[render] 🔄 LOOP STATUS: paused=${action.paused}, time=${action.time?.toFixed(2)}s, clip=${clip?.name}, loopMode=${loopModeStr}`);
                    }
                }
            }
        }
    }
    controls.update();
    resizeRenderer();
    renderer.render(scene, camera);
}
render();

// Expose animation functions globally for message chain integration
window.VRMAnimations = {
    // Generic: allows arbitrary states (e.g. GAMING) without hardcoded additions.
    // NOTE: use the closure variable `animationHandler` as primary reference so
    // that VRM-load-time assignment (window.animationHandler = animationHandler
    // inside loadDefaultAnimations) is always reflected here, regardless of
    // the initial null stub set at module-load time.
    play: (state, opts = {}) => {
        try {
            const s = String(state || '').toLowerCase();
            const handler = animationHandler || window.animationHandler;
            if (!s || !handler) return;
            const animation = opts.animation || null;
            const playOnce = !!opts.playOnce;
            const playSection = opts.playSection || null;
            const descriptor = opts.descriptor || null;
            handler.startAction(s, animation, playOnce, playSection, descriptor);
        } catch (e) {
            console.warn('[synth_webui] VRMAnimations.play failed:', e);
        }
    },
    // Preload an animation file into the cache for instant playback later.
    preload: (state, file, descriptor) => {
        try {
            const handler = animationHandler || window.animationHandler;
            if (!handler || !file) return;
            handler.preloadAnimation(file, descriptor || null);
        } catch (e) {
            console.warn('[synth_webui] VRMAnimations.preload failed:', e);
        }
    },
    // Push VRM blend-shape / face values (emotions, expressions).
    // values: { [name: string]: number (0-1) }
    setFaceValues: (values) => {
        try {
            const handler = animationHandler || window.animationHandler;
            if (!handler || !values) return;
            for (const [key, val] of Object.entries(values)) {
                if (typeof handler._setFaceValue === 'function') {
                    handler._setFaceValue(key, val);
                }
            }
        } catch (e) {
            console.warn('[synth_webui] VRMAnimations.setFaceValues failed:', e);
        }
    },
    // Registry accessors for plugins/interfaces.
    getMappings: () => (window.VRMAnimationMappings || {}),
    setMappings: (m) => { window.VRMAnimationMappings = m || {}; },
    // NOTE: startThinking/startTalking are intentionally NOT exposed here.
    // Animations are now server-driven via vrm_animation WS messages.
    // The frontend plays whichever file the server selects; it never picks animations independently.
};
window.animationHandler = animationHandler;
console.log('[synth_webui] Animation functions exposed globally via window.VRMAnimations');
console.log('[synth_webui] animationHandler exposed globally');
try {
    const keys = Object.keys(animationHandler || {}).sort();
    console.debug('[synth_webui] animationHandler methods:', keys);
    console.debug('[synth_webui] has debug methods:', {
        setDebugFaceOverride: !!animationHandler.setDebugFaceOverride,
        getDebugFaceOverrides: !!animationHandler.getDebugFaceOverrides,
        _setFaceValue: !!animationHandler._setFaceValue,
    });

    // Immediately flush any pending actions queued while the stub was active
    try {
        const pa = window.__synth_pending_actions || [];
        if (Array.isArray(pa) && pa.length) {
            try { console.debug('[synth_webui] Immediately flushing', pa.length, 'pending actions on handler exposure'); } catch (e) { }
            pa.forEach((act) => {
                try {
                    if (!act || !act.type) return;
                    console.debug('[synth_webui] applying pending action:', act.type, act.args || []);
                    if (act.type === 'startAction' && typeof animationHandler.startAction === 'function') {
                        animationHandler.startAction(...(act.args || []));
                    } else if (act.type === 'startTemporaryLoop' && typeof animationHandler.startTemporaryLoop === 'function') {
                        animationHandler.startTemporaryLoop(...(act.args || []));
                    } else if (act.type === 'clearTemporaryOverride' && typeof animationHandler.clearTemporaryOverride === 'function') {
                        animationHandler.clearTemporaryOverride(...(act.args || []));
                    }
                } catch (e) { console.warn('[synth_webui] Failed to apply pending action', e); }
            });
            try { window.__synth_pending_actions = []; } catch (e) { /* ignore */ }
        }
    } catch (e) { /* ignore */ }


    // Install minimal shims for debug APIs if they are missing. This keeps
    // the debug UI functional even when the instance doesn't expose these
    // helpers as direct properties (some builds may attach them differently).
    try {
        if (!animationHandler.setDebugFaceOverride || typeof animationHandler.setDebugFaceOverride !== 'function') {
            animationHandler.setDebugFaceOverride = function (key, value) {
                try {
                    const k = (key !== undefined && key !== null) ? String(key) : '';
                    if (!k) return;
                    if (!this._debugFaceOverrides || typeof this._debugFaceOverrides !== 'object') this._debugFaceOverrides = {};
                    if (value === null || value === undefined || value === '') {
                        delete this._debugFaceOverrides[k];
                        try { this._setFaceValue(k, 0); } catch (e) { }
                        try { this._setFaceValue(k.replace(/\./g, '_'), 0); } catch (e) { }
                        try { this._flushFaceNow(); } catch (e) { }
                        return;
                    }
                    const v = Math.max(0, Math.min(1, Number(value) || 0));
                    try { this._debugFaceOverrides[k] = v; } catch (e) { }
                    try { this._setFaceValue(k, v); } catch (e) { }
                    try { this._flushFaceNow(); } catch (e) { }
                } catch (e) { console.warn('[synth_webui] shim.setDebugFaceOverride failed', e); }
            };
            console.debug('[synth_webui] shim setDebugFaceOverride installed');
        }
        if (!animationHandler.getDebugFaceOverrides || typeof animationHandler.getDebugFaceOverrides !== 'function') {
            animationHandler.getDebugFaceOverrides = function () {
                try { return (this._debugFaceOverrides && typeof this._debugFaceOverrides === 'object') ? this._debugFaceOverrides : {}; } catch (e) { return {}; }
            };
            console.debug('[synth_webui] shim getDebugFaceOverrides installed');
        }
    } catch (e) { /* ignore */ }

} catch (e) { /* ignore */ }

// Flush any preloaded descriptors queued before the handler was ready
try {
    if (window.__synth_pending_preloads) {
        try { console.debug('[synth_webui] Flushing', Object.keys(window.__synth_pending_preloads || {}).length, 'pending animation preloads'); } catch (e) { }
        for (const nm in window.__synth_pending_preloads) {
            try { animationHandler.preloadAnimation(nm, window.__synth_pending_preloads[nm]); } catch (e) { /* ignore */ }
        }
        try { window.__synth_pending_preloads = {}; } catch (e) { /* ignore */ }
    }
} catch (e) { /* ignore */ }

// Utilities: expose robust debug helper functions on window that
// try multiple application strategies so UIs don't rely on fragile
// internal shapes.
try {
    window.__synth_applyDebugFace = function (key, value) {
        try {
            if (!key) return false;
            // Prefer official API if present
            if (animationHandler && typeof animationHandler.setDebugFaceOverride === 'function') {
                try { animationHandler.setDebugFaceOverride(key, value); return true; } catch (e) { /* ignore */ }
            }
            // Fallback to direct setter
            try {
                if (animationHandler && typeof animationHandler._setFaceValue === 'function') {
                    animationHandler._setFaceValue(key, (value === null || value === undefined) ? 0 : Math.max(0, Math.min(1, Number(value) || 0)));
                    animationHandler._flushFaceNow && animationHandler._flushFaceNow();
                    return true;
                }
            } catch (e) { /* ignore */ }

            // Final fallback: attempt to resolve controller and call setValue directly
            try {
                const ctrl = (animationHandler && typeof animationHandler._getFaceController === 'function') ? animationHandler._getFaceController() : null;
                if (ctrl && typeof ctrl.setValue === 'function') {
                    ctrl.setValue(key, (value === null || value === undefined) ? 0 : Math.max(0, Math.min(1, Number(value) || 0)));
                    try { if (animationHandler) animationHandler._flushFaceNow && animationHandler._flushFaceNow(); } catch (e) { }
                    return true;
                }
            } catch (e) { /* ignore */ }

            return false;
        } catch (e) { return false; }
    };

    window.__synth_applyDebugEmotion = function (name, intensity) {
        try {
            if (!name) return false;
            if (animationHandler && typeof animationHandler.setDebugEmotionOverride === 'function') {
                try { animationHandler.setDebugEmotionOverride(name, intensity); return true; } catch (e) { /* ignore */ }
            }
            // Best-effort: store in _debugEmotionOverrides and trigger apply
            try {
                if (animationHandler) {
                    animationHandler._debugEmotionOverrides = animationHandler._debugEmotionOverrides || {};
                    if (intensity === null || intensity === undefined || intensity === '') {
                        delete animationHandler._debugEmotionOverrides[name];
                    } else {
                        animationHandler._debugEmotionOverrides[name] = Math.max(0, Math.min(1, Number(intensity) || 0));
                    }
                    try { animationHandler.setDebugEmotionOverride && animationHandler.setDebugEmotionOverride(name, animationHandler._debugEmotionOverrides[name]); } catch (e) { }
                    return true;
                }
            } catch (e) { /* ignore */ }
            return false;
        } catch (e) { return false; }
    };

    console.debug('[synth_webui] debug helper functions installed on window');
} catch (e) { /* ignore */ }

// Flush queued debug actions (face overrides) if any were queued before handler was ready
try {
    const q = window.__synth_pending_debug_actions || [];
    if (q && Array.isArray(q) && q.length) {
        try { console.debug('[synth_webui] Flushing', q.length, 'pending debug actions'); } catch (e) { }
        for (const act of q) {
            try {
                if (!act || !act.type) continue;
                if (act.type === 'setDebugFaceOverride') {
                    try { window.__synth_applyDebugFace && window.__synth_applyDebugFace(act.key, act.value); } catch (e) { /* ignore */ }
                } else if (act.type === 'clearDebugFaceOverrides') {
                    try { animationHandler.clearDebugFaceOverrides && animationHandler.clearDebugFaceOverrides(); } catch (e) { /* ignore */ }
                }
            } catch (e) { /* ignore */ }
        }
        try { window.__synth_pending_debug_actions = []; } catch (e) { /* ignore */ }
    }
} catch (e) { /* ignore */ }

// Flush pending actions queued before the handler was ready
try {
    const pa = window.__synth_pending_actions || [];
    if (Array.isArray(pa) && pa.length) {
        try { console.debug('[synth_webui] Flushing', pa.length, 'pending actions'); } catch (e) { }
        pa.forEach((act) => {
            try {
                if (!act || !act.type) return;
                try { console.debug('[synth_webui] applying pending action:', act.type, act.args || []); } catch (e) { }
                if (act.type === 'startAction' && typeof animationHandler.startAction === 'function') {
                    animationHandler.startAction(...(act.args || []));
                } else if (act.type === 'startTemporaryLoop' && typeof animationHandler.startTemporaryLoop === 'function') {
                    animationHandler.startTemporaryLoop(...(act.args || []));
                } else if (act.type === 'clearTemporaryOverride' && typeof animationHandler.clearTemporaryOverride === 'function') {
                    animationHandler.clearTemporaryOverride(...(act.args || []));
                }
            } catch (e) { console.warn('[synth_webui] Failed to apply pending action', act.type, e); }
        });
        try { window.__synth_pending_actions = []; } catch (e) { /* ignore */ }
    }
} catch (e) { /* ignore */ }

// Notify interested clients that the animationHandler is now ready
try {
    try { window.dispatchEvent(new CustomEvent('synth_animation_handler_ready')); } catch (e) { }
    try { window.dispatchEvent(new CustomEvent('synth_animation_handler_ready_local')); } catch (e) { }
    try { window.dispatchEvent(new CustomEvent('synth_animation_handler_ready_global')); } catch (e) { }
    if (!window.__synth_animation_outro_completed_handler) {
        window.__synth_animation_outro_completed_handler = (ev) => {
            try {
                console.log('[synth_webui] Received synth_animation_outro_completed for', ev?.detail?.key);
                const pending = window.__synth_pending_action_state || null;
                if (!pending) return;

                // guard: avoid applying the same pending more than once
                const lastApplied = window.__synth_last_applied_pending || null;
                if (lastApplied && lastApplied.action_id && pending.action_id && lastApplied.action_id === pending.action_id) {
                    console.debug('[synth_webui] Skipping duplicate pending application for', pending.action_id);
                    // Clear pending to avoid repeated attempts
                    window.__synth_pending_action_state = null;
                    try { if (window.__synth_pending_action_state_timeout) clearTimeout(window.__synth_pending_action_state_timeout); window.__synth_pending_action_state_timeout = null; } catch (e) { }
                    return;
                }

                console.log('[synth_webui] Applying previously-queued action_state after outro:', pending);
                // Re-run the action_state processing path with the stored payload
                // (we re-run minimal logic to avoid duplicating queuing behavior)
                const phaseToAnimationState = {
                    'THINKING': 'think', 'WRITING': 'write', 'CORRECTING': 'think', 'TALKING': 'talk', 'IDLE': 'idle'
                };
                const incomingPhase = pending.phase || 'IDLE';
                const incomingActionId = pending.action_id || null;
                const animationState = phaseToAnimationState[incomingPhase] || 'idle';

                // Apply the incoming state now that the previous high-priority action is done
                currentDisplayedPhase = incomingPhase;
                currentDisplayedActionId = incomingActionId;

                // IMPORTANT: action_state does not start animations; it's informational/UI only.
                console.log('[synth_webui] Applied queued action_state after outro (no playback):', incomingPhase, incomingActionId, animationState);

                // Mark as applied and Clear pending
                window.__synth_last_applied_pending = { action_id: incomingActionId, phase: incomingPhase, ts: Date.now() };
                window.__synth_pending_action_state = null;
                try { if (window.__synth_pending_action_state_timeout) clearTimeout(window.__synth_pending_action_state_timeout); window.__synth_pending_action_state_timeout = null; } catch (e) { }
            } catch (err) { console.warn('[synth_webui] Error while applying queued action_state after outro:', err); }
        };
        window.addEventListener('synth_animation_outro_completed', window.__synth_animation_outro_completed_handler);
    }
} catch (e) { /* ignore */ }

// Initialize debug UI when WEB_DEBUG is enabled in the base template
try {
    // Consolidation: delegate debug UI to the centralized module and disable legacy inline debug code below.
    try {
        const _synthDbgElem = document.getElementById('synth-debug');
        const _dbgEnabled = _synthDbgElem && (_synthDbgElem.dataset.debugEnabled === '1' || _synthDbgElem.dataset.debugEnabled === 'true');
        if (_dbgEnabled) {
            // Load consolidated debug window module and create window (async)
            (async () => {
                try {
                    const mod = await import('/js/debug-window.mjs');
                    try { if (mod && typeof mod.createDebugWindow === 'function') mod.createDebugWindow(); } catch (e) { /* ignore */ }
                } catch (e) { console.warn('[synth_webui] Failed to import debug-window module', e); }
            })();
            // Prevent legacy inline debug block from running by clearing the attribute.
            try { _synthDbgElem.dataset.debugEnabled = '0'; } catch (e) { /* ignore */ }
        }
    } catch (e) { /* ignore */ }

    const synthDebug = document.getElementById('synth-debug');
    const debugEnabled = synthDebug && (synthDebug.dataset.debugEnabled === '1' || synthDebug.dataset.debugEnabled === 'true');
    try { window.__synth_web_debug_enabled = !!debugEnabled; } catch (e) { /* ignore */ }
    if (debugEnabled) { console.log('[synth_webui] WEB_DEBUG delegated — legacy block removed'); }
    console.log('[synth_webui] WEB_DEBUG enabled — initializing advanced debug window');



    // UI pause flag is tracked locally.
    // The render/WS freeze gates elsewhere still additionally check __synth_web_debug_enabled.
    const isPaused = () => !!window.__synth_debug_pause_all;





    const buildDebugPanel = () => { return null; };

    let win = null;
    let winbox = null;
    const tryCreateWinBox = () => null;



    if (!winbox) {
        try {
            // Dragging (simple, chat-like)
            (function makeDraggable(el) {
                const header = el.querySelector('#synth-debug-title-bar');
                if (!header) return;
                let dragging = false;
                let startX = 0, startY = 0;
                let offsetX = 0, offsetY = 0;
                header.addEventListener('pointerdown', (ev) => {
                    try {
                        // Prevent resize handles beneath the title bar from stealing pointer events
                        try { ev.stopPropagation(); } catch (e) { }
                        // Respect global active interactions (avoid interfering with chat drag/other resizes)
                        try { if (window.__synth_active_interaction) return; } catch (e) { }
                        // Don't start dragging when the user is clicking controls in the title bar.
                        const t = ev && ev.target ? ev.target : null;
                        if (t && typeof t.closest === 'function') {
                            if (t.closest('button, input, select, textarea, a')) return;
                        }
                        dragging = true;
                        const dragPointerId = (ev.pointerId !== undefined) ? ev.pointerId : 'mouse';
                        try { window.__synth_active_interaction = { type: 'debug_drag', id: dragPointerId }; } catch (e) { }
                        // Normalize to left/top positioning so drag+resize behave consistently.
                        const r = el.getBoundingClientRect();
                        try {
                            el.style.left = r.left + 'px';
                            el.style.top = r.top + 'px';
                            el.style.right = 'auto';
                            el.style.bottom = 'auto';
                        } catch (e) { /* ignore */ }
                        startX = ev.clientX;
                        startY = ev.clientY;
                        offsetX = startX - r.left;
                        offsetY = startY - r.top;
                        try { header.setPointerCapture && header.setPointerCapture(ev.pointerId); } catch (e) { }
                    } catch (e) { /* ignore */ }
                });
                window.addEventListener('pointermove', (ev) => {
                    if (!dragging) return;
                    try {
                        const topbar = (document.querySelector('header.top-bar') && document.querySelector('header.top-bar').getBoundingClientRect().height) ? Math.ceil(document.querySelector('header.top-bar').getBoundingClientRect().height) : 0;
                        const w = el.offsetWidth || 320;
                        const h = el.offsetHeight || 240;
                        const viewportW = Math.max(window.innerWidth || 0, document.documentElement.clientWidth || 0);
                        const viewportH = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0);
                        const maxX = Math.max(0, viewportW - w);
                        const maxY = Math.max(topbar, viewportH - h);
                        let tx = Math.round(ev.clientX - offsetX);
                        let ty = Math.round(ev.clientY - offsetY);
                        tx = Math.min(maxX, Math.max(0, tx));
                        ty = Math.min(maxY, Math.max(topbar, ty));
                        el.style.left = tx + 'px';
                        el.style.top = ty + 'px';
                        el.style.right = 'auto';
                        el.style.bottom = 'auto';
                    } catch (e) { /* ignore */ }
                });
                window.addEventListener('pointerup', (ev) => { try { dragging = false; if (window.__synth_active_interaction && window.__synth_active_interaction.type === 'debug_drag') window.__synth_active_interaction = null; } catch (e) { } });
            })(win);

            // Add chat-like resize handles for the debug window so resizing anchors work
            try { createResizeHandlesForElement(win); } catch (e) { /* ignore */ }

            // Dock button for minimized state
            let debugDockBtn = null;
            const ensureDebugDockBtn = () => {
                if (debugDockBtn && debugDockBtn.isConnected) return debugDockBtn;
                debugDockBtn = document.createElement('button');
                debugDockBtn.type = 'button';
                // Match chat bubble styling when minimized.
                debugDockBtn.className = 'chat-toggle-btn';
                debugDockBtn.textContent = '💻';
                debugDockBtn.setAttribute('aria-label', 'Restore debug');
                debugDockBtn.title = 'Restore Debug';
                try {
                    // When in the minimized stack, behave as a normal element.
                    debugDockBtn.style.position = 'static';
                    debugDockBtn.style.top = '';
                    debugDockBtn.style.right = '';
                    debugDockBtn.style.bottom = '';
                    debugDockBtn.style.left = '';
                    debugDockBtn.style.zIndex = '';
                } catch (e) { /* ignore */ }
                debugDockBtn.addEventListener('click', () => {
                    try { win.style.display = ''; } catch (e) { /* ignore */ }
                    try { if (debugDockBtn && debugDockBtn.parentElement) debugDockBtn.parentElement.removeChild(debugDockBtn); } catch (e) { /* ignore */ }
                });
                return debugDockBtn;
            };

            const minimizeBtnLegacy = win.querySelector('#synth-debug-minimize');
            if (minimizeBtnLegacy) {
                minimizeBtnLegacy.addEventListener('click', () => {
                    try { win.style.display = 'none'; } catch (e) { /* ignore */ }
                    try { getDock().appendChild(ensureDebugDockBtn()); } catch (e) { /* ignore */ }
                });
            }
        } catch (e) { /* ignore */ }

        async function resyncFromBackend() {
            try {
                if (isPaused()) return;

                // Apply rich state first if we have it (blink/eye/expressions)
                try {
                    const st = window.__synth_last_rich_animation_state || null;
                    if (st && typeof animationHandler.applyAnimationState === 'function') {
                        animationHandler.applyAnimationState(st);
                    }
                } catch (e) { /* ignore */ }

                try {
                    const resp = await fetch('/api/animation_state');
                    if (resp && resp.ok) {
                        const summary = await resp.json();
                        if (summary && summary.state) {
                            const playOnce = !!(summary.descriptor && summary.descriptor.play_once);
                            await animationHandler.startAction(summary.state, summary.animation || null, playOnce, null, summary.descriptor || null);
                            return;
                        }
                    }
                } catch (e) {
                    // ignore and fall back
                }

                // Fallback: last remote animation payload
                try {
                    const last = (window.__synth_debug_last_remote && window.__synth_debug_last_remote.animation) ? window.__synth_debug_last_remote.animation : null;
                    if (last && last.state) {
                        const playOnce = (last.descriptor && last.descriptor.play_once) || (last.loop === false);
                        if (last.animation_state && typeof animationHandler.applyAnimationState === 'function') {
                            animationHandler.applyAnimationState(last.animation_state);
                        }
                        await animationHandler.startAction(last.state, last.animation || null, !!playOnce, last.play_section || null, last.descriptor || null);
                    }
                } catch (e) { /* ignore */ }
            } catch (e) { /* ignore */ }
        }

        const pauseBtn = null; const resyncBtn = null; const resetBtn = null;

        const setPaused = async (paused) => {
            try {
                window.__synth_debug_pause_all = !!paused;
                // Ensure WEB_DEBUG behavior stays enabled while the debug window is used.
                window.__synth_web_debug_enabled = true;
            } catch (e) { /* ignore */ }
            try {
                if (animationHandler) {
                    if (paused) {
                        try { if (typeof animationHandler._stopBlinkLoop === 'function') animationHandler._stopBlinkLoop(); } catch (e) { /* ignore */ }
                        try { if (typeof animationHandler._stopEyeMovement === 'function') animationHandler._stopEyeMovement(); } catch (e) { /* ignore */ }
                    } else {
                        try { if (animationHandler._blinkAutoEnabled && typeof animationHandler._startBlinkLoop === 'function') animationHandler._startBlinkLoop(); } catch (e) { /* ignore */ }
                        try { if (animationHandler._eyeAutoEnabled && typeof animationHandler._startEyeMovement === 'function') animationHandler._startEyeMovement(); } catch (e) { /* ignore */ }
                    }
                }
            } catch (e) { /* ignore */ }

            try {
                if (pauseBtn) {
                    pauseBtn.textContent = paused ? '▶️' : '⏸️';
                    pauseBtn.title = paused ? 'Play' : 'Pause';
                    pauseBtn.setAttribute('aria-label', paused ? 'Play' : 'Pause');
                }
            } catch (e) { /* ignore */ }
            if (!paused) {
                await resyncFromBackend();
            }
        };

        if (pauseBtn) {
            pauseBtn.addEventListener('click', async () => {
                try { await setPaused(!isPaused()); } catch (e) { /* ignore */ }
            });
        }
        if (resyncBtn) {
            resyncBtn.addEventListener('click', async () => { await resyncFromBackend(); });
        }
        if (resetBtn) {
            resetBtn.addEventListener('click', async () => {
                try { if (animationHandler) { animationHandler.clearDebugFaceOverrides?.(); animationHandler.clearDebugEmotionOverrides?.(); animationHandler.clearTemporaryOverride?.(); } } catch (e) { /* ignore */ }
                try { await resetLoopOverrideUI(); } catch (e) { /* ignore */ }
                await setPaused(false);
                await resyncFromBackend();
            });
        }

        // Loop override controls
        const types = ['idle', 'think', 'talk', 'write', 'touch'];
        const selType = null; const selFile = null; const startInput = null; const endInput = null; const fpsInput = null; const loadedSpan = null;

        if (fpsInput) fpsInput.value = '30';
        if (selType) {
            types.forEach((t) => {
                const opt = document.createElement('option');
                opt.value = t;
                opt.textContent = t;
                selType.appendChild(opt);
            });
        }

        async function refreshFilesForType(actionType) {
            try {
                const files = animationHandler ? await animationHandler.getAnimationsForType(actionType) : (animationMappings[actionType] || []);
                if (selFile) selFile.innerHTML = '';
                if (!files || files.length === 0) {
                    if (selFile) {
                        const o = document.createElement('option');
                        o.value = '';
                        o.textContent = '— no files —';
                        selFile.appendChild(o);
                    }
                    if (loadedSpan) loadedSpan.textContent = '0';
                    return;
                }
                files.forEach((f) => {
                    const o = document.createElement('option');
                    o.value = f;
                    o.textContent = f;
                    if (selFile) selFile.appendChild(o);
                });
                if (loadedSpan) loadedSpan.textContent = String(files.length);
            } catch (err) {
                console.warn('[synth_webui] Failed to refresh debug file list:', err);
            }
        }

        const getDescriptorForFile = (file) => {
            try {
                if (!file) return null;
                // From handler caches
                try {
                    if (animationHandler && animationHandler.loadedDescriptors) {
                        const norm = (typeof animationHandler._normalizeAnimationKey === 'function') ? animationHandler._normalizeAnimationKey(file) : String(file);
                        return animationHandler.loadedDescriptors[norm] || animationHandler.loadedDescriptors[file] || null;
                    }
                } catch (e) { /* ignore */ }
                // From preloads accumulated before handler exists
                try {
                    if (window.__synth_preloaded_animations && window.__synth_preloaded_animations[file]) return window.__synth_preloaded_animations[file];
                    if (window.__synth_pending_preloads && window.__synth_pending_preloads[file]) return window.__synth_pending_preloads[file];
                } catch (e) { /* ignore */ }
                return null;
            } catch (e) {
                return null;
            }
        };

        const computeMaxFramesFromDescriptor = (descriptor) => {
            try {
                if (!descriptor || typeof descriptor !== 'object') return 0;
                const nums = [];
                const pushNum = (n) => { if (Number.isFinite(n)) nums.push(Number(n)); };
                pushNum(descriptor.max_frames);
                pushNum(descriptor.maxFrames);
                try { pushNum(descriptor.intro && descriptor.intro.end_frame); } catch (e) { /* ignore */ }
                try { pushNum(descriptor.loop && descriptor.loop.end_frame); } catch (e) { /* ignore */ }
                try { pushNum(descriptor.outro && descriptor.outro.end_frame); } catch (e) { /* ignore */ }
                return nums.length ? Math.max(0, Math.round(Math.max(...nums))) : 0;
            } catch (e) {
                return 0;
            }
        };

        const computeMaxFramesFromClip = (clip, fps) => {
            try {
                const f = Number(fps);
                if (!clip || !Number.isFinite(f) || f <= 0) return 0;
                const totalFrames = Math.max(1, Math.round(Number(clip.duration || 0) * f));
                // Return max frame index (inclusive)
                return Math.max(0, totalFrames - 1);
            } catch (e) {
                return 0;
            }
        };

        const autofillLoopInputs = async () => {
            try {
                if (!selFile || !startInput || !endInput) return;
                const file = selFile.value;
                if (!file) return;

                const descriptor = getDescriptorForFile(file);
                const desiredFps = (descriptor && Number.isFinite(Number(descriptor.fps))) ? Number(descriptor.fps) : Number((fpsInput && fpsInput.value) ? fpsInput.value : 30);
                const fps = (Number.isFinite(desiredFps) && desiredFps > 0) ? desiredFps : 30;
                try { if (fpsInput) fpsInput.value = String(fps); } catch (e) { /* ignore */ }

                let maxFrameIndex = 0;
                try {
                    if (animationHandler && animationHandler.loadedAnimations) {
                        const norm = (typeof animationHandler._normalizeAnimationKey === 'function') ? animationHandler._normalizeAnimationKey(file) : file;
                        let clip = animationHandler.loadedAnimations[norm] || animationHandler.loadedAnimations[file] || null;
                        if (!clip && typeof animationHandler.loadAnimation === 'function') {
                            const actionType = (selType && selType.value) ? selType.value : 'think';
                            try { clip = await animationHandler.loadAnimation(actionType, file); } catch (e) { /* ignore */ }
                        }
                        maxFrameIndex = computeMaxFramesFromClip(clip, fps);
                    }
                } catch (e) { /* ignore */ }
                if (!maxFrameIndex) maxFrameIndex = computeMaxFramesFromDescriptor(descriptor);

                startInput.value = '0';
                endInput.value = String(maxFrameIndex || 0);
            } catch (e) { /* ignore */ }
        };

        if (selType) {
            selType.addEventListener('change', async () => {
                await refreshFilesForType(selType.value);
                try { if (selFile) selFile.selectedIndex = 0; } catch (e) { /* ignore */ }
                await autofillLoopInputs();
            });
            try { selType.value = 'think'; } catch (e) { /* ignore */ }
            (async () => {
                await refreshFilesForType('think');
                try { if (selFile) selFile.selectedIndex = 0; } catch (e) { /* ignore */ }
                await autofillLoopInputs();
            })();
        }
        // Loop override UI removed (consolidated into debug-window.mjs).

        // Feelings UI removed; consolidated in debug-window.mjs

        // Facial morph UI removed; consolidated in debug-window.mjs

        // Live status updater, loop UI, feelings/face render and VRM hooks removed (consolidated into debug-window.mjs).
    }
} catch (err) {
    console.warn('[synth_webui] Failed to init animation debug panel:', err);
}

// ------------------------------------------------------------------
// Touch interaction: tap (pointer click without drag) on the model
// should trigger an animation from /skins/<skin>/animations/touch/
// We also capture the touched part name for future engine use.
// ------------------------------------------------------------------
(function setupTouchInteraction() {
    try {
        const raycaster = new THREE.Raycaster();
        const mouse = new THREE.Vector2();
        let pointerDownInfo = null;
        let isDragging = false;

        function _playScreenKnockSfx() {
            try {
                const now = Date.now();
                // simple debounce to avoid spamming
                if (now - (__synthLastKnockAt || 0) < 180) return;
                __synthLastKnockAt = now;

                // Prefer WebAudio (already used elsewhere in the page) for stutter-free playback.
                try { ensureAudioContext(true); } catch (_e) { /* ignore */ }
                if (audioContext && __synthKnockSfx && __synthKnockSfx.buffer) {
                    try {
                        const src = audioContext.createBufferSource();
                        const gain = audioContext.createGain();
                        gain.gain.value = 0.55;
                        src.buffer = __synthKnockSfx.buffer;
                        src.connect(gain);
                        gain.connect(audioContext.destination);
                        src.start(0);
                        return;
                    } catch (_e) {
                        // fall through to HTMLAudio fallback
                    }
                }

                // Fallback: HTMLAudio (may stutter on first decode on some browsers)
                if (!__synthKnockAudio) {
                    __synthKnockAudio = new Audio('/static/effects/glass-knock-1-189096.mp3');
                    __synthKnockAudio.preload = 'auto';
                    __synthKnockAudio.volume = 0.55;
                    // Best-effort warmup to reduce first-play stutter.
                    try { __synthKnockAudio.load(); } catch (_e) { /* ignore */ }
                }
                try { __synthKnockAudio.currentTime = 0; } catch (_e) { /* ignore */ }
                const p = __synthKnockAudio.play();
                if (p && typeof p.catch === 'function') p.catch(() => { });
            } catch (e) {
                console.debug('[synth_webui] knock sfx failed:', e);
            }
        }

        async function _ensureKnockSfxDecoded() {
            try {
                if (__synthKnockSfx.buffer) return __synthKnockSfx.buffer;
                if (__synthKnockSfx.loading) return await __synthKnockSfx.loading;
                try { ensureAudioContext(true); } catch (_e) { /* ignore */ }
                if (!audioContext) return null;

                __synthKnockSfx.loading = (async () => {
                    const resp = await fetch('/static/effects/glass-knock-1-189096.mp3', { cache: 'force-cache' });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    const arr = await resp.arrayBuffer();
                    const buf = await new Promise((resolve, reject) => {
                        try {
                            audioContext.decodeAudioData(arr, resolve, reject);
                        } catch (e) {
                            reject(e);
                        }
                    });
                    __synthKnockSfx.buffer = buf;
                    return buf;
                })();

                const out = await __synthKnockSfx.loading;
                return out;
            } catch (e) {
                try { __synthKnockSfx.loading = null; } catch (_e) { /* ignore */ }
                return null;
            }
        }

        function _triggerSoftLookTowardCamera() {
            try {
                if (!currentVRM || !camera) return;
                const now = Date.now();
                __synthKnockLook = __synthKnockLook || { activeUntil: 0, startedAt: 0, durationMs: 520, maxStrength: 0.32 };
                if (!Number.isFinite(__synthKnockLook.durationMs)) __synthKnockLook.durationMs = 520;
                if (!Number.isFinite(__synthKnockLook.maxStrength)) __synthKnockLook.maxStrength = 0.32;
                __synthKnockLook.startedAt = now;
                __synthKnockLook.activeUntil = now + __synthKnockLook.durationMs;
            } catch (e) { /* ignore */ }
        }

        // Map raw node/mesh names to human-friendly body part labels.
        // Heuristics: strip common suffixes like (merged), baked, numeric suffixes,
        // then match common tokens via ordered regex rules. Returns { label, confidence }.
        function mapTouchedNodeToHuman(rawName) {
            if (!rawName) return { label: 'unknown', confidence: 0 };
            let name = String(rawName).toLowerCase();
            // remove parenthesis content and common suffixes
            name = name.replace(/\(.*?\)/g, ' ');
            name = name.replace(/[_\-]+/g, ' ');
            name = name.replace(/\b(merged|baked|combined|mesh|grp|group|lod\d*|low|high|geo)\b/g, ' ');
            name = name.replace(/\d+/g, ' ');
            name = name.replace(/\s+/g, ' ').trim();

            const patterns = [
                { r: /\b(cheek)\b/, label: 'cheek' },
                { r: /\b(hair|bangs|ponytail|braid)\b/, label: 'hair' },
                { r: /\b(head|skull|face|jaw|chin|nose|mouth|lip|ear|eye|brow)\b/, label: 'head' },
                { r: /\b(shoulder|clavicle|collar)\b/, label: 'shoulder' },
                { r: /\b(upper ?arm|bicep|tricep)\b/, label: 'upper_arm' },
                { r: /\b(forearm|fore_arm|radius|ulna)\b/, label: 'forearm' },
                { r: /\b(hand|palm|finger|thumb|index|middle|ring|pinky)\b/, label: 'hand' },
                { r: /\b(chest|breast|bust|torso|thorax)\b/, label: 'bust' },
                { r: /\b(abdomen|belly|stomach)\b/, label: 'abdomen' },
                { r: /\b(pelvis|crotch|groin|hip)\b/, label: 'crotch' },
                { r: /\b(thigh|quadriceps|hamstring)\b/, label: 'thigh' },
                { r: /\b(knee)\b/, label: 'knee' },
                { r: /\b(calf|shin|leg|tibia)\b/, label: 'leg' },
                { r: /\b(foot|toe|heel)\b/, label: 'foot' },
                { r: /\b(back|spine|shoulderblade|scapula)\b/, label: 'back' },
                { r: /\b(eye|nose|mouth|ear)\b/, label: 'face' }
            ];

            for (let p of patterns) {
                if (p.r.test(name)) return { label: p.label, confidence: 0.95 };
            }

            // fallback: use first token or the raw cleaned name
            const first = (name.split(' ')[0] || '').trim();
            if (first) return { label: first, confidence: 0.45 };
            return { label: 'unknown', confidence: 0 };
        }

        canvas.addEventListener('pointerdown', (ev) => {
            pointerDownInfo = { x: ev.clientX, y: ev.clientY, t: Date.now() };
            isDragging = false;
            // User gesture: kick off SFX decode in background to avoid stutter on first knock.
            try { _ensureKnockSfxDecoded(); } catch (_e) { /* ignore */ }
        });

        canvas.addEventListener('pointermove', (ev) => {
            if (!pointerDownInfo) return;
            const dx = ev.clientX - pointerDownInfo.x;
            const dy = ev.clientY - pointerDownInfo.y;
            if (Math.sqrt(dx * dx + dy * dy) > 6) {
                isDragging = true;
            }
        });

        canvas.addEventListener('pointerup', async (ev) => {
            try {
                const down = pointerDownInfo;
                pointerDownInfo = null;
                if (!down) return;
                const dt = Date.now() - down.t;
                if (isDragging || dt > 1000) return; // treat as drag or long press

                // compute normalized device coords
                const rect = canvas.getBoundingClientRect();
                mouse.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
                mouse.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;

                raycaster.setFromCamera(mouse, camera);
                // Only test against the current VRM scene to avoid hitting background
                // geometry or other non-avatar objects which would incorrectly
                // trigger touch animations when clicking empty space.
                if (!currentVRM || !currentVRM.scene) return;
                const targets = (window.__synthRaycastTargets && window.__synthRaycastTargets.length) ? window.__synthRaycastTargets : [currentVRM.scene];
                const intersects = raycaster.intersectObjects(targets, false);
                if (!intersects || intersects.length === 0) {
                    // Empty space tap: play knock SFX and turn towards the camera.
                    _playScreenKnockSfx();
                    _triggerSoftLookTowardCamera();
                    return;
                }

                const intersect = intersects[0];
                let node = intersect.object;
                // climb up to find a named node (bone/mesh) inside the VRM
                let touchedPart = null;
                while (node && node !== currentVRM?.scene && node !== scene && !touchedPart) {
                    if (node.name && node.name.trim()) touchedPart = node.name;
                    node = node.parent;
                }
                if (!touchedPart) touchedPart = intersect.object.name || 'unknown';

                // Map raw node name to a human-friendly label and expose both
                const mapped = (typeof mapTouchedNodeToHuman === 'function') ? mapTouchedNodeToHuman(touchedPart) : { label: touchedPart || 'unknown', confidence: 0 };
                console.log('[synth_webui] Model tapped - touched part:', touchedPart, 'mapped:', mapped);
                window.lastTouchedPart = { part: touchedPart, at: Date.now() };
                window.lastTouchedPartHuman = { part: mapped.label, raw: touchedPart, confidence: mapped.confidence, at: Date.now(), method: 'heuristic' };

                // Load available touch animations for current skin
                if (!animationHandler || typeof animationHandler.getAnimationsForType !== 'function') return;
                const files = await animationHandler.getAnimationsForType('touch');
                if (!files || files.length === 0) {
                    console.log('[synth_webui] No touch animations found for current skin');
                    return;
                }

                // pick a random animation
                const animationFile = files[Math.floor(Math.random() * files.length)];

                // Try to fetch descriptor next to the animation file (filename + .json)
                let descriptor = null;
                try {
                    const skin = window.activeSkinName ? window.activeSkinName.split('/').pop().replace('.vrm', '') : 'Rei';
                    const descUrl = `/api/skins/${skin}/animations/touch/${encodeURIComponent(animationFile)}.json`;
                    const resp = await fetch(descUrl);
                    if (resp.ok) descriptor = await resp.json();
                } catch (err) {
                    // ignore descriptor fetch errors
                }

                const playOnce = !!(descriptor && descriptor.play_once);

                console.log('[synth_webui] Triggering touch animation:', animationFile, 'playOnce=', playOnce, 'descriptor=', descriptor);
                try {
                    animationHandler.startAction('touch', animationFile, playOnce);
                } catch (err) {
                    console.warn('[synth_webui] Failed to start touch action:', err);
                }

                // Notify backend about the touch (optional, reserved for future engine use)
                try {
                    if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
                        try {
                            const touchPayload = {
                                type: 'touch',
                                part: touchedPart,
                                mapped_part: window.lastTouchedPartHuman ? window.lastTouchedPartHuman.part : null,
                                mapped_confidence: window.lastTouchedPartHuman ? window.lastTouchedPartHuman.confidence : null,
                                animation: animationFile,
                                descriptor: descriptor || null
                            };
                            ws.send(JSON.stringify(touchPayload));
                        } catch (err) {
                            console.warn('[synth_webui] Failed to send touch payload:', err);
                        }
                    }
                } catch (err) {
                    console.warn('[synth_webui] Failed to notify backend of touch:', err);
                }

            } catch (err) {
                console.warn('[synth_webui] touch handler error:', err);
            }
        });

        console.log('[synth_webui] Touch interaction initialized (tap to play touch animations)');
    } catch (err) {
        console.warn('[synth_webui] Failed to setup touch interaction:', err);
    }
})();

function renderList(data) {
    if (!listEl) return;
    listEl.innerHTML = '';
    if (!data.models.length) {
        const li = document.createElement('li');
        li.className = 'empty';
        li.textContent = 'No models available';
        listEl.appendChild(li);
        clearVRM();
        currentModel = null;
        return;
    }
    data.models.forEach((model) => {
        const li = document.createElement('li');
        if (model.active) li.classList.add('active');
        const nameSpan = document.createElement('span');
        nameSpan.className = 'name';
        nameSpan.textContent = model.name;
        const actions = document.createElement('div');
        actions.className = 'vrm-actions';

        const activate = document.createElement('button');
        activate.textContent = model.active ? 'Active' : 'Activate';
        if (!model.active) {
            activate.addEventListener('click', (event) => {
                // Provide immediate visual feedback
                activate.disabled = true;
                const prevText = activate.textContent;
                activate.textContent = '⏳ Activating...';
                setActive(model.name, activate).catch(() => {
                    // Restore state on error
                    activate.disabled = false;
                    activate.textContent = prevText;
                });
            });
        } else {
            activate.disabled = true;
        }

        const remove = document.createElement('button');
        remove.textContent = 'Delete';
        remove.addEventListener('click', () => removeModel(model.name));

        actions.appendChild(activate);
        actions.appendChild(remove);
        li.appendChild(nameSpan);
        li.appendChild(actions);
        listEl.appendChild(li);
    });
}

// Call refreshConfig if available, otherwise skip silently to avoid
// hard dependency on load ordering between modules.
(typeof window !== 'undefined' && typeof window.refreshConfig === 'function' ? window.refreshConfig() : Promise.resolve()).catch((error) => console.error(error));

// Restore chat state on page load if we're on home tab. Use a safe lookup
// that tolerates missing globals (modules may not see non-module globals).
(function () {
    try {
        const _activeTab = (typeof activeTab !== 'undefined') ? activeTab : (typeof window !== 'undefined' ? (window.activeTab || (localStorage && localStorage.getItem && localStorage.getItem('synth-webui-active-tab')) || (document.querySelector && document.querySelector('.nav-btn.active') && document.querySelector('.nav-btn.active').getAttribute('data-tab')) || 'home') : 'home');
        if (_activeTab === 'home') {
            try {
                try { if (window.SynthChat && typeof window.SynthChat.restoreChatState === 'function') window.SynthChat.restoreChatState(); } catch (e) { /* ignore */ }
            } catch (error) {
                console.error('[synth_webui] Failed to restore chat state on load:', error);
            }
        }
    } catch (e) { /* ignore */ }
})();

// Diary functionality (legacy diary tab). Guard against missing elements
// since history is now loaded from section templates.
const diarySearchEl = document.getElementById('diary-search');
if (diarySearchEl) {
    let diaryEntries = [];
    let selectedEntries = new Set();

    let currentPage = 1;
    let currentPerPage = 10;
    let totalPages = 1;
    let totalEntries = 0;

    function loadDiaryEntries(page = 1, perPage = currentPerPage) {
        const searchTerm = document.getElementById('diary-search').value.toLowerCase();

        currentPage = page;
        currentPerPage = perPage;

        let url = '/api/diary?';
        const params = [];



        // Always use server-side pagination for better performance
        if (currentPerPage !== 'unlimited') {
            params.push(`page=${page}`);
            params.push(`per_page=${currentPerPage}`);
        } else {
            params.push('limit=1000'); // High limit for unlimited
        }

        // Add search term to server request if present
        if (searchTerm) {
            params.push(`search=${encodeURIComponent(searchTerm)}`);
        }

        url += params.join('&');

        fetch(url)
            .then(response => response.json())
            .then(data => {
                diaryEntries = data.diary?.entries || [];
                totalEntries = data.diary?.total_count || 0;
                totalPages = data.diary?.total_pages || 1;

                updatePaginationControls();
                renderDiaryEntries();
            })
            .catch(error => {
                console.error('Error loading diary entries:', error);
                document.getElementById('diary-entries').innerHTML = '<div class="error">Failed to load diary entries</div>';
            });
    }

    function renderDiaryEntries() {
        const container = document.getElementById('diary-entries');
        const groupByDate = document.getElementById('group-by-date').checked;
        const searchTerm = document.getElementById('diary-search').value.toLowerCase();

        // Since we now use server-side pagination and search, diaryEntries already contains
        // the correct filtered and paginated results
        let displayEntries = diaryEntries;

        if (displayEntries.length === 0) {
            container.innerHTML = searchTerm ? '<div class="meta">No entries found matching your search.</div>' : '<div class="loading">No diary entries found.</div>';
            return;
        }

        if (groupByDate) {
            const grouped = {};
            displayEntries.forEach(entry => {
                const date = new Date(entry.timestamp).toDateString();
                if (!grouped[date]) grouped[date] = [];
                grouped[date].push(entry);
            });

            let html = '';
            Object.keys(grouped).sort((a, b) => new Date(b) - new Date(a)).forEach(date => {
                html += `
                            <div class="diary-date-group">
                                <div class="diary-date-header" onclick="toggleDateGroup(this)">
                                    <span>${date}</span>
                                    
                                </div>
                                <div class="diary-date-content">
                                    ${grouped[date].map(entry => renderDiaryEntry(entry)).join('')}
                                </div>
                            </div>
                        `;
            });
            container.innerHTML = html;
        } else {
            container.innerHTML = displayEntries.map(entry => renderDiaryEntry(entry)).join('');
        }

        updatePaginationControls();
    }

    function renderDiaryEntry(entry) {
        const isArchived = entry.archived || false;
        const isSelected = selectedEntries.has(entry.id);

        // Format emotions array
        let emotionsText = '';
        if (entry.emotions && Array.isArray(entry.emotions)) {
            emotionsText = entry.emotions.map(e => `${e.type} (${e.intensity})`).join(', ');
        }

        // Format involved users
        let usersText = '';
        if (entry.involved_users && Array.isArray(entry.involved_users) && entry.involved_users.length > 0) {
            usersText = entry.involved_users.join(', ');
        }

        return `
                    <div class="diary-entry ${isArchived ? 'archived' : ''}" data-id="${entry.id}">
                        <input type="checkbox" class="diary-entry-checkbox" ${isSelected ? 'checked' : ''} onchange="toggleEntrySelection(${entry.id})" style="display: ${document.getElementById('edit-mode-btn').textContent === 'Done' ? 'inline' : 'none'};">
                        <div class="diary-entry-content">
                            <div class="diary-entry-text">${escapeHtml(entry.content || '')}</div>
                            ${entry.personal_thought ? `<div class="diary-entry-meta"><strong>Personal thought:</strong> ${escapeHtml(entry.personal_thought)}</div>` : ''}
                            ${emotionsText ? `<div class="diary-entry-meta"><strong>Emotions:</strong> ${escapeHtml(emotionsText)}</div>` : ''}
                            ${entry.interaction_summary ? `<div class="diary-entry-meta"><strong>Interaction:</strong> ${escapeHtml(entry.interaction_summary)}</div>` : ''}
                            ${usersText ? `<div class="diary-entry-meta"><strong>Involved users:</strong> ${escapeHtml(usersText)}</div>` : ''}
                            <div class="diary-entry-meta">
                                <span>${formatTimestamp(entry.timestamp)}</span>
                                ${entry.interface ? `<span>• ${entry.interface}</span>` : ''}
                                ${entry.chat_id ? `<span>• Chat ${entry.chat_id}</span>` : ''}
                                ${entry.id ? `<span>• ID: ${entry.id}</span>` : ''}
                            </div>
                        </div>
                    </div>
                `;
    }

    function updatePaginationControls() {
        const paginationDiv = document.getElementById('diary-pagination');
        const infoDiv = document.getElementById('pagination-info');
        const currentPageDisplay = document.getElementById('current-page-display');
        const prevBtn = document.getElementById('prev-page');
        const nextBtn = document.getElementById('next-page');

        if (currentPerPage === 'unlimited' || totalPages <= 1) {
            paginationDiv.style.display = 'none';
            return;
        }

        paginationDiv.style.display = 'block';

        const startEntry = (currentPage - 1) * currentPerPage + 1;
        const endEntry = Math.min(currentPage * currentPerPage, totalEntries);

        infoDiv.textContent = `Showing ${startEntry}-${endEntry} of ${totalEntries} entries`;
        currentPageDisplay.textContent = `Page ${currentPage} of ${totalPages}`;

        prevBtn.disabled = currentPage <= 1;
        nextBtn.disabled = currentPage >= totalPages;
    }

    function goToPage(page) {
        if (page >= 1 && page <= totalPages) {
            loadDiaryEntries(page, currentPerPage);
        }
    }

    function toggleDateGroup(header) {
        const content = header.nextElementSibling;
        if (!content) return;
        // Toggle visibility of the content without manipulating decorative glyphs
        content.style.display = (content.style.display === 'none') ? 'block' : 'none';
    }

    function toggleEntrySelection(id) {
        if (selectedEntries.has(id)) {
            selectedEntries.delete(id);
        } else {
            selectedEntries.add(id);
        }
        updateActionButtons();
    }

    function updateActionButtons() {
        const hasSelection = selectedEntries.size > 0;
        const archiveBtn = document.getElementById('archive-btn');
        const unarchiveBtn = document.getElementById('unarchive-btn');
        const deleteBtn = document.getElementById('delete-btn');
        if (!archiveBtn || !unarchiveBtn || !deleteBtn) return;

        archiveBtn.style.display = hasSelection ? 'inline-block' : 'none';
        // By default, hide unarchive/delete controls; they will be enabled by context-specific logic elsewhere if needed
        unarchiveBtn.style.display = 'none';
        deleteBtn.style.display = 'none';
    }

    function escapeHtml(text) {
        if (text === undefined || text === null) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Event listeners for diary functionality
    // Search functionality - reload entries when search changes
    let searchTimeout;
    diarySearchEl.addEventListener('input', function () {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            currentPage = 1; // Reset to first page when searching
            loadDiaryEntries(1, currentPerPage);
        }, 300); // Debounce search
    });

    const groupByDateEl = document.getElementById('group-by-date');
    if (groupByDateEl) groupByDateEl.addEventListener('change', renderDiaryEntries);

    const editModeBtn = document.getElementById('edit-mode-btn');
    if (editModeBtn) editModeBtn.addEventListener('click', function () {
        const isEditMode = this.textContent === 'Edit';
        this.textContent = isEditMode ? 'Done' : 'Edit';

        const checkboxes = document.querySelectorAll('.diary-entry-checkbox');
        checkboxes.forEach(cb => {
            cb.style.display = isEditMode ? 'inline' : 'none';
            if (!isEditMode) cb.checked = false;
        });

        if (!isEditMode) {
            selectedEntries.clear();
        }
        updateActionButtons();
    });

    const archiveBtn = document.getElementById('archive-btn');
    if (archiveBtn) archiveBtn.addEventListener('click', function () {
        if (selectedEntries.size === 0) return;

        fetch('/api/diary/archive', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: Array.from(selectedEntries) })
        })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    selectedEntries.clear();
                    loadDiaryEntries();
                    updateActionButtons();
                } else {
                    alert('Failed to archive entries');
                }
            })
            .catch(error => {
                console.error('Error archiving entries:', error);
                alert('Failed to archive entries');
            });
    });

    const unarchiveBtn = document.getElementById('unarchive-btn');
    if (unarchiveBtn) unarchiveBtn.addEventListener('click', function () {
        if (selectedEntries.size === 0) return;

        fetch('/api/diary/unarchive', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: Array.from(selectedEntries) })
        })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    selectedEntries.clear();
                    loadDiaryEntries();
                    updateActionButtons();
                } else {
                    alert('Failed to unarchive entries');
                }
            })
            .catch(error => {
                console.error('Error unarchiving entries:', error);
                alert('Failed to unarchive entries');
            });
    });

    const deleteBtn = document.getElementById('delete-btn');
    if (deleteBtn) deleteBtn.addEventListener('click', function () {
        if (selectedEntries.size === 0) return;

        if (!confirm(`Are you sure you want to delete ${selectedEntries.size} archived entries? This action cannot be undone.`)) {
            return;
        }

        fetch('/api/diary/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: Array.from(selectedEntries) })
        })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    selectedEntries.clear();
                    loadDiaryEntries();
                    updateActionButtons();
                } else {
                    alert('Failed to delete entries');
                }
            })
            .catch(error => {
                console.error('Error deleting entries:', error);
                alert('Failed to delete entries');
            });
    });

    // Load diary entries when diary tab becomes active
    const diaryTabBtn = document.querySelector('[data-tab="diary"]');
    if (diaryTabBtn) {
        diaryTabBtn.addEventListener('click', function () {
            setTimeout(loadDiaryEntries, 100); // Small delay to ensure tab is visible
        });
    }

    // Pagination controls
    const entriesPerPageEl = document.getElementById('entries-per-page');
    if (entriesPerPageEl) entriesPerPageEl.addEventListener('change', function () {
        const newPerPage = this.value;
        currentPerPage = newPerPage;
        loadDiaryEntries(1, newPerPage); // Reset to page 1 when changing per-page
    });

    const prevPageEl = document.getElementById('prev-page');
    if (prevPageEl) prevPageEl.addEventListener('click', function () {
        if (currentPage > 1) {
            goToPage(currentPage - 1);
        }
    });

    const nextPageEl = document.getElementById('next-page');
    if (nextPageEl) nextPageEl.addEventListener('click', function () {
        if (currentPage < totalPages) {
            goToPage(currentPage + 1);
        }
    });

    // Date filter
    const dateFilterEl = document.getElementById('date-filter');
    if (dateFilterEl) dateFilterEl.addEventListener('change', function () {
        const selectedDate = this.value;
        if (selectedDate) {
            // Find the page that contains entries from the selected date
            // This is a simplified implementation - in a real app you'd want to query the API for this
            const targetDate = new Date(selectedDate);

            // For now, we'll just reload and let the user navigate
            // A more sophisticated implementation would calculate the correct page
            loadDiaryEntries(1, currentPerPage);

            // Scroll to entries from the selected date
            setTimeout(() => {
                const dateHeaders = document.querySelectorAll('.diary-date-header');
                for (const header of dateHeaders) {
                    const headerDate = new Date(header.textContent.trim());
                    if (headerDate.toDateString() === targetDate.toDateString()) {
                        header.scrollIntoView({ behavior: 'smooth', block: 'start' });
                        // Expand the date group
                        toggleDateGroup(header);
                        break;
                    }
                }
            }, 500);
        }
    });
}

// Chat drag functionality (use pointer events for parity with debug window)
const chat = document.getElementById('chat');
const chatTitleBar = document.getElementById('chat-title-bar');

if (chatTitleBar && chat && false) {
    let isDragging = false;
    let dragOffsetX = 0;
    let dragOffsetY = 0;
    let chatPointerId = null;

    const chatPointerDown = (e) => {
        // Don't start a drag when interacting with buttons/controls in the title bar.
        // Otherwise `preventDefault` / pointer-capture can kill the click.
        try {
            const target = e?.target;
            if (target && typeof target.closest === 'function') {
                if (target.closest('.chat-controls, button, input, textarea, select, option, a, label')) {
                    return;
                }
            }
        } catch (err) { /* ignore */ }
        // Only primary button / primary pointer
        try {
            if (e && typeof e.button === 'number' && e.button !== 0) return;
        } catch (err) { /* ignore */ }
        try { e.stopPropagation(); } catch (err) { }
        // If another interaction is active (resize/other drag), ignore
        try { if (window.__synth_active_interaction) return; } catch (e) { }
        isDragging = true;
        chatPointerId = (e.pointerId !== undefined) ? e.pointerId : 'mouse';
        window.__synth_active_interaction = { type: 'chat_drag', id: chatPointerId };
        try { chatTitleBar.setPointerCapture && chatTitleBar.setPointerCapture(e.pointerId); } catch (e) { }
        dragOffsetX = (e.clientX || 0) - chat.offsetLeft;
        dragOffsetY = (e.clientY || 0) - chat.offsetTop;
        chatTitleBar.style.cursor = 'grabbing';
        try { chatTitleBar.style.zIndex = String(Number(CHAT_Z_INDEX) + 20); } catch (err) { }
        try { e.preventDefault(); } catch (e) { }
    };

    const chatPointerMove = (e) => {
        if (!isDragging) return;
        // Only respond to the pointer that started the drag
        if (chatPointerId !== ((e.pointerId !== undefined) ? e.pointerId : 'mouse')) return;
        try {
            const topbar = (document.querySelector('header.top-bar') && document.querySelector('header.top-bar').getBoundingClientRect().height) ? Math.ceil(document.querySelector('header.top-bar').getBoundingClientRect().height) : 0;
            const w = chat.offsetWidth || 320;
            const h = chat.offsetHeight || 240;
            const viewportW = Math.max(window.innerWidth || 0, document.documentElement.clientWidth || 0);
            const viewportH = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0);
            const maxX = Math.max(0, viewportW - w);
            const maxY = Math.max(topbar, viewportH - h);
            let tx = Math.round(e.clientX - dragOffsetX);
            let ty = Math.round(e.clientY - dragOffsetY);
            tx = Math.min(maxX, Math.max(0, tx));
            ty = Math.min(maxY, Math.max(topbar, ty));
            chat.style.left = tx + 'px';
            chat.style.top = ty + 'px';
            chat.style.bottom = 'auto';
            chat.style.right = 'auto';
        } catch (e) { /* ignore */ }
    };

    const chatPointerUp = (e) => {
        if (!isDragging) return;
        // Clear the drag regardless of pointerId mismatch to avoid stale locks
        isDragging = false;
        chatPointerId = null;
        try { chatTitleBar.releasePointerCapture && chatTitleBar.releasePointerCapture(e.pointerId); } catch (e) { }
        chatTitleBar.style.cursor = 'grab';
        try { if (window.__synth_active_interaction && window.__synth_active_interaction.type === 'chat_drag') window.__synth_active_interaction = null; } catch (e) { }
    };

    // Prefer pointer events, fall back to mouse for older browsers
    chatTitleBar.addEventListener('pointerdown', chatPointerDown);
    chatTitleBar.addEventListener('mousedown', (e) => { try { chatPointerDown(e); } catch (err) { } });
    window.addEventListener('pointermove', chatPointerMove);
    window.addEventListener('mousemove', chatPointerMove);
    window.addEventListener('pointerup', chatPointerUp);
    window.addEventListener('mouseup', chatPointerUp);

    console.log('[chat-control] Chat drag enabled via title-bar (pointer events)');
}

// Drag/resize is handled by WinBox when available.
const chatElement = document.getElementById('chat');
if (chatElement) {
    console.log('[chat-control] Chat initialized');
}

// Create resize handles to allow resizing from any edge (desktop only)
// Use `let` so the runtime toggle can enable/disable the feature by
// updating the variable. Also expose the creator as a global function
// so `setChatResizable` can recreate handles at runtime.
let CHAT_RESIZABLE = (typeof window !== 'undefined' && window.__SYNTH_CONFIG && window.__SYNTH_CONFIG.CHAT_RESIZABLE !== undefined) ? !!window.__SYNTH_CONFIG.CHAT_RESIZABLE : true; // server-controlled at render time, default true

function createChatResizeHandles() {
    // Chat is always managed by WinBox; disable custom handles unconditionally.
    try {
        if (!chatElement) return;
        chatElement.style.resize = 'none';
        chatElement.querySelectorAll('.chat-resize-handle').forEach(h => h.remove());
    } catch (e) { /* ignore */ }
    return;
    // Avoid recreating handles if they already exist
    // IMPORTANT: scope to chat only. Debug window also uses `.chat-resize-handle`.
    if (chatElement.querySelector('.chat-resize-handle')) {
        console.debug('[chat-control] Resize handles already present, skipping creation');
        return;
    }
    // Restore native resize when enabled so browser resize corners work.
    // Custom handles below still enable resizing from any edge.
    try { chatElement.style.resize = 'both'; } catch (err) { console.debug('[chat-control] failed to set native resize', err); }
    // Ensure title bar sits above handles to allow dragging without interference
    try {
        const header = document.getElementById('chat-title-bar');
        if (header) {
            header.style.zIndex = String(Number(CHAT_Z_INDEX) + 20);
            header.style.pointerEvents = 'auto';
        }
    } catch (e) { /* ignore */ }
    const sides = ['top', 'right', 'bottom', 'left', 'tl', 'tr', 'bl', 'br'];
    const handles = {};
    for (const side of sides) {
        const el = document.createElement('div');
        el.className = 'chat-resize-handle handle-' + side;
        // Ensure the handle sits above the title bar so top resize works.
        try { el.style.zIndex = String(Number(CHAT_Z_INDEX) + 30); } catch (e) { /* ignore */ }
        chatElement.appendChild(el);
        handles[side] = el;
    }

    let resizing = false;
    let startX = 0, startY = 0;
    let startRect = null;
    const minWidth = 260, minHeight = 180;

    function pointerDownHandler(side, ev) {
        // Respect global active interactions to avoid conflicts with window drags
        try { if (window.__synth_active_interaction) return; } catch (e) { }
        ev.preventDefault();
        resizing = true;
        startX = ev.clientX; startY = ev.clientY;
        startRect = chatElement.getBoundingClientRect();
        // Normalize to left/top based positioning to keep resize direction intuitive
        try {
            chatElement.style.left = startRect.left + 'px';
            chatElement.style.top = startRect.top + 'px';
            chatElement.style.right = 'auto';
            chatElement.style.bottom = 'auto';
        } catch (e) { /* ignore */ }
        // Register a global active interaction (resize) so other handlers ignore
        try { window.__synth_active_interaction = { type: 'resize', id: ev.pointerId || 'mouse', target: chatElement }; } catch (e) { }
        document.body.style.userSelect = 'none';
    }

    function pointerMoveHandler(ev) {
        if (!resizing || !startRect) return;
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        const el = chatElement;
        // copy values
        let left = startRect.left; let top = startRect.top; let width = startRect.width; let height = startRect.height;
        const side = currentResizeSide;
        if (side === 'right') {
            width = Math.max(minWidth, startRect.width + dx);
        } else if (side === 'left') {
            const newWidth = Math.max(minWidth, startRect.width - dx);
            left = startRect.left + (startRect.width - newWidth);
            width = newWidth;
        } else if (side === 'bottom') {
            height = Math.max(minHeight, startRect.height + dy);
        } else if (side === 'top') {
            const newHeight = Math.max(minHeight, startRect.height - dy);
            top = startRect.top + (startRect.height - newHeight);
            height = newHeight;
        } else if (side === 'br') {
            width = Math.max(minWidth, startRect.width + dx);
            height = Math.max(minHeight, startRect.height + dy);
        } else if (side === 'bl') {
            const newWidth = Math.max(minWidth, startRect.width - dx);
            left = startRect.left + (startRect.width - newWidth);
            width = newWidth;
            height = Math.max(minHeight, startRect.height + dy);
        } else if (side === 'tr') {
            width = Math.max(minWidth, startRect.width + dx);
            const newHeight = Math.max(minHeight, startRect.height - dy);
            top = startRect.top + (startRect.height - newHeight);
            height = newHeight;
        } else if (side === 'tl') {
            const newWidth = Math.max(minWidth, startRect.width - dx);
            left = startRect.left + (startRect.width - newWidth);
            width = newWidth;
            const newHeight2 = Math.max(minHeight, startRect.height - dy);
            top = startRect.top + (startRect.height - newHeight2);
            height = newHeight2;
        }
        // Apply styles
        el.style.left = left + 'px';
        el.style.top = top + 'px';
        el.style.width = width + 'px';
        el.style.height = height + 'px';
        el.style.right = 'auto'; el.style.bottom = 'auto';
    }

    function pointerUpHandler() {
        if (!resizing) return;
        resizing = false;
        startRect = null;
        document.body.style.userSelect = '';
        try { if (window.SynthChat && typeof window.SynthChat.saveChatState === 'function') window.SynthChat.saveChatState(); } catch (e) { /* ignore */ }
        try { if (window.__synth_active_interaction && window.__synth_active_interaction.type === 'resize') window.__synth_active_interaction = null; } catch (e) { }
    }

    let currentResizeSide = null;
    for (const side of Object.keys(handles)) {
        const h = handles[side];
        // Prefer pointer events, but fallback to mouse events for older browsers
        if (h.addEventListener) {
            h.addEventListener('pointerdown', (ev) => { currentResizeSide = side; pointerDownHandler(side, ev); });
            h.addEventListener('mousedown', (ev) => { currentResizeSide = side; pointerDownHandler(side, ev); });
        }
    }
    window.addEventListener('pointermove', pointerMoveHandler);
    window.addEventListener('mousemove', pointerMoveHandler);
    window.addEventListener('pointerup', pointerUpHandler);
    window.addEventListener('mouseup', pointerUpHandler);

    console.debug('[chat-control] Created resize handles for sides:', Object.keys(handles));
}

// Generic resize handle creator for arbitrary element (used for debug window)
function createResizeHandlesForElement(el) {
    try {
        if (!el) return;
        // Avoid duplicating handles
        if (el.querySelector('.chat-resize-handle')) return;
        const sides = ['top', 'right', 'bottom', 'left', 'tl', 'tr', 'bl', 'br'];
        const handles = {};
        for (const side of sides) {
            const div = document.createElement('div');
            div.className = 'chat-resize-handle handle-' + side;
            el.appendChild(div);
            handles[side] = div;
        }

        let resizing = false;
        let startX = 0, startY = 0, startRect = null;
        const minWidth = 220, minHeight = 140;

        const pointerDown = (side, ev) => {
            // Respect global active interactions (drag) to avoid conflicts
            try { if (window.__synth_active_interaction) return; } catch (e) { }
            ev.preventDefault();
            resizing = true;
            startX = ev.clientX; startY = ev.clientY;
            startRect = el.getBoundingClientRect();
            // Normalize to left/top positioning for the element so resize behaves intuitively
            try {
                el.style.left = startRect.left + 'px';
                el.style.top = startRect.top + 'px';
                el.style.right = 'auto';
                el.style.bottom = 'auto';
            } catch (e) { /* ignore */ }
            // Register active interaction so other handlers ignore during resize
            try { window.__synth_active_interaction = { type: 'resize', id: ev.pointerId || 'mouse', target: el }; } catch (e) { }
            document.body.style.userSelect = 'none';
        };

        // Clear global active interaction if pointer canceled / page blurred to avoid stale locks
        try {
            window.addEventListener('pointercancel', (ev) => { try { if (window.__synth_active_interaction) window.__synth_active_interaction = null; } catch (e) { } });
            window.addEventListener('blur', () => { try { window.__synth_active_interaction = null; } catch (e) { } });
            document.addEventListener('visibilitychange', () => { try { if (document.visibilityState !== 'visible') window.__synth_active_interaction = null; } catch (e) { } });
        } catch (e) { /* ignore */ }

        const pointerMove = (ev) => {
            if (!resizing || !startRect) return;
            const dx = ev.clientX - startX;
            const dy = ev.clientY - startY;
            let left = startRect.left; let top = startRect.top; let width = startRect.width; let height = startRect.height;
            const side = currentResizeSideForEl;
            if (side === 'right') {
                width = Math.max(minWidth, startRect.width + dx);
            } else if (side === 'left') {
                const newWidth = Math.max(minWidth, startRect.width - dx);
                left = startRect.left + (startRect.width - newWidth);
                width = newWidth;
            } else if (side === 'bottom') {
                height = Math.max(minHeight, startRect.height + dy);
            } else if (side === 'top') {
                const newHeight = Math.max(minHeight, startRect.height - dy);
                top = startRect.top + (startRect.height - newHeight);
                height = newHeight;
            } else if (side === 'br') {
                width = Math.max(minWidth, startRect.width + dx);
                height = Math.max(minHeight, startRect.height + dy);
            } else if (side === 'bl') {
                const newWidth = Math.max(minWidth, startRect.width - dx);
                left = startRect.left + (startRect.width - newWidth);
                width = newWidth;
                height = Math.max(minHeight, startRect.height + dy);
            } else if (side === 'tr') {
                width = Math.max(minWidth, startRect.width + dx);
                const newHeight = Math.max(minHeight, startRect.height - dy);
                top = startRect.top + (startRect.height - newHeight);
                height = newHeight;
            } else if (side === 'tl') {
                const newWidth = Math.max(minWidth, startRect.width - dx);
                left = startRect.left + (startRect.width - newWidth);
                width = newWidth;
                const newHeight = Math.max(minHeight, startRect.height - dy);
                top = startRect.top + (startRect.height - newHeight);
                height = newHeight;
            }
            el.style.left = left + 'px';
            el.style.top = top + 'px';
            el.style.width = width + 'px';
            el.style.height = height + 'px';
            el.style.right = 'auto'; el.style.bottom = 'auto';
        };

        const pointerUp = () => {
            if (!resizing) return;
            resizing = false; startRect = null; document.body.style.userSelect = '';
            try { saveChatState(); } catch (e) { }
            try { if (window.__synth_active_interaction && window.__synth_active_interaction.type === 'resize') window.__synth_active_interaction = null; } catch (e) { }
        };

        let currentResizeSideForEl = null;
        for (const side of Object.keys(handles)) {
            const h = handles[side];
            if (h.addEventListener) {
                h.addEventListener('pointerdown', (ev) => { currentResizeSideForEl = side; pointerDown(side, ev); });
                h.addEventListener('mousedown', (ev) => { currentResizeSideForEl = side; pointerDown(side, ev); });
            }
        }
        window.addEventListener('pointermove', pointerMove);
        window.addEventListener('mousemove', pointerMove);
        window.addEventListener('pointerup', pointerUp);
        window.addEventListener('mouseup', pointerUp);
        // Ensure that if this element has a draggable title bar, it sits above the
        // resize handles so pointerdown on the title bar is not captured by them.
        try {
            const headerEl = el.querySelector('#synth-debug-title-bar') || el.querySelector('.title-bar') || el.querySelector('.window-title');
            if (headerEl) {
                // Keep header above handles (handles default to ~60003). Use a slightly higher value.
                headerEl.style.zIndex = String(60020);
                headerEl.style.pointerEvents = 'auto';
            }
        } catch (e) { /* ignore */ }
    } catch (e) { console.debug('[synth_webui] createResizeHandlesForElement failed', e); }
}

// Immediately create handles if enabled on load
try { createChatResizeHandles(); } catch (e) { /* ignore */ }

// Allow toggling at runtime via settings
function setChatResizable(enabled) {
    try {
        const chatElement = document.getElementById('chat');
        if (!chatElement) return;
        if (enabled) {
            CHAT_RESIZABLE = true;
            // Restore native resize (regression-guarded by tests).
            try { chatElement.style.resize = 'both'; } catch (e) { }
            // Re-create handles if createChatResizeHandles is available
            try { createChatResizeHandles(); } catch (e) { }
        } else {
            CHAT_RESIZABLE = false;
            try { chatElement.style.resize = 'none'; } catch (e) { }
            // Remove existing handles
            try { chatElement.querySelectorAll('.chat-resize-handle').forEach(h => h.remove()); } catch (e) { }
        }
    } catch (e) { console.debug('[synth_webui] setChatResizable failed', e); }
}

// Archive / Restore / Delete UI handlers
const chatArchiveBtn = document.getElementById('chat-archive');
const chatRestoreBtn = document.getElementById('chat-restore');
// delete button removed from main titlebar (delete available in archive modal)

async function apiPostJson(url, payload) {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
}

// Archive modal is delegated to consolidated module `archive-window.mjs`.
function createArchiveModal() {
    try {
        if (window.ArchiveWindow && typeof window.ArchiveWindow.createArchiveModal === 'function') {
            try { return window.ArchiveWindow.createArchiveModal(); } catch (e) { console.warn('[archive] delegate failed', e); }
        }
        // Best-effort dynamic import to register module for older pages
        try {
            const _ts = (window.__synth_assets_bust || Date.now());
            import(`/js/archive-window.mjs?t=${_ts}`).then((mod) => { try { if (mod && typeof mod.createArchiveModal === 'function') { window.ArchiveWindow = window.ArchiveWindow || {}; window.ArchiveWindow.createArchiveModal = mod.createArchiveModal; } } catch (e) { console.warn('[archive] import handler failed', e); } }).catch((e) => { console.warn('[archive] Failed to import archive-window module', e); });
        } catch (e) { /* ignore */ }
        return null;
    } catch (err) {
        console.warn('[archive] createArchiveModal delegate failed', err);
        return null;
    }
}

// Legacy archive internal helpers removed. Use the consolidated archive module which encapsulates UI+logic.

chatArchiveBtn?.addEventListener('click', async () => {
    try {
        const messagesEl = document.getElementById('messages');
        const hasMessages = (messagesEl && messagesEl.children && messagesEl.children.length > 0) || (Array.isArray(historyBuffer) && historyBuffer.length > 0);
        if (!hasMessages) {
            showToast('Chat is empty. Nothing to archive.', true);
            return;
        }
        showToast('Archiving chat...', false);
        const out = await apiPostJson('/api/chat/archive', {});
        if (out && out.success) {
            showToast('Chat archived: ' + out.archive_id, false);
            // Clear the chat UI and local history to avoid duplicates
            const messagesEl = document.getElementById('messages');
            if (messagesEl) messagesEl.innerHTML = '';
            historyBuffer = [];
            try { persistHistory(); } catch (e) { /* ignore */ }
            try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* ignore */ }
            saveChatState();
            // If archive panel is open, ask it to refresh; also dispatch to potential stale instance
            try { const panel = window.__archive_modal_instance; if (panel && typeof panel.dispatchEvent === 'function') panel.dispatchEvent(new CustomEvent('archive:refresh', { detail: out })); } catch (e) { /* ignore */ }
            // Also set a global flag + dispatch a window-level event so other instances (or future panels) can refresh
            try { window.__archive_last_changed_ts = Date.now(); window.dispatchEvent(new CustomEvent('synth:archive-changed', { detail: out })); } catch (e) { /* ignore */ }
        } else {
            showToast('Archive failed', true);
        }
    } catch (err) {
        console.error('[synth_webui] Archive failed', err);
        showToast('Archive error: ' + err.message, true);
    }
});

chatRestoreBtn?.addEventListener('click', async () => {
    try {
        // Ensure the consolidated module is loaded and call its creator
        let mod = window.ArchiveWindow;
        if (!mod || !mod.createArchiveModal) {
            try { mod = await import('/js/archive-window.mjs'); if (mod && mod.createArchiveModal) { window.ArchiveWindow = window.ArchiveWindow || {}; window.ArchiveWindow.createArchiveModal = mod.createArchiveModal; } }
            catch (e) { console.warn('[synth_webui] Failed to import archive-window module', e); }
        }
        const creator = (window.ArchiveWindow && window.ArchiveWindow.createArchiveModal) ? window.ArchiveWindow.createArchiveModal : (mod && mod.createArchiveModal ? mod.createArchiveModal : null);
        if (!creator) throw new Error('Archive module not available');
        const modal = creator();
        // Try to restore via window manager if available, otherwise show panel
        try {
            if (window.SynthWindowManager && typeof window.SynthWindowManager.restore === 'function') {
                try { window.SynthWindowManager.restore('archives'); return; } catch (e) { }
            }
        } catch (e) { }
        try { if (modal && modal.style) modal.style.display = 'flex'; } catch (e) { }
    } catch (err) {
        console.error('[synth_webui] Open archives failed', err);
        showToast('Open archives error: ' + err.message, true);
    }
});

// Archive delete removed from main UI; handled in the archive modal per-item
