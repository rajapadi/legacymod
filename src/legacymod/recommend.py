"""Stage 3d — evidence-based future-state architecture recommendation.

For each unit, deterministic rules over the knowledge graph pick the
best-fit execution style, compute target, data store, integration
style, and UI approach — every recommendation carries the evidence that
produced it, the alternatives considered, and a confidence. The LLM
never chooses the architecture: ``--enrich`` only appends a marked,
``needs_review`` trade-off narrative to the report.

Output is a HITL gate like ``units.csv``: rows land in
``architecture.csv`` as ``proposed``; a human flips them to
``approved``/``rejected``, and ``generate`` without ``--target`` then
renders every approved ``generate_target`` for the unit. Human-decided
rows are never regenerated.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict

from .config import Config
from .decompose import sync_units_from_csv
from .specgen import find_unit
from .store import Store

log = logging.getLogger(__name__)

_CONCERN_ORDER = ["execution_style", "compute", "data", "integration", "ui"]


def _unit_evidence(store: Store, unit: dict) -> dict:
    """Collect the deterministic signals one unit's rules run over."""
    programs = set(json.loads(unit["programs_json"] or "[]"))
    edges = [dict(r) for r in store.query(
        "SELECT e.edge_type et, s.name sn, s.node_type st,"
        " d.name dn, d.node_type dt FROM edges e"
        " JOIN nodes s ON s.id=e.src_node JOIN nodes d ON d.id=e.dst_node")]

    transactions = sorted({e["sn"] for e in edges
                           if e["et"] == "triggers" and e["dn"] in programs
                           and e["st"] == "transaction"} |
                          {e["dn"] for e in edges
                           if e["et"] == "triggers" and e["sn"] in programs
                           and e["dt"] == "transaction"})
    screens = sorted({e["dn"] for e in edges if e["et"] == "displays"
                      and e["sn"] in programs and e["dt"] == "screen"})
    batch_steps = [e["sn"] for e in edges if e["et"] == "calls"
                   and e["st"] == "step" and e["dn"] in programs]
    jobs = sorted({s.split(".")[0] for s in batch_steps})
    steps_per_job: dict[str, set] = defaultdict(set)
    for e in edges:
        if e["st"] == "step" and e["sn"].split(".")[0] in jobs:
            steps_per_job[e["sn"].split(".")[0]].add(e["sn"])
    multi_step_jobs = sorted(j for j, s in steps_per_job.items() if len(s) > 1)
    outside_callers = sorted({e["sn"] for e in edges if e["et"] == "calls"
                              and e["st"] == "program" and e["dt"] == "program"
                              and e["dn"] in programs
                              and e["sn"] not in programs})
    data_access = [e for e in edges if e["et"] in ("reads", "writes")
                   and e["sn"] in programs and e["st"] == "program"]
    datasets = sorted({e["dn"] for e in data_access if e["dt"] == "dataset"})
    tables = sorted({e["dn"] for e in data_access if e["dt"] == "table"})
    queues = sorted({e["dn"] for e in data_access if e["dt"] == "queue"})

    # per-program facts: CICS/MQ/IMS/SQL presence, keyed (VSAM-style) files
    placeholders = ",".join("?" * len(programs)) or "''"
    fact_counts: dict[str, int] = defaultdict(int)
    keyed_files, occurs_fields = set(), 0
    if programs:
        rows = store.query(
            "SELECT f.fact_type ft, f.name, f.detail_json FROM facts f"
            " WHERE f.artifact_id IN (SELECT artifact_id FROM facts"
            f"  WHERE fact_type='program' AND name IN ({placeholders}))",
            tuple(programs))
        for r in rows:
            fact_counts[r["ft"]] += 1
            d = json.loads(r["detail_json"] or "{}")
            if r["ft"] == "select" and d.get("record_key"):
                keyed_files.add(r["name"])
            if r["ft"] == "data_item" and d.get("occurs", 0) > 0:
                occurs_fields += 1
    blockers = [dict(r) for r in store.query(
        "SELECT program_or_job, blocker_type, detail FROM blockers"
        f" WHERE program_or_job IN ({placeholders})", tuple(programs))] \
        if programs else []
    transfers = [dict(r) for r in store.query(
        "SELECT protocol, direction, dataset_or_queue FROM interfaces")]
    transfers = [t for t in transfers if t["protocol"] != "dataset"]

    return {"programs": sorted(programs), "transactions": transactions,
            "screens": screens, "jobs": jobs,
            "multi_step_jobs": multi_step_jobs,
            "outside_callers": outside_callers, "datasets": datasets,
            "tables": tables, "queues": queues,
            "keyed_files": sorted(keyed_files),
            "occurs_fields": occurs_fields,
            "cics_stmts": fact_counts.get("cics", 0),
            "mq_calls": fact_counts.get("mq_call", 0),
            "ims_calls": fact_counts.get("ims_call", 0),
            "sql_stmts": fact_counts.get("sql", 0),
            "blockers": blockers, "transfers": transfers}


def recommend_rows(ev: dict) -> list[dict]:
    """Pure rule engine: evidence in, ranked recommendation rows out.

    Every row states its evidence; confidences are fixed per rule (the
    signal counts live in the evidence, the number just ranks how
    direct the inference is).
    """
    rows: list[dict] = []
    online = bool(ev["transactions"] or ev["screens"] or ev["cics_stmts"])
    batch = bool(ev["jobs"])
    library = bool(ev["outside_callers"]) and not online and not batch

    def add(concern, recommendation, target="", alternatives="",
            confidence=0.8, **evidence):
        rows.append({"concern": concern, "recommendation": recommendation,
                     "generate_target": target, "alternatives": alternatives,
                     "confidence": confidence, "evidence": evidence})

    # execution style ----------------------------------------------------
    if online and batch:
        add("execution_style",
            "split the unit: online transactions and batch jobs modernize "
            "on different paths (service vs pipeline)",
            confidence=0.85, transactions=ev["transactions"],
            jobs=ev["jobs"], cics_stmts=ev["cics_stmts"])
    elif online:
        add("execution_style", "online transaction processing - target a "
            "request/response service", confidence=0.9,
            transactions=ev["transactions"], screens=ev["screens"],
            cics_stmts=ev["cics_stmts"])
    elif batch:
        add("execution_style", "scheduled batch processing - target a "
            "data pipeline, not a service", confidence=0.9,
            jobs=ev["jobs"], multi_step_jobs=ev["multi_step_jobs"])
    elif library:
        add("execution_style", "called subroutine only - extract as a "
            "shared library/module of the owning service", confidence=0.8,
            callers=ev["outside_callers"])
    else:
        add("execution_style", "no runtime entry point found - verify "
            "against schedules and CSD before assigning a target",
            confidence=0.4, programs=ev["programs"])

    # compute ------------------------------------------------------------
    if online:
        add("compute", "Java Spring Boot microservice(s) exposing the "
            "transactions as REST resources", target="java-spring",
            alternatives="Quarkus/Micronaut; transitional CICS rehost "
            "(different product category)", confidence=0.85,
            transactions=ev["transactions"], cics_stmts=ev["cics_stmts"])
        add("integration", "publish the service contract as OpenAPI",
            target="openapi", alternatives="gRPC for internal-only calls",
            confidence=0.85, screens=ev["screens"])
    if batch:
        if ev["multi_step_jobs"]:
            add("compute", "batch workers orchestrated as DAGs - the "
                "multi-step JCL streams map to task dependencies",
                target="airflow-dag",
                alternatives="Spring Batch under an enterprise scheduler; "
                "AWS Step Functions", confidence=0.8,
                multi_step_jobs=ev["multi_step_jobs"], jobs=ev["jobs"])
        else:
            add("compute", "self-contained batch jobs - Spring Batch "
                "services with the extract logic as testable steps",
                target="java-spring",
                alternatives="Airflow if cross-job orchestration grows",
                confidence=0.8, jobs=ev["jobs"])
    if library and not (online or batch):
        add("compute", "shared Java library consumed by the callers' "
            "services", target="java-spring",
            alternatives="sidecar service if independent scaling is needed",
            confidence=0.7, callers=ev["outside_callers"])

    # data ---------------------------------------------------------------
    if ev["tables"]:
        add("data", "PostgreSQL - the DB2 DDL converts mechanically "
            "(datamig stage)", alternatives="managed PG (RDS/Cloud SQL); "
            "retain DB2 LUW where licensing already exists",
            confidence=0.85, tables=ev["tables"])
    if ev["keyed_files"]:
        alt = "document store (e.g. MongoDB) if the record nesting is deep"
        if ev["occurs_fields"]:
            alt += (f" - {ev['occurs_fields']} repeating-group field(s) "
                    "found, so model that choice per record")
        add("data", "keyed VSAM files become PostgreSQL tables keyed on "
            "the RECORD KEY", alternatives=alt, confidence=0.8,
            keyed_files=ev["keyed_files"],
            occurs_fields=ev["occurs_fields"])
    if ev["ims_calls"]:
        add("data", "IMS hierarchies flatten to relational parent/child "
            "tables", alternatives="MongoDB documents preserve the "
            "hierarchy 1:1 when unload order matters", confidence=0.7,
            ims_calls=ev["ims_calls"])
    if ev["datasets"] and not ev["keyed_files"] and not ev["tables"]:
        add("data", "flat sequential files land in object storage with "
            "relational staging tables for querying", confidence=0.6,
            datasets=ev["datasets"])

    # integration / ui ---------------------------------------------------
    if ev["queues"]:
        add("integration", "MQ queues map to message-broker "
            "topics/queues preserving delivery semantics",
            alternatives="keep IBM MQ; Kafka only if event streaming is "
            "actually needed", confidence=0.8, queues=ev["queues"],
            mq_calls=ev["mq_calls"])
    if ev["transfers"]:
        protos = sorted({t["protocol"] for t in ev["transfers"]})
        add("integration", "file transfers (" + ", ".join(protos) + ") "
            "become API calls or managed file transfer per the interface "
            "catalog", confidence=0.7,
            transfer_count=len(ev["transfers"]), protocols=protos)
    if ev["screens"]:
        add("ui", "BMS screens re-front as an Angular SPA over the REST "
            "APIs", alternatives="React; transitional 3270 emulation",
            confidence=0.75, screens=ev["screens"])

    order = {c: i for i, c in enumerate(_CONCERN_ORDER)}
    rows.sort(key=lambda r: order[r["concern"]])
    return rows


def sync_architecture_from_csv(store: Store, cfg: Config) -> None:
    """Pull human status edits from architecture.csv back into the store."""
    path = cfg.workspace / "architecture.csv"
    if not path.is_file():
        return
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("rec_id") and row.get("status"):
                store.execute(
                    "UPDATE architecture SET status=? WHERE rec_id=?"
                    " AND status<>?",
                    (row["status"], row["rec_id"], row["status"]))
    store.commit()


def approved_targets(store: Store, unit_id: str) -> list[str]:
    """Distinct approved generate targets for a unit, concern-ordered."""
    rows = store.query(
        "SELECT concern, generate_target FROM architecture WHERE unit_id=?"
        " AND status='approved' AND generate_target<>''", (unit_id,))
    order = {c: i for i, c in enumerate(_CONCERN_ORDER)}
    seen: list[str] = []
    for r in sorted(rows, key=lambda r: order.get(r["concern"], 99)):
        if r["generate_target"] not in seen:
            seen.append(r["generate_target"])
    return seen


def _export(store: Store, cfg: Config) -> None:
    store.export_csv(
        "SELECT rec_id, unit_id, concern, recommendation, generate_target,"
        " alternatives, confidence, evidence_json, status, needs_review"
        " FROM architecture ORDER BY unit_id, rec_id",
        cfg.workspace / "architecture.csv")


def _report(store: Store, cfg: Config, units: list[dict],
            enrich_blocks: dict[str, str]) -> None:
    lines = ["# Future-state architecture recommendations", ""]
    for unit in units:
        rows = [dict(r) for r in store.query(
            "SELECT * FROM architecture WHERE unit_id=? ORDER BY rec_id",
            (unit["unit_id"],))]
        if not rows:
            continue
        style = next((r["recommendation"] for r in rows
                      if r["concern"] == "execution_style"), "?")
        targets = sorted({r["generate_target"] for r in rows
                          if r["generate_target"]})
        lines += [
            f"## Unit {unit['name']} ({unit['unit_id']})", "",
            f"**Verdict up front:** {style}. Generate targets: "
            f"{', '.join(targets) if targets else 'none'}. Rows below are "
            "proposed until approved in `architecture.csv`; every one "
            "lists its evidence.", "",
            "| concern | recommendation | conf | evidence | alternatives |",
            "|---|---|---:|---|---|",
        ]
        for r in rows:
            ev = json.loads(r["evidence_json"] or "{}")
            ev_txt = "; ".join(
                f"{k}={v if not isinstance(v, list) else len(v)}"
                for k, v in ev.items() if v)
            lines.append(f"| {r['concern']} | {r['recommendation']} |"
                         f" {r['confidence']:.2f} | {ev_txt} |"
                         f" {r['alternatives'] or '-'} |")
        lines.append("")
        if unit["unit_id"] in enrich_blocks:
            lines += [enrich_blocks[unit["unit_id"]], ""]
    (cfg.workspace / "architecture.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        sync_units_from_csv(store, cfg)
        sync_architecture_from_csv(store, cfg)
        if getattr(args, "unit", None):
            unit = find_unit(store, args.unit)
            if not unit:
                print(f"recommend: no unit {args.unit!r}")
                return 1
            units = [unit]
        else:
            units = [dict(r) for r in store.query(
                "SELECT * FROM units ORDER BY unit_id")]
        if not units:
            print("recommend: no units yet - run `legacymod decompose` "
                  "and approve units.csv first")
            return 1

        enrich_blocks: dict[str, str] = {}
        for unit in units:
            decided = {r["concern"] for r in store.query(
                "SELECT concern FROM architecture WHERE unit_id=?"
                " AND status<>'proposed'", (unit["unit_id"],))}
            store.execute("DELETE FROM architecture WHERE unit_id=?"
                          " AND status='proposed'", (unit["unit_id"],))
            ev = _unit_evidence(store, unit)
            rows = [r for r in recommend_rows(ev)
                    if r["concern"] not in decided]
            for i, r in enumerate(rows, 1):
                store.execute(
                    "INSERT INTO architecture (rec_id, unit_id, concern,"
                    " recommendation, generate_target, alternatives,"
                    " confidence, evidence_json) VALUES (?,?,?,?,?,?,?,?)",
                    (f"{unit['unit_id']}-{i:02d}", unit["unit_id"],
                     r["concern"], r["recommendation"], r["generate_target"],
                     r["alternatives"], r["confidence"],
                     json.dumps(r["evidence"])))
            if decided:
                print(f"  {unit['name']}: kept {len(decided)} "
                      "human-decided concern(s) untouched")
            if getattr(args, "enrich", False) and rows:
                from .llm import ai_block, complete_cached, get_provider
                summary = json.dumps({k: v for k, v in ev.items() if v},
                                     default=str)[:2000]
                picks = "; ".join(f"{r['concern']}: {r['recommendation']}"
                                  for r in rows)
                prompt = ("Narrate the trade-offs of this future-state "
                          "architecture recommendation for a modernization "
                          f"review board.\nUnit: {unit['name']}\n"
                          f"Evidence: {summary}\nRecommendations: {picks}\n")
                result, _ = complete_cached(
                    store, cfg, prompt, purpose="architecture_narrative",
                    artifact=f"unit:{unit['unit_id']}")
                enrich_blocks[unit["unit_id"]] = ai_block(
                    result, get_provider(cfg).name)
        store.commit()
        _export(store, cfg)
        _report(store, cfg, units, enrich_blocks)

        n = store.query("SELECT COUNT(*) c FROM architecture")[0]["c"]
        print(f"recommend: {n} recommendation row(s) across {len(units)} "
              f"unit(s) -> {cfg.workspace / 'architecture.csv'} (HITL gate:"
              " edit status to 'approved'), architecture.md")
        for unit in units:
            for r in store.query(
                    "SELECT concern, recommendation, generate_target,"
                    " confidence FROM architecture WHERE unit_id=?"
                    " ORDER BY rec_id", (unit["unit_id"],)):
                tgt = f" -> {r['generate_target']}" if r["generate_target"] \
                    else ""
                print(f"  {unit['name']:12s} {r['concern']:15s} "
                      f"conf={r['confidence']:.2f}{tgt}  "
                      f"{r['recommendation'][:70]}")
    return 0
