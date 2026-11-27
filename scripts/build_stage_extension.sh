#!/usr/bin/env bash
# Build the SyntH Stage client from frontend/ into the Stage extension package, which is what
# the extension serves. The package lives in the official plugins repository:
#
#   scripts/build_stage_extension.sh /path/to/synthetic-heart-official-plugins/extensions/synth_stage
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${1:?usage: build_stage_extension.sh <the synth_stage extension directory>}"
[ -f "$dest/synth-extension.toml" ] || { echo "$dest is not an extension directory"; exit 1; }
cd "$root/frontend"
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
corepack enable
pnpm install --frozen-lockfile
pnpm build
rm -rf "$dest/web"
cp -r dist "$dest/web"
echo "Stage client built into $dest/web"
