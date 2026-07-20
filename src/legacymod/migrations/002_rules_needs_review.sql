-- 002: rules.needs_review — LLM-enriched explanations must be reviewable.
ALTER TABLE rules ADD COLUMN needs_review INTEGER DEFAULT 0;
