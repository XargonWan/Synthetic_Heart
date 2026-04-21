"""SyntH module installer CLI.

Usage:
    python scripts/module_installer.py list
    python scripts/module_installer.py status
    python scripts/module_installer.py install <module-id>
    python scripts/module_installer.py remove  <module-id>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from modules_registry import MODULES, ModuleSpec  # noqa: E402  # ty: ignore[unresolved-import]

# ---------------------------------------------------------------------------
# Rich console (same pattern as windows_setup.py)
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.table import Table

    console = Console()

    def _print(msg: str) -> None:
        console.print(msg)

    def _ok(msg: str) -> None:
        console.print(f"[green]✓[/green] {msg}")

    def _err(msg: str) -> None:
        console.print(f"[red]✗[/red] {msg}")

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

    def _print(msg: str) -> None:
        print(msg)

    def _ok(msg: str) -> None:
        print(f"[OK] {msg}")

    def _err(msg: str) -> None:
        print(f"[ERR] {msg}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _installed_packages() -> set[str]:
    """Return the set of package names currently installed in the uv venv."""
    try:
        result = subprocess.run(
            ["uv", "pip", "list", "--format=freeze"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        names: set[str] = set()
        for line in result.stdout.splitlines():
            if "==" in line:
                names.add(line.split("==")[0].lower().replace("-", "_"))
        return names
    except Exception:
        return set()


def _is_module_installed(spec: ModuleSpec) -> bool:
    if not spec.uv_packages:
        return True  # in-core module — always "available"
    installed = _installed_packages()
    return all(
        pkg.split(">=")[0].split("==")[0].lower().replace("-", "_") in installed
        for pkg in spec.uv_packages
    )


# ---------------------------------------------------------------------------
# Post-install hooks
# ---------------------------------------------------------------------------


def download_kitten_model() -> None:
    _print("  Triggering KittenTTS model download on first use...")
    try:
        import asyncio

        sys.path.insert(0, str(REPO_ROOT))
        from core.model_manager import MODEL_MANAGER

        models = [
            m for m in MODEL_MANAGER.catalog() if m.get("plugin_id") == "vox_kitten"
        ]
        if models:
            asyncio.run(MODEL_MANAGER.download(models[0]["model_id"]))
            _ok("KittenTTS model downloaded")
        else:
            _print("  KittenTTS model will be downloaded on first use via the WebUI.")
    except Exception as exc:
        _print(f"  Model download skipped (will happen on first use): {exc}")


def download_vosk_model() -> None:
    _print("  VOSK model will be downloaded automatically on first use.")
    _ok("No manual download needed for VOSK")


_POST_INSTALL_HOOKS: dict[str, object] = {
    "download_kitten_model": download_kitten_model,
    "download_vosk_model": download_vosk_model,
}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(_args: argparse.Namespace) -> None:
    if HAS_RICH:
        t = Table(show_header=True, header_style="bold")
        t.add_column("ID", style="cyan")
        t.add_column("Name")
        t.add_column("Packages")
        t.add_column("Description")
        for mid, spec in MODULES.items():
            pkgs = ", ".join(spec.uv_packages) if spec.uv_packages else "(in core)"
            t.add_row(mid, spec.name, pkgs, spec.description)
        console.print(t)
    else:
        for mid, spec in MODULES.items():
            pkgs = ", ".join(spec.uv_packages) if spec.uv_packages else "(in core)"
            print(f"  {mid:25s} {spec.name:20s} {pkgs}")
            print(f"      {spec.description}")


def cmd_status(_args: argparse.Namespace) -> None:
    if HAS_RICH:
        t = Table(show_header=True, header_style="bold")
        t.add_column("ID", style="cyan")
        t.add_column("Name")
        t.add_column("Installed")
        for mid, spec in MODULES.items():
            installed = _is_module_installed(spec)
            t.add_row(
                mid, spec.name, "[green]yes[/green]" if installed else "[dim]no[/dim]"
            )
        console.print(t)
    else:
        for mid, spec in MODULES.items():
            installed = _is_module_installed(spec)
            print(f"  {mid:25s} {'installed' if installed else 'not installed'}")


def cmd_install(args: argparse.Namespace) -> None:
    mid = args.module
    if mid not in MODULES:
        _err(f"Unknown module: {mid}")
        _print(f"Available: {', '.join(MODULES)}")
        sys.exit(1)

    spec = MODULES[mid]
    _print(
        f"Installing module: [bold]{spec.name}[/bold]"
        if HAS_RICH
        else f"Installing module: {spec.name}"
    )

    if spec.uv_packages:
        rc = subprocess.run(
            ["uv", "add"] + spec.uv_packages,
            cwd=REPO_ROOT,
        ).returncode
        if rc != 0:
            _err("Package install failed")
            sys.exit(1)
        _ok(f"Packages installed: {', '.join(spec.uv_packages)}")
    else:
        _print("  (no extra packages — module is part of core deps)")

    if spec.post_install and spec.post_install in _POST_INSTALL_HOOKS:
        hook = _POST_INSTALL_HOOKS[spec.post_install]
        if callable(hook):
            hook()  # type: ignore[call-arg]

    if spec.env_vars:
        _print("\n  Suggested .env additions:")
        for k, v in spec.env_vars.items():
            _print(f"    {k}={v}")
        _print(
            "  Add these to your .env (or re-run: python scripts/windows_setup.py --reconfigure)"
        )

    if spec.requires_restart:
        _ok(f"Module '{mid}' installed. Restart SyntH to activate.")
    else:
        _ok(f"Module '{mid}' installed.")


def cmd_remove(args: argparse.Namespace) -> None:
    mid = args.module
    if mid not in MODULES:
        _err(f"Unknown module: {mid}")
        sys.exit(1)

    spec = MODULES[mid]
    if not spec.uv_packages:
        _err(f"Module '{mid}' is part of core deps — cannot remove it here.")
        sys.exit(1)

    rc = subprocess.run(
        ["uv", "remove"] + spec.uv_packages,
        cwd=REPO_ROOT,
    ).returncode
    if rc != 0:
        _err("Package removal failed")
        sys.exit(1)
    _ok(f"Module '{mid}' removed. Restart SyntH to deactivate.")


# ---------------------------------------------------------------------------
# Argument parsing & entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="SyntH module installer")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all available modules")
    sub.add_parser("status", help="Show install status for all modules")

    pi = sub.add_parser("install", help="Install a module")
    pi.add_argument("module", help="Module ID (see: list)")

    pr = sub.add_parser("remove", help="Remove a module")
    pr.add_argument("module", help="Module ID (see: list)")

    args = p.parse_args()
    dispatch = {
        "list": cmd_list,
        "status": cmd_status,
        "install": cmd_install,
        "remove": cmd_remove,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
