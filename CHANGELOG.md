# Changelog

All notable changes to legacymod are documented here.

## [0.1.2] — 2026-07-20

First real execution of the GnuCOBOL oracle — validated end to end
against AWS CardDemo: CBACT01C compiled and run under GnuCOBOL 3.2 as
the legacy side, a Python re-implementation as the modern side, 50
accounts compared field-by-field to equivalence PASS. Everything below
was found by that exercise.

### Added
- **Oracle options in `case.json`** — `oracle_std` (cobc dialect),
  `oracle_includes` (estate-relative copybook dirs), and
  `oracle_outputs` (ddname → case file). Without the output mapping a
  GnuCOBOL program writes to the literal ASSIGN name and the
  regenerated baseline never reaches `expected.*`.
- **`encoding` option** (`ebcdic` default, `ascii`) for flat-field
  comparison, so ASCII-runtime fixtures produce readable values in
  mismatch reports.

### Fixed
- **Oracle DD paths are resolved absolute.** They were built from the
  (usually relative) workspace path but consumed by a process whose cwd
  is the case directory — every input open failed with FILE STATUS 35
  when the workspace was given as a relative path.
- **A failing oracle/`legacy_cmd`/`modern_cmd` is now a recorded case
  failure** (named in the report with the runtime's last error line)
  instead of an unhandled traceback; oracle failures log stdout too,
  which is where batch programs print which file failed.

## [0.1.1] — 2026-07-19

### Fixed
- **Inventory classification gaps found by running the pipeline against
  AWS's open-source CardDemo estate**
  (aws-samples/aws-mainframe-modernization-carddemo): standalone JCL
  procedures with the common `.prc` extension now classify as `jcl`
  (the adapter already parsed PROCs), backed by a content sniff for
  `//name PROC` cards; standalone utility control-card members
  (`.ctl` — IDCAMS, DB2 utility input) classify as `utility_ctl`
  instead of `unknown`. Parsing control cards outside a JCL instream
  context remains a roadmap item.

## [0.1.0] — 2026-07-18

Initial release: the full analysis-to-validation pipeline.

### Added
- **Ingest & inventory** — extension + content classification for 16
  artifact types, EBCDIC byte-histogram detection (flagged, never
  converted), SHA-256, LOC, confidence; `inventory.csv`.
- **14 technology adapters** — COBOL (island parser: paragraphs,
  PERFORM/CALL/COPY, SELECT...ASSIGN, EXEC SQL/CICS, data refs,
  88-levels, MQ/DLI calls), copybooks (PIC-derived field metadata with
  offsets), JCL + utility control cards (IDCAMS, SORT, IEBGENER,
  Connect:Direct/NDM, batch FTP/SFTP, BPXBATCH, XCOM, IKJEFT01), DB2
  DDL, CICS BMS, CICS CSD, MQSC, IMS DBD/PSB, REXX, Easytrieve, CA7,
  Control-M, HPNS COBOL (ENTER TAL/Pathway divergence marking), TAL
  (llm_assisted; regex skeleton + marked LLM proposal).
- **Knowledge graph** — impact, lineage, dead-code, cycles, and
  field-level where-used queries; JSON + Mermaid export.
- **Assessment** — cyclomatic metrics, clone detection, completeness
  check with known-utilities allowlist, migration-blocker scan,
  documented t-shirt effort formula; static backward slicing.
- **Docs & rules** — current-state docs with CRUD matrices;
  deterministic rule mining with line-level traceability and stable ids.
- **LLM subsystem** — provider protocol, deterministic offline stub
  (default), optional claude_cli provider, prompt-hash cache, full call
  log, HITL review queue (CSV out / CSV in).
- **Decompose/spec/codegen** — seeded label-propagation clustering,
  wave plan by afferent coupling, disposition recommendations with
  evidence, HITL approval gate, spec-first codegen for java-spring,
  airflow-dag, and openapi targets with full traceability.
- **Data migration** — DB2→PostgreSQL DDL conversion with documented
  type mapping, copybook→relational proposals, generated cp037/COMP-3
  converters with executable round-trip self-tests.
- **Validation harness** — EBCDIC/packed-aware field comparator,
  GnuCOBOL oracle (feature-detected), Dual Run-style report with
  informational timings; equivalence PASS only at 100%.
- **Operational analysis** — interface/transmission catalog (NDM, FTP,
  SFTP, XCOM, MQ resolved through MQSC chains, dataset handoffs), run
  history analytics, schedule/library/runs reconciliation, batch-stream
  and CICS-navigation flows, online↔batch shared resources, capability
  roll-ups, status report.
- Embedded sample estate (24 artifacts) + ops CSVs + validation
  fixtures; 65+ tests; CI on Windows/Ubuntu, Python 3.12/3.13.
