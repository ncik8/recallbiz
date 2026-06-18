-- ============================================================
-- 002 — Fix contacts.search_vector (broken in 001: missing || between string literals)
-- ============================================================
-- Run in: Supabase Dashboard → SQL Editor → New Query → paste → Run
--
-- The original migration had:
--   coalesce(name, '')  ' '  coalesce(handle, '')  -- missing ||
-- Postgres needs || for concat, so the search_vector column may not exist
-- or may exist with broken contents. This migration:
--   1. Drops the column (no data loss — it's a generated column)
--   2. Recreates it with correct || operators
--   3. Recreates the GIN index
-- ============================================================

-- Drop index first (if exists), then column
DROP INDEX IF EXISTS idx_contacts_search;
ALTER TABLE contacts DROP COLUMN IF EXISTS search_vector;

-- Recreate with correct syntax
ALTER TABLE contacts ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(name, '') || ' ' ||
            coalesce(handle, '') || ' ' ||
            coalesce(company, '') || ' ' ||
            coalesce(title, '') || ' ' ||
            coalesce(notes, '')
        )
    ) STORED;

-- Recreate the GIN index
CREATE INDEX idx_contacts_search ON contacts USING GIN(search_vector);

-- Sanity check: this query should return ALL contacts (all have a search_vector now)
-- SELECT count(*) FROM contacts;
-- Expected: matches SELECT count(*) FROM contacts (every row has a search_vector)
