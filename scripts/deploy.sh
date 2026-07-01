#!/usr/bin/env bash
# Publish the current state of resolve-cache.sqlite to the live site.
#
# Folds the four manual steps into one, in the order the two-phase watermark
# needs: export -> apply to remote D1 -> mark applied -> deploy. `set -e`
# guarantees the watermark is only marked applied if the D1 import actually
# succeeded (a failed import aborts the script before --mark-applied runs,
# so the next run safely regenerates the same delta).
#
# This does NOT fetch/resolve new data — run `uv run python -m cna.update_all`
# for that first. This script just publishes whatever is in the store now.
#
# Usage:
#   scripts/deploy.sh           incremental: delta since last applied
#   scripts/deploy.sh --full    full resync (re-import every ok row)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_NAME="cn-leaderboard"   # matches [[d1_databases]].database_name in web/wrangler.toml

# Load a long-lived Cloudflare API token from a gitignored file so wrangler
# never drops into the browser OAuth flow (its login tokens expire fast). The
# file holds just the token on one line. If it's missing/empty we fall back to
# whatever auth wrangler already has (env var or `wrangler login`).
TOKEN_FILE="$ROOT/.cloudflare-token"
if [[ -z "${CLOUDFLARE_API_TOKEN:-}" && -s "$TOKEN_FILE" ]]; then
  CLOUDFLARE_API_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  export CLOUDFLARE_API_TOKEN
fi

# --- run wrangler under Node, not bun --------------------------------------
# Wrangler is a Node CLI and we'd normally invoke it as `bunx wrangler`. Don't:
# bun 1.3.13 (the version currently pinned in nixpkgs) enables ECH GREASE in
# the TLS ClientHello of its fetch(), and api.cloudflare.com completes the TLS
# handshake but then never answers a request carrying that extension. So
# wrangler's Cloudflare API calls (deploy, d1 execute, ...) silently stall and
# the process exits 0 having done NOTHING — the live site goes stale with no
# error and a success exit code, which even `set -e` cannot catch. bun fixed
# this in 1.3.14 (ECH GREASE off by default: https://bun.com/blog/bun-v1.3.14),
# but nixpkgs has not advanced past 1.3.13. Node's fetch never sent ECH GREASE,
# so route wrangler through Node until nixpkgs ships bun >= 1.3.14 — then this
# shim can revert to plain `bunx wrangler`.
WRANGLER_JS="$ROOT/web/node_modules/wrangler/bin/wrangler.js"
if ! command -v node >/dev/null 2>&1; then
  echo "error: 'node' is not on PATH; this script runs wrangler under Node on" >&2
  echo "       purpose (see comment above). Add nodejs to your environment." >&2
  exit 1
fi
# Every wrangler call goes through here: cd into web/ (so wrangler.toml is
# found) and run the pinned wrangler under Node instead of bun.
wrangler() { ( cd "$ROOT/web" && node "$WRANGLER_JS" "$@" ); }

EXPORT_ARGS=()
[[ "${1:-}" == "--full" ]] && EXPORT_ARGS+=(--full)

echo "==> 1/4 export site artifacts from the store"
uv run python -m cna.export_site "${EXPORT_ARGS[@]}"

echo "==> 2/4 apply sync.sql to remote D1 ($DB_NAME)"
wrangler d1 execute "$DB_NAME" --remote --file d1/sync.sql

echo "==> 3/4 mark watermark applied"
uv run python -m cna.export_site --mark-applied

echo "==> 4/4 deploy worker + static assets"
wrangler deploy

echo "==> done. https://cn-leaderboard.echo-6d6.workers.dev"
