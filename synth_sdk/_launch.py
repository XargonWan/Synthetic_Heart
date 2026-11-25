"""Script the Core runs to start an extension process.

Usage (by the launcher): ``python -S -I _launch.py <sdk_package_dir> <install_dir>``

``-S`` and ``-I`` keep the interpreter from reading ``site-packages`` or the
environment, and the repository root is **never** put on ``sys.path``: the SDK is
loaded from its own directory by explicit path and registered as ``synth_sdk``.
The extension therefore cannot ``import core`` by accident. (It is a convenience
for authors, not a security boundary: the boundary is the user the process runs as.)
"""

import importlib.util
import sys


def _main() -> int:
    sdk_dir, install_dir = sys.argv[1], sys.argv[2]
    spec = importlib.util.spec_from_file_location(
        "synth_sdk", f"{sdk_dir}/__init__.py", submodule_search_locations=[sdk_dir]
    )
    if spec is None or spec.loader is None:
        sys.stderr.write("synth_sdk: cannot load the SDK\n")
        return 64
    module = importlib.util.module_from_spec(spec)
    sys.modules["synth_sdk"] = module
    spec.loader.exec_module(module)
    sys.path[:0] = [install_dir, f"{install_dir}/lib"]
    from synth_sdk import _bootstrap

    return _bootstrap.main()


if __name__ == "__main__":
    raise SystemExit(_main())
