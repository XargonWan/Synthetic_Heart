#!/usr/bin/env bash
# Build the SyntH Stage client from frontend/ into the extension package
# (extensions_src/synth_stage/web/), which is what the extension serves.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/frontend"
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
corepack enable
pnpm install --frozen-lockfile
pnpm build
rm -rf "$root/extensions_src/synth_stage/web"
cp -r dist "$root/extensions_src/synth_stage/web"
echo "Stage client built into extensions_src/synth_stage/web"
