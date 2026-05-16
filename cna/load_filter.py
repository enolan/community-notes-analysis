"""Step 1: load Community Notes data, keep notes that were publicly shown.

Data source resolution (first match wins):
  1. Fresh zips in zips/  (notes-*.zip + noteStatusHistory-*.zip), exactly as
     downloaded while logged in at x.com/i/communitynotes/download-data. Only
     these two datasets are read; ratings/userEnrollment/batSignals are ignored.
  2. Fresh TSV shards in data/fresh/  (notes-*.tsv + noteStatusHistory-*.tsv)
  3. HF mirror parquet at data/notes.parquet + data/nsh.parquet (Feb 2025 snapshot)

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
from contextlib import ExitStack

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from cna.db import TERMINAL, connect

HELPFUL = "CURRENTLY_RATED_HELPFUL"
# createdAtMillis = when the note was written -> earliest = when this post got noted
NOTE_COLS = ["noteId", "tweetId", "createdAtMillis"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "out")
FRESH_DIR = os.path.join(ROOT, "data", "fresh")
ZIPS_DIR = os.path.join(ROOT, "zips")

# These TSVs have no quoting and newlines are stripped from free-text fields,
# so disable the CSV reader's quote handling (note summaries contain `"`).
_PARSE = pacsv.ParseOptions(delimiter="\t", quote_char=False)


def _str_col(table: pa.Table, name: str) -> pa.ChunkedArray:
    """tweetId/noteId are 64-bit ids; normalize to string to avoid float/int skew."""
    return pc.cast(table[name], pa.string())


def _read_streams(streams: list, columns: list[str]) -> pa.Table:
    """Parse TSV from binary file-like streams, keeping only `columns`.

    pyarrow's CSV reader is block-streaming, so a multi-GB shard never lands
    in memory whole — only the two selected string columns are retained.
    """
    conv = pacsv.ConvertOptions(include_columns=columns)
    tables = [
        pacsv.read_csv(s, parse_options=_PARSE, convert_options=conv) for s in streams
    ]
    return pa.concat_tables(tables, promote_options="default")


def _zip_streams(pattern: str, stack: ExitStack):
    """Yield a binary stream for the single TSV inside each matched zip.

    Both the ZipFile and the inner member stream are registered on `stack`,
    so they close once parsing finishes rather than leaking until GC.
    """
    for zp in sorted(glob.glob(pattern)):
        zf = stack.enter_context(zipfile.ZipFile(zp))
        name = next(n for n in zf.namelist() if n.endswith(".tsv"))
        yield stack.enter_context(zf.open(name, "r"))


def load_tables() -> tuple[pa.Table, pa.Table, str]:
    """Return (notes[noteId,tweetId], status[noteId,currentStatus], source_label)."""
    zip_notes = os.path.join(ZIPS_DIR, "notes-*.zip")
    zip_nsh = os.path.join(ZIPS_DIR, "noteStatusHistory-*.zip")
    if glob.glob(zip_notes) and glob.glob(zip_nsh):
        with ExitStack() as stack:
            notes = _read_streams(list(_zip_streams(zip_notes, stack)), NOTE_COLS)
            nsh = _read_streams(
                list(_zip_streams(zip_nsh, stack)), ["noteId", "currentStatus"]
            )
        return notes, nsh, f"fresh zips ({ZIPS_DIR})"

    fresh_notes = os.path.join(FRESH_DIR, "notes-*.tsv")
    fresh_nsh = os.path.join(FRESH_DIR, "noteStatusHistory-*.tsv")
    if glob.glob(fresh_notes) and glob.glob(fresh_nsh):
        with ExitStack() as stack:
            notes = _read_streams(
                [
                    stack.enter_context(open(p, "rb"))
                    for p in sorted(glob.glob(fresh_notes))
                ],
                NOTE_COLS,
            )
            nsh = _read_streams(
                [
                    stack.enter_context(open(p, "rb"))
                    for p in sorted(glob.glob(fresh_nsh))
                ],
                ["noteId", "currentStatus"],
            )
        return notes, nsh, f"fresh TSV ({FRESH_DIR})"

    notes_pq = os.path.join(ROOT, "data", "notes.parquet")
    nsh_pq = os.path.join(ROOT, "data", "nsh.parquet")
    if os.path.exists(notes_pq) and os.path.exists(nsh_pq):
        notes = pq.read_table(notes_pq, columns=NOTE_COLS)
        nsh = pq.read_table(nsh_pq, columns=["noteId", "currentStatus"])
        return notes, nsh, "HF mirror parquet (deadbirds, ~Feb 2025)"

    sys.exit(
        "No data found. Provide one of: zips/ with notes-*.zip + "
        "noteStatusHistory-*.zip; data/fresh/ with the matching .tsv shards; "
        "or data/notes.parquet + data/nsh.parquet."
    )


def main() -> None:
    notes, nsh, source = load_tables()

    nsh = nsh.set_column(0, "noteId", _str_col(nsh, "noteId"))
    notes = notes.set_column(0, "noteId", _str_col(notes, "noteId"))
    notes = notes.set_column(
        notes.column_names.index("tweetId"), "tweetId", _str_col(notes, "tweetId")
    )

    helpful_note_ids = nsh.filter(pc.equal(nsh["currentStatus"], HELPFUL))["noteId"]
    helpful_set = pa.array(set(helpful_note_ids.to_pylist()), type=pa.string())

    helpful_notes = notes.filter(pc.is_in(notes["noteId"], value_set=helpful_set))
    # earliest helpful note per tweet = when that post first got a note
    first_noted = (
        helpful_notes.select(["tweetId", "createdAtMillis"])
        .group_by("tweetId")
        .aggregate([("createdAtMillis", "min")])
    )
    rows = [
        (tid, int(ms))
        for tid, ms in zip(
            first_noted["tweetId"].to_pylist(),
            first_noted["createdAtMillis_min"].to_pylist(),
            strict=True,
        )
        if tid and tid not in ("0", "-1") and ms is not None
    ]

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
        "total_notes": notes.num_rows,
        "helpful_notes": len(helpful_set),
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
