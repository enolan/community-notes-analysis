---
name: refresh-and-deploy
description: Refresh the Community Notes data pipeline and publish it to the live site. Runs cna.update_all then scripts/deploy.sh. User-invoked only because it writes to the live D1 site.
disable-model-invocation: true
---

Refresh the data and publish it to the live site, in two steps.

Arguments: `$ARGUMENTS`
- If it contains `--date YYYY-MM-DD`, pass that date to `update_all` to pin the snapshot.
- If it contains `--full`, pass `--full` to `deploy.sh` for a full resync instead of an incremental delta.
- Empty arguments = latest snapshot, incremental deploy.

Steps:

1. Run the pipeline: `uv run python -m cna.update_all` (append `--date YYYY-MM-DD`
   if the user supplied one). This fetches, filters, resolves authors, builds the
   leaderboard, and exports site artifacts. The first ever run backfills ~131k
   tweets and takes ~3 hours; incremental runs are minutes. It is resumable —
   if it is interrupted, rerun the same command.

2. If step 1 succeeds, publish: `scripts/deploy.sh` (or `scripts/deploy.sh --full`
   if requested). This applies the D1 delta and deploys the Worker. It needs an
   authenticated `wrangler`; if it fails on auth, tell the user to run
   `bunx wrangler login` themselves and stop.

Do not run step 2 if step 1 failed. Report the live URL
(https://cn-leaderboard.echo-6d6.workers.dev) on success.
