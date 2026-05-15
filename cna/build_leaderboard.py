"""Step 3: rank accounts by number of distinct publicly-shown-noted tweets.

Joins the distinct helpful-noted tweet set (out/helpful_tweet_ids.txt) against
the resolver cache. Aggregation key is author_id (stable across handle changes);
the displayed handle is the one X returned at resolve time.

Output:
  out/leaderboard.csv   rank, author_id, screen_name, noted_tweets
and a printed top-N plus a coverage/loss report.

Read the coverage report before trusting the ranking: deleted/suspended tweets
cannot be attributed, and the heaviest-noted accounts are the most likely to be
suspended, so this undercounts exactly the accounts most likely to top the list.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDS_PATH = os.path.join(ROOT, "out", "helpful_tweet_ids.txt")
DB_PATH = os.path.join(ROOT, "resolve-cache.sqlite")
OUT_CSV = os.path.join(ROOT, "out", "leaderboard.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30, help="rows to print")
    args = ap.parse_args()

    with open(IDS_PATH) as f:
        universe = {ln.strip() for ln in f if ln.strip()}

    db = sqlite3.connect(DB_PATH)
    rows = db.execute(
        "SELECT tweet_id, author_id, screen_name, status FROM resolved"
    ).fetchall()

    status_counts: Counter[str] = Counter()
    tweets_by_author: defaultdict[str, int] = defaultdict(int)
    handle: dict[str, str] = {}
    resolved_in_universe = 0

    for tweet_id, author_id, screen_name, status in rows:
        if tweet_id not in universe:
            continue  # cache may hold ids from a wider/older run
        status_counts[status] += 1
        if status == "ok" and author_id:
            resolved_in_universe += 1
            tweets_by_author[author_id] += 1
            handle[author_id] = screen_name or handle.get(author_id, "")

    leaderboard = sorted(
        tweets_by_author.items(), key=lambda kv: (-kv[1], handle.get(kv[0], ""))
    )

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "author_id", "screen_name", "noted_tweets"])
        for rank, (aid, n) in enumerate(leaderboard, 1):
            w.writerow([rank, aid, handle.get(aid, ""), n])

    total = len(universe)
    cached = sum(status_counts.values())
    unresolved = cached - resolved_in_universe

    print(f"Distinct publicly-shown-noted tweets (universe): {total:,}")
    print(f"  in resolver cache:                             {cached:,}")
    print(f"  attributed to an author (status=ok):           {resolved_in_universe:,}")
    print(f"  unattributable (deleted/suspended/etc.):       {unresolved:,}")
    if total:
        print(f"  attribution rate:                              "
              f"{resolved_in_universe / total:.1%}")
    if cached < total:
        print(f"  NOT YET RESOLVED:                              "
              f"{total - cached:,}  (run cna.resolve_authors)")
    print("  cache status breakdown: " + ", ".join(
        f"{s}={c:,}" for s, c in status_counts.most_common()))
    print(f"\nUnique accounts ranked: {len(leaderboard):,}  ->  {OUT_CSV}")
    print(f"\nTop {min(args.top, len(leaderboard))} accounts by publicly-shown "
          f"Community Notes:")
    print(f"{'#':>4}  {'handle':<24} {'noted tweets':>12}")
    for rank, (aid, n) in enumerate(leaderboard[: args.top], 1):
        print(f"{rank:>4}  @{handle.get(aid, ''):<23} {n:>12,}")

    if unresolved:
        print(
            "\nCaveat: unattributable tweets are concentrated among deleted/"
            "suspended accounts, which skews toward the heaviest-noted accounts. "
            "Treat this as a lower bound per account."
        )


if __name__ == "__main__":
    main()
