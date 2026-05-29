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

EXPORT_ARGS=()
[[ "${1:-}" == "--full" ]] && EXPORT_ARGS+=(--full)

echo "==> 1/4 export site artifacts from the store"
uv run python -m cna.export_site "${EXPORT_ARGS[@]}"

echo "==> 2/4 apply sync.sql to remote D1 ($DB_NAME)"
( cd web && bunx wrangler d1 execute "$DB_NAME" --remote --file d1/sync.sql )

echo "==> 3/4 mark watermark applied"
uv run python -m cna.export_site --mark-applied

echo "==> 4/4 deploy worker + static assets"
( cd web && bunx wrangler deploy )

echo "==> done. https://cn-leaderboard.echo-6d6.workers.dev"
