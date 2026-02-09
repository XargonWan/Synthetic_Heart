import pytest

from core.session_meta import _merge_meta


def test_merge_top_level_simple():
    existing = {'camera': {'zoom': 1}, 'processing': False}
    incoming = {'processing': True}
    out = _merge_meta(existing, incoming)
    assert out['processing'] is True
    assert out['camera']['zoom'] == 1


def test_merge_nested_dicts():
    existing = {'chat_rect': {'left': 10, 'width': 400}}
    incoming = {'chat_rect': {'width': 420, 'height': 300}}
    out = _merge_meta(existing, incoming)
    assert out['chat_rect']['left'] == 10
    assert out['chat_rect']['width'] == 420
    assert out['chat_rect']['height'] == 300


def test_device_scoped_merge():
    existing = {'viewports': {'desktop': {'chat_rect': {'width': 400}}}, 'processing': False}
    incoming = {'device': 'desktop', 'chat_rect': {'width': 360, 'height': 280}}
    out = _merge_meta(existing, incoming)
    assert 'viewports' in out
    assert out['viewports']['desktop']['chat_rect']['width'] == 360
    assert out['viewports']['desktop']['chat_rect']['height'] == 280
    # ensure top-level unaffected
    assert out['processing'] is False


def test_device_scoped_new_device():
    existing = {}
    incoming = {'device': 'mobile', 'chat_rect': {'width': 300, 'height': 200}}
    out = _merge_meta(existing, incoming)
    assert out['viewports']['mobile']['chat_rect']['width'] == 300
