"""Command line for extensions: repositories, install, update, remove, and building.

::

    uv run python -m core.extensions.cli repo add official /path/to/repo --key <public key>
    uv run python -m core.extensions.cli search telegram
    uv run python -m core.extensions.cli install official/telegram --grant
    uv run python -m core.extensions.cli update
    uv run python -m core.extensions.cli pack /path/to/synthetic-heart-official-plugins/extensions/telegram --out ./myrepo
    uv run python -m core.extensions.cli index ./myrepo --sign ./publisher.key

It does the same things the WebUI Store does, through the same installer. A running Core
picks up an install or a removal at its next start (or from the Store, which restarts
the extension).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from core.extensions.repo import index as repo_index
from core.extensions.repo.config import ConfigError, Repository
from core.extensions.repo.installer import InstallError, Installer, UntrustedRepository
from core.extensions.repo.pack import (
    PackError,
    build_index,
    build_release,
    pack_extension,
)
from core.extensions.repo.transports import guess_kind

Out = Callable[[str], None]


def default_root() -> Path:
    override = os.environ.get("SYNTH_EXT_ROOT", "").strip()
    if override:
        return Path(override)
    from core.app_paths import data_root

    return data_root() / "extensions"


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synth-ext", description=(__doc__ or "").split("::")[0].strip()
    )
    parser.add_argument(
        "--root", type=Path, help="runtime root (default: <data>/extensions)"
    )
    parser.add_argument(
        "--bundled", type=Path, help="read-only repository baked into the image"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    repo = sub.add_parser("repo", help="manage repositories")
    rsub = repo.add_subparsers(dest="repo_command", required=True)
    add = rsub.add_parser("add", help="add a repository")
    add.add_argument("name")
    add.add_argument(
        "location", help="a directory, an https:// URL, a Git URL or github:owner/name"
    )
    add.add_argument("--kind", choices=("local", "http", "git", "github"))
    add.add_argument("--priority", type=int, default=100)
    add.add_argument("--key", default="", help="the publisher's public key (base64)")
    add.add_argument(
        "--unsigned", action="store_true", help="do not require a signature"
    )
    add.add_argument(
        "--trusted", action="store_true", help="install from it without asking"
    )
    add.add_argument(
        "--branch",
        default="",
        help="a Git branch, or the release tag of a github repository",
    )
    add.add_argument("--token", default="")
    rsub.add_parser("list", help="list repositories")
    rm = rsub.add_parser("remove", help="remove a repository")
    rm.add_argument("name")
    refresh = rsub.add_parser(
        "refresh", help="fetch and verify the index of a repository"
    )
    refresh.add_argument("name", nargs="?")

    search = sub.add_parser("search", help="what the repositories offer")
    search.add_argument("text", nargs="?", default="")
    sub.add_parser("list", help="what is installed")

    install = sub.add_parser("install", help="install extensions (and what they need)")
    install.add_argument(
        "ids", nargs="+", help="publisher/name or publisher/name==version"
    )
    install.add_argument("--repo")
    install.add_argument(
        "--yes", action="store_true", help="confirm untrusted repositories"
    )
    install.add_argument(
        "--grant", action="store_true", help="grant what the manifest declares"
    )

    update = sub.add_parser("update", help="update one extension, or all")
    update.add_argument("id", nargs="?")
    update.add_argument("--yes", action="store_true")

    remove = sub.add_parser(
        "remove", help="uninstall (files go; data stays unless --purge)"
    )
    remove.add_argument("id")
    remove.add_argument("--purge", action="store_true")
    rollback = sub.add_parser("rollback", help="go back to the previous version")
    rollback.add_argument("id")
    for name in ("enable", "disable"):
        toggle = sub.add_parser(name, help=f"{name} an installed extension")
        toggle.add_argument("id")
    grant = sub.add_parser("grant", help="grant the capabilities an extension declared")
    grant.add_argument("id")
    grant.add_argument(
        "capabilities", nargs="*", help="a subset (default: all declared)"
    )
    sub.add_parser("defaults", help="install the first-start defaults (once)")

    pack = sub.add_parser("pack", help="build a package from an extension folder")
    pack.add_argument("source", type=Path)
    pack.add_argument(
        "--out", type=Path, required=True, help="the repository directory"
    )
    pack.add_argument("--no-libs", action="store_true", help="do not build lib/")
    index = sub.add_parser("index", help="write (and sign) a repository's index")
    index.add_argument("directory", type=Path)
    index.add_argument("--name", default="")
    index.add_argument("--sign", type=Path, help="private key file")
    index.add_argument("--default-install", nargs="*", default=[])
    index.add_argument(
        "--url-base",
        default="",
        help="where oversized packages are hosted (a release download URL prefix)",
    )
    index.add_argument(
        "--url-above-mb",
        type=int,
        default=90,
        help="packages larger than this are listed with a URL (default 90, below a Git host's 100)",
    )
    release = sub.add_parser(
        "release",
        help="build the assets of a rolling GitHub release (one zip per extension + signed catalog)",
    )
    release.add_argument("sources", nargs="+", type=Path, help="extension folders")
    release.add_argument("--out", type=Path, required=True, help="where the assets go")
    release.add_argument("--name", default="official", help="the catalog's name")
    release.add_argument("--sign", type=Path, help="private key file")
    release.add_argument("--default-install", nargs="*", default=[])
    release.add_argument("--no-libs", action="store_true", help="do not build lib/")
    keygen = sub.add_parser("keygen", help="make a publisher key pair")
    keygen.add_argument("path", type=Path, help="where to write the private key")
    return parser


def _split(spec: str) -> tuple[str, str | None]:
    ext_id, _, version = spec.partition("==")
    return ext_id, (version or None)


def _run_repo(args: argparse.Namespace, inst: Installer, out: Out) -> int:
    if args.repo_command == "add":
        kind = args.kind or guess_kind(args.location)
        location = (
            args.location
            if kind != "local"
            else str(Path(args.location).expanduser().resolve())
        )
        repo = Repository(
            name=args.name,
            location=location,
            kind=kind,
            priority=args.priority,
            public_key=args.key,
            require_signature=bool(args.key) and not args.unsigned,
            trusted=args.trusted,
            branch=args.branch,
            token=args.token,
        )
        inst.repos.add(repo)
        try:
            index, signed = inst.load_index(repo, refresh=True)
        except InstallError as exc:
            inst.repos.remove(args.name)
            out(f"not added: {exc}")
            return 1
        out(
            f"added {args.name} ({kind}): {len(index.packages)} package(s), {'signed' if signed else 'NOT signed'}"
        )
        if args.key:
            out(f"pinned key {repo_index.key_fingerprint(args.key)}")
        return 0
    if args.repo_command == "list":
        for repo in inst.repos.load():
            key = (
                repo_index.key_fingerprint(repo.public_key)[:12]
                if repo.public_key
                else "-"
            )
            out(
                f"{repo.name:12} {repo.kind:5} prio {repo.priority:<4} {'on ' if repo.enabled else 'off'} "
                f"{'trusted' if repo.trusted else 'untrusted':9} key {key}  {repo.location}"
            )
        return 0
    if args.repo_command == "remove":
        if not inst.repos.remove(args.name):
            out(f"no repository named {args.name!r}")
            return 1
        out(f"removed {args.name}")
        return 0
    status = 0
    for repo in inst.repos.load():
        if args.name and repo.name != args.name:
            continue
        try:
            index, signed = inst.load_index(repo, refresh=True)
            out(
                f"{repo.name}: ok, {len(index.packages)} package(s), {'signed' if signed else 'not signed'}"
            )
        except InstallError as exc:
            out(f"{repo.name}: {exc}")
            status = 1
    return status


def run(argv: Sequence[str], out: Out = print) -> int:
    args = build_parser().parse_args(list(argv))
    if args.command == "keygen":
        private, public = repo_index.generate_keypair()
        args.path.write_bytes(private)
        args.path.chmod(0o600)
        out(f"private key written to {args.path} (keep it secret)")
        out(f"public key {public}")
        out(f"fingerprint {repo_index.key_fingerprint(public)}")
        return 0
    try:
        if args.command == "pack":
            archive, manifest = pack_extension(
                args.source, args.out, bundle_libs=not args.no_libs
            )
            out(f"packed {manifest.id} {manifest.version}: {archive}")
            return 0
        if args.command == "release":
            key = args.sign.read_bytes() if args.sign else None
            assets = build_release(
                args.sources,
                args.out,
                name=args.name,
                private_key=key,
                default_install=tuple(args.default_install),
                bundle_libs=not args.no_libs,
            )
            for asset in assets:
                out(f"{asset.name}  {asset.stat().st_size} bytes")
            out(
                f"{len(assets)} asset(s) in {args.out}{'' if key else ' (catalog NOT signed)'}"
            )
            return 0
        if args.command == "index":
            key = args.sign.read_bytes() if args.sign else None
            path = build_index(
                args.directory,
                name=args.name or args.directory.name,
                private_key=key,
                default_install=tuple(args.default_install),
                url_base=args.url_base,
                url_above=args.url_above_mb * 1024 * 1024,
            )
            out(f"wrote {path}{' (signed)' if key else ' (NOT signed)'}")
            return 0
        inst = Installer(args.root or default_root(), bundled=args.bundled)
        if args.command == "repo":
            return _run_repo(args, inst, out)
        return _run_manage(args, inst, out)
    except (InstallError, ConfigError, PackError) as exc:
        out(f"error: {exc}")
        return 1


def _run_manage(args: argparse.Namespace, inst: Installer, out: Out) -> int:
    if args.command in ("search", "list"):
        rows, problems = inst.rows()
        for row in rows:
            if args.command == "list" and not row.installed:
                continue
            if (
                args.command == "search"
                and args.text
                and args.text.lower()
                not in f"{row.id} {row.name} {row.description}".lower()
            ):
                continue
            state = row.installed or "-"
            flags = (
                ("update " if row.update else "")
                + ("experimental " if row.experimental else "")
                + ("" if row.enabled or not row.installed else "off")
            )
            out(
                f"{row.id:28} installed {state:9} available {row.available or '-':9} {flags.strip()}"
            )
        for problem in problems:
            out(f"warning: {problem}")
        return 0
    if args.command == "install":
        for spec in args.ids:
            ext_id, version = _split(spec)
            try:
                result = inst.install(
                    ext_id, version=version, repo=args.repo, allow_untrusted=args.yes
                )
            except UntrustedRepository as exc:
                if not _confirm(f"{exc}. Install anyway?", False):
                    out(f"error: {exc} (use --yes to confirm)")
                    return 1
                result = inst.install(
                    ext_id, version=version, repo=args.repo, allow_untrusted=True
                )
            for line in result.installed:
                out(f"installed {line}")
            if not result.installed:
                out(f"{ext_id} is already up to date")
            for warning in result.warnings:
                out(f"warning: {warning}")
            if args.grant:
                out(f"granted {', '.join(inst.grant(ext_id)) or 'nothing'}")
            else:
                wanted = ", ".join(inst.requested_capabilities(ext_id)) or "nothing"
                out(
                    f"{ext_id} asks for: {wanted} (nothing is granted yet: `grant {ext_id}`)"
                )
        return 0
    if args.command == "update":
        pending = inst.updates(refresh=True)
        if args.id:
            pending = [p for p in pending if p[0] == args.id]
        if not pending:
            out("everything is up to date")
            return 0
        for ext_id, old, new in pending:
            result = inst.update(ext_id, allow_untrusted=args.yes)
            out(
                f"updated {ext_id} {old} -> {new}"
                if result.installed
                else f"{ext_id} unchanged"
            )
        return 0
    if args.command == "remove":
        inst.remove(args.id, purge=args.purge)
        out(
            f"removed {args.id}"
            + (" and its data" if args.purge else " (its data is kept)")
        )
        return 0
    if args.command == "rollback":
        out(f"{args.id} is back at {inst.rollback(args.id)}")
        return 0
    if args.command in ("enable", "disable"):
        inst.set_enabled(args.id, args.command == "enable")
        out(f"{args.id} {args.command}d")
        return 0
    if args.command == "grant":
        out(f"granted {', '.join(inst.grant(args.id, args.capabilities or None))}")
        return 0
    installed, failed = inst.ensure_defaults()
    for item in installed:
        out(f"installed {item}")
    for item in failed:
        out(f"failed {item}")
    if not installed and not failed:
        out("nothing to do")
    return 1 if failed else 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
