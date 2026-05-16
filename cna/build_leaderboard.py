"""Step 3: rank accounts, and export every noted tweet's text.

Reads the persistent `tweets` store (the cross-run record of every post that
ever got a publicly-shown Community Note). Ranking key is author_id (stable
across handle changes); the displayed handle is the one X returned at resolve
time.

Outputs:
  out/leaderboard.csv    rank, author_id, screen_name, noted_tweets
  out/noted_tweets.csv   tweet_id, screen_name, author_id, first_noted_at,
                         tweet_created_at, favorite_count, status, text
                         (every tracked tweet; text present for resolved ones)

Read the coverage report before trusting the ranking: deleted/suspended tweets
can't be attributed, and the heaviest-noted accounts are the most likely to be
gone, so this undercounts exactly the accounts most likely to top the list.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
from collections import Counter, defaultdict

from cna.db import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "out")
LEADERBOARD_CSV = os.path.join(OUT_DIR, "leaderboard.csv")
TWEETS_CSV = os.path.join(OUT_DIR, "noted_tweets.csv")


def _iso(ms: int | None) -> str:
    if not ms:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30, help="rows to print")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    db = connect()
    rows = db.execute(
        """SELECT tweet_id, author_id, screen_name, status, first_noted_at_ms,
                  tweet_created_at, favorite_count, text
           FROM tweets"""
    ).fetchall()

    status_counts: Counter[str] = Counter()
    tweets_by_author: defaultdict[str, int] = defaultdict(int)
    handle: dict[str, str] = {}

    with open(TWEETS_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "tweet_id",
                "screen_name",
                "author_id",
                "first_noted_at",
                "tweet_created_at",
                "favorite_count",
                "status",
                "text",
            ]
        )
        for tid, aid, sn, status, noted_ms, created, favs, text in rows:
            status_counts[status or "(unresolved)"] += 1
            if status == "ok" and aid:
                tweets_by_author[aid] += 1
                handle[aid] = sn or handle.get(aid, "")
            w.writerow(
                [
                    tid,
                    sn or "",
                    aid or "",
                    _iso(noted_ms),
                    created or "",
                    favs if favs is not None else "",
                    status or "",
                    text or "",
                ]
            )

    leaderboard = sorted(
        tweets_by_author.items(), key=lambda kv: (-kv[1], handle.get(kv[0], ""))
    )
    with open(LEADERBOARD_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "author_id", "screen_name", "noted_tweets"])
        for rank, (aid, n) in enumerate(leaderboard, 1):
            w.writerow([rank, aid, handle.get(aid, ""), n])

    total = len(rows)
    attributed = sum(tweets_by_author.values())
    text_saved = sum(1 for r in rows if r[7])
    pending = status_counts.get("(unresolved)", 0) + status_counts.get("error", 0)

    print(f"Tweets tracked (all runs):       {total:,}")
    print(
        f"  attributed to an author (ok):  {attributed:,}  ({attributed / total:.1%})"
        if total
        else ""
    )
    print(f"  text captured & stored:        {text_saved:,}")
    print(f"  unattributable (deleted/susp): {total - attributed - pending:,}")
    if pending:
        print(
            f"  NOT YET RESOLVED:              {pending:,}  (run cna.resolve_authors)"
        )
    print(
        "  status breakdown: "
        + ", ".join(f"{s}={c:,}" for s, c in status_counts.most_common())
    )
    print(f"\nUnique accounts ranked: {len(leaderboard):,}")
    print(f"  -> {LEADERBOARD_CSV}")
    print(f"  -> {TWEETS_CSV}  (tweet text for every tracked tweet)")

    print(
        f"\nTop {min(args.top, len(leaderboard))} accounts by publicly-shown "
        f"Community Notes:"
    )
    print(f"{'#':>4}  {'handle':<24} {'noted tweets':>12}")
    for rank, (aid, n) in enumerate(leaderboard[: args.top], 1):
        print(f"{rank:>4}  @{handle.get(aid, ''):<23} {n:>12,}")

    if total - attributed - pending:
        print(
            "\nCaveat: unattributable tweets are concentrated among deleted/"
            "suspended accounts, skewing toward the heaviest-noted accounts. "
            "Treat per-account counts as a lower bound."
        )


if __name__ == "__main__":
    main()
