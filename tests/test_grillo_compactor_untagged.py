import pytest

pytest.skip(
    "Test removed: compactor now skips batches with no tagged candidates",
    allow_module_level=True,
)
