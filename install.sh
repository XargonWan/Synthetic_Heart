#!/usr/bin/env bash
# Synthetic Heart — one-click installer for Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/XargonWan/Synthetic_Heart/develop/install.sh | bash
#
# It installs the system packages SyntH needs, installs uv (which brings its own
# Python, so no Python prerequisite), fetches the source, then hands over to
# scripts/bootstrap.py, which creates the database, writes .env, picks free ports
# and installs the Python environment. Finally it adds a launcher and a desktop
# entry, so the user never has to touch a terminal again.
#
# Nothing about your Synth is decided here: their name, your name, your location,
# timezone and the engine + API key are collected by the WebUI setup page on the
# first launch.
#
# Usage:
#   ./install.sh                     install for the current user
#   ./install.sh --dir ~/SyntH       choose the install directory
#   ./install.sh --portable          no sudo: private PostgreSQL cluster
#   ./install.sh --extra local-voice add offline TTS/STT (large, ~2 GB with torch)
#   ./install.sh --no-start          install only; do not start SyntH afterwards
#   ./install.sh --uninstall         remove the app and ask what to do with the database
#   ./install.sh --uninstall --purge remove everything, database included
#   ./install.sh --dry-run           print what would happen
#
# A plain --uninstall asks (when run in a terminal) whether to remove the application
# alone or everything including the database, so the choice is made before anything is
# deleted. With no terminal - a script, a pipe, cron - it keeps the database and prints
# the commands that drop it. SYNTH_UNINSTALL_CHOICE=2 answers the question without a
# prompt; --purge does the same.
#
set -euo pipefail

APP_NAME="SyntH"
DEFAULT_REPO="https://github.com/XargonWan/Synthetic_Heart.git"
DEFAULT_BRANCH="develop"
INSTALL_DIR="${SYNTH_INSTALL_DIR:-$HOME/.local/share/SyntH}"
REPO_URL="${SYNTH_REPO_URL:-$DEFAULT_REPO}"
BRANCH="${SYNTH_BRANCH:-$DEFAULT_BRANCH}"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
EXTRAS=()
DO_DESKTOP=1
PORTABLE=0
DRY_RUN=0
UNINSTALL=0
PURGE=0
SKIP_PACKAGES=0
START_AFTER=1

# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s\n' "${BOLD}$*${RESET}"; }
ok()   { printf '  %s %s\n' "${GREEN}ok${RESET}" "$*"; }
note() { printf '  %s\n' "${DIM}$*${RESET}"; }
warn() { printf '  %s %s\n' "${YELLOW}warning:${RESET}" "$*" >&2; }
die()  { printf '%s %s\n' "${RED}error:${RESET}" "$*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  %s %s\n' "${DIM}would run:${RESET}" "$*"
        return 0
    fi
    "$@"
}

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --repo) REPO_URL="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --extra) EXTRAS+=("$2"); shift 2 ;;
        --portable) PORTABLE=1; shift ;;
        --no-desktop) DO_DESKTOP=0; shift ;;
        --no-start) START_AFTER=0; shift ;;
        --skip-packages) SKIP_PACKAGES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge) PURGE=1; UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

case "$(uname -s)" in
    Linux) ;;
    Darwin) die "this installer is for Linux; on macOS use the Docker or manual path" ;;
    *) die "unsupported platform: $(uname -s)" ;;
esac

# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
uninstall() {
    step "Removing $APP_NAME"

    # Read the database identity while .env still exists: removing the folder is what
    # this does, and --purge needs to know what to drop afterwards.
    local ENV_FILE="$INSTALL_DIR/.env"
    local DB_NAME="synth" DB_USER="synth" DB_HOST="" DB_PORT="5432"
    if [ -f "$ENV_FILE" ]; then
        DB_NAME="$(sed -n 's/^DB_NAME=//p' "$ENV_FILE" | head -1 | tr -d '"'\'' \r')"
        DB_USER="$(sed -n 's/^DB_USER=//p' "$ENV_FILE" | head -1 | tr -d '"'\'' \r')"
        DB_HOST="$(sed -n 's/^DB_HOST=//p' "$ENV_FILE" | head -1 | tr -d '"'\'' \r')"
        DB_PORT="$(sed -n 's/^DB_PORT=//p' "$ENV_FILE" | head -1 | tr -d '"'\'' \r')"
        [ -n "$DB_NAME" ] || DB_NAME="synth"
        [ -n "$DB_USER" ] || DB_USER="synth"
        [ -n "$DB_PORT" ] || DB_PORT="5432"
    fi

    # Ask before anything is removed, because the answer decides whether the database
    # outlives this run. The folder being deleted holds .env and data/, so once it is gone
    # there is nothing left to ask and no script left to pass --purge to, which is how the
    # hint about purging used to arrive too late to be useful. --purge skips the question;
    # a run with no terminal (a pipe, a script, cron) never blocks on one and keeps the
    # database, which is the only safe default.
    if [ "$PURGE" -eq 0 ] && [ -n "${SYNTH_UNINSTALL_CHOICE:-}" ]; then
        case "$(printf '%s' "$SYNTH_UNINSTALL_CHOICE" | tr -d '[:space:]')" in
            2|p|purge|full) PURGE=1 ;;
            *) PURGE=0 ;;
        esac
    elif [ "$PURGE" -eq 0 ] && { [ -t 0 ] || [ "${SYNTH_UNINSTALL_PROMPT:-0}" = "1" ]; }; then
        say ""
        say "How much should be removed?"
        say ""
        say "  1  the application only, keeping your Synth's database   (default)"
        say "  2  everything, database included - this cannot be undone"
        say ""
        printf "Choose 1 or 2 [1]: "
        local answer=""
        read -r answer || answer=""
        case "$(printf '%s' "$answer" | tr -d '[:space:]')" in
            2|p|purge|full) PURGE=1 ;;
            *) PURGE=0 ;;
        esac
        say ""
    fi

    if [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
        run "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/start_synth.py" --stop || true
    fi
    run rm -f "$BIN_DIR/synth"
    run rm -f "$DESKTOP_DIR/synth.desktop"
    run rm -rf "$INSTALL_DIR"
    ok "application files removed"

    # Everything under the folder went with it: the .env that held the generated database
    # credentials and any API keys, and everything in data/. That is not recoverable, so
    # it is said plainly whether or not --purge was asked for.
    if [ "$PURGE" -eq 1 ]; then
        step "Deleting the database"
        # Run the drop as the postgres superuser, whichever way this script was started. A
        # bare "sudo" prefix that is emptied out for root leaves "-n -u postgres dropdb" as
        # the command, which is not one; and a minimal system may have no sudo at all, so
        # root uses runuser (util-linux) instead.
        local -a PG=()
        if [ "$(id -u)" -eq 0 ]; then
            PG=(runuser -u postgres --)
        else
            PG=(sudo -n -u postgres)
        fi
        case "$DB_HOST" in
            ""|localhost|127.0.0.1|::1)
                run ${PG[@]+"${PG[@]}"} dropdb --if-exists "$DB_NAME" \
                    || warn "could not drop the database $DB_NAME; it is still there"
                run ${PG[@]+"${PG[@]}"} dropuser --if-exists "$DB_USER" \
                    || warn "could not drop the database user $DB_USER"
                ;;
            *)
                warn "the database is on $DB_HOST, not this machine, so it was not touched"
                note "on that host, run:  dropdb --if-exists $DB_NAME && dropuser --if-exists $DB_USER"
                ;;
        esac
    fi

    say ""
    if [ "$PURGE" -eq 1 ]; then
        say "Your database is gone as well. ${APP_NAME} lived there, so there is nothing"
        say "left: the persona, the chat history, the memories and the diary were all in it."
    else
        say "Your database was left alone, and that is where your Synth lives: the persona,"
        say "the chat history, the memories and the diary. Installing again brings them back"
        say "exactly as they were."
        say ""
        say "To delete that too, now that the application is gone:"
        say ""
        say "  sudo -u postgres dropdb --if-exists $DB_NAME"
        say "  sudo -u postgres dropuser --if-exists $DB_USER"
    fi
    say ""
    say "Removed with the folder, and not recoverable: settings, credentials and API keys"
    say "in .env, and everything under data/ (uploads included)."
    exit 0
}
[ "$UNINSTALL" -eq 1 ] && uninstall

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
step "Preparing to install $APP_NAME"
note "install directory: $INSTALL_DIR"
note "source:            $REPO_URL ($BRANCH)"

PKG=""
if command -v apt-get >/dev/null 2>&1; then PKG="apt"
elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
elif command -v pacman >/dev/null 2>&1; then PKG="pacman"
elif command -v zypper >/dev/null 2>&1; then PKG="zypper"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
fi

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 1. system packages
# ---------------------------------------------------------------------------
if [ "$SKIP_PACKAGES" -eq 0 ] && [ "$PORTABLE" -eq 0 ]; then
    step "Installing system packages (PostgreSQL + pgvector, ffmpeg)"
    if [ -z "$PKG" ]; then
        warn "unknown distribution: install PostgreSQL 14+ with pgvector and ffmpeg yourself"
    else
        case "$PKG" in
            apt)
                run $SUDO apt-get update -qq
                run $SUDO apt-get install -y postgresql postgresql-contrib ffmpeg git curl ca-certificates
                PG_MAJOR="$( (psql --version 2>/dev/null || true) | grep -oE '[0-9]+' | head -1)"
                if [ -n "$PG_MAJOR" ]; then
                    run $SUDO apt-get install -y "postgresql-${PG_MAJOR}-pgvector" \
                        || warn "package postgresql-${PG_MAJOR}-pgvector not available; memory search needs pgvector"
                fi
                ;;
            dnf)
                run $SUDO dnf install -y postgresql-server postgresql-contrib ffmpeg git curl
                run $SUDO dnf install -y pgvector || warn "pgvector package not available on this distribution"
                if ! [ -d /var/lib/pgsql/data ]; then
                    run $SUDO postgresql-setup --initdb || true
                fi
                run $SUDO systemctl enable --now postgresql || warn "could not start PostgreSQL automatically"
                ;;
            pacman)
                run $SUDO pacman -S --noconfirm postgresql pgvector ffmpeg git curl
                if ! [ -d /var/lib/postgres/data ]; then
                    run $SUDO -u postgres initdb -D /var/lib/postgres/data || true
                fi
                run $SUDO systemctl enable --now postgresql || warn "could not start PostgreSQL automatically"
                ;;
            zypper)
                run $SUDO zypper --non-interactive install postgresql-server postgresql-contrib ffmpeg git curl
                run $SUDO zypper --non-interactive install postgresql16-pgvector \
                    || warn "pgvector package not available; memory search needs pgvector"
                ;;
        esac
        ok "system packages present"
    fi
else
    step "Skipping system packages"
    if [ "$PORTABLE" -eq 1 ]; then
        note "--portable: scripts/bootstrap.py creates a private PostgreSQL cluster"
    fi
fi

# ---------------------------------------------------------------------------
# 2. uv (brings its own Python, so Python is not a prerequisite)
# ---------------------------------------------------------------------------
step "Installing uv"
if have uv; then
    ok "uv already installed ($(uv --version 2>/dev/null | head -1))"
elif [ "$DRY_RUN" -eq 1 ]; then
    note "would install uv from https://astral.sh/uv/install.sh"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv installation failed"
    export PATH="$HOME/.local/bin:$PATH"
    have uv || die "uv was installed but is not on PATH; open a new terminal and re-run"
    ok "uv installed"
fi

# ---------------------------------------------------------------------------
# 3. source
# ---------------------------------------------------------------------------
step "Fetching $APP_NAME"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -f "$SCRIPT_DIR/main.py" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
    ok "running from an existing checkout: $INSTALL_DIR"
elif [ -f "$INSTALL_DIR/main.py" ] && [ -f "$INSTALL_DIR/scripts/bootstrap.py" ]; then
    ok "existing installation found, updating"
    run git -C "$INSTALL_DIR" pull --ff-only || warn "could not update; continuing with the current version"
else
    have git || die "git is required to fetch the source"
    run mkdir -p "$(dirname "$INSTALL_DIR")"
    run git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" \
        || die "could not clone $REPO_URL (branch $BRANCH)"
    ok "source ready"
fi

cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 4. database, .env, ports, dependencies  (scripts/bootstrap.py)
# ---------------------------------------------------------------------------
BOOTSTRAP_ARGS=()
[ "$PORTABLE" -eq 1 ] && BOOTSTRAP_ARGS+=(--portable)
[ "$DRY_RUN" -eq 1 ] && BOOTSTRAP_ARGS+=(--dry-run)
[ "$DRY_RUN" -eq 1 ] && BOOTSTRAP_ARGS+=(--skip-sync)
for extra in "${EXTRAS[@]:-}"; do
    [ -n "$extra" ] && BOOTSTRAP_ARGS+=(--extra "$extra")
done

PY_FOR_BOOTSTRAP=""
for candidate in python3 python; do
    # Verify it actually runs: a Windows "App execution alias" stub, or a broken
    # interpreter, would otherwise be chosen and fail only later.
    if have "$candidate" && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        PY_FOR_BOOTSTRAP="$candidate"
        break
    fi
done
if [ -z "$PY_FOR_BOOTSTRAP" ]; then
    # uv can run the script without any usable system Python at all.
    note "no usable system Python found; using the uv-managed interpreter"
    PY_FOR_BOOTSTRAP="uv run --no-project python"
fi

# shellcheck disable=SC2086
$PY_FOR_BOOTSTRAP scripts/bootstrap.py "${BOOTSTRAP_ARGS[@]}" || die "setup failed (see the messages above)"

# ---------------------------------------------------------------------------
# 5. launcher + desktop entry
# ---------------------------------------------------------------------------
step "Adding the launcher"
if [ "$DRY_RUN" -eq 1 ]; then
    note "would write $BIN_DIR/synth and $DESKTOP_DIR/synth.desktop"
else
    mkdir -p "$BIN_DIR" "$DESKTOP_DIR"
    cat > "$BIN_DIR/synth" <<EOF
#!/usr/bin/env bash
# Start SyntH (and open the WebUI). Use: synth [--status|--stop|--foreground]
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/start_synth.py" "\$@"
EOF
    chmod +x "$BIN_DIR/synth"
    ok "command: synth"

    if [ "$DO_DESKTOP" -eq 1 ]; then
        ICON="$INSTALL_DIR/installer/synth-256.png"
        [ -f "$ICON" ] || ICON="$INSTALL_DIR/website/assets/synth_logo_bg.png"
        cat > "$DESKTOP_DIR/synth.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Talk to your SyntH
Exec=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/scripts/start_synth.py
Icon=$ICON
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
        chmod +x "$DESKTOP_DIR/synth.desktop"
        ok "desktop entry installed (it appears in your applications menu)"
    fi
fi

# ---------------------------------------------------------------------------
# 6. start it, on the setup page
# ---------------------------------------------------------------------------
# The installer used to stop here and leave the user to run `synth`, with nothing
# listening at the URL it printed: the browser that was opened as part of the
# bootstrap found no server. Starting it here is what makes the install one action,
# and `--setup` is the launcher's own path: it starts the application detached,
# waits for the WebUI to answer, and only then opens the browser - on the setup
# page, which is the one thing a native install still asks the user for.
if [ "$DRY_RUN" -eq 1 ]; then
    step "Starting $APP_NAME"
    note "would start it and open the setup page"
elif [ "$START_AFTER" -eq 0 ]; then
    step "Not starting $APP_NAME (--no-start)"
else
    step "Starting $APP_NAME"
    LAUNCHER="$BIN_DIR/synth"
    if [ ! -x "$LAUNCHER" ]; then
        LAUNCHER="$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/scripts/start_synth.py"
    fi
    # shellcheck disable=SC2086
    $LAUNCHER --setup || warn "SyntH did not start; try 'synth --setup' and see $INSTALL_DIR/logs/synth.log"
fi

# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH; add it to run 'synth' by name" ;;
esac

say ""
if [ "$DRY_RUN" -eq 1 ]; then
    say "${BOLD}Dry run complete.${RESET} Nothing was changed."
else
    say "${BOLD}$APP_NAME is installed.${RESET}"
    if [ "$START_AFTER" -eq 1 ]; then
        say "  Your browser is opening the setup page: give your SyntH a name, your"
        say "  own name, your location and timezone, and the engine + API key they"
        say "  should think with. Nothing else is needed."
        say ""
    else
        say "  Start it:   synth --setup   (opens the setup page)"
    fi
    say "  Status:     synth --status"
    say "  Stop it:    synth --stop"
    say "  Logs:       $INSTALL_DIR/logs/"
fi
