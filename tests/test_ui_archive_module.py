def test_archive_module_and_delegation():
    """Archive UI should be provided by `archive-window.mjs` and vrm-viewer should delegate to it."""
    from pathlib import Path

    repo_root = Path(__file__).parent.parent

    # Module exists and exports the factory
    mod = repo_root / "res" / "synth_webui" / "js" / "archive-window.mjs"
    assert mod.exists(), f"File not found: {mod}"
    mtxt = mod.read_text(encoding="utf-8")
    assert ("export function createArchiveModal" in mtxt) or (
        "export default" in mtxt and "createArchiveModal" in mtxt
    ), "createArchiveModal API not found in archive-window.mjs"

    # vrm-viewer must not contain the legacy inline archive markup and should reference the consolidated module
    viewer = repo_root / "res" / "synth_webui" / "js" / "vrm-viewer.mjs"
    assert viewer.exists(), f"File not found: {viewer}"
    vtxt = viewer.read_text(encoding="utf-8")

    # Legacy markers that indicate inline archive UI should not be present
    for legacy_marker in [
        "panel.id = 'archive-panel'",
        "#archive-header",
        "archive-minimize",
        "archive-list",
        "archive-footer",
    ]:
        assert legacy_marker not in vtxt, (
            f"Legacy archive UI still present in vrm-viewer.mjs: found {legacy_marker}"
        )

    # The file should reference the consolidated module by path or name
    assert (
        "/js/archive-window.mjs" in vtxt
        or "/res/synth_webui/js/archive-window.mjs" in vtxt
        or "archive-window.mjs" in vtxt
    ), "vrm-viewer.mjs should reference archive-window.mjs"
