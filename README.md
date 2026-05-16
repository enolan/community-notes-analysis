# community-notes-analysis

A persistent watchlist of X/Twitter accounts whose posts get **publicly-shown
Community Notes** (notes that reached `CURRENTLY_RATED_HELPFUL`). Tracks every
noted tweet across runs and stores each tweet's **text** (kept forever, even if
the post is later deleted), so you can see who repeatedly posts noted claims and
what they said.

## Why it's built this way

- **The public data has no author.** Community Notes' open data (`notes`,
  `noteStatusHistory`, `ratings`, …) keys everything on `tweetId` — there is no
  column for the noted account. A leaderboard of *accounts* therefore requires
  resolving each `tweetId` → author separately.
- **The official X API is too expensive.** Author resolution via `GET /2/tweets`
  costs **$0.005 / post read** → roughly **$1,000+** for this job. Rejected.
- **We use X's free syndication endpoint instead** (`cdn.syndication.twimg.com`,
  the unauthenticated backend behind embedded tweets): ~$0, the only limits
  being per-IP rate limiting and the fact that deleted/suspended/protected
  tweets return a tombstone instead of an author.
- **Suspension bias is inherent.** Unattributable tweets cluster on
  deleted/suspended accounts — exactly the accounts most likely to top the
  list. Every count is a **lower bound**; read the coverage report.

## Data source

The logged-in download page at `x.com/i/communitynotes/download-data` just
links plain public files: `ton.twimg.com/birdwatch-public-data/YYYY/MM/DD/
<dataset>/<dataset>-NNNNN.zip` (`.zip`, published daily, no auth). `fetch_data`
pulls these directly. Sources, first match wins:

1. **Fresh zips** — `zips/notes-*.zip` + `zips/noteStatusHistory-*.zip` exactly
   as downloaded from the logged-in page. Only those two datasets are read;
   `ratings`/`userEnrollment`/`batSignals` are ignored.
2. **Fresh TSV** — the same shards unzipped into `data/fresh/`.
3. **Mirror** — `deadbirds/x-community-notes-parquet-20250222` (HF), a ~Feb 2025
   snapshot, at `data/notes.parquet` + `data/nsh.parquet` (dev fallback).

## Run

One command does everything (fetch latest → filter → resolve → leaderboard):

```sh
uv run python -m cna.update_all          # add --date YYYY-MM-DD to pin a snapshot
```

Safe to re-run or schedule: `fetch_data` skips shards whose size is unchanged,
and **only newly-noted tweets are ever fetched** — every tweet's author + text
is stored once in `resolve-cache.sqlite` (table `tweets`) and never refetched.
Steady-state runs only resolve tweets noted since the last run.

Or run stages individually:

```sh
uv run python -m cna.fetch_data         # -> zips/  (latest snapshot)
uv run python -m cna.load_filter        # register newly-noted tweets into the store
uv run python -m cna.resolve_authors    # fetch author + text for new tweets (resumable)
uv run python -m cna.build_leaderboard  # -> out/leaderboard.csv + out/noted_tweets.csv
```

`resolve_authors` is resumable and `.lock`-guarded (no two at once). Knobs:
`--rps` (default 10), `--concurrency` (default 8), `--limit N` (test).

## Deploy the site

`update_all` step 5 (`cna.export_site`) writes `web/assets/{leaderboard,meta}.json`
and a delta `web/d1/sync.sql`. To publish the current store to the live
Cloudflare Worker + D1 site:

```sh
scripts/deploy.sh           # export → apply delta to remote D1 → mark applied → deploy
scripts/deploy.sh --full    # full resync (after a big backfill; run occasionally)
```

It only publishes what's already in `resolve-cache.sqlite` — run
`cna.update_all` first to pull/resolve new data. Needs an authenticated
`bunx wrangler`. Live: <https://cn-leaderboard.echo-6d6.workers.dev>.

## Persistent store & outputs

`resolve-cache.sqlite` → table `tweets`, one row per tweet ever noted, keyed by
`tweet_id`, accumulating across all runs. Key columns: `first_noted_at_ms` (when
the post first got a note), `author_id`/`screen_name`, `text`, `tweet_created_at`,
`favorite_count`, `status`. Outputs:

- `out/leaderboard.csv` — accounts ranked by # of publicly-shown-noted posts.
- `out/noted_tweets.csv` — every tracked tweet with its text, author, when it
  got noted, likes, and status.

## Status (current data, 2026-05-15)

2,692,626 notes → 245,575 `CURRENTLY_RATED_HELPFUL` → **201,902 distinct
publicly-shown-noted tweets** tracked; ~65% attributable to a live account
(rest deleted/suspended before we could capture them). One-time text backfill
of the ~131k live tweets takes ~3 h at 12 rps; incremental runs after that are
minutes.
