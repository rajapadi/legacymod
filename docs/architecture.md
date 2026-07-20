# legacymod architecture

**Verdict up front:** a 14-stage deterministic pipeline over a SQLite
fact/graph store, with an optional, cached, logged LLM subsystem that
can only annotate — never decide. Every stage is an idempotent CLI
subcommand reading the previous stage's outputs from `workspace/`; every
human-facing catalog is mirrored to plain CSV, which is also the medium
for human decisions (approval gates).

## The one architectural law

The LLM never gets the final word. Deterministic parsing and the
knowledge graph are the source of truth; LLM output is always
(a) traceable to source lines, (b) marked `origin=llm` with confidence
and `needs_review=1`, and (c) gated by human approval and/or the
validation harness before anything downstream consumes it.

## Stages

1. **ingest** (`inventory.py`) — walk the tree; classify every file by
   extension + content sniffing; detect EBCDIC via byte histogram (flag,
   never convert); unknown types kept as `artifact_type=unknown`.
2. **analyze** (`adapters/`) — every registered adapter emits typed
   Facts. Two tiers: `deterministic` (island parsing, never crashes,
   counts `parse_errors`) and `llm_assisted` (TAL; every fact forced to
   `origin=llm, needs_review=1`). A post-pass derives program→PSB links.
3. **graph** (`graph.py`) — nodes/edges per the §schemas below, with
   derived program→dataset access (SELECT...ASSIGN ddname joined to the
   DD cards of every step executing the program). Queries: `--impact`,
   `--lineage`, `--dead`, `--cycles`, `--where-used`. Exports
   `graph.json` + `graph.mmd` (per-domain subgraphs once units exist).
3b. **assess** (`assess.py`) + **slice** (`slice.py`) — cyclomatic
   complexity, clone detection (normalized line windows), completeness
   (known-utilities allowlist), migration blockers, t-shirt efforts;
   static backward slicing at paragraph granularity.
4. **docs** (`docsgen.py`) — deterministic markdown; `--enrich` appends
   marked AI narrative blocks.
5. **rules** (`rules.py`) — candidate mining: conditionals guarding
   persistent writes, computations feeding them, 88-levels, status
   assignments; stable rule ids preserve human statuses across re-runs.
6. **decompose** (`decompose.py`) — seeded label propagation over
   call+data edges; units with wave plan by afferent coupling and a
   disposition recommendation justified from blockers + metrics.
   **HITL gate:** `units.csv` status must be edited to `approved`.
7. **spec** (`specgen.py`) — scope, interfaces with modern proposals,
   PIC-derived data model, approved rules verbatim, NFRs, open
   questions. Spec-first is mandatory for codegen.
7b. **datamig** (`datamig.py`) — DB2→PostgreSQL DDL (documented type
   map, unmapped types flagged), copybook→relational proposal
   (OCCURS→child tables, REDEFINES flagged), generated cp037/COMP-3
   converters with executable round-trip self-tests.
8. **generate** (`codegen.py` + `templates/`) — java-spring skeleton
   (entities, rule-stub service, controller, failing-by-design
   characterization tests, traceability README), airflow-dag, openapi.
9. **validate** (`validate.py`) — fixture cases under
   `workspace/fixtures/<unit>/case_*/`; EBCDIC/packed-aware field
   comparator; GnuCOBOL oracle feature-detected (`cobc` on PATH);
   Dual Run-style report with informational timing columns; equivalence
   PASS only at 100%.
10. **interfaces** (`interfaces.py`) — NDM/FTP/SFTP/XCOM transfers, MQ
   resolved through the MQSC chain (QREMOTE→XMITQ→SDR channel→CONNAME),
   dataset handoffs; `transfers_to` edges to external_node.
11. **runs** (`opsdata.py`) — documented CSV exports only (no SMF
   parsing); frequencies from date gaps, durations, abend rates, volume
   joins onto the interface catalog.
12. **reconcile** (`reconcile.py`) — schedules vs library vs runs;
   healthy / decommission_candidate / on_request / broken_reference /
   transitively_dead; evidence lists, never deletions.
13. **flows** (`flows.py`) — batch-stream Mermaid (TIME=/calendars,
   handoffs, external deps), CICS navigation, online↔batch
   `shares_resource` edges (CICS FILEs resolved via CSD DSNAME),
   capability roll-up pages.
14. **report** (`report.py`) — one page of honest numbers.

## Data schemas

SQLite tables (mirrored as CSV in `workspace/`): `artifacts`, `facts`,
`nodes`, `edges`, `interfaces`, `job_runs`, `dataset_stats`,
`reconcile`, `capabilities`, `rules`, `units`, `metrics`, `clones`,
`blockers`, `missing_artifacts`, `llm_log`, `llm_cache`,
`validation_results` — full DDL in
`src/legacymod/migrations/001_initial.sql`. Schema versioning: the
`schema_version` table + numbered migration SQL files applied in order
(`002_rules_needs_review.sql` is the first increment).

Node types: program, paragraph, copybook, file, dataset, table, screen,
job, step, schedule_job, transaction, queue, unit, external_node,
capability. Edge types: calls, performs, includes, reads, writes, binds,
precedes, triggers, displays, transfers_to, shares_resource, belongs_to.

Fact `detail_json` is schemaless; every adapter documents its shapes in
its module docstring.

## Validation fixture format

```
workspace/fixtures/<unit>/case_<name>/
    case.json        {"record": "PAY-RECORD", "format": "flat"|"csv",
                      "legacy_cmd": [...], "modern_cmd": [...],
                      "oracle_source": "cobol/X.cbl"}
    expected.dat     legacy baseline (EBCDIC fixed records or CSV)
    actual.dat       modernized output to compare
    input.<DDNAME>   optional inputs (mapped to DD_<DDNAME> for the
                     GnuCOBOL oracle)
```

## Trust and audit surfaces

- `llm_log.csv` — every LLM call: provider, model, purpose, artifact,
  prompt/response hashes, cache_hit, accepted_by_human.
- `review_queue.csv` — everything awaiting a human decision.
- `needs_review` on facts/rules — nothing LLM-derived or heuristic loses
  the flag without a human action.
- The estate directory is read-only to every stage.
