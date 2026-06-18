-- Migration 002: Fix search_vector column on contacts
--
-- Issue: The original 001_initial_schema.sql had a syntax error in the
-- GENERATED ALWAYS AS expression — adjacent string literals need ||
-- between them in Postgres (e.g. `coalesce(name, '') || ' ' ||`).
-- Without ||, Postgres would throw a syntax error on creation.
--
-- But the column IS in the schema (your query confirmed this). Possible
-- explanations:
--   a) Postgres created the column with an empty/broken expression
--   b) Adjacent literals were concatenated differently than I expected
--   c) The IF NOT EXISTS clause masked a partial failure
--
-- Either way: this migration drops + recreates the column with the
-- correct expression. All 5 fields are included.
--
-- Run in: Supabase Dashboard → SQL Editor → New query → paste → Run

-- Drop the existing column (CASCADE to also drop the GIN index)
ALTER TABLE contacts DROP COLUMN IF EXISTS search_vector CASCADE;

-- Recreate with proper || operators
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

-- Recreate the GIN index for fast full-text search
CREATE INDEX IF NOT EXISTS idx_contacts_search ON contacts USING GIN(search_vector);

-- ============================================================
-- VERIFICATION — run this AFTER the migration to confirm it's correct:
-- ============================================================
-- SELECT generation_expression
-- FROM information_schema.columns
-- WHERE table_name = 'contacts' AND column_name = 'search_vector';
--
-- Expected: should show "to_tsvector('english'::regconfig, ...)" with
-- all 5 fields (name, handle, company, title, notes).
--
-- FUNCTIONAL TEST — insert + query a test row (replace USER_ID with
-- a real UUID from your users table):
--
-- INSERT INTO contacts (user_id, name, company, handle, source)
-- VALUES ('YOUR_USER_UUID_HERE', 'Vitalik Buterin', 'Ethereum', 'vitalik', 'test');
--
-- SELECT name, company FROM contacts
-- WHERE search_vector @@ to_tsquery('english', 'vitalik');
--
-- Expected: returns the row you just inserted.
