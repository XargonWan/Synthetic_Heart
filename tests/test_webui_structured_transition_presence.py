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
    queued_block_start = content.find("Starting queued transition after structured outro")
    assert queued_block_start != -1
    # Find the Promise.resolve().then block after the queued transition log
    promise_pos = content.find("Promise.resolve().then", queued_block_start)
    assert promise_pos != -1
    # Extract the code between these two positions
    between = content[queued_block_start:promise_pos]
    # All three critical state clears must appear BEFORE the Promise.resolve().then call
    assert "currentActionPhase = null" in between, "currentActionPhase not cleared before micro-task"
    assert "currentStructuredAction = null" in between, "currentStructuredAction not cleared before micro-task"
    assert "_currentAnimationFile = null" in between, "_currentAnimationFile not cleared before micro-task"


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
    css = Path("core/webui_templates/synth_webui_shell.html").read_text(encoding="utf-8")

    # HTML: overlay element present inside home-vrm
    assert "vrm-loading-overlay" in html, "#vrm-loading-overlay element missing from home.html"
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
    assert "vrm-loading-name" in mjs, "overlay name injection missing from vrm-viewer.mjs"
    # hide must be called in both the success (unhide VRM) and error branches
    hide_count = mjs.count("_hideVrmLoadingOverlay()")
    assert hide_count >= 2, (
        f"_hideVrmLoadingOverlay() should be called in at least 2 places (success + error), found {hide_count}"
    )

