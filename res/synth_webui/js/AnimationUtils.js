/**
 * AnimationUtils for the SyntH WebUI.
 *
 * Provides the clip slicing helpers used by vrm-viewer.mjs and
 * vrm-animation-engine.mjs to cut a Mixamo clip into descriptor sections
 * (intro / loop / outro).
 *
 * Both helpers take frame indices with an EXCLUSIVE end frame (descriptors use
 * inclusive ``end_frame``, so callers pass ``end_frame + 1``) and return a clip
 * whose keyframe times are re-based to start at t=0.
 */
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';

// Float32 keyframe times (e.g. 1.6333333 * 30 = 48.9999997) must not drop the
// boundary frame, so frame comparisons use a small tolerance.
const FRAME_EPS = 1e-3;

function _cloneValues(track, index) {
    const size = track.getValueSize();
    return Array.from(track.values.slice(index * size, (index + 1) * size));
}

function _makeTrack(track, times, values) {
    const out = track.clone();
    out.times = new track.times.constructor(times);
    out.values = new track.values.constructor(values);
    return out;
}

function _finishClip(sourceClip, name, tracks, duration) {
    const clip = sourceClip.clone();
    clip.name = name;
    clip.tracks = tracks;
    clip.duration = duration;
    return clip;
}

/**
 * Cut ``[startFrame, endFrame)`` out of ``sourceClip`` and re-base time to 0.
 *
 * NOTE: ``KeyframeTrack.trim()`` alone is not enough — it keeps the original
 * keyframe times, so a section starting at 1.6s would be evaluated by the mixer
 * over t=0..duration, i.e. entirely *before* its first key, freezing the pose.
 */
function subclip(sourceClip, name, startFrame, endFrame, fps = 30) {
    const t0 = startFrame / fps;
    const tracks = [];
    let lastTime = 0;
    for (const track of sourceClip.tracks) {
        const times = [];
        const values = [];
        for (let j = 0; j < track.times.length; j++) {
            const frame = track.times[j] * fps;
            if (frame < startFrame - FRAME_EPS || frame >= endFrame - FRAME_EPS) continue;
            const t = track.times[j] - t0;
            times.push(Math.max(0, t));
            values.push(..._cloneValues(track, j));
            lastTime = Math.max(lastTime, t);
        }
        if (times.length === 0) continue;
        tracks.push(_makeTrack(track, times, values));
    }
    return _finishClip(sourceClip, name, tracks, lastTime);
}

function _blend(track, a, b, w) {
    // a/b are arrays of the track's value size; w=0 -> a, w=1 -> b.
    const out = new Array(a.length);
    if (track.ValueTypeName === 'quaternion') {
        THREE.Quaternion.slerpFlat(out, 0, a, 0, b, 0, w);
    } else {
        for (let i = 0; i < a.length; i++) out[i] = a[i] + (b[i] - a[i]) * w;
    }
    return out;
}

/**
 * Build a seamless looping clip from ``[startFrame, endFrame)``.
 *
 * A raw section only loops cleanly if its last pose equals its first, which is
 * almost never true for hand-picked frame ranges. This bakes the wrap in: the
 * last ``crossfadeFrames`` frames of the section are cross-faded into its first
 * ones, shortening the loop by that many frames, and a closing key equal to the
 * first pose is appended so the mixer interpolates across the wrap instead of
 * holding a frame and jumping.
 *
 * With ``crossfadeFrames`` <= 0 this is a plain ``subclip``.
 */
function loopSubclip(sourceClip, name, startFrame, endFrame, fps = 30, crossfadeFrames = 0) {
    const total = Math.max(0, Math.round(endFrame - startFrame));
    const requested = Math.max(0, Math.floor(Number(crossfadeFrames) || 0));
    // Need at least one un-blended frame between the two cross-fade zones.
    const blend = Math.min(requested, Math.floor((total - 1) / 2));
    if (blend <= 0) return subclip(sourceClip, name, startFrame, endFrame, fps);

    const loopLen = total - blend; // frames in the final loop
    const tracks = [];
    for (const track of sourceClip.tracks) {
        const interp = track.createInterpolant();
        const sample = (frame) => Array.from(interp.evaluate(frame / fps));
        const times = [];
        const values = [];
        for (let i = 0; i <= loopLen; i++) {
            const idx = i % loopLen; // closing key (i === loopLen) repeats frame 0
            let v;
            if (idx < blend) {
                // Ease from the tail of the section (continuing the motion that
                // led into the wrap) to the head, so frame loopLen == frame 0.
                const tail = sample(startFrame + idx + loopLen);
                const head = sample(startFrame + idx);
                v = _blend(track, tail, head, idx / blend);
            } else {
                v = sample(startFrame + idx);
            }
            times.push(i / fps);
            values.push(...v);
        }
        tracks.push(_makeTrack(track, times, values));
    }
    return _finishClip(sourceClip, name, tracks, loopLen / fps);
}

export const AnimationUtils = {
    subclip,
    loopSubclip,
};

export default AnimationUtils;
