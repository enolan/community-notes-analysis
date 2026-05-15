# community-notes-analysis

Rank X/Twitter accounts by how many of their posts received a **publicly-shown
Community Note** (a note that reached `CURRENTLY_RATED_HELPFUL`).

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

The old `ton.twimg.com/birdwatch-public-data/...` URLs are dead (404 for all
dates); the official data is now gated behind the logged-in page at
`x.com/i/communitynotes/download-data`. Sources, first match wins:

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

`cna.fetch_data` downloads the latest public zips directly from
`ton.twimg.com/birdwatch-public-data/...` (no login — the logged-in page just
links these files), skipping shards whose size already matches. Safe to re-run
or schedule daily: it's incremental and the resolver only touches uncached
tweetIds. A `.lock` prevents two resolvers running at once.

Or run stages individually:

```sh
uv run python -m cna.fetch_data         # -> zips/notes-*.zip + noteStatusHistory-*.zip
uv run python -m cna.load_filter        # -> out/helpful_tweet_ids.txt (distinct)
uv run python -m cna.resolve_authors    # -> resolve-cache.sqlite (resumable)
uv run python -m cna.build_leaderboard  # -> out/leaderboard.csv + top-N
```

`resolve_authors` is resumable: Ctrl-C and re-run; terminal results are cached,
only transient errors retried. Knobs: `--rps` (default 10), `--concurrency`
(default 8), `--limit N` (test).

## Status (current data, 2026-05-15)

2,692,626 notes → 245,575 `CURRENTLY_RATED_HELPFUL` →
**201,902 distinct publicly-shown-noted tweets** to resolve. Sample resolve rate
~80% (rest deleted/suspended). At 10 rps the full resolve takes ~5–6 h.
