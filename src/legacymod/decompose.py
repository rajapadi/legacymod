"""Stage 6 — domain decomposition, migratable units, wave plan.

Seed-based clustering: ``workspace/domains.seed.csv`` (domain,program)
labels seed programs; hand-rolled label propagation over call + shared-
data edges spreads labels to the rest; anything untouched lands in
``unassigned``. One migratable unit per domain.

HITL gate: units are written ``status=proposed`` to ``units.csv`` — the
CSV is the review surface. A human edits status to ``approved``; spec /
datamig / generate sync the CSV back and refuse non-approved units.

Each unit also gets a **disposition recommendation** (refactor /
replatform / rehost / retire / retain, default undecided) justified from
blockers + metrics in ``disposition_evidence_json`` — a recommendation,
never a decision.

Wave plan: units ordered by afferent coupling (inbound calls from
outside the unit; fewest first) -> ``waves.csv``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_SEED_FILE = "domains.seed.csv"
_SEED_TEMPLATE = """domain,program
# One row per seed: the domain label, and a program that anchors it.
# Everything reachable through calls/shared data inherits the nearest label.
# Example:
# payroll,PAYCALC
"""


def _program_links(store: Store) -> tuple[set[str], dict[str, set[str]]]:
    """Programs with source + undirected adjacency via calls/shared data."""
    progs = {r["name"] for r in store.query(
        "SELECT name FROM nodes WHERE node_type='program'"
        " AND artifact_id IS NOT NULL")}
    adj: dict[str, set[str]] = defaultdict(set)
    for e in store.query(
            "SELECT s.name sn, s.node_type st, d.name dn, d.node_type dt,"
            " e.edge_type et FROM edges e"
            " JOIN nodes s ON s.id=e.src_node JOIN nodes d ON d.id=e.dst_node"):
        if e["et"] == "calls" and e["st"] == "program" and e["dt"] == "program":
            if e["sn"] in progs and e["dn"] in progs:
                adj[e["sn"]].add(e["dn"])
                adj[e["dn"]].add(e["sn"])
    # shared data resources link programs (payroll batch <-> inquiry etc.)
    touch: dict[str, set[str]] = defaultdict(set)
    for e in store.query(
            "SELECT s.name sn, s.node_type st, d.name dn, d.node_type dt"
            " FROM edges e JOIN nodes s ON s.id=e.src_node"
            " JOIN nodes d ON d.id=e.dst_node"
            " WHERE e.edge_type IN ('reads', 'writes')"):
        if e["st"] == "program" and e["dt"] in ("dataset", "table", "file",
                                                "queue"):
            touch[e["dn"]].add(e["sn"])
    for users in touch.values():
        users = users & progs
        for a in users:
            for b in users:
                if a != b:
                    adj[a].add(b)
    return progs, adj


def _propagate(progs: set[str], adj: dict[str, set[str]],
               seeds: dict[str, str]) -> dict[str, str]:
    labels = dict(seeds)
    for _ in range(20):
        changed = False
        for p in sorted(progs):
            if p in seeds:
                continue
            votes = Counter(labels[n] for n in adj.get(p, ()) if n in labels)
            if votes:
                best = votes.most_common(1)[0][0]
                if labels.get(p) != best:
                    labels[p] = best
                    changed = True
        if not changed:
            break
    for p in progs:
        labels.setdefault(p, "unassigned")
    return labels


def _disposition(programs: list[str], blockers: dict[str, list[dict]],
                 referenced: set[str],
                 metrics: dict[str, dict]) -> tuple[str, dict]:
    unit_blockers = [dict(b, program=p) for p in programs
                     for b in blockers.get(p, [])]
    evidence: dict = {"blockers": unit_blockers,
                      "programs": programs,
                      "metrics": {p: metrics.get(p, {}) for p in programs}}
    kinds = {b["blocker_type"] for b in unit_blockers}
    if not kinds and programs and all(p not in referenced for p in programs):
        evidence["reason"] = ("no static references to any program in the "
                              "unit - decommission candidate; confirm with "
                              "run history before retiring")
        return "retire", evidence
    if "enter_tal" in kinds or "pathway_serverclass" in kinds:
        evidence["reason"] = ("NonStop-specific constructs (ENTER TAL / "
                              "Pathway SERVERCLASS) block a straight "
                              "refactor; replatform or wrap first. Blockers "
                              "listed above cite the exact lines.")
        return "replatform", evidence
    if "assembler_call" in kinds or "low_level_io" in kinds:
        evidence["reason"] = ("assembler/low-level dependencies pin this "
                              "unit to the platform until they are replaced; "
                              "rehost preserves behavior meanwhile")
        return "rehost", evidence
    evidence["reason"] = ("no migration blockers found (in particular no "
                          "assembler CALLs such as ASMXIT01); complexity is "
                          "manageable per metrics - candidate for full "
                          "refactor to a modern service")
    return "refactor", evidence


def sync_units_from_csv(store: Store, cfg: Config) -> None:
    """Pull human status edits from units.csv back into the store."""
    path = cfg.workspace / "units.csv"
    if not path.is_file():
        return
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("unit_id") and row.get("status"):
                store.execute("UPDATE units SET status=? WHERE unit_id=?"
                              " AND status<>?",
                              (row["status"], row["unit_id"], row["status"]))
    store.commit()


def run(args: argparse.Namespace, cfg: Config) -> int:
    seed_path = cfg.workspace / _SEED_FILE
    if not seed_path.is_file():
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(_SEED_TEMPLATE, encoding="utf-8")
        print(f"decompose: no seeds yet - template written to {seed_path}")
        print("  add rows like `payroll,PAYCALC`, then re-run "
              "`legacymod decompose`")
        return 0
    seeds: dict[str, str] = {}
    with open(seed_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#") or row[0] == "domain":
                continue
            if len(row) >= 2:
                seeds[row[1].strip().upper()] = row[0].strip()

    with Store(cfg) as store:
        sync_units_from_csv(store, cfg)   # keep prior human edits
        progs, adj = _program_links(store)
        unknown = {p for p in seeds if p not in progs}
        for p in sorted(unknown):
            log.warning("seed program %s not found in the estate - ignored", p)
            seeds.pop(p)
        labels = _propagate(progs, adj, seeds)

        blockers: dict[str, list[dict]] = defaultdict(list)
        for r in store.query("SELECT * FROM blockers"):
            blockers[r["program_or_job"]].append(
                {"blocker_type": r["blocker_type"],
                 "evidence_line": r["evidence_line"], "detail": r["detail"]})
        metrics = {r["program"]: {"cyclomatic": r["cyclomatic"],
                                  "loc": r["loc"]}
                   for r in store.query("SELECT * FROM metrics")}
        referenced = {r["dn"] for r in store.query(
            "SELECT d.name dn FROM edges e JOIN nodes d ON d.id=e.dst_node"
            " WHERE e.edge_type IN ('calls', 'triggers')"
            " AND d.node_type='program'")}
        calls = [(r["sn"], r["dn"]) for r in store.query(
            "SELECT s.name sn, d.name dn FROM edges e"
            " JOIN nodes s ON s.id=e.src_node JOIN nodes d ON d.id=e.dst_node"
            " WHERE e.edge_type='calls' AND s.node_type='program'"
            " AND d.node_type='program'")]

        existing = {r["name"]: dict(r) for r in store.query("SELECT * FROM units")}
        by_domain: dict[str, list[str]] = defaultdict(list)
        for p, dom in sorted(labels.items()):
            by_domain[dom].append(p)

        units = []
        for i, (dom, programs) in enumerate(sorted(by_domain.items()), 1):
            unit_id = f"U{i:03d}"
            disposition, evidence = _disposition(programs, blockers,
                                                 referenced, metrics)
            afferent = sum(1 for src, dst in calls
                           if dst in programs and src not in programs)
            score = sum((metrics.get(p, {}).get("loc") or 0) / 100
                        + (metrics.get(p, {}).get("cyclomatic") or 0) / 5
                        for p in programs) + 2 * sum(len(blockers.get(p, []))
                                                     for p in programs)
            size = "S" if score < 2 else "M" if score < 5 else \
                "L" if score < 10 else "XL"
            old = existing.get(dom)
            status = old["status"] if old else "proposed"
            units.append({"unit_id": unit_id, "name": dom, "domain": dom,
                          "programs": programs, "status": status,
                          "disposition": disposition, "evidence": evidence,
                          "effort": size, "afferent": afferent})

        store.execute("DELETE FROM units")
        for u in units:
            store.execute(
                "INSERT INTO units (unit_id, name, domain, programs_json,"
                " status, disposition, disposition_evidence_json,"
                " effort_tshirt) VALUES (?,?,?,?,?,?,?,?)",
                (u["unit_id"], u["name"], u["domain"],
                 json.dumps(u["programs"]), u["status"], u["disposition"],
                 json.dumps(u["evidence"]), u["effort"]))
        store.commit()
        store.export_csv(
            "SELECT unit_id, name, domain, programs_json, status,"
            " disposition, effort_tshirt, disposition_evidence_json"
            " FROM units ORDER BY unit_id", cfg.workspace / "units.csv")

        waves_path = cfg.workspace / "waves.csv"
        wave_no = 0
        last_coupling = None
        with open(waves_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["wave", "unit_id", "name", "afferent_coupling",
                        "disposition", "effort_tshirt"])
            for u in sorted(units, key=lambda u: (u["afferent"], u["unit_id"])):
                if u["afferent"] != last_coupling:
                    wave_no += 1
                    last_coupling = u["afferent"]
                w.writerow([wave_no, u["unit_id"], u["name"], u["afferent"],
                            u["disposition"], u["effort"]])

        print(f"decompose: {len(units)} unit(s) from {len(seeds)} seed(s) -> "
              f"units.csv (HITL gate: edit status to 'approved'), waves.csv")
        for u in sorted(units, key=lambda u: u["afferent"]):
            print(f"  {u['unit_id']} {u['name']:12s} programs={len(u['programs'])} "
                  f"afferent={u['afferent']} disposition={u['disposition']} "
                  f"size={u['effort']} status={u['status']}")
    return 0
