"""Stage 4 — current-state documentation generated from the graph.

Deterministic markdown under ``workspace/docs/``: system overview,
per-program pages (calls, data, screens, rules, CRUD row), job-flow
pages, and an estate-wide CRUD matrix (program x table). With
``--enrich``, LLM narrative summaries are appended in clearly marked
``> AI-generated`` blocks (provider, model, date, confidence) — never
silently mixed into deterministic content.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_CRUD_OP = {"INSERT": "C", "SELECT": "R", "UPDATE": "U", "DELETE": "D",
            "DECLARE CURSOR": "R"}


def _edges_with_names(store: Store) -> list[dict]:
    rows = store.query(
        "SELECT e.edge_type, e.detail_json, s.node_type st, s.name sn,"
        " d.node_type dt, d.name dn FROM edges e"
        " JOIN nodes s ON s.id=e.src_node JOIN nodes d ON d.id=e.dst_node")
    return [dict(r) | {"detail": json.loads(r["detail_json"] or "{}")}
            for r in rows]


def crud_matrix(store: Store) -> dict[str, dict[str, str]]:
    """program -> table -> CRUD letters, from SQL facts."""
    matrix: dict[str, dict[str, str]] = defaultdict(dict)
    for f in store.query(
            "SELECT f.name tbl, f.detail_json, p.name prog FROM facts f"
            " JOIN facts p ON p.artifact_id=f.artifact_id"
            "  AND p.fact_type='program'"
            " WHERE f.fact_type='sql'"):
        op = json.loads(f["detail_json"] or "{}").get("op", "")
        letter = _CRUD_OP.get(op, "")
        if not letter:
            continue
        cur = matrix[f["prog"]].get(f["tbl"], "")
        if letter not in cur:
            matrix[f["prog"]][f["tbl"]] = "".join(
                sorted(cur + letter, key="CRUD".index))
    return matrix


def _overview(store: Store, docs: Path) -> None:
    inv = store.query(
        "SELECT artifact_type, COUNT(*) c, SUM(loc) loc,"
        " SUM(CASE WHEN adapter IS NOT NULL THEN 1 ELSE 0 END) parsed,"
        " SUM(parse_errors) errs FROM artifacts GROUP BY artifact_type"
        " ORDER BY artifact_type")
    total = sum(r["c"] for r in inv)
    parsed = sum(r["parsed"] or 0 for r in inv)
    nodes = store.query("SELECT COUNT(*) c FROM nodes")[0]["c"]
    edges = store.query("SELECT COUNT(*) c FROM edges")[0]["c"]
    nrules = store.query("SELECT COUNT(*) c FROM rules")[0]["c"]
    pct = (100 * parsed // total) if total else 0
    lines = [
        "# System overview",
        "",
        f"**Verdict up front:** {total} artifacts inventoried, {parsed} "
        f"({pct}%) parsed by a registered adapter into {nodes} graph nodes "
        f"and {edges} edges; {nrules} business-rule candidates mined so far. "
        "Everything below is generated deterministically from parsed facts — "
        "line-level traceability, no narrative embellishment.",
        "",
        "| artifact type | files | LOC | parsed | parse errors |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in inv:
        lines.append(f"| {r['artifact_type']} | {r['c']} | {r['loc'] or 0} |"
                     f" {r['parsed'] or 0} | {r['errs'] or 0} |")
    lines += ["", "- Per-program pages: `programs/`",
              "- Job pages: `jobs/`",
              "- CRUD matrix: [crud.md](crud.md)", ""]
    (docs / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _crud_page(store: Store, docs: Path) -> None:
    matrix = crud_matrix(store)
    tables = sorted({t for m in matrix.values() for t in m})
    lines = ["# CRUD matrix (program x DB2 table)", ""]
    if not tables:
        lines.append("No SQL table access found.")
    else:
        lines.append("| program | " + " | ".join(tables) + " |")
        lines.append("|---|" + "---|" * len(tables))
        for prog in sorted(matrix):
            row = [matrix[prog].get(t, "") for t in tables]
            lines.append(f"| {prog} | " + " | ".join(row) + " |")
    (docs / "crud.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _program_pages(store: Store, docs: Path, enrich: bool, cfg: Config) -> int:
    edges = _edges_with_names(store)
    matrix = crud_matrix(store)
    progs = store.query(
        "SELECT n.name, a.path, a.loc, a.parse_errors FROM nodes n"
        " LEFT JOIN artifacts a ON a.id=n.artifact_id"
        " WHERE n.node_type='program' AND n.artifact_id IS NOT NULL"
        " ORDER BY n.name")
    pdir = docs / "programs"
    pdir.mkdir(parents=True, exist_ok=True)
    for p in progs:
        name = p["name"]
        mine_out = [e for e in edges if e["sn"] == name and e["st"] == "program"]
        mine_in = [e for e in edges if e["dn"] == name and e["dt"] == "program"]
        calls = [e for e in mine_out if e["edge_type"] == "calls"]
        callers = [e for e in mine_in if e["edge_type"] in ("calls", "triggers")]
        data = [e for e in mine_out
                if e["edge_type"] in ("reads", "writes")
                and e["dt"] in ("dataset", "file", "table", "queue")]
        screens = [e for e in mine_out if e["edge_type"] == "displays"]
        copies = [e for e in mine_out if e["edge_type"] == "includes"]
        rules = store.query(
            "SELECT rule_id, source_lines, category, status FROM rules"
            " WHERE program=? ORDER BY source_lines", (name,))
        lines = [
            f"# Program {name}",
            "",
            f"**Summary:** {p['loc'] or '?'} LOC ({p['path']}); "
            f"{len(callers)} inbound reference(s), {len(calls)} outbound "
            f"call(s), touches {len(data)} data resource(s); "
            f"{len(rules)} mined rule candidate(s); "
            f"parse errors: {p['parse_errors'] or 0}.",
            "",
            "## Referenced by"]
        lines += [f"- {e['st']} {e['sn']} ({e['edge_type']}"
                  + (f", via {e['detail'].get('via')}" if e['detail'].get('via') else "")
                  + ")" for e in callers] or ["- (none found)"]
        lines += ["", "## Calls"]
        lines += [f"- {e['dn']}"
                  + (" (dynamic)" if e["detail"].get("dynamic") else "")
                  for e in calls] or ["- (none)"]
        lines += ["", "## Data access"]
        lines += [f"- {e['edge_type']} {e['dt']} {e['dn']}"
                  + (f" ({e['detail'].get('via') or e['detail'].get('op', '')})"
                     if e['detail'].get('via') or e['detail'].get('op') else "")
                  for e in data] or ["- (none)"]
        if name in matrix:
            tables = sorted(matrix[name])
            lines += ["", "### CRUD (this program)",
                      "| table | ops |", "|---|---|"]
            lines += [f"| {t} | {matrix[name][t]} |" for t in tables]
        if screens:
            lines += ["", "## Screens"]
            lines += [f"- {e['dn']} ({e['detail'].get('command', '')})"
                      for e in screens]
        if copies:
            lines += ["", "## Copybooks"]
            lines += [f"- {e['dn']}" for e in copies]
        if rules:
            lines += ["", "## Rule candidates",
                      "| rule | lines | category | status |", "|---|---|---|---|"]
            lines += [f"| {r['rule_id']} | {r['source_lines']} |"
                      f" {r['category']} | {r['status']} |" for r in rules]
        if enrich:
            from .llm import enrich_program_doc
            block = enrich_program_doc(store, cfg, name)
            if block:
                lines += ["", block]
        (pdir / f"{name}.md").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
    return len(progs)


def _job_pages(store: Store, docs: Path) -> int:
    jdir = docs / "jobs"
    jdir.mkdir(parents=True, exist_ok=True)
    jobs = store.query("SELECT name FROM nodes WHERE node_type='job'"
                       " ORDER BY name")
    edges = _edges_with_names(store)
    for j in jobs:
        name = j["name"]
        steps = sorted({e["sn"] for e in edges
                        if e["edge_type"] == "belongs_to" and e["dn"] == name
                        and e["st"] == "step"})
        lines = [f"# Job {name}", ""]
        for s in steps:
            run = [e for e in edges if e["sn"] == s and e["edge_type"] == "calls"]
            binds = [e for e in edges if e["sn"] == s and e["edge_type"] == "binds"]
            io = [e for e in edges if e["sn"] == s
                  and e["edge_type"] in ("reads", "writes")]
            lines.append(f"## {s}")
            for e in run:
                via = e["detail"].get("via", "")
                lines.append(f"- runs program {e['dn']}"
                             + (f" ({via})" if via else ""))
            for e in binds:
                lines.append(f"- DD {e['detail'].get('ddname', '?')} -> "
                             f"{e['dn']} (DISP={e['detail'].get('disp', '')})")
            for e in io:
                lines.append(f"- {e['edge_type']} {e['dn']} "
                             f"({e['detail'].get('via', '')})")
            lines.append("")
        (jdir / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    return len(jobs)


def run(args: argparse.Namespace, cfg: Config) -> int:
    enrich = getattr(args, "enrich", False)
    with Store(cfg) as store:
        docs = cfg.workspace / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        _overview(store, docs)
        _crud_page(store, docs)
        nprog = _program_pages(store, docs, enrich, cfg)
        njob = _job_pages(store, docs)
        print(f"docs: overview + CRUD matrix + {nprog} program pages + "
              f"{njob} job pages -> {docs}")
        if enrich:
            print("  AI enrichment blocks appended (marked, needs_review)")
    return 0
