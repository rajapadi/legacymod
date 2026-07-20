"""Stage 7 — per-unit modernization spec.

Spec-first is mandatory: `generate` consumes the same assembled spec
data, and refuses units that have not passed through here. A unit must
be human-approved (units.csv edited to ``status=approved``) before a
spec is produced; on success the unit advances to ``spec_done``.

The spec assembles: unit scope (programs/jobs/screens), interfaces
in/out with proposed modern equivalents, the data model derived from
copybooks/records (PIC-derived types), the unit's rules from the catalog
(approved ones verbatim), NFRs (volumes when run data is loaded), and
open questions (missing artifacts, dynamic calls, REDEFINES).
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .config import Config
from .store import Store
from .decompose import sync_units_from_csv

log = logging.getLogger(__name__)


def templates_dir() -> Path:
    """Repo-root templates/ (editable install) or cwd fallback."""
    cand = Path(__file__).resolve().parents[2] / "templates"
    if cand.is_dir():
        return cand
    if (Path.cwd() / "templates").is_dir():
        return Path.cwd() / "templates"
    raise FileNotFoundError(
        "templates/ directory not found (looked next to the package and in "
        "the current directory)")


def jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(templates_dir()),
                       undefined=StrictUndefined, keep_trailing_newline=True,
                       trim_blocks=True, lstrip_blocks=True)


def find_unit(store: Store, key: str) -> dict | None:
    row = store.query("SELECT * FROM units WHERE unit_id=? OR name=?",
                      (key, key))
    return dict(row[0]) if row else None


def _java_name(legacy: str, cap: bool = False) -> str:
    parts = [p for p in legacy.replace("_", "-").split("-") if p]
    name = "".join(p.capitalize() for p in parts)
    return name if cap else (name[:1].lower() + name[1:] if name else name)


def java_type(detail: dict) -> str:
    pic = (detail.get("pic") or "").upper()
    if not pic or "X" in pic or "A" in pic:
        return "String"
    if detail.get("decimals"):
        return "BigDecimal"
    return "long" if (detail.get("digits") or 0) > 9 else "int"


def json_type(detail: dict) -> dict:
    pic = (detail.get("pic") or "").upper()
    if not pic or "X" in pic or "A" in pic:
        out = {"type": "string"}
        if detail.get("length"):
            out["maxLength"] = detail["length"]
        return out
    if detail.get("decimals"):
        return {"type": "number",
                "multipleOf": round(10 ** -detail["decimals"],
                                    detail["decimals"])}
    return {"type": "integer"}


def spec_data(store: Store, cfg: Config, unit: dict) -> dict:
    """Everything the spec (and codegen) needs, assembled once."""
    programs = json.loads(unit["programs_json"] or "[]")
    pset = set(programs)
    edges = [dict(r) | {"detail": json.loads(r["detail_json"] or "{}")}
             for r in store.query(
                 "SELECT e.edge_type et, e.detail_json, s.name sn,"
                 " s.node_type st, d.name dn, d.node_type dt FROM edges e"
                 " JOIN nodes s ON s.id=e.src_node"
                 " JOIN nodes d ON d.id=e.dst_node")]
    jobs = sorted({e["sn"].split(".")[0] for e in edges
                   if e["et"] == "calls" and e["st"] == "step"
                   and e["dn"] in pset})
    screens = sorted({e["dn"] for e in edges
                      if e["et"] == "displays" and e["sn"] in pset})
    transactions = sorted({e["sn"] for e in edges
                           if e["et"] == "triggers" and e["dn"] in pset
                           and e["st"] == "transaction"} |
                          {e["dn"] for e in edges
                           if e["et"] == "triggers" and e["sn"] in pset
                           and e["dt"] == "transaction"})
    data_access = [e for e in edges
                   if e["et"] in ("reads", "writes") and e["sn"] in pset
                   and e["st"] == "program"]
    datasets = sorted({e["dn"] for e in data_access if e["dt"] == "dataset"})
    tables = sorted({e["dn"] for e in data_access if e["dt"] == "table"})
    queues = sorted({e["dn"] for e in data_access if e["dt"] == "queue"})

    # interfaces: screens -> REST, queues -> events, dataset in/out -> files
    interfaces = []
    for s in screens:
        interfaces.append({"kind": "screen", "legacy": s,
                           "proposal": f"REST resource (GET/POST) replacing "
                                       f"BMS map {s}"})
    for q in queues:
        interfaces.append({"kind": "queue", "legacy": q,
                           "proposal": f"event topic replacing MQ queue {q}"})
    for d in datasets:
        direction = "in/out"
        reads = any(e["dn"] == d and e["et"] == "reads" for e in data_access)
        writes = any(e["dn"] == d and e["et"] == "writes" for e in data_access)
        direction = "in" if reads and not writes else \
            "out" if writes and not reads else "in/out"
        interfaces.append({"kind": "dataset", "legacy": d,
                           "proposal": f"batch file interface ({direction}); "
                                       "candidate for API or table replacement"})

    # data model: records from copybooks included by unit programs + FD records
    records = []
    copybooks = sorted({e["dn"] for e in edges
                        if e["et"] == "includes" and e["sn"] in pset})
    for cb in copybooks:
        rows = store.query(
            "SELECT f.name, f.detail_json, f.source_line_start, a.path"
            " FROM facts f JOIN artifacts a ON a.id=f.artifact_id"
            " WHERE f.fact_type='field' AND a.artifact_type='copybook'"
            " AND UPPER(a.path) LIKE ? ORDER BY f.id", (f"%{cb}%",))
        fields = []
        group = cb
        for r in rows:
            d = json.loads(r["detail_json"] or "{}")
            if d.get("level") == 1:
                group = r["name"]
                continue
            if not d.get("pic"):
                continue
            fields.append({"legacy_name": r["name"], "source": r["path"],
                           "line": r["source_line_start"], **d,
                           "java_name": _java_name(r["name"]),
                           "java_name_cap": _java_name(r["name"], cap=True),
                           "java_type": java_type(d),
                           "json": json_type(d)})
        if fields:
            records.append({"legacy_name": group, "copybook": cb,
                            "source": rows[0]["path"] if rows else "",
                            "class_name": _java_name(group, cap=True),
                            "fields": fields})
    for prog in programs:
        rows = store.query(
            "SELECT f.name, f.detail_json, f.source_line_start, a.path"
            " FROM facts f JOIN artifacts a ON a.id=f.artifact_id"
            " JOIN facts p ON p.artifact_id=a.id AND p.fact_type='program'"
            " AND p.name=? WHERE f.fact_type='data_item' ORDER BY f.id",
            (prog,))
        current = None
        for r in rows:
            d = json.loads(r["detail_json"] or "{}")
            # program-level data_item facts carry PIC but not the storage
            # metadata the copybook adapter computes — derive it here
            from .adapters.copybook import pic_meta
            d |= pic_meta(d.get("pic", ""), d.get("usage", ""))
            if d.get("level") == 1 and d.get("fd"):
                current = {"legacy_name": r["name"], "copybook": "",
                           "source": r["path"],
                           "class_name": _java_name(r["name"], cap=True),
                           "fields": []}
                records.append(current)
            elif current is not None and d.get("parent") == \
                    current["legacy_name"] and d.get("pic"):
                current["fields"].append(
                    {"legacy_name": r["name"], "source": r["path"],
                     "line": r["source_line_start"], **d,
                     "java_name": _java_name(r["name"]),
                     "java_name_cap": _java_name(r["name"], cap=True),
                     "java_type": java_type(d),
                     "json": json_type(d)})
    records = [r for r in records if r["fields"]]

    rules_approved = [dict(r) for r in store.query(
        "SELECT * FROM rules WHERE program IN (%s) AND status='approved'"
        " ORDER BY program, source_lines"
        % ",".join("?" * len(programs)), programs)] if programs else []
    rules_other = store.query(
        "SELECT status, COUNT(*) c FROM rules WHERE program IN (%s)"
        " AND status<>'approved' GROUP BY status"
        % ",".join("?" * len(programs)), programs) if programs else []

    volumes = [dict(r) for r in store.query(
        "SELECT * FROM dataset_stats WHERE dataset IN (%s)"
        % ",".join("?" * len(datasets)), datasets)] if datasets else []

    missing = [dict(r) for r in store.query(
        "SELECT * FROM missing_artifacts WHERE referenced_by IN (%s)"
        % ",".join("?" * (len(programs) + len(jobs))),
        programs + jobs)] if programs or jobs else []
    dynamic_calls = [dict(r) | {"detail": json.loads(r["detail_json"] or "{}")}
                     for r in store.query(
                         "SELECT f.name, f.detail_json, f.source_line_start"
                         " FROM facts f JOIN facts p ON"
                         " p.artifact_id=f.artifact_id AND"
                         " p.fact_type='program' WHERE f.fact_type='calls'"
                         " AND p.name IN (%s)" % ",".join("?" * len(programs)),
                         programs)] if programs else []
    dynamic_calls = [c for c in dynamic_calls if c["detail"].get("dynamic")]
    redefines = [f for rec in records for f in rec["fields"]
                 if f.get("redefines")]

    return {"unit": dict(unit), "programs": programs, "jobs": jobs,
            "screens": screens, "transactions": transactions,
            "datasets": datasets, "tables": tables, "queues": queues,
            "interfaces": interfaces, "records": records,
            "rules_approved": rules_approved,
            "rules_other": [dict(r) for r in rules_other],
            "volumes": volumes, "missing": missing,
            "dynamic_calls": dynamic_calls, "redefines": redefines,
            "evidence": json.loads(unit["disposition_evidence_json"] or "{}")}


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        sync_units_from_csv(store, cfg)
        unit = find_unit(store, args.unit)
        if not unit:
            print(f"spec: no unit {args.unit!r} - run `legacymod decompose` "
                  "and check units.csv")
            return 1
        if unit["status"] == "proposed":
            print(f"spec: unit {unit['name']} is status=proposed - HITL gate: "
                  "edit workspace/units.csv status to 'approved' first")
            return 1
        data = spec_data(store, cfg, unit)
        env = jinja_env()
        out = cfg.workspace / "specs" / f"{unit['name']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(env.get_template("docs/spec.md.j2").render(**data),
                       encoding="utf-8")
        if unit["status"] == "approved":
            store.execute("UPDATE units SET status='spec_done' WHERE unit_id=?",
                          (unit["unit_id"],))
            store.commit()
            store.export_csv(
                "SELECT unit_id, name, domain, programs_json, status,"
                " disposition, effort_tshirt, disposition_evidence_json"
                " FROM units ORDER BY unit_id", cfg.workspace / "units.csv")
        print(f"spec: {out} written "
              f"({len(data['rules_approved'])} approved rule(s) verbatim, "
              f"{len(data['records'])} record layout(s), "
              f"{len(data['interfaces'])} interface(s)); unit -> spec_done")
    return 0
