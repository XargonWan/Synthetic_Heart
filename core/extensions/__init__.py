"""Extension platform: manifests, archives, resolution, discovery and runtime.

See ``docs/plugin_platform``. The modules ``manifest``, ``archive`` and
``resolver`` import nothing from ``core`` so they can be shared with the extension
SDK. There is no compatibility layer for old in-process plugins.
"""

from core.extensions.archive import archive_sha256, check_archive, read_archive_manifest
from core.extensions.manager import ExtensionManager, LoadIssue
from core.extensions.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    load_manifest,
    parse_manifest,
    parse_manifest_text,
)
from core.extensions.resolver import (
    CORE_API_VERSION,
    Issue,
    Resolution,
    check_install,
    resolve,
)

__all__ = [
    "CORE_API_VERSION",
    "ExtensionManager",
    "Issue",
    "LoadIssue",
    "MANIFEST_FILENAME",
    "Manifest",
    "ManifestError",
    "Resolution",
    "archive_sha256",
    "check_archive",
    "check_install",
    "load_manifest",
    "parse_manifest",
    "parse_manifest_text",
    "read_archive_manifest",
    "resolve",
]
