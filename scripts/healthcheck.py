#!/usr/bin/env python3
"""Report whether a SyntH install is up, in one line.

Used by the installers, the launchers and by a user who just wants to know
"is it running?".  Stdlib only, so it works before ``uv sync`` has run.

Checks, in order:

1. the database is reachable (a TCP connect, no driver needed);
2. the WebUI answers over HTTP on the configured port;
3. the OpenAI-compatible API answers, when that interface is enabled.

Exit codes: ``0`` everything checked is up, ``1`` something is down,
``2`` the configuration could not be read.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_env_file(path: Path) -> dict[str, str]:
    """Read ``KEY=VALUE`` pairs from a ``.env`` file, ignoring comments."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def effective_env(env_file: Path) -> dict[str, str]:
    """Environment for the app: ``.env`` values, with the real environment winning."""
    merged = parse_env_file(env_file)
    merged.update({key: value for key, value in os.environ.items() if key in merged})
    return merged


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """Return True when a TCP connection to ``host:port`` succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_reachable(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for an HTTP GET, tolerating a self-signed cert."""
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return True, str(response.status)
    except urllib.error.HTTPError as exc:
        # Any HTTP status proves something is listening and answering.
        return True, str(exc.code)
    except Exception as exc:  # noqa: BLE001 - reported verbatim to the user
        return False, type(exc).__name__


def _webui_port(values: dict[str, str], tls: bool) -> str:
    if tls:
        return values.get("SYNTH_WEBUI_HTTPS_PORT") or values.get(
            "SYNTH_WEBUI_HTTP_PORT", "8080"
        )
    return values.get("SYNTH_WEBUI_HTTP_PORT", "8080")


def _webui_host(values: dict[str, str]) -> str:
    host = values.get("SYNTH_WEBUI_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return host


def _webui_candidates(values: dict[str, str]) -> list[tuple[str, bool]]:
    """Return ``(url, is_configured_scheme)`` pairs to probe, best guess first.

    The configured scheme is tried first, then the other one: a running server
    that disagrees with the ``.env`` on TLS still counts as up, and the
    disagreement is reported instead of showing a healthy SyntH as "down".
    """
    host = _webui_host(values)
    tls = values.get("SYNTH_WEBUI_TLS", values.get("SECURE_CONNECTION"))
    if tls is None:
        tls = "0" if host in {"127.0.0.1", "localhost"} else "1"
    configured_tls = tls == "1"
    candidates = [
        (
            f"{'https' if configured_tls else 'http'}://{host}:{_webui_port(values, configured_tls)}/",
            True,
        ),
    ]
    # The same port serves the other scheme when TLS was never given a port.
    other_scheme_url = f"{'http' if configured_tls else 'https'}://{host}:{_webui_port(values, not configured_tls)}/"
    if other_scheme_url not in {url for url, _ in candidates}:
        candidates.append((other_scheme_url, False))
    return candidates


def _probe_webui(values: dict[str, str]) -> dict[str, object]:
    """Probe the WebUI, returning the URL that answered and any TLS mismatch."""
    configured_tls = (
        values.get("SYNTH_WEBUI_TLS", values.get("SECURE_CONNECTION")) == "1"
    )
    for url, is_configured in _webui_candidates(values):
        ok, detail = http_reachable(url)
        if ok:
            answered_https = url.startswith("https://")
            return {
                "url": url,
                "ok": True,
                "detail": detail,
                "scheme_mismatch": answered_https != configured_tls,
            }
    first_url = _webui_candidates(values)[0][0]
    return {
        "url": first_url,
        "ok": False,
        "detail": "no response",
        "scheme_mismatch": False,
    }


def _webui_url(values: dict[str, str]) -> str:
    """Return the WebUI URL implied by the configuration."""
    return _webui_candidates(values)[0][0]


def check(env_file: Path) -> dict[str, Any]:
    """Return a structured status report."""
    values = effective_env(env_file)
    checks: dict[str, dict[str, Any]] = {}

    db_host = values.get("DB_HOST", "127.0.0.1")
    db_port = int(values.get("DB_PORT", "5432") or 5432)
    db_up = tcp_reachable(db_host, db_port)
    checks["database"] = {
        "target": f"{db_host}:{db_port}",
        "ok": db_up,
    }

    webui = _probe_webui(values)
    checks["webui"] = webui

    api_port = int(values.get("OPENAI_API_SERVER_PORT", "11435") or 11435)
    api_up, api_detail = http_reachable(f"http://127.0.0.1:{api_port}/v1/models")
    checks["api"] = {
        "url": f"http://127.0.0.1:{api_port}/v1/models",
        "ok": api_up,
        "detail": api_detail,
    }

    report: dict[str, Any] = {
        "env_file": str(env_file),
        "checks": checks,
        "ok": bool(db_up and webui.get("ok")),
        "url": webui.get("url"),
    }
    if webui.get("scheme_mismatch"):
        report["warnings"] = [
            "the WebUI answered with a different scheme than .env configures; "
            "the running process was started with different TLS settings"
        ]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check whether SyntH is up.")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--quiet", action="store_true", help="no output; the exit code is the answer"
    )
    args = parser.parse_args(argv)

    env_file = Path(args.env_file).expanduser()
    if not env_file.is_file() and not args.json and not args.quiet:
        print(f"no configuration at {env_file}: SyntH has not been set up yet")
        return 2

    report = check(env_file)
    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        checks = report["checks"]
        for name, entry in checks.items():
            state = "up  " if entry["ok"] else "down"
            target = entry.get("target") or entry.get("url")
            print(f"{state}  {name:9} {target}")
        for warning in report.get("warnings", []):
            print(f"note  {warning}")
        if report["ok"]:
            print(f"\nSyntH is running: {report['url']}")
        else:
            print("\nSyntH is not reachable.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
