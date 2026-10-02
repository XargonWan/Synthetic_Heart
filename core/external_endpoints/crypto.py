# core/external_endpoints/crypto.py
"""Symmetric encryption for API key storage using Fernet.

The encryption key is resolved in this order:
1. ``SYNTH_SECRET_KEY`` environment variable (arbitrary string; derived via
   PBKDF2HMAC so the user does not need to supply a raw Fernet key).
2. ``SYNTH_SECRET_FILE``, when set.
3. The resolved data root (``core.app_paths.data_root()``), which is where a
   native install keeps it and where ``core.legacy_state`` adopts an existing
   secret from an older, container-path-based run.
4. The genuine container path ``/config/.synth_secret``.
5. ``~/.synthetic_heart/.synth_secret`` as a last resort.

Existing file wins over creating a new one at every step: a key must never be
regenerated while an old one still exists, or the stored endpoint keys become
undecryptable.

Neither plain-text keys nor the raw Fernet key are ever stored in the DB.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet

from core.app_paths import data_root


def _resolve_secret_file() -> Path:
    """Resolve where the Fernet key is persisted (see the module docstring)."""

    override = os.environ.get("SYNTH_SECRET_FILE", "").strip()
    if override:
        return Path(override).expanduser()

    home_path = Path.home() / ".synthetic_heart" / ".synth_secret"

    # An order that always prefers an existing file. The container path is
    # checked by existence rather than by writability: on Windows it is
    # drive-relative ("\config"), so "can I create it?" is not the question.
    candidates = [
        data_root() / ".synth_secret",
        Path("/config/.synth_secret"),
        home_path,
    ]

    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue

    preferred = candidates[0]
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        return home_path


_SECRET_FILE = _resolve_secret_file()

_fernet: Fernet | None = None


def _derive_key(password: str) -> bytes:
    """Derive a 32-byte key from an arbitrary password string (PBKDF2-HMAC-SHA256)."""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        b"synth-external-endpoints-salt-v1",
        iterations=100_000,
        dklen=32,
    )
    return base64.urlsafe_b64encode(dk)


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    password = os.environ.get("SYNTH_SECRET_KEY")
    if password:
        fernet_key = _derive_key(password)
    else:
        if _SECRET_FILE.exists():
            fernet_key = _SECRET_FILE.read_bytes().strip()
        else:
            fernet_key = Fernet.generate_key()
            try:
                _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
                _SECRET_FILE.write_bytes(fernet_key)
            except OSError:
                pass  # In-memory key; will change on restart

    _fernet = Fernet(fernet_key)
    return _fernet


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt a plaintext API key for DB storage.

    Returns an empty string when *plaintext* is empty.
    """
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt a previously encrypted API key.

    Returns an empty string when *ciphertext* is empty or decryption fails.
    """
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ""
