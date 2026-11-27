"""The repository index: what a repository offers, and who vouches for it.

``synth-repo.json`` lists one entry per extension version (id, version, file, size,
SHA-256, the Core API range and a few flags). It is signed as a whole: ``synth-repo.json.sig``
holds an Ed25519 signature over the **exact bytes** of the index file, so there is no
canonicalisation to get wrong. An entry's SHA-256 is checked on every download, so the
signature covers every package without signing each one.

Key distribution is trust on first use: a repository is added with the public key the
operator got from the publisher (or accepts the fingerprint shown the first time); the
key is then pinned in the repository's configuration. A different key later is a refusal,
not a silent update.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlparse
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from core.extensions.manifest import EXTENSION_ID_RE

INDEX_NAME = "synth-repo.json"
SIGNATURE_NAME = "synth-repo.json.sig"
SCHEMA = 1
#: A repository index larger than this is refused.
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_PACKAGES = 5000
#: One package file larger than this is refused (before any download finishes).
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALGORITHM = "ed25519"


class IndexError_(ValueError):  # noqa: N818 - named to avoid shadowing the builtin
    """The index (or its signature) is not acceptable. ``problems`` lists why."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class PackageEntry:
    """One installable version of one extension."""

    id: str
    version: str
    file: str
    size: int
    sha256: str
    core_api: str = ""
    platform: str = "any"
    name: str = ""
    description: str = ""
    experimental: bool = False
    default_install: bool = False
    capabilities: tuple[str, ...] = ()
    #: Who wrote it (from the manifest), for the catalog.
    authors: tuple[str, ...] = ()
    #: Where to download the package when it is not kept in the repository itself (a
    #: package too big for a Git host, hosted as a release asset). ``https`` only; the
    #: SHA-256 and size above still decide whether the file is the right one.
    url: str = ""

    @property
    def parsed_version(self) -> Version:
        return Version(self.version)


@dataclass(frozen=True)
class RepoIndex:
    name: str
    generated: str
    packages: tuple[PackageEntry, ...] = ()
    #: SHA-256 of the exact index bytes this was parsed from.
    digest: str = ""
    raw: bytes = field(default=b"", repr=False, compare=False)

    def versions_of(self, extension_id: str) -> list[PackageEntry]:
        found = [p for p in self.packages if p.id == extension_id]
        return sorted(found, key=lambda p: p.parsed_version, reverse=True)


def _entry(raw: Any, position: int) -> PackageEntry:
    problems: list[str] = []
    where = f"packages[{position}]"
    if not isinstance(raw, dict):
        raise IndexError_([f"{where} must be an object"])

    def text(key: str, required: bool = True) -> str:
        value = raw.get(key, "")
        if not isinstance(value, str) or (required and not value):
            problems.append(f"{where}.{key} must be a non-empty string")
            return ""
        return value

    ext_id = text("id")
    if ext_id and not EXTENSION_ID_RE.match(ext_id):
        problems.append(f"{where}.id {ext_id!r} is not publisher/name")
    version = text("version")
    try:
        Version(version)
    except InvalidVersion:
        if version:
            problems.append(f"{where}.version {version!r} is not a valid version")
    file = text("file")
    parts = PurePosixPath(file).parts
    if file and (file.startswith("/") or ".." in parts or "\\" in file or ":" in file):
        problems.append(
            f"{where}.file {file!r} must be a relative path inside the repository"
        )
    url = text("url", required=False)
    if url:
        parsed_url = urlparse(url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or len(url) > 512
        ):
            problems.append(
                f"{where}.url must be an https:// URL without credentials (at most 512 characters)"
            )
    sha = text("sha256")
    if sha and not _SHA256.match(sha):
        problems.append(f"{where}.sha256 must be 64 lowercase hex digits")
    size = raw.get("size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= MAX_PACKAGE_BYTES
    ):
        problems.append(
            f"{where}.size must be a positive integer up to {MAX_PACKAGE_BYTES}"
        )
        size = 0
    core_api = text("core_api", required=False)
    if core_api:
        try:
            SpecifierSet(core_api)
        except InvalidSpecifier:
            problems.append(f"{where}.core_api {core_api!r} is not a version range")
    caps = raw.get("capabilities", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        problems.append(f"{where}.capabilities must be a list of strings")
        caps = []
    raw_authors = raw.get("authors", [])
    if not isinstance(raw_authors, list) or not all(
        isinstance(a, str) for a in raw_authors
    ):
        problems.append(f"{where}.authors must be a list of names")
        raw_authors = []
    for flag in ("experimental", "default_install"):
        if not isinstance(raw.get(flag, False), bool):
            problems.append(f"{where}.{flag} must be true or false")
    if problems:
        raise IndexError_(problems)
    return PackageEntry(
        id=ext_id,
        version=version,
        file=file,
        size=size,
        sha256=sha,
        core_api=core_api,
        platform=text("platform", required=False) or "any",
        name=text("name", required=False)[:120],
        description=text("description", required=False)[:500],
        experimental=bool(raw.get("experimental", False)),
        default_install=bool(raw.get("default_install", False)),
        capabilities=tuple(caps),
        authors=tuple(a[:80] for a in raw_authors[:8]),
        url=url,
    )


def parse_index(data: bytes) -> RepoIndex:
    """Parse and validate an index. Raises :class:`IndexError_` listing every problem."""
    if len(data) > MAX_INDEX_BYTES:
        raise IndexError_([f"the index is larger than {MAX_INDEX_BYTES} bytes"])
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IndexError_([f"the index is not valid JSON: {exc}"]) from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise IndexError_([f"the index must be an object with schema = {SCHEMA}"])
    packages_raw = raw.get("packages", [])
    if not isinstance(packages_raw, list) or len(packages_raw) > MAX_PACKAGES:
        raise IndexError_([f"packages must be a list of at most {MAX_PACKAGES}"])
    problems: list[str] = []
    packages: list[PackageEntry] = []
    for position, item in enumerate(packages_raw):
        try:
            packages.append(_entry(item, position))
        except IndexError_ as exc:
            problems.extend(exc.problems)
    seen: set[tuple[str, str]] = set()
    for entry in packages:
        key = (entry.id, str(entry.parsed_version))
        if key in seen:
            problems.append(f"{entry.id} {entry.version} is listed twice")
        seen.add(key)
    if problems:
        raise IndexError_(problems)
    return RepoIndex(
        name=str(raw.get("name", ""))[:80],
        generated=str(raw.get("generated", ""))[:40],
        packages=tuple(packages),
        digest=hashlib.sha256(data).hexdigest(),
        raw=data,
    )


# ----------------------------------------------------------------- signing


def key_fingerprint(public_key: str) -> str:
    """A short, comparable fingerprint of a base64 public key (32 hex digits)."""
    return hashlib.sha256(base64.b64decode(public_key)).hexdigest()[:32]


def generate_keypair() -> tuple[bytes, str]:
    """``(private key PEM, public key base64)`` for a new publisher key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return pem, base64.b64encode(public).decode("ascii")


def _load_private(private_pem: bytes) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("a repository key must be an Ed25519 private key")
    return key


def public_key_of(private_pem: bytes) -> str:
    from cryptography.hazmat.primitives import serialization

    key = _load_private(private_pem)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(public).decode("ascii")


def sign_index(data: bytes, private_pem: bytes) -> bytes:
    """The ``.sig`` document for ``data`` (the exact bytes of the index file)."""

    key = _load_private(private_pem)
    signature = key.sign(data)
    document = {
        "algorithm": ALGORITHM,
        "key_id": key_fingerprint(public_key_of(private_pem)),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return json.dumps(document, indent=2).encode("utf-8")


def verify_index(data: bytes, signature_doc: bytes, public_key: str) -> str | None:
    """``None`` when the signature is valid for ``public_key``; otherwise why not."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        document = json.loads(signature_doc.decode("utf-8"))
        if document.get("algorithm") != ALGORITHM:
            return f"unsupported signature algorithm {document.get('algorithm')!r}"
        signature = base64.b64decode(document["signature"], validate=True)
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key, validate=True)
        )
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return "the signature or the public key is malformed"
    try:
        key.verify(signature, data)
    except InvalidSignature:
        return "the signature does not match this key"
    return None
