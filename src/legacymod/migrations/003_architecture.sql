-- 003: future-state architecture recommendations (recommend stage).
CREATE TABLE IF NOT EXISTS architecture (
    rec_id TEXT PRIMARY KEY,
    unit_id TEXT REFERENCES units(unit_id),
    concern TEXT CHECK (concern IN ('execution_style', 'compute', 'data',
        'integration', 'ui')),
    recommendation TEXT,
    generate_target TEXT DEFAULT '',
    alternatives TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0,
    evidence_json TEXT,
    status TEXT CHECK (status IN ('proposed', 'approved', 'rejected'))
        DEFAULT 'proposed',
    needs_review INTEGER DEFAULT 1
);
