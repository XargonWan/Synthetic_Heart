"""The RetroXR rift's tests live with the rift. They need the same environment defaults the repository's
own tests get (the database host, so importing core modules does not look for a host that does not exist)."""

try:
    from tests.conftest import _load_repo_env_defaults

    _load_repo_env_defaults()
except Exception:  # the repository's conftest is not importable from here: nothing to load
    pass
