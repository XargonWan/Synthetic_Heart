#!/usr/bin/env bash
# Synthetic Heart — uninstaller for Linux.
#
#   ./uninstall.sh            remove it, and ask what to do with your Synth's database
#   ./uninstall.sh --purge    remove everything, including the database, without asking
#
# A thin wrapper so the command is where people look for it. The work happens in
# install.sh's --uninstall path, which is kept as the single copy of that logic.
# Anything else you pass is forwarded (--dir to point at another install, --dry-run to
# see what would happen).
#
# The database is the part that matters: the persona, the chat history, the memories and
# the diary are all in it, so removing the application leaves them intact and installing
# again picks up where you left off. --purge drops it as well, and that is the one step
# nothing can undo.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Default to this checkout, since that is where the script is being run from, but let an
# explicit --dir win.
explicit_dir=0
for arg in "$@"; do
    if [ "$arg" = "--dir" ]; then explicit_dir=1; fi
done
if [ "$explicit_dir" -eq 0 ]; then
    set -- --dir "$here" "$@"
fi

exec "$here/install.sh" --uninstall "$@"
