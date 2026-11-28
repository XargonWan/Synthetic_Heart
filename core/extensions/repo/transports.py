"""How a repository's files are fetched. The format on top is always the same.

``LocalTransport``  a directory on this machine (also a Git working tree);
``HttpTransport``   HTTPS (GitHub raw/releases, GitLab, any web server);
``GitTransport``    a Git URL, kept as a shallow checkout and read like a local one.

A transport only does two things: read a small file (the index, the signature) and copy
a file to a destination with a size limit. Verification is done by the caller, never by
a transport, so a new transport cannot weaken it. Credentials of a private repository
belong to the transport and are Core secrets: they are never given to an extension.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlparse

from core.extensions.repo import progress

CHUNK = 1024 * 1024


class TransportError(RuntimeError):
    """A file could not be fetched (missing, too large, network, unsafe path)."""


class Transport(Protocol):
    def describe(self) -> str: ...

    def read(self, relpath: str, limit: int) -> bytes:
        """A small file's bytes; ``TransportError`` if missing or larger than ``limit``."""
        ...

    def fetch(self, relpath: str, dest: Path, limit: int) -> None:
        """Copy a file to ``dest`` (created, mode 0600); never more than ``limit`` bytes."""
        ...

    def refresh(self) -> None:
        """Update whatever is cached (a Git checkout); a no-op for the others."""
        ...


def _safe_relative(relpath: str) -> PurePosixPath:
    pure = PurePosixPath(relpath)
    if not relpath or pure.is_absolute() or ".." in pure.parts or "\\" in relpath:
        raise TransportError(f"unsafe repository path {relpath!r}")
    return pure


class LocalTransport:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()

    def describe(self) -> str:
        return str(self.root)

    def _resolve(self, relpath: str) -> Path:
        pure = _safe_relative(relpath)
        base = self.root.resolve()
        target = (base / pure).resolve()
        # A symbolic link inside the repository must not lead out of it.
        if base != target and base not in target.parents:
            raise TransportError(f"{relpath!r} leads outside the repository")
        if not target.is_file():
            raise TransportError(f"{relpath!r} is not in the repository")
        return target

    def read(self, relpath: str, limit: int) -> bytes:
        path = self._resolve(relpath)
        if path.stat().st_size > limit:
            raise TransportError(f"{relpath!r} is larger than {limit} bytes")
        return path.read_bytes()

    def fetch(self, relpath: str, dest: Path, limit: int) -> None:
        source = self._resolve(relpath)
        copied = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with source.open("rb") as handle, os.fdopen(fd, "wb") as out:
                while chunk := handle.read(CHUNK):
                    copied += len(chunk)
                    if copied > limit:
                        raise TransportError(
                            f"{relpath!r} is larger than {limit} bytes"
                        )
                    out.write(chunk)
                    progress.advance(len(chunk))
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

    def refresh(self) -> None:
        return None


class HttpTransport:
    def __init__(
        self,
        base_url: str,
        *,
        allow_insecure: bool = False,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and not (
            allow_insecure and parsed.scheme == "http"
        ):
            raise TransportError(
                "a repository URL must be https:// (http:// only when allowed)"
            )
        if not parsed.netloc:
            raise TransportError("a repository URL needs a host")
        self.base = base_url.rstrip("/") + "/"
        self._token = token
        self._timeout = timeout
        self._insecure = allow_insecure

    def describe(self) -> str:
        return self.base

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _url(self, relpath: str) -> str:
        return self.base + str(_safe_relative(relpath))

    def _stream(self, relpath: str, limit: int) -> Iterator[bytes]:
        import httpx

        url = self._url(relpath)
        try:
            # No redirect to another scheme or host is followed: the index names its
            # files relative to the repository, so there is nothing to redirect to.
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                with client.stream("GET", url, headers=self._headers()) as response:
                    if response.status_code != 200:
                        raise TransportError(
                            f"{relpath!r}: HTTP {response.status_code}"
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise TransportError(
                            f"{relpath!r} is larger than {limit} bytes"
                        )
                    received = 0
                    for chunk in response.iter_bytes(CHUNK):
                        received += len(chunk)
                        if received > limit:
                            raise TransportError(
                                f"{relpath!r} is larger than {limit} bytes"
                            )
                        yield chunk
        except httpx.HTTPError as exc:
            raise TransportError(f"{relpath!r}: {type(exc).__name__}") from exc

    def read(self, relpath: str, limit: int) -> bytes:
        return b"".join(self._stream(relpath, limit))

    def fetch(self, relpath: str, dest: Path, limit: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in self._stream(relpath, limit):
                    out.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

    def refresh(self) -> None:
        return None


MAX_REDIRECTS = 5


def _https_stream(
    url: str,
    limit: int,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
    allow_loopback_http: bool = False,
) -> Iterator[bytes]:
    """Stream ``url`` (``https`` only), following redirects that stay on ``https``.

    A release asset is served from another host than the one in its URL, so redirects are
    followed (at most ``MAX_REDIRECTS``); the credentials in ``headers`` are never sent to
    a host other than the first. The size is bounded here, and what the bytes must be is
    decided by the caller (the signed index's SHA-256). ``allow_loopback_http`` exists for
    tests that serve from localhost.
    """
    import httpx

    def acceptable(target: str) -> bool:
        parsed = urlparse(target)
        if parsed.scheme == "https" and parsed.hostname:
            return True
        return bool(
            allow_loopback_http
            and parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        )

    if not acceptable(url):
        raise TransportError("a download URL must be https://")
    current = url
    first_host = urlparse(url).hostname
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                same_host = urlparse(current).hostname == first_host
                with client.stream(
                    "GET", current, headers=headers if same_host else None
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = str(
                            httpx.URL(current).join(
                                response.headers.get("location", "")
                            )
                        )
                        if not acceptable(current):
                            raise TransportError(
                                "the download redirected away from https"
                            )
                        continue
                    if response.status_code != 200:
                        raise TransportError(
                            f"download failed: HTTP {response.status_code}"
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise TransportError(
                            f"the download is larger than {limit} bytes"
                        )
                    received = 0
                    for chunk in response.iter_bytes(CHUNK):
                        received += len(chunk)
                        if received > limit:
                            raise TransportError(
                                f"the download is larger than {limit} bytes"
                            )
                        yield chunk
                    return
            raise TransportError("too many redirects")
    except httpx.HTTPError as exc:
        raise TransportError(f"download failed: {type(exc).__name__}") from exc


def _write_stream(chunks: Iterator[bytes], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                out.write(chunk)
                progress.advance(len(chunk))
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def fetch_url(
    url: str,
    dest: Path,
    limit: int,
    *,
    timeout: float = 60.0,
    allow_loopback_http: bool = False,
) -> None:
    """Download one package from the ``url`` its index entry names (see ``_https_stream``)."""
    _write_stream(
        _https_stream(
            url, limit, timeout=timeout, allow_loopback_http=allow_loopback_http
        ),
        dest,
    )


_GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_RELEASE_JSON_LIMIT = 4 * 1024 * 1024
_GITHUB_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class GithubReleaseTransport:
    """A repository whose files are the assets of one GitHub release.

    ``owner/repo`` and a release ``tag`` (a rolling one such as ``latest`` or
    ``latest-develop``): the catalog (``synth-repo.json`` and its signature) and every
    package are assets of that release, fetched from
    ``https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>``. Asset names are
    plain file names. Integrity does not rest on GitHub: the index is signed and names each
    package's SHA-256.
    """

    def __init__(
        self,
        repo: str,
        tag: str = "latest",
        *,
        token: str | None = None,
        base: str = "https://github.com",
        api_base: str = "https://api.github.com",
        timeout: float = 60.0,
        allow_loopback_http: bool = False,
    ) -> None:
        if not _GITHUB_REPO.match(repo):
            raise TransportError("a GitHub repository is written owner/name")
        if not _GITHUB_TAG.match(tag or "latest"):
            raise TransportError(f"{tag!r} is not a usable release tag")
        self.repo, self.tag = repo, tag or "latest"
        self._base = base.rstrip("/")
        self._api = api_base.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._loopback = allow_loopback_http

    def describe(self) -> str:
        return f"github:{self.repo}@{self.tag}"

    def _url(self, relpath: str) -> str:
        pure = _safe_relative(relpath)
        if len(pure.parts) != 1:
            raise TransportError(f"a release asset has a plain name, not {relpath!r}")
        return f"{self._base}/{self.repo}/releases/download/{self.tag}/{pure.name}"

    def _api_headers(self, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _asset_api_url(self, name: str) -> str:
        """The API URL of one asset: a private release is only readable through it."""
        import json

        try:
            release = json.loads(
                b"".join(
                    _https_stream(
                        f"{self._api}/repos/{self.repo}/releases/tags/{self.tag}",
                        _RELEASE_JSON_LIMIT,
                        headers=self._api_headers("application/vnd.github+json"),
                        timeout=self._timeout,
                        allow_loopback_http=self._loopback,
                    )
                )
            )
        except ValueError as exc:
            raise TransportError("GitHub answered with something unreadable") from exc
        for asset in release.get("assets", []):
            if asset.get("name") == name and isinstance(asset.get("url"), str):
                return asset["url"]
        raise TransportError(f"download failed: HTTP 404 (no asset {name!r})")

    def _stream(self, relpath: str, limit: int) -> Iterator[bytes]:
        if not self._token:
            return _https_stream(
                self._url(relpath),
                limit,
                timeout=self._timeout,
                allow_loopback_http=self._loopback,
            )
        pure = _safe_relative(relpath)
        if len(pure.parts) != 1:
            raise TransportError(f"a release asset has a plain name, not {relpath!r}")
        return _https_stream(
            self._asset_api_url(pure.name),
            limit,
            headers=self._api_headers("application/octet-stream"),
            timeout=self._timeout,
            allow_loopback_http=self._loopback,
        )

    def _friendly(self, exc: TransportError) -> TransportError | None:
        from core.extensions.repo.github_access import HINT, HINT_WITH_TOKEN

        text = str(exc)
        if any(f"HTTP {code}" in text for code in (401, 403, 429)):
            return TransportError(HINT_WITH_TOKEN if self._token else HINT)
        if "HTTP 404" in text:
            return TransportError(
                f"nothing is published yet: the release {self.tag!r} of {self.repo} has "
                "no catalog (the publishing pipeline has not run for it; HTTP 404)"
                + (
                    ""
                    if self._token
                    else ". If the repository is private, enter a GitHub access token "
                    "in Settings > Extensions > GitHub access token"
                )
            )
        return None

    def read(self, relpath: str, limit: int) -> bytes:
        try:
            return b"".join(self._stream(relpath, limit))
        except TransportError as exc:
            friendly = self._friendly(exc)
            if friendly is None:
                raise
            raise friendly from exc

    def fetch(self, relpath: str, dest: Path, limit: int) -> None:
        try:
            _write_stream(self._stream(relpath, limit), dest)
        except TransportError as exc:
            friendly = self._friendly(exc)
            if friendly is None:
                raise
            raise friendly from exc

    def refresh(self) -> None:
        return None


_GIT_URL_SCHEMES = ("https://", "ssh://", "git@")


class GitTransport:
    """A Git repository kept as a shallow checkout under ``cache_dir``."""

    def __init__(self, url: str, cache_dir: Path, *, branch: str = "") -> None:
        if not url.startswith(_GIT_URL_SCHEMES):
            raise TransportError(
                "a Git repository URL must be https://, ssh:// or git@host:path"
            )
        if branch and not all(c.isalnum() or c in "._/-" for c in branch):
            raise TransportError("unusable branch name")
        self.url = url
        self.branch = branch
        slug = hashlib.sha256(f"{url}\n{branch}".encode()).hexdigest()[:16]
        self.checkout = Path(cache_dir) / slug
        self._local = LocalTransport(self.checkout)

    def describe(self) -> str:
        return self.url + (f"@{self.branch}" if self.branch else "")

    def _git(self, *args: str, cwd: Path | None = None) -> None:
        # Only the protocols the URL check allows; no prompts; the URL after "--".
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "https:ssh",
        }
        try:
            subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                check=True,
                capture_output=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                detail = exc.stderr.decode("utf-8", "replace").strip()[-200:]
            raise TransportError(
                f"git {args[0]} failed: {detail or type(exc).__name__}"
            ) from exc

    def refresh(self) -> None:
        if (self.checkout / ".git").is_dir():
            self._git(
                "fetch",
                "--depth",
                "1",
                "origin",
                self.branch or "HEAD",
                cwd=self.checkout,
            )
            self._git("reset", "--hard", "FETCH_HEAD", cwd=self.checkout)
            return
        shutil.rmtree(self.checkout, ignore_errors=True)
        self.checkout.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--depth", "1"]
        if self.branch:
            args += ["--branch", self.branch]
        self._git(*args, "--", self.url, str(self.checkout))

    def read(self, relpath: str, limit: int) -> bytes:
        return self._local.read(relpath, limit)

    def fetch(self, relpath: str, dest: Path, limit: int) -> None:
        self._local.fetch(relpath, dest, limit)


def guess_kind(location: str) -> str:
    """``local``, ``http``, ``git`` or ``github`` for what the operator typed
    (``github:owner/name`` names a repository served as release assets)."""
    if location.startswith("github:"):
        return "github"
    if location.startswith(("https://", "http://")):
        return "git" if location.rstrip("/").endswith(".git") else "http"
    if location.startswith(("ssh://", "git@")):
        return "git"
    return "local"
