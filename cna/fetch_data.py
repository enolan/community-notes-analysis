"""Step 0: fetch the latest Community Notes data, no login required.

The logged-in download page just links plain public files:
  https://ton.twimg.com/birdwatch-public-data/YYYY/MM/DD/<dataset>/<dataset>-NNNNN.zip
Data is published daily; `notes` is sharded (00000, 00001, ...),
`noteStatusHistory` is a single shard. We only need those two datasets for the
leaderboard (ratings/userEnrollment/batSignals are skipped — they're huge and
unused).

Finds the most recent date whose snapshot is *complete* (every dataset
published, not just `notes` — the provider publishes them at different times),
then downloads every shard for each dataset into zips/, overwriting the same
filenames so the rest of the pipeline (which globs zips/notes-*.zip) always
sees one date. Skips a shard whose local size already matches the server's,
and prunes shards left from an older snapshot that had more of them.

Usage:
  uv run python -m cna.fetch_data [--date YYYY-MM-DD] [--max-lookback 10]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import zipfile

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIPS_DIR = os.path.join(ROOT, "zips")
BASE = "https://ton.twimg.com/birdwatch-public-data"
DATASETS = ("notes", "noteStatusHistory")
UA = "community-notes-analysis/0.1 (+https://github.com/)"


def _url(date: dt.date, dataset: str, shard: int) -> str:
    return f"{BASE}/{date:%Y/%m/%d}/{dataset}/{dataset}-{shard:05d}.zip"


def find_latest_date(
    client: httpx.Client, start: dt.date, max_lookback: int
) -> dt.date:
    """Most recent date whose snapshot is *complete* — shard 0 of every
    dataset we need exists. The provider publishes `notes` before
    `noteStatusHistory`, so checking only `notes` would pick a half-published
    day and fail mid-fetch; requiring all DATASETS makes selection atomic."""
    for back in range(max_lookback + 1):
        d = start - dt.timedelta(days=back)
        if all(client.head(_url(d, ds, 0)).status_code == 200 for ds in DATASETS):
            return d
    sys.exit(
        f"No complete Community Notes snapshot in the {max_lookback} days "
        f"before {start:%Y-%m-%d}. The publish URL scheme may have changed."
    )


def download(client: httpx.Client, url: str, dest: str) -> int:
    """Stream `url` to `dest` atomically. Returns bytes written."""
    tmp = dest + ".part"
    n = 0
    with client.stream("GET", url) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                n += len(chunk)
    if not zipfile.is_zipfile(tmp):
        os.remove(tmp)
        raise RuntimeError(f"{url} did not yield a valid zip")
    os.replace(tmp, dest)
    return n


def fetch(date: dt.date | None, max_lookback: int) -> dt.date:
    os.makedirs(ZIPS_DIR, exist_ok=True)
    with httpx.Client(
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(60.0, read=300.0),
        follow_redirects=True,
    ) as client:
        if date is None:
            date = find_latest_date(client, dt.date.today(), max_lookback)
        print(f"Using data snapshot {date:%Y-%m-%d}")

        for dataset in DATASETS:
            shard = 0
            while True:
                url = _url(date, dataset, shard)
                head = client.head(url)
                if head.status_code == 404:
                    if shard == 0:
                        sys.exit(f"Expected at least one shard for {dataset}: {url}")
                    break
                head.raise_for_status()
                remote = int(head.headers.get("content-length", -1))
                dest = os.path.join(ZIPS_DIR, f"{dataset}-{shard:05d}.zip")
                if os.path.exists(dest) and os.path.getsize(dest) == remote:
                    print(
                        f"  skip  {os.path.basename(dest)} (up to date, "
                        f"{remote / 1e6:.1f} MB)"
                    )
                else:
                    got = download(client, url, dest)
                    print(f"  got   {os.path.basename(dest)} ({got / 1e6:.1f} MB)")
                shard += 1

            # Drop shards left over from a previous snapshot that had more
            # of them — otherwise load_filter's notes-*.zip glob would mix
            # two dates' data.
            stale = shard
            while True:
                p = os.path.join(ZIPS_DIR, f"{dataset}-{stale:05d}.zip")
                if not os.path.exists(p):
                    break
                os.remove(p)
                print(f"  prune {os.path.basename(p)} (stale from older snapshot)")
                stale += 1
    return date


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="force a specific snapshot, YYYY-MM-DD")
    ap.add_argument("--max-lookback", type=int, default=10)
    args = ap.parse_args()
    forced = dt.date.fromisoformat(args.date) if args.date else None
    date = fetch(forced, args.max_lookback)
    with open(os.path.join(ZIPS_DIR, "SNAPSHOT_DATE"), "w") as f:
        f.write(f"{date:%Y-%m-%d}\n")
    print(f"\nData ready in {ZIPS_DIR} (snapshot {date:%Y-%m-%d}).")


if __name__ == "__main__":
    main()
