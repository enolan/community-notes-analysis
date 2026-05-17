"""Step 5: export the persistent store into web-site artifacts.

Produces everything the Cloudflare Worker site needs and nothing it doesn't:

  web/assets/leaderboard.json  ranked accounts, served statically and
                               virtual-scrolled / filtered client-side
  web/assets/meta.json         totals + disclaimer for the page banner
  web/d1/sync.sql              schema (IF NOT EXISTS) + an idempotent
                               `INSERT OR REPLACE` batch of the rows the
                               Worker serves; apply with `wrangler d1 execute`
  web/d1/sync_state.json       watermark so steady-state runs emit a small
                               delta instead of all ~130k rows

Only `status='ok'` rows go to D1: those are the only tweets with a resolved
author, i.e. the only ones an account page can show. The leaderboard is
precomputed here so the Worker never aggregates ~200k rows per request.

Watermark: rows newly resolved since the last export (by `resolved_at`) are
re-emitted. `INSERT OR REPLACE` makes re-applying harmless. Known limitation:
`first_noted_at_ms` (and `note_text`, which tracks it) can tighten on a later
run for an *already-synced* row without changing `resolved_at`; that change
won't propagate until a `--full` rebuild. Run `--full` periodically (or after
a large backfill) to resync everything.

Usage:
  uv run python -m cna.export_site [--full]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time

from cna.db import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")
D1_DIR = os.path.join(WEB_DIR, "d1")
LEADERBOARD_JSON = os.path.join(ASSETS_DIR, "leaderboard.json")
META_JSON = os.path.join(ASSETS_DIR, "meta.json")
SYNC_SQL = os.path.join(D1_DIR, "sync.sql")
SYNC_STATE = os.path.join(D1_DIR, "sync_state.json")

# Columns shipped to D1 — enough to render an account page and resolve a
# /@handle redirect. status is omitted (every shipped row is 'ok').
D1_COLS = (
    "tweet_id",
    "author_id",
    "screen_name",
    "text",
    "text_truncated",
    "note_text",
    "first_noted_at_ms",
    "tweet_created_at",
    "favorite_count",
    "display_name",
    "avatar_url",
    "resolved_at",
)

D1_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id          TEXT PRIMARY KEY,
    author_id         TEXT,
    screen_name       TEXT,
    text              TEXT,
    text_truncated    INTEGER,
    note_text         TEXT,
    first_noted_at_ms INTEGER,
    tweet_created_at  TEXT,
    favorite_count    INTEGER,
    display_name      TEXT,
    avatar_url        TEXT,
    resolved_at       REAL
);
CREATE INDEX IF NOT EXISTS tweets_author ON tweets(author_id);
CREATE INDEX IF NOT EXISTS tweets_screen ON tweets(screen_name);
"""

# Multi-row INSERT sizing. Literals are inlined, so the real constraint is
# D1's ~100 KB per-statement cap (exceeding it -> SQLITE_TOOBIG). The cap is
# on UTF-8 *bytes*, and note_text/tweet text is heavily multibyte (emoji,
# CJK), so size by encoded byte length, not str length. Row ceiling is only
# a secondary bound.
BATCH = 200
MAX_STMT_BYTES = 80_000


def _sql_lit(v: object) -> str:
    """Render a Python value as a SQLite literal. Only `''` needs escaping in
    a SQLite string literal (no backslash escapes); newlines are fine."""
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _read_state() -> dict:
    if not os.path.exists(SYNC_STATE):
        return {}
    with open(SYNC_STATE) as f:
        return json.load(f)


def _write_state(state: dict) -> None:
    state["updated_at"] = time.time()
    with open(SYNC_STATE, "w") as f:
        json.dump(state, f, indent=2)


def _mark_applied() -> None:
    """Promote the pending watermark to applied — call ONLY after the
    generated sync.sql has been successfully applied to D1."""
    os.makedirs(D1_DIR, exist_ok=True)
    state = _read_state()
    pending = state.get("pending_resolved_at")
    if pending is None:
        print("Nothing pending; sync.sql was never generated. Nothing to do.")
        return
    state["applied_resolved_at"] = pending
    _write_state(state)
    print(f"Marked applied: resolved_at watermark = {pending}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--full",
        action="store_true",
        help="ignore the watermark and re-emit every ok row",
    )
    ap.add_argument(
        "--mark-applied",
        action="store_true",
        help="promote the pending watermark to applied after a successful "
        "`wrangler d1 execute`; does not regenerate anything",
    )
    args = ap.parse_args()
    if args.mark_applied:
        _mark_applied()
        return
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(D1_DIR, exist_ok=True)

    db = connect()

    # --- leaderboard.json (always a full rebuild; it's small) ---------------
    # Rank by author_id. Handle, display name and avatar are all taken from
    # that author's most recently resolved row (the one-time backfill refetches
    # each avatar-less author's *latest* tweet, so this is the freshest known
    # profile). One pass in Python beats three correlated subqueries per group.
    counts: dict[str, int] = {}
    latest: dict[str, tuple[float, str, str, str]] = {}
    for aid, sn, dname, avatar, ra in db.execute(
        """SELECT author_id, screen_name, display_name, avatar_url, resolved_at
           FROM tweets WHERE status='ok' AND author_id IS NOT NULL"""
    ):
        counts[aid] = counts.get(aid, 0) + 1
        ra = ra or 0.0
        if aid not in latest or ra > latest[aid][0]:
            latest[aid] = (ra, sn or "", dname or "", avatar or "")
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], latest[kv[0]][1].lower()))
    # rank is implicit in position
    leaderboard = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "columns": [
            "author_id",
            "screen_name",
            "display_name",
            "avatar_url",
            "noted_tweets",
        ],
        "rows": [
            [aid, latest[aid][1], latest[aid][2], latest[aid][3], n]
            for aid, n in ranked
        ],
    }
    with open(LEADERBOARD_JSON, "w") as f:
        json.dump(leaderboard, f, separators=(",", ":"))

    # --- meta.json (banner: totals + the lower-bound disclaimer) -----------
    total, attributed, accounts = db.execute(
        """SELECT COUNT(*),
                  SUM(status='ok'),
                  COUNT(DISTINCT CASE WHEN status='ok' THEN author_id END)
           FROM tweets"""
    ).fetchone()
    meta = {
        "generated_at": leaderboard["generated_at"],
        "tweets_tracked": total,
        "tweets_attributed": attributed,
        "accounts_ranked": accounts,
        "disclaimer": (
            "Counts are a lower bound. Tweets on deleted or suspended accounts "
            "can't be attributed to an author, and the heaviest-noted accounts "
            "are the most likely to be gone — so this undercounts exactly the "
            "accounts most likely to top the list."
        ),
    }
    with open(META_JSON, "w") as f:
        json.dump(meta, f, indent=2)

    # --- D1 sync.sql (delta by resolved_at, or full with --full) -----------
    # Delta is measured from the last *applied* watermark, never advanced
    # here — only `--mark-applied` (post-wrangler) moves it. So re-running
    # export before applying just regenerates the same delta; nothing is lost.
    state = _read_state()
    watermark = None if args.full else state.get("applied_resolved_at")
    if watermark is None:
        cur = db.execute(
            f"SELECT {','.join(D1_COLS)} FROM tweets "
            "WHERE status='ok' ORDER BY resolved_at"
        )
    else:
        cur = db.execute(
            f"SELECT {','.join(D1_COLS)} FROM tweets "
            "WHERE status='ok' AND resolved_at > ? ORDER BY resolved_at",
            (watermark,),
        )

    n_rows = 0
    max_resolved = watermark or 0.0
    insert_head = f"INSERT OR REPLACE INTO tweets ({','.join(D1_COLS)}) VALUES\n"
    with open(SYNC_SQL, "w") as f:
        f.write("-- generated by cna.export_site; apply with:\n")
        f.write("--   bunx wrangler d1 execute <DB> --remote --file web/d1/sync.sql\n")
        f.write(D1_SCHEMA)
        batch: list[str] = []
        batch_bytes = 0

        def flush() -> None:
            nonlocal batch_bytes
            if batch:
                f.write(insert_head + ",\n".join(batch) + ";\n")
                batch.clear()
                batch_bytes = 0

        head_bytes = len(insert_head.encode())
        for row in cur:
            tup = "(" + ",".join(_sql_lit(v) for v in row) + ")"
            tup_bytes = len(tup.encode()) + 2  # +",\n" joiner
            # Flush *before* appending if this row would push the statement
            # over D1's cap (a single row is always emitted, even if huge).
            if batch and (
                batch_bytes + tup_bytes + head_bytes >= MAX_STMT_BYTES
                or len(batch) >= BATCH
            ):
                flush()
            batch.append(tup)
            batch_bytes += tup_bytes
            n_rows += 1
            resolved_at = row[-1]
            if resolved_at is not None and resolved_at > max_resolved:
                max_resolved = resolved_at
        flush()

    # Record the candidate watermark as *pending*; applied_resolved_at is
    # left untouched until --mark-applied confirms D1 actually has these rows.
    state["applied_resolved_at"] = watermark
    state["pending_resolved_at"] = max_resolved if n_rows else watermark
    state["mode"] = "full" if watermark is None else "delta"
    _write_state(state)

    mode = "full" if watermark is None else f"delta (resolved_at > {watermark})"
    print(f"leaderboard.json: {len(ranked):,} accounts -> {LEADERBOARD_JSON}")
    print(f"meta.json:        {attributed:,}/{total:,} attributed -> {META_JSON}")
    print(f"sync.sql:         {n_rows:,} rows, {mode} -> {SYNC_SQL}")
    if n_rows:
        print(
            "\nApply to D1, then mark the watermark applied:\n"
            "  bunx wrangler d1 execute <DB_NAME> --remote --file web/d1/sync.sql\n"
            "  uv run python -m cna.export_site --mark-applied\n"
            "(The watermark only advances on --mark-applied, so re-running "
            "export before you apply is safe — it regenerates the same delta.)"
        )
    else:
        print("\nNo new rows since last applied export; D1 already current.")


if __name__ == "__main__":
    main()
