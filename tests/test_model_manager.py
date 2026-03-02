import shutil
from pathlib import Path

import pytest

from core.model_manager import MODEL_MANAGER, ModelSpec


@pytest.fixture(autouse=True)
def isolate_models_dir(tmp_path, monkeypatch):
    """Ensure the models directory is isolated per-test via env var."""
    monkeypatch.setenv("SYNTH_MODELS_DIR", str(tmp_path))
    # Clear any previously registered test models to avoid leakage
    orig = dict(MODEL_MANAGER._models)
    yield
    MODEL_MANAGER._models.clear()
    MODEL_MANAGER._models.update(orig)
    # remove any directories created
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_register_and_catalog():
    spec = ModelSpec(
        model_id="foo-model",
        plugin_id="test_plugin",
        display_name="Foo Model",
        description="A test model",
        voices=["alice", "bob"],
        language="en",
        size_mb=1,
    )
    MODEL_MANAGER.register(spec)
    catalog = MODEL_MANAGER.catalog()
    assert any(m["model_id"] == "foo-model" for m in catalog)


def test_ensure_sample_generates(tmp_path):
    # register spec with one voice
    spec = ModelSpec(
        model_id="bar-model",
        plugin_id="test_plugin",
        display_name="Bar",
        description="Another test",
        voices=["v1"],
        sample_text="hello world",
    )
    MODEL_MANAGER.register(spec)

    # simulate downloaded model by writing manifest
    dest = MODEL_MANAGER.model_dir("bar-model")
    dest.mkdir(parents=True, exist_ok=True)
    MODEL_MANAGER._write_manifest("bar-model")

    # remove any stale static sample file that may have been left over from
    # previous test runs or manual operations.  This ensures the generator is
    # actually called on first invocation.
    stale = Path("res/synth_webui/static/audio/model_samples/bar-model/v1.mp3")
    try:
        stale.unlink()
    except Exception:
        pass

    called = {"count": 0}

    def generator(text, voice):
        called["count"] += 1
        assert text == "hello world"
        assert voice == "v1"
        return b"fake-mp3"

    path = MODEL_MANAGER.ensure_sample("bar-model", "v1", generator)
    assert path is not None
    assert path.exists()
    assert path.read_bytes() == b"fake-mp3"
    # calling again should not invoke generator a second time
    path2 = MODEL_MANAGER.ensure_sample("bar-model", "v1", generator)
    assert path2 == path
    assert called["count"] == 1


def test_delete_model_removes_files(tmp_path):
    spec = ModelSpec(
        model_id="baz-model",
        plugin_id="test_plugin",
        display_name="Baz",
        description="Again",
    )
    MODEL_MANAGER.register(spec)
    dest = MODEL_MANAGER.model_dir("baz-model")
    dest.mkdir(parents=True, exist_ok=True)
    MODEL_MANAGER._write_manifest("baz-model")
    assert MODEL_MANAGER.is_downloaded("baz-model")
    assert dest.exists()
    ok = MODEL_MANAGER.delete("baz-model")
    assert ok
    assert not dest.exists()
    assert not MODEL_MANAGER.is_downloaded("baz-model")


def test_list_samples_direct_file(tmp_path):
    # simulate a downloaded model with an existing sample file
    spec = ModelSpec(
        model_id="qux-model",
        plugin_id="test_plugin",
        display_name="Qux",
        description="Sample test",
        voices=["v1", "v2"],
    )
    MODEL_MANAGER.register(spec)
    dest = MODEL_MANAGER.model_dir("qux-model")
    dest.mkdir(parents=True, exist_ok=True)
    MODEL_MANAGER._write_manifest("qux-model")
    # create a dummy sample for v1
    sample_dir = Path("res/synth_webui/static/audio/model_samples/qux-model")
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample_path = sample_dir / "v1.mp3"
    sample_path.write_bytes(b"dummy")
    entries = MODEL_MANAGER.list_samples("qux-model")
    assert any(e["voice"] == "v1" for e in entries)
    # ensure ensure_sample does not regenerate when file exists
    called = {"x": False}

    def gen(txt, v):
        called["x"] = True
        return b"no"

    p = MODEL_MANAGER.ensure_sample("qux-model", "v1", gen)
    assert p is not None
    assert p.resolve() == sample_path.resolve()
    assert not called["x"]


def test_download_progress_not_started():
    # no download in progress
    assert MODEL_MANAGER.download_progress("nonexistent") is None
