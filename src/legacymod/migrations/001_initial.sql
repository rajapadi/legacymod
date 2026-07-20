-- 001: full initial schema (see docs/architecture.md §data-schemas).
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    artifact_type TEXT NOT NULL,
    language TEXT,
    encoding TEXT,
    loc INTEGER,
    sha256 TEXT,
    parse_errors INTEGER DEFAULT 0,
    adapter TEXT,
    tier TEXT,
    confidence REAL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    artifact_id INTEGER REFERENCES artifacts(id),
    fact_type TEXT NOT NULL,
    name TEXT,
    detail_json TEXT,
    source_line_start INTEGER,
    source_line_end INTEGER,
    origin TEXT CHECK (origin IN ('parser', 'llm')) DEFAULT 'parser',
    confidence REAL DEFAULT 1.0,
    needs_review INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_facts_type ON facts (fact_type);
CREATE INDEX IF NOT EXISTS ix_facts_artifact ON facts (artifact_id);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    node_type TEXT NOT NULL,
    name TEXT NOT NULL,
    artifact_id INTEGER,
    UNIQUE (node_type, name)
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    src_node INTEGER NOT NULL REFERENCES nodes(id),
    dst_node INTEGER NOT NULL REFERENCES nodes(id),
    edge_type TEXT NOT NULL,
    detail_json TEXT,
    origin TEXT DEFAULT 'parser'
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges (src_node);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges (dst_node);

CREATE TABLE IF NOT EXISTS interfaces (
    interface_id TEXT PRIMARY KEY,
    direction TEXT CHECK (direction IN ('inbound', 'outbound', 'internal')),
    protocol TEXT CHECK (protocol IN
        ('ndm', 'ftp', 'ftps', 'sftp', 'xcom', 'mq', 'dataset', 'other')),
    source_job_or_program TEXT,
    dataset_or_queue TEXT,
    target_node TEXT,
    frequency TEXT,
    frequency_source TEXT,
    external INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_runs (
    job_name TEXT,
    run_date TEXT,
    start_time TEXT,
    end_time TEXT,
    cond_code TEXT
);

CREATE TABLE IF NOT EXISTS dataset_stats (
    dataset TEXT,
    avg_bytes_per_run INTEGER,
    records_per_run INTEGER,
    as_of TEXT
);

CREATE TABLE IF NOT EXISTS reconcile (
    name TEXT,
    kind TEXT,
    status TEXT CHECK (status IN ('healthy', 'decommission_candidate',
        'on_request', 'broken_reference', 'transitively_dead')),
    evidence_json TEXT,
    needs_review INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS capabilities (
    capability TEXT,
    seed_type TEXT,
    seed_name TEXT
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    program TEXT,
    source_lines TEXT,
    category TEXT,
    snippet TEXT,
    plain_english TEXT DEFAULT '',
    origin TEXT DEFAULT 'parser',
    confidence REAL DEFAULT 1.0,
    status TEXT CHECK (status IN ('candidate', 'explained', 'approved',
        'rejected')) DEFAULT 'candidate'
);

CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT PRIMARY KEY,
    name TEXT,
    domain TEXT,
    programs_json TEXT,
    status TEXT CHECK (status IN ('proposed', 'approved', 'spec_done',
        'generated', 'validated')) DEFAULT 'proposed',
    disposition TEXT CHECK (disposition IN ('refactor', 'replatform',
        'rehost', 'retire', 'retain', 'undecided')) DEFAULT 'undecided',
    disposition_evidence_json TEXT,
    effort_tshirt TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    artifact_id INTEGER,
    program TEXT,
    cyclomatic INTEGER,
    loc INTEGER,
    goto_count INTEGER,
    nesting_max INTEGER
);

CREATE TABLE IF NOT EXISTS clones (
    clone_id TEXT PRIMARY KEY,
    file_a TEXT,
    lines_a TEXT,
    file_b TEXT,
    lines_b TEXT,
    token_count INTEGER
);

CREATE TABLE IF NOT EXISTS blockers (
    id INTEGER PRIMARY KEY,
    program_or_job TEXT,
    blocker_type TEXT,
    evidence_line INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS missing_artifacts (
    name TEXT,
    kind TEXT,
    referenced_by TEXT,
    reference_line INTEGER
);

CREATE TABLE IF NOT EXISTS llm_log (
    timestamp TEXT,
    provider TEXT,
    model TEXT,
    purpose TEXT,
    artifact TEXT,
    prompt_sha256 TEXT,
    response_sha256 TEXT,
    accepted_by_human INTEGER DEFAULT 0,
    cache_hit INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_sha256 TEXT PRIMARY KEY,
    provider TEXT,
    model TEXT,
    response TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS validation_results (
    unit TEXT,
    case_name TEXT,
    passed INTEGER,
    mismatched_fields TEXT,
    legacy_elapsed_ms REAL,
    modern_elapsed_ms REAL
);
