"""SyntH Windows Setup Wizard.

Run after cloning / installing SyntH on Windows to:
  - Install Python deps via uv
  - Configure MariaDB
  - Write the .env file interactively (all common vars)
  - Optionally set up the SOUL Postgres backend + pgvector
  - Optionally register SyntH as a Windows service via NSSM

Usage:
    python scripts/windows_setup.py                 # full wizard
    python scripts/windows_setup.py --reconfigure   # re-run .env wizard only
    python scripts/windows_setup.py --stage db      # re-run one stage
    python scripts/windows_setup.py --non-interactive  # use all defaults (CI)

Type < at any prompt to go back one field.
"""

from __future__ import annotations

import argparse
import platform
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_SQL = REPO_ROOT / "init-db.sql"
SOUL_SQL_TEMPLATE = REPO_ROOT / "scripts" / "sql" / "soul_memory_postgres.sql"
ENV_FILE = REPO_ROOT / ".env"
NSSM_EXE = REPO_ROOT / "tools" / "nssm.exe"

# ---------------------------------------------------------------------------
# Back-navigation sentinel
# ---------------------------------------------------------------------------


class _Back(Exception):
    """Raised when user types '<' to go back one field."""


# ---------------------------------------------------------------------------
# Rich console (optional — graceful fallback to plain print)
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.rule import Rule

    console = Console()

    def _print(msg: str) -> None:
        console.print(msg)

    def _banner() -> None:
        console.print()
        console.print(
            Panel(
                "[bold magenta]  ♥  Synthetic Heart  ♥[/bold magenta]\n"
                "[dim]       Windows Setup Wizard[/dim]",
                border_style="magenta",
                expand=False,
                padding=(0, 6),
            )
        )
        console.print()

    def _header(title: str) -> None:
        console.print(Rule(f"[bold cyan]{title}[/bold cyan]", style="cyan"))

    def _section(title: str) -> None:
        console.print(f"\n[bold white]{title}[/bold white]")

    def _hint(msg: str) -> None:
        console.print(f"    [dim]{msg}[/dim]")

    def _ok(msg: str) -> None:
        console.print(f"[bold green] ✓ [/bold green] {msg}")

    def _warn(msg: str) -> None:
        console.print(f"[bold yellow] ! [/bold yellow] {msg}")

    def _err(msg: str) -> None:
        console.print(f"[bold red] ✗ [/bold red] {msg}")

    def _ask(prompt: str, default: str = "") -> str:
        val = Prompt.ask(
            f"  [cyan]{prompt}[/cyan] [dim]( < = back )[/dim]", default=default
        )
        if val.strip() == "<":
            raise _Back()
        return val

    def _confirm(prompt: str, default: bool = False) -> bool:
        return Confirm.ask(f"  {prompt}", default=default)

    def _choice(prompt: str, choices: list[str], default: str) -> str:
        joined = " [dim]/[/dim] ".join(
            f"[green]{c}[/green]" if c == default else c for c in choices
        )
        val = Prompt.ask(
            f"  [cyan]{prompt}[/cyan] [{joined}] [dim]( < = back )[/dim]",
            default=default,
        )
        if val.strip() == "<":
            raise _Back()
        while val not in choices:
            val = Prompt.ask(
                f"  Please choose one of: {' / '.join(choices)}", default=default
            )
            if val.strip() == "<":
                raise _Back()
        return val

    HAS_RICH = True

except ImportError:
    HAS_RICH = False

    def _print(msg: str) -> None:
        print(msg)

    def _banner() -> None:
        print("\n" + "=" * 52)
        print("  ♥  Synthetic Heart — Windows Setup Wizard  ♥")
        print("=" * 52 + "\n")

    def _header(title: str) -> None:
        print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")

    def _section(title: str) -> None:
        print(f"\n{title}")

    def _hint(msg: str) -> None:
        print(f"    ({msg})")

    def _ok(msg: str) -> None:
        print(f" [OK] {msg}")

    def _warn(msg: str) -> None:
        print(f" [!!] {msg}")

    def _err(msg: str) -> None:
        print(f" [XX] {msg}")

    def _ask(prompt: str, default: str = "") -> str:
        val = input(f"  {prompt} [{default}] (< = back): ").strip()
        if val == "<":
            raise _Back()
        return val if val else default

    def _confirm(prompt: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        raw = input(f"  {prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")

    def _choice(prompt: str, choices: list[str], default: str) -> str:
        joined = " / ".join(choices)
        val = input(f"  {prompt} [{joined}] (default: {default}, < = back): ").strip()
        if val == "<":
            raise _Back()
        return val if val in choices else default


# ---------------------------------------------------------------------------
# Step-based back-navigable field runner
# ---------------------------------------------------------------------------


@dataclass
class _Step:
    key: str
    label: str
    default: str
    tip: str = ""
    choices: list[str] | None = None


def _run_steps(
    steps: list[_Step],
    existing: dict[str, str],
    non_interactive: bool,
) -> dict[str, str]:
    """Run a list of prompts with < = back navigation."""
    answers: dict[str, str] = {}
    i = 0
    while i < len(steps):
        s = steps[i]
        cur_default = answers.get(s.key, existing.get(s.key, s.default))
        if non_interactive:
            answers[s.key] = cur_default
            i += 1
            continue
        if s.tip:
            _hint(s.tip)
        try:
            if s.choices:
                answers[s.key] = _choice(s.label, s.choices, cur_default)
            else:
                answers[s.key] = _ask(s.label, default=cur_default)
            i += 1
        except _Back:
            if i > 0:
                i -= 1
                _print("  [dim]<< going back[/dim]" if HAS_RICH else "  << going back")
    return answers


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SyntH Windows Setup Wizard")
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Accept all defaults; skip optional stages (CI mode)",
    )
    p.add_argument(
        "--reconfigure",
        action="store_true",
        help="Re-run the .env wizard only (Stage 3)",
    )
    p.add_argument(
        "--stage",
        metavar="NAME",
        help="Re-run one stage: check / deps / db / env / gitnexus / soul / service",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd or REPO_ROOT, check=check, text=True, capture_output=True
    )


def _run_visible(cmd: list[str], *, cwd: Path | None = None) -> int:
    return subprocess.run(cmd, cwd=cwd or REPO_ROOT).returncode


def _generate_password(length: int = 20) -> str:
    return secrets.token_urlsafe(length)


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _write_env(values: dict[str, str]) -> None:
    lines = [
        "# SyntH configuration — generated by windows_setup.py",
        "# Re-run: python scripts/windows_setup.py --reconfigure",
        "",
    ]
    sections: list[tuple[str, list[str]]] = [
        (
            "Persona & engine defaults",
            [
                "SYNTH_NAME",
                "SYNTH_PROFILE",
                "BASE_CORTEX",
                "ACTIVE_VOX_ENGINE",
                "ACTIVE_AURIS_ENGINE",
                "SYNTH_AUTONOMY_MODE",
            ],
        ),
        (
            "LLM credentials",
            [
                "GEMINI_API_KEY",
                "OPENAI_API_KEY",
                "OPENROUTER_API_KEY",
                "ANTHROPIC_API_KEY",
            ],
        ),
        (
            "Interface credentials",
            [
                "BOTFATHER_TOKEN",
                "DISCORD_BOT_TOKEN",
                "MATRIX_HOMESERVER",
                "MATRIX_USER",
                "MATRIX_PASSWORD",
            ],
        ),
        (
            "Trainer / notifications",
            ["TRAINER_CHAT_ID", "LOG_CHAT_ID", "LOG_CHAT_INTERFACE"],
        ),
        (
            "WebUI",
            [
                "SYNTH_WEBUI_HTTP_PORT",
                "SYNTH_WEBUI_HTTPS_PORT",
                "SYNTH_WEBUI_TLS",
                "TZ",
            ],
        ),
        ("Database", ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASS", "DB_NAME"]),
        (
            "SOUL / Embedder",
            [
                "SOUL_REPOSITORY_BACKEND",
                "SOUL_POSTGRES_DSN",
                "SOUL_EMBEDDER_ID",
                "SOUL_EMBEDDER_USE_GPU",
            ],
        ),
        (
            "Behavior defaults",
            [
                "PROJECT_DEFAULT_LANGUAGE",
                "PROJECT_DEFAULT_TONE",
                "DIARY_HISTORY_DAYS",
                "GRILLO_BEAT_INTERVAL",
                "ENABLE_RECON",
                "ENABLE_DEBRIEF",
                "SYNTH_HOST_OS",
            ],
        ),
        (
            "Observability",
            [
                "LANGFUSE_ENABLED",
                "LANGFUSE_HOST",
                "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY",
                "CORTEX_API_LOG_ENABLED",
            ],
        ),
    ]
    written: set[str] = set()
    for section_name, keys in sections:
        section_lines = [f"{k}={values[k]}" for k in keys if k in values]
        if section_lines:
            lines += (
                [f"# {'─' * 60}", f"# {section_name}", f"# {'─' * 60}"]
                + section_lines
                + [""]
            )
            written.update(keys)
    extras = {k: v for k, v in values.items() if k not in written}
    if extras:
        lines.append("# Other")
        lines += [f"{k}={v}" for k, v in extras.items()]
        lines.append("")
    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
    _ok(f".env written to {ENV_FILE}")


# ---------------------------------------------------------------------------
# Stage 0 — Environment check
# ---------------------------------------------------------------------------


def stage_check(_args: argparse.Namespace) -> bool:
    _header("Stage 0: Environment check")
    ok = True
    vi = sys.version_info
    if vi < (3, 11):
        _err(f"Python 3.11+ required — found {vi.major}.{vi.minor}")
        ok = False
    else:
        _ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")

    if platform.system() != "Windows":
        _warn("This wizard is designed for Windows. Some steps may behave differently.")

    uv_path = shutil.which("uv")
    if uv_path:
        _ok(f"uv found at {uv_path}")
    else:
        _warn("uv not found — attempting install via PowerShell...")
        rc = _run_visible(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "irm https://astral.sh/uv/install.ps1 | iex",
            ]
        )
        if rc != 0:
            _err("uv install failed. Install manually: https://docs.astral.sh/uv/")
            ok = False
        else:
            _ok("uv installed")

    node_path = shutil.which("node")
    if node_path:
        _ok(f"Node.js found at {node_path}")
    else:
        _warn(
            "Node.js not found — GitNexus MCP server won't work. Install via: winget install OpenJS.NodeJS.LTS"
        )

    return ok


# ---------------------------------------------------------------------------
# Stage 1 — Python deps
# ---------------------------------------------------------------------------


def stage_deps(_args: argparse.Namespace) -> bool:
    _header("Stage 1: Installing Python dependencies")
    try:
        _print("  Running uv lock (updates lockfile for Windows platform)...")
        _run(["uv", "lock"], check=True)
        _ok("uv lock complete")
        _print("  Running uv sync...")
        rc = _run_visible(["uv", "sync"])
        if rc != 0:
            _err("uv sync failed — check the output above")
            return False
        _ok("uv sync complete")
        return True
    except Exception as exc:
        _err(f"Dependency install failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Stage 2 — MariaDB setup
# ---------------------------------------------------------------------------


def stage_db(args: argparse.Namespace) -> tuple[bool, dict[str, str]]:
    _header("Stage 2: MariaDB setup")
    _hint(
        "SyntH uses MariaDB as its primary database for chat history, memory, config, and emotion state."
    )
    db_conf: dict[str, str] = {
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "3306",
        "DB_USER": "synth",
        "DB_NAME": "synth",
    }

    _print("  Checking MariaDB service status...")
    for svc_name in ("MariaDB", "MySQL"):
        result = subprocess.run(
            ["sc", "query", svc_name], capture_output=True, text=True
        )
        if result.returncode == 0:
            if "RUNNING" not in result.stdout:
                _print(f"  Starting {svc_name} service...")
                subprocess.run(["sc", "start", svc_name], check=False)
                for _ in range(15):
                    time.sleep(2)
                    r = subprocess.run(
                        ["sc", "query", svc_name], capture_output=True, text=True
                    )
                    if "RUNNING" in r.stdout:
                        break
            _ok(f"{svc_name} service is running")
            break
    else:
        _warn(
            "MariaDB service not found. If you installed via the SyntH installer, this is unexpected."
        )
        _warn(
            "Re-run: python scripts/windows_setup.py --stage db  after starting the service."
        )

    if args.non_interactive:
        root_pass = ""
        db_pass = _generate_password()
    else:
        _hint(
            "Enter the MariaDB root password. For a fresh install from the SyntH installer, this is blank — just press Enter."
        )
        root_pass = _ask("MariaDB root password", default="")
        _hint(
            "This password will be set for the 'synth' database user. Leave blank to auto-generate a secure one."
        )
        db_pass = _ask(
            "Password for 'synth' DB user (blank = auto-generate)", default=""
        )
        if not db_pass:
            db_pass = _generate_password()
            _print(
                f"  Generated password: [bold magenta]{db_pass}[/bold magenta]"
                if HAS_RICH
                else f"  Generated password: {db_pass}"
            )
    db_conf["DB_PASS"] = db_pass

    try:
        import pymysql

        conn = pymysql.connect(
            host="127.0.0.1",
            port=3306,
            user="root",
            password=root_pass,
            connect_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `synth` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(
                "CREATE USER IF NOT EXISTS 'synth'@'localhost' IDENTIFIED BY %s",
                (db_pass,),
            )
            cur.execute("GRANT ALL PRIVILEGES ON `synth`.* TO 'synth'@'localhost'")
            cur.execute("FLUSH PRIVILEGES")
        conn.close()
        _ok("Database and user created")
    except Exception as exc:
        _err(f"MariaDB setup failed: {exc}")
        _warn("Fix this and re-run: python scripts/windows_setup.py --stage db")
        return False, db_conf

    if INIT_SQL.exists():
        try:
            conn = pymysql.connect(
                host="127.0.0.1",
                port=3306,
                user="synth",
                password=db_pass,
                database="synth",
                connect_timeout=10,
            )
            sql_text = INIT_SQL.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                for stmt in [s.strip() for s in sql_text.split(";") if s.strip()]:
                    try:
                        cur.execute(stmt)
                    except Exception:
                        pass
            conn.commit()
            conn.close()
            _ok("init-db.sql seed applied")
        except Exception as exc:
            _warn(f"init-db.sql seed failed (non-fatal): {exc}")

    return True, db_conf


# ---------------------------------------------------------------------------
# Stage 3 — .env wizard
# ---------------------------------------------------------------------------


def stage_env(args: argparse.Namespace, db_conf: dict[str, str]) -> bool:
    _header("Stage 3: Configuration wizard")
    _hint("Type < at any prompt to go back one field.")

    existing = _load_env()
    if ENV_FILE.exists() and not args.non_interactive:
        if not _confirm(
            ".env already exists — overwrite with new values?", default=False
        ):
            _ok(".env kept unchanged")
            return True

    values: dict[str, str] = {}

    # ── Group A: Persona ──────────────────────────────────────────────────────
    _section("A. Persona & engine")
    group_a = _run_steps(
        [
            _Step(
                "SYNTH_NAME",
                "AI name",
                "Synth",
                "The name your AI responds to and uses in conversations.",
            ),
            _Step(
                "SYNTH_PROFILE",
                "Persona profile",
                "You are Synth, a modular AI persona.",
                "System prompt prefix defining personality. Keep it concise — it's prepended to every request.",
            ),
            _Step(
                "BASE_CORTEX",
                "Base LLM engine",
                existing.get("BASE_CORTEX", "gemini"),
                "Which AI provider to use. gemini = Google Gemini API. anthropic = Claude. openrouter = multi-provider gateway.",
                choices=["gemini", "openapi", "openrouter", "anthropic"],
            ),
            _Step(
                "ACTIVE_VOX_ENGINE",
                "TTS engine",
                existing.get("ACTIVE_VOX_ENGINE", "edge_tts"),
                "Text-to-speech engine. edge_tts = free Microsoft cloud voices. kitten = local neural TTS (~150 MB). gtts = Google Translate.",
                choices=["edge_tts", "kitten", "gtts"],
            ),
            _Step(
                "ACTIVE_AURIS_ENGINE",
                "STT engine",
                existing.get("ACTIVE_AURIS_ENGINE", "vosk"),
                "Speech-to-text engine. vosk = offline (~50 MB model). whisper = OpenAI Whisper, better accuracy.",
                choices=["vosk", "whisper"],
            ),
            _Step(
                "SYNTH_AUTONOMY_MODE",
                "Autonomy mode",
                existing.get("SYNTH_AUTONOMY_MODE", "always_ask"),
                "always_ask = confirm every action. disabled = no autonomous actions. whitelist = allowed list only. always_approve = fully autonomous.",
                choices=["always_ask", "disabled", "whitelist", "always_approve"],
            ),
        ],
        existing,
        args.non_interactive,
    )
    values.update(group_a)

    # ── Group B: LLM credentials ──────────────────────────────────────────────
    _section(
        "B. LLM API keys  [dim](leave blank to skip)[/dim]"
        if HAS_RICH
        else "B. LLM API keys (leave blank to skip)"
    )
    _hint(
        "Only fill in keys for engines you plan to use. All can be added later by re-running --reconfigure."
    )
    b_steps = [
        _Step(
            "GEMINI_API_KEY",
            "Gemini API key",
            existing.get("GEMINI_API_KEY", ""),
            "Required for BASE_CORTEX=gemini. Get one at https://aistudio.google.com/apikey",
        ),
        _Step(
            "OPENAI_API_KEY",
            "OpenAI API key",
            existing.get("OPENAI_API_KEY", ""),
            "Required for BASE_CORTEX=openapi or any OpenAI-compatible endpoint.",
        ),
        _Step(
            "OPENROUTER_API_KEY",
            "OpenRouter API key",
            existing.get("OPENROUTER_API_KEY", ""),
            "Required for BASE_CORTEX=openrouter. Gives access to 100+ models via one key.",
        ),
        _Step(
            "ANTHROPIC_API_KEY",
            "Anthropic API key",
            existing.get("ANTHROPIC_API_KEY", ""),
            "Required for BASE_CORTEX=anthropic (Claude models).",
        ),
    ]
    group_b = _run_steps(b_steps, existing, args.non_interactive)
    for k, v in group_b.items():
        if v:
            values[k] = v

    # ── Group C: Interfaces ───────────────────────────────────────────────────
    _section("C. Interfaces")
    _hint(
        "Chat interfaces let users talk to SyntH via messaging platforms. All optional."
    )
    if not args.non_interactive:
        _hint("Telegram: fast setup, great for mobile. Get a token from @BotFather.")
        if _confirm(
            "Enable Telegram bot?", default=bool(existing.get("BOTFATHER_TOKEN"))
        ):
            try:
                t = _ask("BotFather token", default=existing.get("BOTFATHER_TOKEN", ""))
                if t:
                    values["BOTFATHER_TOKEN"] = t
            except _Back:
                pass

        _hint(
            "Discord: rich embeds, server-based. Create a bot at discord.com/developers."
        )
        if _confirm(
            "Enable Discord bot?", default=bool(existing.get("DISCORD_BOT_TOKEN"))
        ):
            try:
                t = _ask(
                    "Discord bot token", default=existing.get("DISCORD_BOT_TOKEN", "")
                )
                if t:
                    values["DISCORD_BOT_TOKEN"] = t
            except _Back:
                pass

        _hint("Matrix: open-source federated chat. Works with Element, Beeper, etc.")
        if _confirm("Enable Matrix?", default=bool(existing.get("MATRIX_HOMESERVER"))):
            for key, label, tip in [
                (
                    "MATRIX_HOMESERVER",
                    "Matrix homeserver URL",
                    "e.g. https://matrix.org",
                ),
                ("MATRIX_USER", "Matrix user", "e.g. @synth:matrix.org"),
                ("MATRIX_PASSWORD", "Matrix password", ""),
            ]:
                try:
                    v = _ask(label, default=existing.get(key, ""))
                    if v:
                        values[key] = v
                except _Back:
                    break

    # ── Group D: Trainer ──────────────────────────────────────────────────────
    _section("D. Trainer / notifications")
    _hint(
        "Optional: the 'trainer' user receives status messages and can send commands to SyntH."
    )
    d_steps = [
        _Step(
            "TRAINER_CHAT_ID",
            "Trainer chat ID",
            existing.get("TRAINER_CHAT_ID", ""),
            "Format: interface/user_id — e.g. telegram_bot/123456789. Leave blank to skip.",
        ),
        _Step(
            "LOG_CHAT_ID",
            "Log chat ID",
            existing.get("LOG_CHAT_ID", ""),
            "Where to send system log messages. Same format as trainer ID.",
        ),
        _Step(
            "LOG_CHAT_INTERFACE",
            "Log chat interface",
            existing.get("LOG_CHAT_INTERFACE", ""),
            "Interface name for the log channel, e.g. telegram_bot.",
        ),
    ]
    group_d = _run_steps(d_steps, existing, args.non_interactive)
    for k, v in group_d.items():
        if v:
            values[k] = v

    # ── Group E: WebUI ────────────────────────────────────────────────────────
    _section("E. WebUI")
    _hint(
        "SyntH's browser-based control panel. Accessible at http://localhost:<port> by default."
    )
    group_e = _run_steps(
        [
            _Step(
                "SYNTH_WEBUI_HTTP_PORT",
                "HTTP port",
                existing.get("SYNTH_WEBUI_HTTP_PORT", "8001"),
                "Default 8001. Change if that port is already in use on your machine.",
            ),
            _Step(
                "SYNTH_WEBUI_HTTPS_PORT",
                "HTTPS port",
                existing.get("SYNTH_WEBUI_HTTPS_PORT", "8000"),
                "Only used if TLS is enabled below.",
            ),
            _Step(
                "SYNTH_WEBUI_TLS",
                "Enable TLS (1/0)",
                existing.get("SYNTH_WEBUI_TLS", "0"),
                "Set to 1 if you have SSL certificates and want HTTPS. Leave 0 for local use.",
            ),
            _Step(
                "TZ",
                "Timezone",
                existing.get("TZ", "UTC"),
                "IANA timezone name, e.g. Europe/London, America/New_York. Affects log timestamps.",
            ),
        ],
        existing,
        args.non_interactive,
    )
    values.update(
        {
            k: v or s.default
            for s, (k, v) in zip(
                [
                    _Step("SYNTH_WEBUI_HTTP_PORT", "", "8001"),
                    _Step("SYNTH_WEBUI_HTTPS_PORT", "", "8000"),
                    _Step("SYNTH_WEBUI_TLS", "", "0"),
                    _Step("TZ", "", "UTC"),
                ],
                group_e.items(),
            )
        }
    )
    # simpler: just merge with fallback
    for k, v in group_e.items():
        values[k] = v or existing.get(k, "")

    # ── Group F: Database ─────────────────────────────────────────────────────
    _section("F. Database")
    _hint(
        "Connection settings for MariaDB. Pre-filled from Stage 2 — only change if using a remote DB."
    )
    group_f = _run_steps(
        [
            _Step(
                "DB_HOST",
                "DB host",
                db_conf.get("DB_HOST", existing.get("DB_HOST", "127.0.0.1")),
                "127.0.0.1 for local MariaDB. Change if connecting to a remote server.",
            ),
            _Step(
                "DB_PORT",
                "DB port",
                db_conf.get("DB_PORT", existing.get("DB_PORT", "3306")),
                "Default MariaDB port is 3306.",
            ),
            _Step(
                "DB_USER",
                "DB user",
                db_conf.get("DB_USER", existing.get("DB_USER", "synth")),
                "The database user created in Stage 2.",
            ),
            _Step(
                "DB_PASS",
                "DB password",
                db_conf.get("DB_PASS", existing.get("DB_PASS", "")),
                "Password for the synth DB user set in Stage 2.",
            ),
            _Step(
                "DB_NAME",
                "DB name",
                db_conf.get("DB_NAME", existing.get("DB_NAME", "synth")),
                "The database name. 'synth' unless you changed it.",
            ),
        ],
        existing,
        args.non_interactive,
    )
    values.update(group_f)

    # ── Group G: Behavior ─────────────────────────────────────────────────────
    _section("G. Behavior defaults")
    group_g = _run_steps(
        [
            _Step(
                "PROJECT_DEFAULT_LANGUAGE",
                "Default language",
                existing.get("PROJECT_DEFAULT_LANGUAGE", "en"),
                "ISO 639-1 language code, e.g. en, de, fr, ja. Used for generated content.",
            ),
            _Step(
                "PROJECT_DEFAULT_TONE",
                "Default tone",
                existing.get("PROJECT_DEFAULT_TONE", "balanced"),
                "balanced / formal / casual / playful. Influences response style.",
            ),
            _Step(
                "DIARY_HISTORY_DAYS",
                "Diary history days",
                existing.get("DIARY_HISTORY_DAYS", "7"),
                "How many days of diary entries to include in context. Higher = more memory, more tokens.",
            ),
            _Step(
                "GRILLO_BEAT_INTERVAL",
                "Grillo beat interval (seconds)",
                existing.get("GRILLO_BEAT_INTERVAL", "900"),
                "How often Grillo autonomous beats run. 900 = 15 min. Set to 0 to disable.",
            ),
            _Step(
                "ENABLE_RECON",
                "Enable recon (1/0)",
                existing.get("ENABLE_RECON", "0"),
                "Recon runs background research tasks. Set 1 to enable, 0 to disable (default off).",
            ),
            _Step(
                "ENABLE_DEBRIEF",
                "Enable debrief (1/0)",
                existing.get("ENABLE_DEBRIEF", "0"),
                "Debrief summarizes sessions after they end. Set 1 to enable, 0 to disable (default off).",
            ),
        ],
        existing,
        args.non_interactive,
    )
    values.update(group_g)
    values["SYNTH_HOST_OS"] = "windows"

    # ── Group H: Observability ────────────────────────────────────────────────
    _section("H. Observability")
    group_h = _run_steps(
        [
            _Step(
                "CORTEX_API_LOG_ENABLED",
                "Enable cortex API log (1/0)",
                existing.get("CORTEX_API_LOG_ENABLED", "0"),
                "Logs every LLM request/response to cortex_api.log. Useful for debugging but grows fast. Default off.",
            ),
            _Step(
                "LANGFUSE_ENABLED",
                "Enable Langfuse (1/0)",
                existing.get("LANGFUSE_ENABLED", "0"),
                "Langfuse provides LLM observability dashboards. Requires a Langfuse account. Default off.",
            ),
        ],
        existing,
        args.non_interactive,
    )
    values.update(group_h)

    if values.get("LANGFUSE_ENABLED") == "1" and not args.non_interactive:
        _hint("Langfuse credentials — find these in your Langfuse project settings.")
        for key, label in [
            ("LANGFUSE_HOST", "Langfuse host"),
            ("LANGFUSE_PUBLIC_KEY", "Langfuse public key"),
            ("LANGFUSE_SECRET_KEY", "Langfuse secret key"),
        ]:
            try:
                v = _ask(label, default=existing.get(key, ""))
                if v:
                    values[key] = v
            except _Back:
                break

    _write_env(values)
    return True


# ---------------------------------------------------------------------------
# Stage 4 — GitNexus
# ---------------------------------------------------------------------------


def stage_gitnexus(args: argparse.Namespace) -> bool:
    _header("Stage 4: GitNexus code-intelligence index")
    _hint(
        "GitNexus indexes the codebase so Claude Code (and other MCP clients) can query it with context-aware searches."
    )
    if not shutil.which("npx"):
        _warn("npx not found — skipping GitNexus index. Install Node.js to enable.")
        return True
    if not args.non_interactive and not _confirm(
        "Build GitNexus index now? (~1-2 min)", default=True
    ):
        _ok("Skipped")
        return True
    _print("  Running npx gitnexus analyze...")
    rc = _run_visible(["npx", "gitnexus", "analyze"])
    if rc == 0:
        _ok("GitNexus index built")
        return True
    _warn("GitNexus analyze failed (non-fatal). Run manually: npx gitnexus analyze")
    return True


# ---------------------------------------------------------------------------
# Stage 5 — SOUL Postgres + pgvector
# ---------------------------------------------------------------------------


def stage_soul(args: argparse.Namespace, env_values: dict[str, str]) -> bool:
    _header("Stage 5: SOUL long-term memory  (optional Postgres/pgvector)")
    _hint("SOUL gives SyntH persistent long-term memory using semantic vector search.")
    _hint("Requires a PostgreSQL database with the pgvector extension installed.")
    _hint(
        "Skip this now and re-run later: python scripts/windows_setup.py --stage soul"
    )

    if args.non_interactive:
        _ok("Skipped (non-interactive mode)")
        return True
    if not _confirm(
        "Enable SOUL long-term memory with Postgres/pgvector?", default=False
    ):
        _ok("Skipped")
        return True

    try:
        from embedders_registry import DEFAULT_EMBEDDER_KEY, EMBEDDERS, EmbedderSpec  # ty: ignore[unresolved-import]
    except ImportError:
        _err("embedders_registry.py not found — cannot configure embedder")
        return False

    _print("\n  Select embedding model:")
    _hint(
        "The embedder converts text to vectors for semantic memory search. Larger = better quality but slower/heavier."
    )
    keys = list(EMBEDDERS.keys())
    for i, k in enumerate(keys, 1):
        spec: EmbedderSpec = EMBEDDERS[k]
        gpu_note = (
            " [bold yellow][GPU recommended][/bold yellow]"
            if not spec.cpu_optimized
            else ""
        )
        _print(
            f"    [cyan]{i}.[/cyan] [bold]{spec.display_name}[/bold] — {spec.dims}-dim, ~{spec.size_mb} MB{gpu_note}"
            if HAS_RICH
            else f"    {i}. {spec.display_name} — {spec.dims}-dim, ~{spec.size_mb} MB{'  [GPU recommended]' if not spec.cpu_optimized else ''}"
        )
        _print(
            f"       [dim]{spec.description}[/dim]"
            if HAS_RICH
            else f"       {spec.description}"
        )

    i = 0
    while True:
        try:
            choice_str = _ask(f"Choose [1-{len(keys)}]", default="1")
            try:
                chosen_key = keys[int(choice_str) - 1]
                break
            except (ValueError, IndexError):
                chosen_key = DEFAULT_EMBEDDER_KEY
                break
        except _Back:
            if i > 0:
                i -= 1
            else:
                _ok("Skipped")
                return True

    chosen_spec: EmbedderSpec = EMBEDDERS[chosen_key]
    _ok(f"Selected: {chosen_spec.display_name} ({chosen_spec.dims}-dim)")

    if not chosen_spec.cpu_optimized:
        _warn(
            "This model is large and GPU-intensive. CPU inference will be very slow (~minutes per query)."
        )

    _hint(
        "DirectML works on any Windows GPU (NVIDIA, AMD, Intel) without CUDA. CUDA is for Linux NVIDIA only."
    )
    use_gpu = _confirm(
        "Use GPU for embeddings? (DirectML on Windows, CUDA on Linux)", default=False
    )

    _hint("Format: postgresql://user:password@host:port/database")
    _hint("Example: postgresql://soul:soul@localhost:5432/soul_memory")
    dsn = env_values.get(
        "SOUL_POSTGRES_DSN", "postgresql://soul:soul@localhost:5432/soul_memory"
    )
    try:
        dsn = _ask("Postgres DSN", default=dsn)
    except _Back:
        _ok("Skipped")
        return True

    _print("  Validating Postgres connection...")
    try:
        import asyncio
        from core.db import connect_postgres_dsn

        async def _check() -> None:
            conn = await connect_postgres_dsn(dsn)
            await conn.close()

        asyncio.run(_check())
        _ok("Postgres connection OK")
    except Exception as exc:
        _err(f"Postgres connection failed: {exc}")
        _warn("Ensure Postgres is running and pgvector is installed, then re-run:")
        _warn("  python scripts/windows_setup.py --stage soul")
        _warn("Install Postgres: winget install PostgreSQL.PostgreSQL.16")
        _warn("Install pgvector: see https://github.com/pgvector/pgvector#windows")
        return False

    if SOUL_SQL_TEMPLATE.exists():
        sql = SOUL_SQL_TEMPLATE.read_text(encoding="utf-8").replace(
            "{EMBEDDING_DIM}", str(chosen_spec.dims)
        )
        _print(f"  Applying SOUL schema (VECTOR({chosen_spec.dims}))...")
        try:
            import asyncio
            from core.db import connect_postgres_dsn

            async def _apply() -> None:
                conn = await connect_postgres_dsn(dsn)
                await conn.execute(sql)
                await conn.close()

            asyncio.run(_apply())
            _ok("SOUL schema applied")
        except Exception as exc:
            _warn(f"Schema apply failed (may already be applied): {exc}")

    _print(
        f"  Downloading {chosen_spec.display_name} model (~{chosen_spec.size_mb} MB)..."
    )
    try:
        import os as _os

        _os.environ["SOUL_EMBEDDER_USE_GPU"] = "1" if use_gpu else "0"
        sys.path.insert(0, str(REPO_ROOT))
        from core.soul.fastembed_embedder import FastEmbedder

        embedder = FastEmbedder(model_id=chosen_spec.model_id)
        embedder._model  # noqa: B018
        _ok(
            f"Model downloaded to {REPO_ROOT / '.cache' / 'synth' / 'models' / 'fastembed'}"
        )
    except Exception as exc:
        _warn(f"Model pre-download failed (will download on first use): {exc}")

    env_values["SOUL_REPOSITORY_BACKEND"] = "postgres"
    env_values["SOUL_POSTGRES_DSN"] = dsn
    env_values["SOUL_EMBEDDER_ID"] = chosen_spec.model_id
    env_values["SOUL_EMBEDDER_USE_GPU"] = "1" if use_gpu else "0"
    _write_env(env_values)
    _ok("SOUL configuration written to .env")
    return True


# ---------------------------------------------------------------------------
# Stage 6 — Windows Service
# ---------------------------------------------------------------------------


def stage_service(args: argparse.Namespace) -> bool:
    _header("Stage 6: Windows Service registration  (optional)")
    _hint("Registers SyntH as a Windows service so it starts automatically on boot.")
    _hint("Uses NSSM (Non-Sucking Service Manager) — bundled with the installer.")

    if args.non_interactive:
        _ok("Skipped (non-interactive mode)")
        return True
    if not _confirm(
        "Register SyntH as a Windows service (auto-starts on boot)?", default=False
    ):
        _ok("Skipped")
        return True

    if not NSSM_EXE.exists():
        _warn(f"NSSM not found at {NSSM_EXE}.")
        _warn("Download from https://nssm.cc/download and place at tools/nssm.exe")
        return False

    start_bat = REPO_ROOT / "scripts" / "start_synth.bat"
    nssm = str(NSSM_EXE)
    try:
        subprocess.run([nssm, "install", "SyntH", str(start_bat)], check=True)
        subprocess.run(
            [nssm, "set", "SyntH", "AppDirectory", str(REPO_ROOT)], check=True
        )
        subprocess.run(
            [nssm, "set", "SyntH", "Start", "SERVICE_AUTO_START"], check=True
        )
        _ok("SyntH service registered. Start with: sc start SyntH")
    except Exception as exc:
        _err(f"Service registration failed: {exc}")
        return False
    return True


# ---------------------------------------------------------------------------
# Stage 7 — Summary
# ---------------------------------------------------------------------------


def stage_summary(results: dict[str, bool]) -> None:
    _header("Setup Summary")
    if HAS_RICH:
        from rich.table import Table as RTable

        t = RTable(
            show_header=True, header_style="bold magenta", border_style="magenta"
        )
        t.add_column("Stage", style="cyan")
        t.add_column("Result")
        for stage, ok in results.items():
            t.add_row(
                stage,
                "[bold green] ✓ OK[/bold green]"
                if ok
                else "[bold red] ✗ FAILED / SKIPPED[/bold red]",
            )
        console.print(t)
    else:
        for stage, ok in results.items():
            print(f"  {stage:30s} {'OK' if ok else 'FAILED/SKIPPED'}")

    env = _load_env()
    port = env.get("SYNTH_WEBUI_HTTP_PORT", "8001")
    _print(
        f"\n[bold magenta]♥  SyntH is ready![/bold magenta]  WebUI → [link]http://localhost:{port}[/link]"
        if HAS_RICH
        else f"\n♥  SyntH is ready!  WebUI → http://localhost:{port}"
    )
    _print("  Start:       uv run python main.py")
    _print("  Modules:     python scripts/module_installer.py list")
    _print("  Reconfigure: python scripts/windows_setup.py --reconfigure")
    _print("  SOUL/memory: python scripts/windows_setup.py --stage soul")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _banner()
    args = _parse_args()
    results: dict[str, bool] = {}
    db_conf: dict[str, str] = {}
    env_values: dict[str, str] = _load_env()

    only = args.stage

    def should_run(name: str) -> bool:
        return only is None or only == name

    if args.reconfigure:
        stage_env(args, db_conf)
        return

    if should_run("check"):
        results["check"] = stage_check(args)
        if not results["check"] and not args.non_interactive:
            _err("Environment check failed — resolve issues above before continuing.")
            sys.exit(1)

    if should_run("deps"):
        results["deps"] = stage_deps(args)

    if should_run("db"):
        ok, db_conf = stage_db(args)
        results["db"] = ok

    if should_run("env"):
        env_values.update(db_conf)
        results["env"] = stage_env(args, env_values)
        env_values = _load_env()

    if should_run("gitnexus"):
        results["gitnexus"] = stage_gitnexus(args)

    if should_run("soul"):
        results["soul"] = stage_soul(args, dict(env_values))

    if should_run("service"):
        results["service"] = stage_service(args)

    if only is None:
        stage_summary(results)


if __name__ == "__main__":
    main()
