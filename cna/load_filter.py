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
import zipfile

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

HELPFUL = "CURRENTLY_RATED_HELPFUL"
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
        pacsv.read_csv(s, parse_options=_PARSE, convert_options=conv)
        for s in streams
    ]
    return pa.concat_tables(tables, promote_options="default")


def _zip_streams(pattern: str):
    """Yield (open binary stream) for the single TSV inside each matched zip."""
    for zp in sorted(glob.glob(pattern)):
        zf = zipfile.ZipFile(zp)
        name = next(n for n in zf.namelist() if n.endswith(".tsv"))
        yield zf.open(name, "r")


def load_tables() -> tuple[pa.Table, pa.Table, str]:
    """Return (notes[noteId,tweetId], status[noteId,currentStatus], source_label)."""
    zip_notes = os.path.join(ZIPS_DIR, "notes-*.zip")
    zip_nsh = os.path.join(ZIPS_DIR, "noteStatusHistory-*.zip")
    if glob.glob(zip_notes) and glob.glob(zip_nsh):
        notes = _read_streams(list(_zip_streams(zip_notes)), ["noteId", "tweetId"])
        nsh = _read_streams(
            list(_zip_streams(zip_nsh)), ["noteId", "currentStatus"]
        )
        return notes, nsh, f"fresh zips ({ZIPS_DIR})"

    fresh_notes = os.path.join(FRESH_DIR, "notes-*.tsv")
    fresh_nsh = os.path.join(FRESH_DIR, "noteStatusHistory-*.tsv")
    if glob.glob(fresh_notes) and glob.glob(fresh_nsh):
        notes = _read_streams(
            [open(p, "rb") for p in sorted(glob.glob(fresh_notes))],
            ["noteId", "tweetId"],
        )
        nsh = _read_streams(
            [open(p, "rb") for p in sorted(glob.glob(fresh_nsh))],
            ["noteId", "currentStatus"],
        )
        return notes, nsh, f"fresh TSV ({FRESH_DIR})"

    notes_pq = os.path.join(ROOT, "data", "notes.parquet")
    nsh_pq = os.path.join(ROOT, "data", "nsh.parquet")
    if os.path.exists(notes_pq) and os.path.exists(nsh_pq):
        notes = pq.read_table(notes_pq, columns=["noteId", "tweetId"])
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
    tweet_ids = helpful_notes["tweetId"].to_pylist()
    distinct = sorted(t for t in set(tweet_ids) if t and t not in ("0", "-1"))

    os.makedirs(OUT_DIR, exist_ok=True)
    ids_path = os.path.join(OUT_DIR, "helpful_tweet_ids.txt")
    with open(ids_path, "w") as f:
        f.write("\n".join(distinct) + "\n")

    stats = {
        "source": source,
        "total_notes": notes.num_rows,
        "total_status_rows": nsh.num_rows,
        "helpful_notes": len(helpful_set),
        "helpful_notes_joined_to_a_tweet": len(tweet_ids),
        "distinct_helpful_noted_tweets": len(distinct),
    }
    with open(os.path.join(OUT_DIR, "filter_stats.json"), "w") as f:
        json.dump(stats, f, indent=2)

    print(json.dumps(stats, indent=2))
    print(f"\nWrote {len(distinct):,} distinct tweetIds -> {ids_path}")


if __name__ == "__main__":
    main()
