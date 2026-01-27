import os
import json
import shutil
from pathlib import Path
import pytest
from datetime import datetime, timezone, timedelta

from core.webui import SynthWebUIInterface
from core.animation_uploads import (
    sanitize_upload_id,
    get_state_dir,
    record_upload,
    read_meta,
    list_uploads,
    delete_upload,
    promote_upload,
    cleanup_expired_uploads,
)


@pytest.mark.asyncio
async def test_record_list_and_delete_upload(tmp_path):
    webui = SynthWebUIInterface(autostart=False)

    upload_id = sanitize_upload_id('test123')
    state = 'idle'
    state_dir = get_state_dir(upload_id, state)
    state_dir.mkdir(parents=True, exist_ok=True)
    fpath = state_dir / 'idle1.fbx'
    fpath.write_text('dummy-fbx')

    meta = record_upload(upload_id, state, 'idle1.fbx', size_bytes=10, tags=['dance'])
    assert meta['upload_id'] == upload_id

    uploads = list_uploads()
    assert any(u['upload_id'] == upload_id for u in uploads)

    # Call the webui listing endpoint
    res = await webui.list_animation_uploads()
    body = json.loads(res.body)
    assert 'uploads' in body
    assert any(item['upload_id'] == upload_id for item in body['uploads'])

    # Delete via API
    res2 = await webui.delete_animation_upload(upload_id)
    body2 = json.loads(res2.body)
    assert body2.get('removed') == upload_id

    # Now list should not show it
    assert not any(u['upload_id'] == upload_id for u in list_uploads())


@pytest.mark.asyncio
async def test_promote_upload_endpoint_and_guard(tmp_path):
    webui = SynthWebUIInterface(autostart=False)

    upload_id = sanitize_upload_id('promote1')
    state = 'think'
    state_dir = get_state_dir(upload_id, state)
    state_dir.mkdir(parents=True, exist_ok=True)
    fpath = state_dir / 'move.fbx'
    fpath.write_text('fbx')
    record_upload(upload_id, state, 'move.fbx', size_bytes=5)

    class Req:
        async def json(self):
            return {"upload_id": upload_id, "target_skin": "TargetSkin", "target_state": state}

    # Promotion disabled by default
    if 'SYNTH_MATEENGINE_PROMOTE_ENABLED' in os.environ:
        del os.environ['SYNTH_MATEENGINE_PROMOTE_ENABLED']

    with pytest.raises(Exception):
        await webui.promote_animation_upload(Req())

    # Enable promotion
    os.environ['SYNTH_MATEENGINE_PROMOTE_ENABLED'] = '1'
    try:
        res = await webui.promote_animation_upload(Req())
        body = json.loads(res.body)
        assert body.get('status') == 'ok'
        # File should exist under skins/TargetSkin/animations/think/move.fbx
        dest = Path('skins') / 'TargetSkin' / 'animations' / state / 'move.fbx'
        assert dest.exists()
    finally:
        # Cleanup
        if Path('skins') / 'TargetSkin':
            shutil.rmtree(Path('skins') / 'TargetSkin')
        if os.environ.get('SYNTH_MATEENGINE_PROMOTE_ENABLED'):
            del os.environ['SYNTH_MATEENGINE_PROMOTE_ENABLED']
        delete_upload(upload_id)


def test_cleanup_expired_uploads(tmp_path):
    upload_id = sanitize_upload_id('old')
    state = 'idle'
    state_dir = get_state_dir(upload_id, state)
    state_dir.mkdir(parents=True, exist_ok=True)
    fpath = state_dir / 'old.fbx'
    fpath.write_text('x')
    record_upload(upload_id, state, 'old.fbx', size_bytes=1)

    # Manually set created_at to old date
    meta_path = Path('skins') / 'temp' / upload_id / 'meta.json'
    meta = read_meta(upload_id)
    meta['created_at'] = (datetime.now(tz=timezone.utc) - timedelta(days=10)).isoformat()
    with meta_path.open('w', encoding='utf-8') as h:
        json.dump(meta, h)

    removed = cleanup_expired_uploads(ttl_days=7)
    assert upload_id in removed
    assert not Path('skins') .joinpath('temp', upload_id).exists()
