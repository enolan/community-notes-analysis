-- One-off migration for the live D1 `tweets` table: add the columns introduced
-- alongside avatars + note display. CREATE TABLE IF NOT EXISTS in sync.sql
-- can't add columns to an existing table, and SQLite has no
-- ADD COLUMN IF NOT EXISTS, so run this exactly once, before the first
-- `scripts/deploy.sh --full` that ships these features:
--
--   cd web && bunx wrangler d1 execute cn-leaderboard --remote --file d1/migrate.sql
--
-- Re-running errors with "duplicate column name" — that's expected and means
-- the migration is already applied; it's safe to ignore.
ALTER TABLE tweets ADD COLUMN display_name TEXT;
ALTER TABLE tweets ADD COLUMN avatar_url TEXT;
ALTER TABLE tweets ADD COLUMN note_text TEXT;
ALTER TABLE tweets ADD COLUMN text_truncated INTEGER;
-- Added alongside attached-media + quoted-tweet display. Run the two lines
-- below once (same caveat: a re-run errors with "duplicate column name",
-- which just means it's already applied — safe to ignore). Each is a small
-- JSON string derived from the raw payload by cna.export_site; NULL when the
-- post had no media / wasn't a quote-tweet.
ALTER TABLE tweets ADD COLUMN media_json TEXT;
ALTER TABLE tweets ADD COLUMN quoted_json TEXT;
