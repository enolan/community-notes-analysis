"""Step 1: load Community Notes data, keep notes that were publicly shown.

Data source resolution (first match wins):
  1. Fresh zips in zips/  (notes-*.zip + noteStatusHistory-*.zip), exactly as
     downloaded while logged in at x.com/i/communitynotes/download-data. Only
     these two datasets are read; ratings/userEnrollment/batSignals are ignored.
  2. Fresh TSV shards in data/fresh/  (notes-*.tsv + noteStatusHistory-*.tsv)

A note "was publicly shown" iff its noteStatusHistory.currentStatus is
CURRENTLY_RATED_HELPFUL. We join notes->status on noteId and emit the set of
distinct tweetIds that have >=1 such note. That distinct-tweet set is the input
to author resolution and the denominator for the leaderboard.

Output:
  out/helpful_tweet_ids.txt  one tweetId per line, distinct, sorted
  out/filter_stats.json      counts for sanity / reporting
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from io import TextIOWrapper

from cna.db import TERMINAL, connect

HELPFUL = "CURRENTLY_RATED_HELPFUL"
# createdAtMillis = when the note was written -> earliest = when this post got noted
NOTE_COLS = ["noteId", "tweetId", "createdAtMillis"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "out")
FRESH_DIR = os.path.join(ROOT, "data", "fresh")
ZIPS_DIR = os.path.join(ROOT, "zips")


def _iter_rows(streams: Iterable, columns: list[str]) -> Iterator[tuple[str, ...]]:
    """Yield each row's `columns` (as strings) from tab-separated binary streams.

    These TSVs have no quoting and newlines/tabs are stripped from free-text
    fields, so a bare split on TAB is exact — and a plain line iterator keeps a
    multi-GB shard out of memory, only one row is held at a time.
    """
    for s in streams:
        text = TextIOWrapper(s, encoding="utf-8", newline="")
        header = text.readline().rstrip("\r\n").split("\t")
        idx = [header.index(c) for c in columns]
        for line in text:
            fields = line.rstrip("\r\n").split("\t")
            yield tuple(fields[i] for i in idx)


def _zip_streams(pattern: str, stack: ExitStack):
    """Yield a binary stream for the single TSV inside each matched zip.

    Both the ZipFile and the inner member stream are registered on `stack`,
    so they close once parsing finishes rather than leaking until GC.
    """
    for zp in sorted(glob.glob(pattern)):
        zf = stack.enter_context(zipfile.ZipFile(zp))
        name = next(n for n in zf.namelist() if n.endswith(".tsv"))
        yield stack.enter_context(zf.open(name, "r"))


def open_sources(stack: ExitStack) -> tuple[list, list, str]:
    """Return (notes_streams, nsh_streams, source_label) from the first source."""
    zip_notes = os.path.join(ZIPS_DIR, "notes-*.zip")
    zip_nsh = os.path.join(ZIPS_DIR, "noteStatusHistory-*.zip")
    if glob.glob(zip_notes) and glob.glob(zip_nsh):
        return (
            list(_zip_streams(zip_notes, stack)),
            list(_zip_streams(zip_nsh, stack)),
            f"fresh zips ({ZIPS_DIR})",
        )

    fresh_notes = os.path.join(FRESH_DIR, "notes-*.tsv")
    fresh_nsh = os.path.join(FRESH_DIR, "noteStatusHistory-*.tsv")
    if glob.glob(fresh_notes) and glob.glob(fresh_nsh):
        return (
            [
                stack.enter_context(open(p, "rb"))
                for p in sorted(glob.glob(fresh_notes))
            ],
            [stack.enter_context(open(p, "rb")) for p in sorted(glob.glob(fresh_nsh))],
            f"fresh TSV ({FRESH_DIR})",
        )

    sys.exit(
        "No data found. Provide one of: zips/ with notes-*.zip + "
        "noteStatusHistory-*.zip; or data/fresh/ with the matching .tsv shards."
    )


def main() -> None:
    with ExitStack() as stack:
        notes_streams, nsh_streams, source = open_sources(stack)

        # Pass 1: noteIds whose status is CURRENTLY_RATED_HELPFUL.
        helpful: set[str] = set()
        for note_id, status in _iter_rows(nsh_streams, ["noteId", "currentStatus"]):
            if status == HELPFUL:
                helpful.add(note_id)

        # Pass 2: earliest helpful note per tweet = when that post first got a note.
        first_noted: dict[str, int] = {}
        total_notes = 0
        for note_id, tweet_id, created in _iter_rows(notes_streams, NOTE_COLS):
            total_notes += 1
            if note_id not in helpful:
                continue
            if not tweet_id or tweet_id in ("0", "-1") or not created:
                continue
            ms = int(created)
            prev = first_noted.get(tweet_id)
            if prev is None or ms < prev:
                first_noted[tweet_id] = ms

    rows = list(first_noted.items())

    # Register into the persistent store. New tweets get status='' (unresolved,
    # the resolver will pick them up); existing rows keep their resolved data
    # and only have first_noted_at_ms tightened to the earliest seen.
    now = time.time()
    db = connect()
    db.executemany(
        """INSERT INTO tweets (tweet_id, first_noted_at_ms, first_seen_at, status)
           VALUES (?, ?, ?, '')
           ON CONFLICT(tweet_id) DO UPDATE SET
               first_noted_at_ms = MIN(
                   IFNULL(tweets.first_noted_at_ms, excluded.first_noted_at_ms),
                   excluded.first_noted_at_ms)""",
        [(tid, ms, now) for tid, ms in rows],
    )
    db.commit()

    total = db.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
    pending = db.execute(
        "SELECT COUNT(*) FROM tweets WHERE status NOT IN (?,?,?,?)", TERMINAL
    ).fetchone()[0]
    backfill = db.execute(
        "SELECT COUNT(*) FROM tweets WHERE status='ok' AND text IS NULL"
    ).fetchone()[0]

    os.makedirs(OUT_DIR, exist_ok=True)
    stats = {
        "source": source,
        "total_notes": total_notes,
        "helpful_notes": len(helpful),
        "distinct_helpful_noted_tweets_this_snapshot": len(rows),
        "tweets_tracked_total_all_runs": total,
        "tweets_pending_resolution": pending,
        "ok_tweets_missing_text_backfill": backfill,
    }
    with open(os.path.join(OUT_DIR, "filter_stats.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
