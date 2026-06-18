-- Migration 002: Add website column + rebuild search_vector with proper concatenation
--
-- Why this exists:
-- 001_initial_schema.sql had a typo: `coalesce(name, '') ' '` (missing ||).
-- Postgres silently ignored the broken tsvector column definition or created
-- a partial one. The UI's "generation_expression" field shows it was fixed
-- to use || at some point, but Postgres does NOT auto-recompute STORED
-- generated columns when the definition changes — only on INSERT/UPDATE.
--
-- This migration:
-- 1. Drops + recreates search_vector with correct || operators (triggers rebuild)
-- 2. Adds website column to contacts
-- 3. Re-creates the GIN index

-- Step 1: Add website column
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS website TEXT;

-- Step 2: Drop + recreate search_vector (Postgres recomputes on ADD)
ALTER TABLE contacts DROP COLUMN IF EXISTS search_vector;

ALTER TABLE contacts ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(name, '') || ' ' ||
            coalesce(handle, '') || ' ' ||
            coalesce(company, '') || ' ' ||
            coalesce(title, '') || ' ' ||
            coalesce(email, '') || ' ' ||
            coalesce(phone, '') || ' ' ||
            coalesce(notes, '') || ' ' ||
            coalesce(website, '')
        )
    ) STORED;

-- Step 3: Re-create the GIN index
CREATE INDEX IF NOT EXISTS idx_contacts_search ON contacts USING GIN(search_vector);

-- Verification (run this after migration):
-- SELECT name, company, search_vector IS NOT NULL AS has_vector
-- FROM contacts ORDER BY saved_at DESC LIMIT 5;
