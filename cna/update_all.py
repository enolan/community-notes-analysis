"""One command: fetch latest data -> filter -> resolve -> leaderboard.

Each stage runs as a subprocess so the resumable resolver and its
single-instance lock behave exactly as when run by hand. Safe to re-run and
safe to schedule: fetch skips unchanged shards, resolve only does uncached
tweetIds, so a daily run is cheap once the back-catalogue is resolved.

Usage:
  uv run python -m cna.update_all [--rps R] [--concurrency C] [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def step(title: str, args: list[str]) -> None:
    print(f"\n{'=' * 70}\n== {title}\n{'=' * 70}", flush=True)
    t0 = time.monotonic()
    subprocess.run([sys.executable, "-m", *args], check=True)
    print(f"-- {title}: {time.monotonic() - t0:.0f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="force snapshot date YYYY-MM-DD")
    ap.add_argument("--rps", type=float, default=12.0)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    fetch = ["cna.fetch_data"] + (["--date", args.date] if args.date else [])
    step("1/4 fetch latest data", fetch)
    step("2/4 filter to publicly-shown notes", ["cna.load_filter"])
    step(
        "3/4 resolve tweet authors (resumable)",
        ["cna.resolve_authors", "--rps", str(args.rps),
         "--concurrency", str(args.concurrency)],
    )
    step("4/4 build leaderboard", ["cna.build_leaderboard", "--top", str(args.top)])
    print("\nDone. See out/leaderboard.csv")


if __name__ == "__main__":
    main()
