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
  text_truncated     1 if `text` is only the ~280-char head of a longer post
                     (syndication advertised a note_tweet but gave no body);
                     0 if it's the full post; NULL if unknown/not applicable
  note_text          the body of the *primary* Community Note on this post —
                     the earliest currently-helpful note (same note whose
                     createdAtMillis is first_noted_at_ms). From the CN data,
                     so it's full text and needs no fetch. Append-only: kept
                     even if the note later loses helpful status.
  tweet_created_at   when the post itself was created (from syndication)
  favorite_count     like count at resolve time
  display_name       the account's display name at resolve time
  avatar_url         the account's profile image URL (pbs.twimg.com) at
                     resolve time — the `_normal` 48px variant
  is_blue_verified   1 if the account had a blue check at resolve time
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
    text_truncated    INTEGER,
    note_text         TEXT,
    tweet_created_at  TEXT,
    favorite_count    INTEGER,
    display_name      TEXT,
    avatar_url        TEXT,
    is_blue_verified  INTEGER,
    resolved_at       REAL
);
CREATE INDEX IF NOT EXISTS tweets_status ON tweets(status);
CREATE INDEX IF NOT EXISTS tweets_author ON tweets(author_id);
"""

# Columns added after the original schema shipped. SQLite has no
# `ADD COLUMN IF NOT EXISTS`, so we diff against PRAGMA table_info and add
# only what's missing — cheap and idempotent on every connect().
_LATER_COLUMNS = {
    "display_name": "TEXT",
    "avatar_url": "TEXT",
    "is_blue_verified": "INTEGER",
    "note_text": "TEXT",
    "text_truncated": "INTEGER",
}

# Statuses that are final: once set we never refetch the tweet.
TERMINAL = ("ok", "tombstone", "suspended", "notfound")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    _add_missing_columns(db)
    _migrate_legacy(db)
    db.commit()
    return db


def _add_missing_columns(db: sqlite3.Connection) -> None:
    """Add columns introduced after the first schema to a pre-existing
    `tweets` table. No-op once they're present."""
    have = {r[1] for r in db.execute("PRAGMA table_info(tweets)")}
    for name, decl in _LATER_COLUMNS.items():
        if name not in have:
            db.execute(f"ALTER TABLE tweets ADD COLUMN {name} {decl}")


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
