"""Step 2: resolve tweetId -> author via X's free public syndication endpoint.

Endpoint: https://cdn.syndication.twimg.com/tweet-result?id=<id>&token=<t>
This is the unauthenticated backend behind embedded tweets. No API key, no
per-call cost. The only real limits are X's per-IP rate limiting and the fact
that deleted/suspended/protected tweets return a tombstone instead of a user
(the same suspension bias discussed up front: the most-noted accounts are the
most likely to be gone).

Resumable: every result (including terminal failures) is written to a SQLite
cache. Re-running skips anything already resolved and only retries transient
errors, so you can Ctrl-C and continue.

Usage:
  uv run python -m cna.resolve_authors [--limit N] [--concurrency C] [--rps R]
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import math
import os
import re
import sqlite3
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDS_PATH = os.path.join(ROOT, "out", "helpful_tweet_ids.txt")
DB_PATH = os.path.join(ROOT, "resolve-cache.sqlite")
ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"

# Terminal statuses: never retried on a re-run. "error" is transient -> retried.
TERMINAL = {"ok", "notfound", "tombstone", "protected", "suspended"}
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def make_token(tweet_id: str) -> str:
    """Reproduce the syndication widget's token derivation.

    JS: ((id / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, "")
    """
    v = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    int_part = int(v)
    frac = v - int_part
    s = ""
    if int_part == 0:
        s = "0"
    else:
        n = int_part
        while n:
            s = digits[n % 36] + s
            n //= 36
    s += "."
    for _ in range(24):
        frac *= 36
        d = int(frac)
        s += digits[d]
        frac -= d
    return re.sub(r"(0+|\.)", "", s) or "0"


def init_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS resolved (
            tweet_id    TEXT PRIMARY KEY,
            author_id   TEXT,
            screen_name TEXT,
            status      TEXT NOT NULL,
            fetched_at  REAL NOT NULL
        )"""
    )
    db.commit()
    return db


def pending_ids(db: sqlite3.Connection) -> list[str]:
    if not os.path.exists(IDS_PATH):
        raise SystemExit(f"{IDS_PATH} missing. Run `python -m cna.load_filter` first.")
    with open(IDS_PATH) as f:
        all_ids = [ln.strip() for ln in f if ln.strip()]
    done = {
        row[0]
        for row in db.execute(
            f"SELECT tweet_id FROM resolved WHERE status IN "
            f"({','.join('?' * len(TERMINAL))})",
            tuple(TERMINAL),
        )
    }
    return [i for i in all_ids if i not in done]


class RateLimiter:
    """Simple async token bucket: at most `rps` starts per second."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._next = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now) + self._interval
            sleep_for = self._next - now - self._interval
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


def classify(resp: httpx.Response) -> tuple[str, str | None, str | None]:
    """Return (status, author_id, screen_name)."""
    if resp.status_code == 404:
        return "notfound", None, None
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return "tombstone", None, None
        if not data:
            return "tombstone", None, None
        typename = data.get("__typename")
        if typename == "Tweet" and isinstance(data.get("user"), dict):
            u = data["user"]
            return "ok", u.get("id_str"), u.get("screen_name")
        # Tombstone: deleted / suspended / protected / age-restricted.
        text = (data.get("tombstone", {}) or {}).get("text", {}) or {}
        msg = (text.get("text") or "").lower()
        if "suspend" in msg:
            return "suspended", None, None
        if "protect" in msg or "private" in msg:
            return "protected", None, None
        return "tombstone", None, None
    raise httpx.HTTPStatusError("retry", request=resp.request, response=resp)


async def fetch_one(
    client: httpx.AsyncClient, limiter: RateLimiter, tweet_id: str
) -> tuple[str, str, str | None, str | None]:
    delay = 1.0
    for attempt in range(6):
        await limiter.wait()
        try:
            resp = await client.get(
                ENDPOINT,
                params={"id": tweet_id, "lang": "en", "token": make_token(tweet_id)},
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                ra = resp.headers.get("retry-after")
                wait = float(ra) if ra and ra.isdigit() else delay
                await asyncio.sleep(min(wait, 60.0))
                delay = min(delay * 2, 60.0)
                continue
            status, aid, sn = classify(resp)
            return tweet_id, status, aid, sn
        except (httpx.TransportError, httpx.HTTPStatusError):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
    return tweet_id, "error", None, None


async def run(limit: int | None, concurrency: int, rps: float) -> None:
    db = init_db()
    todo = pending_ids(db)
    if limit:
        todo = todo[:limit]
    total = len(todo)
    if not total:
        print("Nothing to resolve — cache already covers all tweetIds.")
        return
    print(f"Resolving {total:,} tweetIds (concurrency={concurrency}, rps={rps})")

    limiter = RateLimiter(rps)
    sem = asyncio.Semaphore(concurrency)
    done = 0
    t0 = time.monotonic()

    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept": "application/json"},
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
        http2=False,
    ) as client:

        async def worker(tid: str):
            async with sem:
                return await fetch_one(client, limiter, tid)

        tasks = [asyncio.create_task(worker(t)) for t in todo]
        batch: list[tuple] = []
        for fut in asyncio.as_completed(tasks):
            tid, status, aid, sn = await fut
            batch.append((tid, aid, sn, status, time.time()))
            done += 1
            if len(batch) >= 200:
                db.executemany(
                    "INSERT OR REPLACE INTO resolved VALUES (?,?,?,?,?)", batch
                )
                db.commit()
                batch.clear()
            if done % 1000 == 0 or done == total:
                rate = done / (time.monotonic() - t0)
                eta = (total - done) / rate / 60 if rate else 0
                print(
                    f"  {done:,}/{total:,}  {rate:4.1f}/s  ETA {eta:5.1f} min",
                    flush=True,
                )
        if batch:
            db.executemany(
                "INSERT OR REPLACE INTO resolved VALUES (?,?,?,?,?)", batch
            )
            db.commit()

    by_status = dict(
        db.execute("SELECT status, COUNT(*) FROM resolved GROUP BY status")
    )
    print("\nCache status breakdown:")
    for s, c in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {s:10s} {c:,}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="resolve at most N (test)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--rps", type=float, default=10.0, help="max requests/sec")
    args = ap.parse_args()

    # Single-resolver lock: the SQLite cache and the shared endpoint rate budget
    # must not be hit by two resolvers at once. Auto-released on process exit.
    lock_path = DB_PATH + ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(
            f"Another resolver is already running (lock: {lock_path}). "
            "Wait for it to finish or stop it before starting another."
        )
    asyncio.run(run(args.limit, args.concurrency, args.rps))


if __name__ == "__main__":
    main()
