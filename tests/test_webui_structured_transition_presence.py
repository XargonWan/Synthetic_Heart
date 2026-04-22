from pathlib import Path


def test_webui_structured_transition_guards_present() -> None:
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "preserveLocalStructuredPlayback" in content
    assert "_queueTransitionAfterStructuredOutro" in content
    assert "_consumeQueuedTransitionAfterStructuredOutro" in content
    assert "queued for finished outro crossfade" in content


def test_webui_startup_idle_before_preload() -> None:
    """_ensureBaseIdle must be called BEFORE preloadAllAnimations to prevent
    T-pose during the preload timeout window (e.g. write animation timing out)."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    # Use the unique comment markers placed at the specific startup context
    idle_marker = "Bootstrap the base idle FIRST"
    preload_marker = "NOTE: done AFTER bootstrapping base idle"
    idle_pos = content.find(idle_marker)
    preload_pos = content.find(preload_marker)
    assert idle_pos != -1, f"'{idle_marker}' marker not found"
    assert preload_pos != -1, f"'{preload_marker}' marker not found"
    assert idle_pos < preload_pos, (
        "_ensureBaseIdle bootstrap comment must appear before preloadAllAnimations comment"
    )


def test_webui_queued_transition_clears_state_before_microtask() -> None:
    """When a queued transition fires after outro, state must be cleared BEFORE the
    Promise micro-task so _startActionInternal does not see a stale structured action
    (which would cause a permanent deadlock)."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    queued_block_start = content.find(
        "Starting queued transition after structured outro"
    )
    assert queued_block_start != -1
    # Find the Promise.resolve().then block after the queued transition log
    promise_pos = content.find("Promise.resolve().then", queued_block_start)
    assert promise_pos != -1
    # Extract the code between these two positions
    between = content[queued_block_start:promise_pos]
    # All three critical state clears must appear BEFORE the Promise.resolve().then call
    assert "currentActionPhase = null" in between, (
        "currentActionPhase not cleared before micro-task"
    )
    assert "currentStructuredAction = null" in between, (
        "currentStructuredAction not cleared before micro-task"
    )
    assert "_currentAnimationFile = null" in between, (
        "_currentAnimationFile not cleared before micro-task"
    )


def test_webui_cache_bypass_for_structured_descriptor() -> None:
    """When a descriptorOverride with intro/outro is provided but the cached action
    is a plain clip (no intro/outro), the code must bypass the cache to create the
    correct structured action."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "descExpectsStructured" in content
    assert "cachedIsStructured" in content
    assert "useCached" in content
    assert "Bypassing stale simple-action cache" in content


def test_webui_loading_overlay_present() -> None:
    """The loading overlay element, CSS, and wiring must be in place so that
    the VRM canvas is hidden behind a 'Summoning <name>' screen while the model
    and animations initialise."""
    mjs = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    html = Path("core/webui_templates/sections/home.html").read_text(encoding="utf-8")
    css = Path("core/webui_templates/synth_webui_shell.html").read_text(
        encoding="utf-8"
    )

    # HTML: overlay element present inside home-vrm
    assert "vrm-loading-overlay" in html, (
        "#vrm-loading-overlay element missing from home.html"
    )
    assert "vrm-loading-name" in html, "#vrm-loading-name span missing from home.html"
    assert "Summoning" in html, "'Summoning' text missing from home.html"

    # CSS: overlay rules present
    assert "#vrm-loading-overlay" in css, "#vrm-loading-overlay CSS missing from shell"
    assert "vrm-loading-spinner" in css, ".vrm-loading-spinner CSS missing from shell"
    assert "vrm-spin" in css, "vrm-spin keyframe missing from shell"
    assert "fade-out" in css, ".fade-out transition class missing from shell"

    # JS: helper functions present and wired at both success and error paths
    assert "_hideVrmLoadingOverlay" in mjs, "_hideVrmLoadingOverlay() helper missing"
    assert "_showVrmLoadingOverlay" in mjs, "_showVrmLoadingOverlay() helper missing"
    assert "vrm-loading-name" in mjs, (
        "overlay name injection missing from vrm-viewer.mjs"
    )
    # hide must be called in both the success (unhide VRM) and error branches
    hide_count = mjs.count("_hideVrmLoadingOverlay()")
    assert hide_count >= 2, (
        f"_hideVrmLoadingOverlay() should be called in at least 2 places (success + error), found {hide_count}"
    )


def test_webui_structured_phase_preserved_for_non_authoritative_summary() -> None:
    """A non-authoritative backend summary with phase='clip' or phase='loop' must
    not stomp the real local structured phase while intro/outro are running."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "incomingPhaseAuthoritative" in content
    assert "incomingPhase === 'loop' || incomingPhase === 'clip'" in content
    assert (
        "String(this.currentActionName).toLowerCase() === incomingActionName" in content
    )
    assert "!incomingPhaseAuthoritative" in content


def test_webui_threads_rei_fallback_path_and_metadata() -> None:
    """When the backend resolves a full fallback path (for example under Rei), the
    viewer must keep using that exact file path during pending-command replay,
    initial restore, and resync rather than rebuilding a path from the active skin."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "last.animation || last.file || null" in content
    assert "summary.animation || summary.file || null" in content
    assert "frameRange = null, phaseAuthoritative = false" in content
    assert "summary.frame_range || null" in content
    assert "!!summary.phase_authoritative" in content


def test_webui_crossfade_timeouts_are_generation_guarded() -> None:
    """Delayed cleanup from an old transition must not interfere with the next one."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "this._transitionGeneration = 0" in content
    assert "this._baseIdleDropTimer = null" in content
    assert (
        "this._transitionGeneration = (this._transitionGeneration || 0) + 1" in content
    )
    assert "this._cancelBaseIdleFloorDrop();" in content
    assert "_scheduleBaseIdleFloorDrop" in content
    assert "if (this._transitionGeneration !== generation) return;" in content
    assert "action.__synthFadeStopTimer" in content
    assert "reclaimedByCurrentTransition" in content
    assert "action === this.currentAction" in content
    assert "action === this._baseIdleAction" in content


def test_webui_idle_fallback_filters_non_loopable_variants() -> None:
    """Base idle selection must not randomly choose play-once transition clips
    placed under the idle folder at runtime."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "Excluding non-loopable IDLE variants from fallback queue" in content
    assert "const hasStructuredNoLoop" in content
    assert "const isPlayOnce = !!(descriptor && descriptor.play_once)" in content
    assert "isLoopable: !(isPlayOnce || hasStructuredNoLoop)" in content
    assert "!files.includes(this._idleQueue.currentFile)" in content
    assert "!files.includes(this._idleQueue.nextFile)" in content


def test_webui_summoning_bootstrap_uses_fresh_server_state() -> None:
    """Summoning must reset local bootstrap caches and rehydrate pose/expression
    from a fresh Karada snapshot instead of reusing browser globals."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "_resetSummoningBootstrapCaches" in content
    assert "window.__synth_current_animation_state = null" in content
    assert "window.__synth_last_rich_animation_state = null" in content
    assert "fetch('/api/karada/state', { cache: 'no-store' })" in content
    assert "animationHandler.resetBootstrapState()" in content
    assert "_applyFreshSummoningFaceValues" in content


def test_webui_touch_dispatches_authoritative_server_state() -> None:
    """Touch interaction must no longer start a local-only animation; it must
    send an authoritative touch state to the server so all clients stay in sync."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "const __synthTouchOverlayContextId = '__webui_touch_overlay'" in content
    assert "type: 'touch'" in content
    assert "fetch('/api/animation_state'" in content
    assert "state: 'touch'" in content
    assert "Dispatched authoritative touch state to server" in content
    assert "animationHandler.startAction('touch'" not in content


def test_webui_remote_face_updates_replace_previous_snapshot() -> None:
    """Server-driven vrm_face updates must replace the previous remote face
    snapshot so stale morphs do not stick around after upstream failures."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "clearRemoteFaceValues()" in content
    assert "applyRemoteFaceValues(values)" in content
    assert "this._remoteFaceValueKeys = new Set()" in content
    assert "if (incomingKeys.has(key)) return;" in content
    assert "handler.applyRemoteFaceValues(values || {});" in content


def test_webui_speech_tags_override_idle_emotion_layer() -> None:
    """When speech-tag expressions are active, background emotion layers must
    back off so the speaking face is controlled only by explicit tag expressions."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "_hasActiveFacialExpressionSource()" in content
    assert (
        "const suppressBaseEmotionLayers = hasExplicitFacialExpression || this._lipsyncEnabled || currentActionKey === 'talk';"
        in content
    )
    assert "if (!suppressBaseEmotionLayers && isIdleLikeAction)" in content
    assert (
        "const allowEmotionMicroOverlay = !suppressBaseEmotionLayers && isIdleLikeAction;"
        in content
    )
    assert "source: 'idle_emotion_hint'" in content


def test_webui_idle_emotion_hint_is_subtle() -> None:
    """Idle emotional state should become only a subtle dominant hint, not a
    theatrical composite mask."""
    content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "_buildIdleEmotionHintTargets(state)" in content
    assert "const subtleCeil = (dominantKey === 'relaxed') ? 0.14 : 0.18;" in content
    assert "const subtleNorm = Math.min(0.22, 0.05 + norm * 0.17);" in content
    assert "priority: 10," in content
