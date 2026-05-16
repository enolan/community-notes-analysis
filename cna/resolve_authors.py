"""Step 2: resolve tweetId -> author via X's free public syndication endpoint.

Endpoint: https://cdn.syndication.twimg.com/tweet-result?id=<id>&token=<t>
This is the unauthenticated backend behind embedded tweets. No API key, no
per-call cost. The only real limits are X's per-IP rate limiting and the fact
that deleted/suspended/protected tweets return a tombstone instead of a user
(the same suspension bias discussed up front: the most-noted accounts are the
most likely to be gone).

Captures author id/handle, the tweet's text (kept forever, even if the post is
later deleted), creation time and like count, into the persistent `tweets`
table. Resumable: re-running only fetches tweets that are new, errored, or
resolved-OK-but-missing-text (one-time backfill); terminally-resolved tweets
with text are never refetched, so you can Ctrl-C and continue.

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

from cna.db import DB_PATH, TERMINAL, connect

ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
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


def pending_ids(db: sqlite3.Connection) -> list[str]:
    """Tweets needing a fetch: never resolved, a transient error, or resolved
    OK before we captured text (one-time backfill). Terminally-resolved tweets
    that already have text are skipped — we never refetch them."""
    q = db.execute(
        f"""SELECT tweet_id FROM tweets
            WHERE status NOT IN ({",".join("?" * len(TERMINAL))})
               OR (status = 'ok' AND text IS NULL)""",
        TERMINAL,
    )
    return [r[0] for r in q]


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


# (status, author_id, screen_name, text, tweet_created_at, favorite_count)
Result = tuple[str, str | None, str | None, str | None, str | None, int | None]
_MISS: Result = ("tombstone", None, None, None, None, None)


def classify(resp: httpx.Response) -> Result:
    if resp.status_code == 404:
        return ("notfound", None, None, None, None, None)
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return _MISS
        if not data:
            return _MISS
        if data.get("__typename") == "Tweet" and isinstance(data.get("user"), dict):
            u = data["user"]
            # `note_tweet` carries the full body of long (>280) posts.
            text = (
                data.get("note_tweet", {})
                .get("note_tweet_results", {})
                .get("result", {})
                .get("text")
                if isinstance(data.get("note_tweet"), dict)
                else None
            ) or data.get("text")
            return (
                "ok",
                u.get("id_str"),
                u.get("screen_name"),
                text,
                data.get("created_at"),
                data.get("favorite_count"),
            )
        # Tombstone: deleted / suspended / protected / age-restricted.
        msg = (
            ((data.get("tombstone", {}) or {}).get("text", {}) or {}).get("text") or ""
        ).lower()
        if "suspend" in msg:
            return ("suspended", None, None, None, None, None)
        if "protect" in msg or "private" in msg:
            return ("protected", None, None, None, None, None)
        return _MISS
    raise httpx.HTTPStatusError("retry", request=resp.request, response=resp)


async def fetch_one(
    client: httpx.AsyncClient, limiter: RateLimiter, tweet_id: str
) -> tuple[str, Result]:
    delay = 1.0
    for _ in range(6):
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
            return tweet_id, classify(resp)
        except (httpx.TransportError, httpx.HTTPStatusError):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
    return tweet_id, ("error", None, None, None, None, None)


async def run(limit: int | None, concurrency: int, rps: float) -> None:
    db = connect()
    todo = pending_ids(db)
    if limit:
        todo = todo[:limit]
    total = len(todo)
    if not total:
        print("Nothing to resolve — every tracked tweet is already resolved.")
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

        upd = (
            "UPDATE tweets SET status=?, author_id=?, screen_name=?, text=?, "
            "tweet_created_at=?, favorite_count=?, resolved_at=? WHERE tweet_id=?"
        )

        def flush(batch: list[tuple]) -> None:
            db.executemany(upd, batch)
            db.commit()
            batch.clear()

        tasks = [asyncio.create_task(worker(t)) for t in todo]
        batch: list[tuple] = []
        for fut in asyncio.as_completed(tasks):
            tid, (status, aid, sn, text, created, favs) = await fut
            batch.append((status, aid, sn, text, created, favs, time.time(), tid))
            done += 1
            if len(batch) >= 200:
                flush(batch)
            if done % 1000 == 0 or done == total:
                rate = done / (time.monotonic() - t0)
                eta = (total - done) / rate / 60 if rate else 0
                print(
                    f"  {done:,}/{total:,}  {rate:4.1f}/s  ETA {eta:5.1f} min",
                    flush=True,
                )
        if batch:
            flush(batch)

    by_status = dict(
        db.execute("SELECT status, COUNT(*) FROM tweets GROUP BY status")
    )
    with_text = db.execute(
        "SELECT COUNT(*) FROM tweets WHERE text IS NOT NULL"
    ).fetchone()[0]
    print("\nTweet store status breakdown:")
    for s, c in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {s or '(unresolved)':14s} {c:,}")
    print(f"  tweets with text saved: {with_text:,}")


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
