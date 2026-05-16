# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python data pipeline that ranks X/Twitter accounts by how many publicly-shown
Community Notes their posts received, plus a Cloudflare Worker + D1 site that
serves the results. Single package (`cna/`); web frontend in `web/`.

## Commands

- Python: `uv` (Python 3.12). Run anything as `uv run python -m cna.<module>`.
- Web: `bun` / `bunx wrangler` in `web/`.
- Full refresh: `uv run python -m cna.update_all` (add `--date YYYY-MM-DD` to pin a snapshot).
  This runs the 5 stages in order: `fetch_data` → `load_filter` → `resolve_authors`
  → `build_leaderboard` → `export_site`.
- Lint/format: `uv run ruff check .` and `uv run ruff format .`. Pre-commit enforces
  both on every commit (`.pre-commit-config.yaml`).
- No test suite. For ad-hoc testing, `cna.resolve_authors` takes `--limit N`.

## Deploy

`scripts/deploy.sh` (incremental) or `scripts/deploy.sh --full` (full resync).
It does NOT fetch or resolve new data — run `uv run python -m cna.update_all`
first if you want fresh data live. Deploy writes to the live D1 site, so it
needs an authenticated `wrangler`.

## Gotchas

- `resolve-cache.sqlite` is the persistent linchpin store: it accumulates across
  runs, is never truncated, and tweet text is captured once and kept forever
  (even if the tweet is later deleted). Do not delete or reset it.
- `resolve_authors` is resumable and uses an `fcntl` `.lock` file to block
  concurrent runs. Safe to Ctrl-C; resume with the same command.
- Leaderboard counts are lower bounds: suspended/deleted accounts return
  tombstones, and the most-noted accounts are the most likely to be gone.
- `export_site` uses a two-phase watermark on `resolved_at`. If `first_noted_at_ms`
  tightens on already-synced rows, an incremental export misses it — use
  `scripts/deploy.sh --full` to correct.
- Data source precedence (first match wins): fresh zips in `zips/`, then TSVs in
  `data/fresh/`. Both are the X-generated TSVs read directly; there is no
  parquet/arrow path.

## Git

A GitHub remote will be added later; until then commit to `main`. Once a remote
exists, use normal branch/PR etiquette. Commit subjects follow `Add <feature>`
for features and `module: <change>` for fixes.
