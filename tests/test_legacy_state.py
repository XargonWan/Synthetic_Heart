"""Tests for adopting state written to container paths by older builds.

The behaviour under test is what protects an existing install from looking like
it lost its data: the encrypted-endpoint secret, attachments and generated TLS
material that a Docker-era run wrote to the drive-relative ``\\config`` /
``\\app`` paths on Windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import legacy_state as ls


def _make_legacy(root: Path) -> Path:
    """Create a plausible leftover directory under *root* and return it."""
    legacy = root / "config"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / ".synth_secret").write_bytes(b"fernet-key-bytes")
    (legacy / "db-cutover-state.json").write_text("{}", encoding="utf-8")
    attachments = legacy / "attachments"
    attachments.mkdir()
    (attachments / "photo.png").write_bytes(b"png-bytes")
    nested = attachments / "old"
    nested.mkdir()
    (nested / "note.txt").write_bytes(b"note")
    ssl_dir = legacy / "ssl"
    ssl_dir.mkdir()
    (ssl_dir / "cert.pem").write_bytes(b"cert")
    return legacy


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def test_inspects_a_directory_holding_synth_state(tmp_path: Path) -> None:
    legacy = _make_legacy(tmp_path)
    source = ls.inspect_legacy_dir(legacy)
    assert source is not None
    assert ".synth_secret" in source.files
    assert "db-cutover-state.json" in source.files
    assert set(source.dirs) == {"attachments", "ssl"}


def test_ignores_an_unrelated_directory(tmp_path: Path) -> None:
    """A plain folder called 'app' or 'config' must be left alone."""
    unrelated = tmp_path / "app"
    unrelated.mkdir()
    (unrelated / "main.py").write_text("print('hi')", encoding="utf-8")
    assert ls.inspect_legacy_dir(unrelated) is None


def test_ignores_a_missing_or_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "config"
    empty.mkdir()
    assert ls.inspect_legacy_dir(empty) is None
    assert ls.inspect_legacy_dir(tmp_path / "absent") is None


def test_explicit_sources_are_included() -> None:
    candidates = ls.container_path_leftovers(["/somewhere/config"])
    assert Path("/somewhere/config") in candidates


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_adopts_missing_items_and_never_overwrites(tmp_path: Path) -> None:
    legacy = _make_legacy(tmp_path)
    destination = tmp_path / "data"
    destination.mkdir()
    # A file the user already has at the destination must win.
    (destination / ".synth_secret").write_bytes(b"newer-key")

    report = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(legacy)]
    )

    assert (destination / ".synth_secret").read_bytes() == b"newer-key"
    assert str(destination / ".synth_secret") in report.skipped_existing
    assert (destination / "attachments" / "photo.png").read_bytes() == b"png-bytes"
    assert (destination / "attachments" / "old" / "note.txt").read_bytes() == b"note"
    assert (destination / "ssl" / "cert.pem").read_bytes() == b"cert"
    assert (destination / "db-cutover-state.json").is_file()


def test_originals_are_left_in_place(tmp_path: Path) -> None:
    legacy = _make_legacy(tmp_path)
    destination = tmp_path / "data"
    ls.migrate_legacy_state(destination=destination, explicit_sources=[str(legacy)])
    assert (legacy / ".synth_secret").is_file()
    assert (legacy / "attachments" / "photo.png").is_file()


def test_migration_runs_once(tmp_path: Path) -> None:
    legacy = _make_legacy(tmp_path)
    destination = tmp_path / "data"
    first = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(legacy)]
    )
    assert first.copied

    (destination / "attachments" / "photo.png").unlink()
    second = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(legacy)]
    )
    assert second.already_done is True
    assert not second.copied
    assert not (destination / "attachments" / "photo.png").exists()


def test_force_re_runs_the_migration(tmp_path: Path) -> None:
    legacy = _make_legacy(tmp_path)
    destination = tmp_path / "data"
    ls.migrate_legacy_state(destination=destination, explicit_sources=[str(legacy)])
    (destination / "attachments" / "photo.png").unlink()
    forced = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(legacy)], force=True
    )
    assert forced.already_done is False
    assert (destination / "attachments" / "photo.png").exists()


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    legacy = _make_legacy(tmp_path)
    destination = tmp_path / "data"
    report = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(legacy)], dry_run=True
    )
    assert report.dry_run is True
    # Nothing is written anywhere: not the data root, not the marker.
    assert not destination.exists()
    # ... but the report still says exactly what a real run would adopt.
    assert report.copied
    assert report.sources and report.sources[0].path == legacy


def test_marker_records_what_happened(tmp_path: Path) -> None:
    legacy = _make_legacy(tmp_path)
    destination = tmp_path / "data"
    report = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(legacy)]
    )
    marker = json.loads((destination / ls.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["copied"] == len(report.copied)
    assert marker["sources"][0]["path"] == str(legacy)


def test_no_sources_still_writes_the_marker(tmp_path: Path) -> None:
    """An install with nothing to adopt should not rescan every launch."""
    destination = tmp_path / "data"
    report = ls.migrate_legacy_state(destination=destination, explicit_sources=[])
    assert report.sources == []
    assert (destination / ls.MARKER_NAME).is_file()


def test_a_large_file_is_skipped_rather_than_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy budget stops a first launch from hanging on old attachments."""
    legacy = _make_legacy(tmp_path)
    monkeypatch.setattr(ls, "MAX_BYTES", 1)
    destination = tmp_path / "data"
    report = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(legacy)]
    )
    assert report.skipped_budget
    assert not (destination / "attachments" / "photo.png").exists()


def test_the_own_data_root_is_never_treated_as_a_source(tmp_path: Path) -> None:
    destination = tmp_path / "config"
    (destination / "attachments").mkdir(parents=True)
    (destination / ".synth_secret").write_bytes(b"key")
    report = ls.migrate_legacy_state(
        destination=destination, explicit_sources=[str(destination)]
    )
    assert report.sources == []


def test_migration_inside_a_container_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the container those paths are correct; adopting them would be wrong."""
    legacy = _make_legacy(tmp_path)
    monkeypatch.setattr(ls, "in_container", lambda: True)
    report = ls.migrate_legacy_state(
        destination=tmp_path / "data", explicit_sources=[str(legacy)]
    )
    assert report.errors
    assert report.copied == []


def test_a_dry_run_still_answers_after_the_real_run(tmp_path: Path) -> None:
    """``--dry-run`` is a question; once answered it must stay answerable.

    The marker stops a real run from adopting twice. It must not stop a dry run
    from *reporting*, otherwise the command is useless exactly when someone wants
    to check what an upgrade adopted.
    """
    import core.legacy_state as ls  # local import keeps the module load explicit

    legacy = _make_legacy(tmp_path)
    data = tmp_path / "data"

    real = ls.migrate_legacy_state(destination=data, explicit_sources=[str(legacy)])
    assert real.copied, "the first real run should adopt something"
    assert not real.dry_run

    again = ls.migrate_legacy_state(
        destination=data, explicit_sources=[str(legacy)], dry_run=True
    )
    assert again.already_done is False, "a dry run must still answer"
    assert [s.path for s in again.sources] == [legacy]
    # The earlier run already adopted these files, so a dry run now reports them
    # as present rather than as work to do.
    assert again.copied == []
    assert again.skipped_existing


def test_startup_migration_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ls, "migrate_legacy_state", boom)
    assert ls.run_startup_migration() is None


def test_cli_dry_run_reports_without_copying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = _make_legacy(tmp_path)
    exit_code = ls.main(["--dry-run", "--json", "--source", str(legacy)])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["sources"][0]["path"] == str(legacy)
