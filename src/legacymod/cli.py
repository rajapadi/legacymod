"""legacymod command-line interface.

Each pipeline stage is a subcommand. Stages are idempotent and read the
previous stage's outputs from the workspace directory. Stage modules
expose ``run(args, cfg) -> int``; the CLI is a thin dispatcher.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys

from .config import Config, load_config

log = logging.getLogger("legacymod")

# command -> (module implementing run(args, cfg), one-line purpose)
_STAGES: dict[str, tuple[str, str]] = {
    "ingest": ("legacymod.inventory",
               "walk the source tree, classify every artifact, write inventory.csv + SQLite"),
    "analyze": ("legacymod.adapters",
                "run all applicable technology adapters, emit Facts into the store"),
    "graph": ("legacymod.graph",
              "build the knowledge graph; answer impact/lineage/dead/cycles/where-used queries"),
    "assess": ("legacymod.assess",
               "complexity metrics, clone detection, completeness check, blockers, effort sizing"),
    "slice": ("legacymod.slice",
              "static backward program slice from a seed field or paragraph"),
    "docs": ("legacymod.docsgen",
             "generate current-state documentation from the graph (optionally LLM-enriched)"),
    "rules": ("legacymod.rules",
              "mine business-rule candidates with line-level traceability (optionally LLM-enriched)"),
    "interfaces": ("legacymod.interfaces",
                   "catalog external transmissions (NDM/FTP/SFTP/XCOM/MQ) and dataset handoffs"),
    "runs": ("legacymod.opsdata",
             "ingest job run history + dataset stats CSVs; derive frequency/duration/volume analytics"),
    "reconcile": ("legacymod.reconcile",
                  "cross-check schedules vs libraries vs run history; flag decommission candidates"),
    "flows": ("legacymod.flows",
              "batch stream flows, CICS navigation, online<->batch map, capability roll-up"),
    "decompose": ("legacymod.decompose",
                  "cluster the graph into domains and migratable units with a wave plan (HITL gate)"),
    "recommend": ("legacymod.recommend",
                  "evidence-based future-state architecture recommendation per unit (HITL gate)"),
    "spec": ("legacymod.specgen",
             "assemble the modernization spec for an approved unit"),
    "datamig": ("legacymod.datamig",
                "data-migration planning: DDL conversion, relational proposals, conversion scripts"),
    "generate": ("legacymod.codegen",
                 "render target skeletons (java-spring / airflow-dag / openapi) from the spec"),
    "validate": ("legacymod.validate",
                 "behavior-first equivalence harness over fixtures; field-level diff report"),
    "review": ("legacymod.review",
               "export/apply the human-in-the-loop review queue (CSV in/out)"),
    "report": ("legacymod.report",
               "one-page status report: counts, coverage, pass rates, LLM usage"),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="legacymod",
        description="Local-first, auditable mainframe-modernization platform.",
    )
    p.add_argument("--config", metavar="PATH", help="path to legacymod.toml")
    p.add_argument("--workspace", metavar="DIR",
                   help="workspace directory (default ./workspace or TOML value)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("ingest", help=_STAGES["ingest"][1])
    sp.add_argument("src_dir", help="root of the legacy source tree (read-only)")

    sub.add_parser("analyze", help=_STAGES["analyze"][1])

    sp = sub.add_parser("graph", help=_STAGES["graph"][1])
    sp.add_argument("--impact", metavar="NAME", help="what breaks if NAME changes")
    sp.add_argument("--lineage", metavar="DATASET", help="lineage chain for a dataset")
    sp.add_argument("--dead", action="store_true", help="unreachable programs")
    sp.add_argument("--cycles", action="store_true", help="call cycles")
    sp.add_argument("--where-used", metavar="FIELD", dest="where_used",
                    help="field-level cross reference")

    sub.add_parser("assess", help=_STAGES["assess"][1])

    sp = sub.add_parser("slice", help=_STAGES["slice"][1])
    sp.add_argument("program", help="program to slice")
    sp.add_argument("--seed", required=True, help="seed field or paragraph")

    sp = sub.add_parser("docs", help=_STAGES["docs"][1])
    sp.add_argument("--enrich", action="store_true",
                    help="add LLM narrative summaries (marked, needs_review)")

    sp = sub.add_parser("rules", help=_STAGES["rules"][1])
    sp.add_argument("--enrich", action="store_true",
                    help="LLM plain-English explanations (marked, needs_review)")

    sub.add_parser("interfaces", help=_STAGES["interfaces"][1])

    sp = sub.add_parser("runs", help=_STAGES["runs"][1])
    sp.add_argument("run_history", help="job run history CSV export")
    sp.add_argument("--dataset-stats", metavar="CSV", dest="dataset_stats",
                    help="dataset statistics CSV export")

    sub.add_parser("reconcile", help=_STAGES["reconcile"][1])
    sub.add_parser("flows", help=_STAGES["flows"][1])
    sub.add_parser("decompose", help=_STAGES["decompose"][1])

    sp = sub.add_parser("recommend", help=_STAGES["recommend"][1])
    sp.add_argument("unit", nargs="?", help="unit id or name (default: all)")
    sp.add_argument("--enrich", action="store_true",
                    help="LLM trade-off narrative (marked, needs_review)")

    sp = sub.add_parser("spec", help=_STAGES["spec"][1])
    sp.add_argument("unit", help="approved unit id or name")

    sp = sub.add_parser("datamig", help=_STAGES["datamig"][1])
    sp.add_argument("unit", help="approved unit id or name")

    sp = sub.add_parser("generate", help=_STAGES["generate"][1])
    sp.add_argument("unit", help="approved unit id or name")
    sp.add_argument("--target", default=None,
                    choices=["java-spring", "airflow-dag", "openapi"],
                    help="omit to render every approved architecture "
                         "recommendation for the unit")
    sp.add_argument("--llm-impl", action="store_true", dest="llm_impl",
                    help="LLM-proposed method bodies as marked drafts")

    sp = sub.add_parser("validate", help=_STAGES["validate"][1])
    sp.add_argument("unit", help="unit id or name")

    sp = sub.add_parser("review", help=_STAGES["review"][1])
    sp.add_argument("--apply", metavar="CSV", nargs="?", const="",
                    help="apply an edited review CSV back to the store")

    sub.add_parser("report", help=_STAGES["report"][1])
    sub.add_parser("status", help="show workspace/pipeline status")
    return p


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    """Print a short pipeline status without requiring any stage to have run."""
    print(f"legacymod status — workspace: {cfg.workspace.resolve()}")
    if not cfg.db_path.is_file():
        print("  no store yet — run `legacymod ingest <src-dir>` to begin")
        return 0
    import sqlite3
    con = sqlite3.connect(cfg.db_path)
    try:
        for table in ("artifacts", "facts", "nodes", "edges", "rules", "units",
                      "interfaces", "reconcile", "validation_results"):
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {table:20s} {n:>8d} rows")
            except sqlite3.OperationalError:
                pass
    finally:
        con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.command:
        parser.print_help()
        return 2
    cfg = load_config(args.config, args.workspace)
    if args.command == "status":
        return cmd_status(args, cfg)
    module_name, purpose = _STAGES[args.command]
    try:
        module = importlib.import_module(module_name)
        runner = getattr(module, "run")
    except (ModuleNotFoundError, AttributeError):
        print(f"legacymod {args.command}: not implemented yet — will {purpose}.")
        return 0
    return int(runner(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
