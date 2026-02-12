import os
import json
import shutil
from pathlib import Path
import pytest

from plugins.mate_engine import MateEnginePlugin
from core.animation_uploads import (
    sanitize_upload_id,
    record_upload,
    get_state_dir,
    ensure_uploads_root,
    delete_upload,
)
from core.webui import SynthWebUIInterface


@pytest.mark.asyncio
async def test_send_mate_message_enqueues_outbox(monkeypatch, tmp_path):
    # Ensure webui instance exists and is fresh
    webui = SynthWebUIInterface(autostart=False)

    plugin = MateEnginePlugin()

    # Bind module-level synth_webui_interface to our test instance so plugin uses it
    import core.webui as webui_mod

    webui_mod.synth_webui_interface = webui

    # Ensure outbox empty
    res = await webui.get_integration_outbox(
        type("R", (), {"query_params": {"source": "mate"}})()
    )
    assert json.loads(res.body)["messages"] == []

    action = {
        "type": "send_mate_message",
        "payload": {"text": "hello mate", "target": "unit123"},
    }
    await plugin.execute_action(action, context={}, bot=None, original_message=None)

    # Retrieve mate outbox
    res2 = await webui.get_integration_outbox(
        type("R", (), {"query_params": {"source": "mate"}})()
    )
    msgs = json.loads(res2.body)["messages"]
    assert len(msgs) == 1
    assert msgs[0]["text"] == "hello mate"
    assert msgs[0]["target"] == "unit123"


@pytest.mark.asyncio
async def test_promote_upload_requires_guard(monkeypatch, tmp_path):
    # Prepare a fake upload with one file
    ensure_uploads_root()
    upload_id = sanitize_upload_id("testupload")
    state = "think"
    state_dir = get_state_dir(upload_id, state)
    state_dir.mkdir(parents=True, exist_ok=True)
    fpath = state_dir / "dance.fbx"
    fpath.write_text("fbx-data")
    record_upload(upload_id, state, "dance.fbx", size_bytes=10)

    plugin = MateEnginePlugin()

    # Guard should prevent promotion by default
    if "SYNTH_MATEENGINE_PROMOTE_ENABLED" in os.environ:
        del os.environ["SYNTH_MATEENGINE_PROMOTE_ENABLED"]

    with pytest.raises(PermissionError):
        await plugin.execute_action(
            {
                "type": "promote_upload",
                "payload": {"upload_id": upload_id, "target_skin": "TestSkin"},
            },
            context={},
            bot=None,
            original_message=None,
        )

    # Enable promotion and try again
    os.environ["SYNTH_MATEENGINE_PROMOTE_ENABLED"] = "1"
    await plugin.execute_action(
        {
            "type": "promote_upload",
            "payload": {"upload_id": upload_id, "target_skin": "TestSkin"},
        },
        context={},
        bot=None,
        original_message=None,
    )

    dest = Path("skins") / "TestSkin" / "animations" / state / "dance.fbx"
    assert dest.exists()

    # Cleanup
    delete_upload(upload_id)
    shutil.rmtree(Path("skins") / "TestSkin")
    del os.environ["SYNTH_MATEENGINE_PROMOTE_ENABLED"]
