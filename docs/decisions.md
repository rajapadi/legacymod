# Decisions log

Non-obvious implementation choices, recorded per the build contract.

- **Clone-detection window is 5 normalized lines, not 30.** The build
  prompt's stage description names 30-line windows (large-estate
  practice), but its own acceptance fixture is a 6-line clone
  (PAYCALC/ORPHAN overtime block). Acceptance criteria are the spec, so
  `find_clones(window=5)` is the default, with the window an explicit
  parameter; matches are extended to maximal ranges and filtered by a
  20-token minimum to suppress boilerplate. (2026-07-18)
- **FTP steps to hosts whose name indicates SFTP are cataloged as
  `sftp`.** `XFERJOB` STEP020 runs PGM=FTP against `sftp.vendorx.com`;
  the acceptance criteria call this "the SFTP transfer". The parser
  records protocol `sftp` when the host name contains `sftp`, else
  `ftp`/`ftps` by program name. (2026-07-18)
- **`artifacts` table carries a `confidence` column** beyond the §6
  minimum, because inventory.csv is specified to include classification
  confidence and the CSV mirrors the table. (2026-07-18)
- **`report` is implemented as `src/legacymod/report.py`.** The §3
  layout lists no module for the `report` subcommand; a dedicated small
  module keeps the CLI dispatch uniform. (2026-07-18)
- **Assembler-call blockers use a name heuristic** (`CALL` target
  starting with `ASM`), since member language is unknowable without the
  member; the blocker detail says "name heuristic; verify". (2026-07-18)
- **Program→PSB linkage** (Phase 6): a program making CBLTDLI/AIBTDLI
  calls is linked to a PSB by name-stem match when possible, else — when
  the estate has exactly one PSB — to that PSB with confidence 0.5 and
  `needs_review=1`. Real linkage lives in JCL (DFSRRC00 PARM) or online
  PSB scheduling, neither present in a source-only estate. (2026-07-18)
- **CSV exports are UTF-8 without BOM; detail strings stay ASCII** so
  Excel-on-Windows double-click opens don't mangle text. (2026-07-18)
- **Standalone `.ctl` members are classified `utility_ctl` with no
  adapter.** Found by running the pipeline against AWS CardDemo:
  control cards shipped as PDS members (IDCAMS REPRO, DB2 utility
  input) landed as `unknown`. Classification restores inventory
  accuracy; parsing is deliberately not duplicated outside the JCL
  adapter's instream/step context, where the same cards carry job/step
  provenance. (2026-07-19)
- **Oracle fixture contract is explicit, not inferred.** First real
  oracle run (CardDemo CBACT01C under GnuCOBOL 3.2) showed the compile
  needs dialect and copybook dirs and the run needs output-ddname
  mapping; all three are per-case `case.json` keys (`oracle_std`,
  `oracle_includes`, `oracle_outputs`) rather than global config,
  because they are properties of the program under test. CALLed
  modules (e.g. a COBOL stand-in for a z/OS assembler routine) are
  dropped into the case dir as compiled modules, where the runtime's
  cwd-based resolution finds them. (2026-07-20)
- **The architecture recommender is a rules engine, not an LLM.**
  `recommend` derives future-state fit from graph evidence with fixed
  per-rule confidences (signal counts live in the evidence JSON; the
  confidence only ranks how direct the inference is). The LLM's role
  is narrating trade-offs behind `--enrich`, marked `needs_review` —
  same one-architectural-law reasoning as everywhere else: the choice
  must be traceable to facts a human can audit. Human-decided rows
  (approved/rejected) are never regenerated; re-running `recommend`
  only refreshes still-proposed rows. (2026-07-20)
