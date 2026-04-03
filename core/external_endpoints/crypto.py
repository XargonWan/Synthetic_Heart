# core/external_endpoints/crypto.py
"""Symmetric encryption for API key storage using Fernet.

The encryption key is resolved in this order:
1. ``SYNTH_SECRET_KEY`` environment variable (arbitrary string; derived via
   PBKDF2HMAC so the user does not need to supply a raw Fernet key).
2. A persisted random key in ``/config/.synth_secret`` (auto-generated on
   first use, survives container restarts via a mounted volume).

Neither plain-text keys nor the raw Fernet key are ever stored in the DB.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet

_SECRET_FILE = Path("/config/.synth_secret")

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
