"""Persistent store of every tweet that has ever received a publicly-shown
Community Note.

One SQLite file, one `tweets` table, keyed by tweet_id. It accumulates across
runs and is never truncated: `load_filter` registers newly-noted tweets, the
resolver fills in author + text the first (and only) time a tweet is seen.
Tweets already terminally resolved are never re-fetched.

Columns:
  tweet_id           the post that got noted (primary key)
  first_noted_at_ms  earliest helpful-note creation time (from CN data) — i.e.
                     when this post first got a note
  first_seen_at      unix time we first registered it (run timestamp)
  status             ''(unresolved) | ok | tombstone | suspended | notfound | error
  author_id          stable numeric id of the posting account
  screen_name        the account handle at resolve time
  text               the tweet's text, captured at resolve time and kept
                     forever even if the post is later deleted
  tweet_created_at   when the post itself was created (from syndication)
  favorite_count     like count at resolve time
  resolved_at        unix time of the successful/terminal resolve
"""

from __future__ import annotations

import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "resolve-cache.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id          TEXT PRIMARY KEY,
    first_noted_at_ms INTEGER,
    first_seen_at     REAL NOT NULL,
    status            TEXT NOT NULL DEFAULT '',
    author_id         TEXT,
    screen_name       TEXT,
    text              TEXT,
    tweet_created_at  TEXT,
    favorite_count    INTEGER,
    resolved_at       REAL
);
CREATE INDEX IF NOT EXISTS tweets_status ON tweets(status);
CREATE INDEX IF NOT EXISTS tweets_author ON tweets(author_id);
"""

# Statuses that are final: once set we never refetch the tweet.
TERMINAL = ("ok", "tombstone", "suspended", "notfound")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    _migrate_legacy(db)
    db.commit()
    return db


def _migrate_legacy(db: sqlite3.Connection) -> None:
    """Fold a pre-existing `resolved` table (tweet_id, author_id, screen_name,
    status, fetched_at) into `tweets`, preserving already-resolved statuses.
    Text was not captured by the old schema, so it stays NULL and those OK
    rows get re-fetched once to backfill text."""
    has_legacy = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='resolved'"
    ).fetchone()
    if not has_legacy:
        return
    if db.execute("SELECT 1 FROM tweets LIMIT 1").fetchone():
        return  # already migrated
    db.execute(
        """INSERT OR IGNORE INTO tweets
               (tweet_id, first_noted_at_ms, first_seen_at, status,
                author_id, screen_name, resolved_at)
           SELECT tweet_id, NULL, fetched_at, status,
                  author_id, screen_name, fetched_at
           FROM resolved"""
    )
    db.execute("DROP TABLE resolved")
