/**
 * Minimal AnimationUtils implementation for Three.js
 * Provides the subclip function used in vrm-viewer.mjs
 */

export const AnimationUtils = {
    subclip(sourceClip, name, startFrame, endFrame, fps) {
        const clip = sourceClip.clone();
        clip.name = name;
        const startTime = startFrame / fps;
        const endTime = endFrame / fps;
        // Trim the tracks
        for (let i = clip.tracks.length - 1; i >= 0; i--) {
            const track = clip.tracks[i];
            track.trim(startTime, endTime);
            if (track.times.length === 0) {
                clip.tracks.splice(i, 1);
            }
        }
        clip.duration = endTime - startTime;
        return clip;
    },
};

export default AnimationUtils;
