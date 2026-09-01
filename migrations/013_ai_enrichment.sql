-- ============================================================
-- 013: AI enrichment fields for contacts
-- ============================================================
-- Adds ai_description (the AI-generated summary), ai_description_sources
-- (the URLs that backed the summary so users can spot-check), and
-- ai_description_updated_at (so we can refresh stale descriptions).

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS ai_description TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS ai_description_sources JSONB;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS ai_description_updated_at TIMESTAMPTZ;

-- Partial index for finding contacts with stale AI descriptions
-- (e.g., refresh everything older than 30 days)
CREATE INDEX IF NOT EXISTS idx_contacts_ai_updated
    ON contacts(ai_description_updated_at)
    WHERE ai_description IS NOT NULL;

COMMENT ON COLUMN contacts.ai_description IS 'Auto-generated 3-sentence summary of the company, written by MiniMax M3 from search results';
COMMENT ON COLUMN contacts.ai_description_sources IS 'List of URLs that backed the AI summary, for transparency and spot-checking';
COMMENT ON COLUMN contacts.ai_description_updated_at IS 'When the AI description was last generated';