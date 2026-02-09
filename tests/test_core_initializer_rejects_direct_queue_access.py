import os
from pathlib import Path
from core.core_initializer import CoreInitializer


def test_core_initializer_flags_plugins_with_direct_queue_access(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    dev_plugins_dir = repo_root / 'plugins_dev'
    dev_plugins_dir.mkdir(parents=True, exist_ok=True)

    bad_plugin_file = dev_plugins_dir / 'bad_plugin.py'
    bad_plugin_code = '''# Test plugin
PLUGIN_CLASS = type('P', (), {})
# This plugin erroneously uses the internal queue:
# message_queue._queue.put((1, 1, {}))
'''
    try:
        bad_plugin_file.write_text(bad_plugin_code, encoding='utf-8')
        ci = CoreInitializer()
        ci.enable_dev_components(True)
        # Call the internal loader
        ci._load_plugins()
        # Expect a startup error regarding queue internals
        assert any('writes directly to queue internals' in e for e in ci.startup_errors)
    finally:
        try:
            bad_plugin_file.unlink()
        except Exception:
            pass
