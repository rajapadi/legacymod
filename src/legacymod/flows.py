"""Stage 13 — the complete picture: streams, navigation, shared
resources, capabilities.

(a) Batch stream flow: per connected component of the schedule graph, a
    Mermaid flow with scheduler start times/calendars on nodes, dataset
    handoffs on edges, external dependencies as distinct nodes.
(b) CICS navigation: transaction -> program (CSD) -> XCTL/LINK chains ->
    SEND/RECEIVE MAP screens -> RETURN TRANSID hops.
(c) Online<->batch dependency map: CICS FILEs resolved to datasets via
    CSD DSNAME, DB2 tables and MQ queues intersected with batch access;
    every shared resource becomes an explicit ``shares_resource`` edge —
    the raw material for batch-window and contention analysis.
(d) Capability view: ``workspace/capabilities.csv`` seeds a roll-up page
    per capability (programs, jobs, screens, rules, interfaces, volumes,
    reconcile status).

Outputs under ``workspace/flows/``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_CAP_TEMPLATE = """capability,seed_type,seed_name
# One row per business capability; seed_type is program|job|transaction.
# Example:
# Payroll Calculation,program,PAYCALC
"""


def _edges(store: Store) -> list[dict]:
    return [dict(r) | {"detail": json.loads(r["detail_json"] or "{}")}
            for r in store.query(
                "SELECT e.edge_type et, e.detail_json, s.name sn,"
                " s.node_type st, d.name dn, d.node_type dt FROM edges e"
                " JOIN nodes s ON s.id=e.src_node"
                " JOIN nodes d ON d.id=e.dst_node")]


def _mid(kind: str, name: str) -> str:
    return kind[:2] + "_" + "".join(c if c.isalnum() else "_" for c in name)


def batch_streams(store: Store, out: Path) -> str:
    edges = _edges(store)
    sched = {r["name"]: json.loads(r["detail_json"] or "{}")
             for r in store.query("SELECT name, detail_json FROM facts"
                                  " WHERE fact_type='sched_job'")}
    precedes = [(e["sn"], e["dn"], e["st"]) for e in edges
                if e["et"] == "precedes"]
    # job-level dataset handoffs
    writers: dict[str, set[str]] = defaultdict(set)
    readers: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e["et"] in ("reads", "writes") and e["dt"] == "dataset":
            job = e["detail"].get("job") or (e["sn"].split(".")[0]
                                             if e["st"] == "step" else "")
            if job:
                (writers if e["et"] == "writes" else readers)[e["dn"]].add(job)
    handoffs = [(w, ds, r) for ds in writers for w in writers[ds]
                for r in readers.get(ds, set()) if r != w]

    # connected components over schedule nodes
    adj: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set(sched)
    for a, b, _ in precedes:
        adj[a].add(b)
        adj[b].add(a)
        nodes |= {a, b}
    for w, _, r in handoffs:
        if w in nodes or r in nodes:
            adj[w].add(r)
            adj[r].add(w)
            nodes |= {w, r}
    comps: list[set[str]] = []
    seen: set[str] = set()
    for n in sorted(nodes):
        if n in seen:
            continue
        comp = {n}
        stack = [n]
        while stack:
            for m in adj[stack.pop()]:
                if m not in comp:
                    comp.add(m)
                    stack.append(m)
        seen |= comp
        comps.append(comp)

    lines = ["flowchart LR"]
    externals = {e["sn"] for e in edges
                 if e["et"] == "precedes" and e["st"] == "external_node"}
    for i, comp in enumerate(comps, 1):
        lines.append(f"  subgraph stream_{i}")
        for job in sorted(comp):
            if job in externals:
                continue
            d = sched.get(job, {})
            note = ""
            if d.get("time"):
                note = f"<br/>TIME={d['time']}"
            if d.get("calendar"):
                note += f"<br/>{d['calendar']}"
            lines.append(f'    {_mid("job", job)}["{job}{note}"]')
        lines.append("  end")
    for ext in sorted(externals):
        lines.append(f'  {_mid("ext", ext)}(("external: {ext}"))')
    for a, b, st in precedes:
        src = _mid("ext" if st == "external_node" else "job", a)
        lines.append(f"  {src} -->|precedes| {_mid('job', b)}")
    for w, ds, r in sorted(set(handoffs)):
        lines.append(f"  {_mid('job', w)} -->|\"{ds}\"| {_mid('job', r)}")
    text = "\n".join(lines) + "\n"
    (out / "batch_streams.mmd").write_text(text, encoding="utf-8")
    return text


def cics_navigation(store: Store, out: Path) -> str:
    edges = _edges(store)
    lines = ["flowchart LR"]
    shown: set[str] = set()

    def node(kind: str, name: str) -> str:
        mid = _mid(kind, name)
        if mid not in shown:
            shown.add(mid)
            shape = {"transaction": ('(["', '"])'), "screen": ('[/"', '"/]'),
                     "program": ('["', '"]')}.get(kind, ('["', '"]'))
            lines.append(f"  {mid}{shape[0]}{kind}: {name}{shape[1]}")
        return mid

    for e in edges:
        if e["et"] == "triggers" and e["st"] == "transaction":
            lines.append(f"  {node('transaction', e['sn'])} --> "
                         f"{node('program', e['dn'])}")
        elif e["et"] == "triggers" and e["dt"] == "transaction":
            lines.append(f"  {node('program', e['sn'])} -->|RETURN TRANSID| "
                         f"{node('transaction', e['dn'])}")
        elif e["et"] == "displays":
            lines.append(f"  {node('program', e['sn'])} -->|"
                         f"{e['detail'].get('command', 'MAP')}| "
                         f"{node('screen', e['dn'])}")
        elif e["et"] == "calls" and e["st"] == "program" \
                and e["dt"] == "program" and \
                e["detail"].get("via", "").split()[:1] in (["LINK"], ["XCTL"]):
            lines.append(f"  {node('program', e['sn'])} -->|"
                         f"{e['detail']['via']}| {node('program', e['dn'])}")
    text = "\n".join(lines) + "\n"
    (out / "cics_navigation.mmd").write_text(text, encoding="utf-8")
    return text


def online_batch_map(store: Store, out: Path) -> list[tuple[str, str, str]]:
    """shares_resource edges between online and batch users of a resource."""
    edges = _edges(store)
    file_to_dsn = {e["sn"]: e["dn"] for e in edges
                   if e["et"] == "binds" and e["st"] == "file"
                   and e["dt"] == "dataset"}
    online_progs = {r["name"] for r in store.query(
        "SELECT DISTINCT p.name FROM facts f JOIN facts p"
        " ON p.artifact_id=f.artifact_id AND p.fact_type='program'"
        " WHERE f.fact_type='cics'")}
    batch_progs = {e["dn"] for e in edges
                   if e["et"] == "calls" and e["st"] == "step"
                   and e["dt"] == "program"}

    users: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"via": set(), "progs": set()})
    for e in edges:
        if e["st"] != "program" or e["et"] not in ("reads", "writes"):
            continue
        prog = e["sn"]
        if e["dt"] in ("dataset", "table", "queue"):
            res = (e["dt"], e["dn"])
            users[res]["progs"].add(prog)
            users[res]["via"].add("direct")
        elif e["dt"] == "file" and e["dn"] in file_to_dsn:
            res = ("dataset", file_to_dsn[e["dn"]])
            users[res]["progs"].add(prog)
            users[res]["via"].add(f"CSD FILE {e['dn']}")

    store.execute("DELETE FROM edges WHERE edge_type='shares_resource'")
    shared: list[tuple[str, str, str]] = []
    for (kind, res), info in sorted(users.items()):
        progs = sorted(info["progs"])
        if len(progs) < 2:
            continue
        for i, a in enumerate(progs):
            for b in progs[i + 1:]:
                aid = store.node_id("program", a)
                bid = store.node_id("program", b)
                store.add_edge(aid, bid, "shares_resource",
                               {"resource": res, "kind": kind,
                                "via": sorted(info["via"]),
                                "online_batch": (a in online_progs) !=
                                                (b in online_progs)
                                or (a in batch_progs) != (b in batch_progs)})
                shared.append((a, b, res))
    store.commit()
    lines = ["# Online <-> batch shared resources", ""]
    if shared:
        lines.append("| program A | program B | shared resource |")
        lines.append("|---|---|---|")
        lines += [f"| {a} | {b} | {r} |" for a, b, r in shared]
    else:
        lines.append("No shared resources found.")
    (out / "online_batch.md").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
    return shared


def capability_pages(store: Store, cfg: Config, out: Path) -> int:
    cap_path = cfg.workspace / "capabilities.csv"
    if not cap_path.is_file():
        cap_path.write_text(_CAP_TEMPLATE, encoding="utf-8")
        print(f"flows: no capabilities yet - template written to {cap_path}")
        return 0
    store.execute("DELETE FROM capabilities")
    caps = []
    with open(cap_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#") or row[0] == "capability":
                continue
            if len(row) >= 3:
                caps.append((row[0].strip(), row[1].strip().lower(),
                             row[2].strip().upper()))
                store.execute("INSERT INTO capabilities VALUES (?,?,?)",
                              caps[-1])
    store.commit()
    edges = _edges(store)

    for capability, seed_type, seed_name in caps:
        # expand from the seed over calls/triggers/belongs_to edges
        programs: set[str] = set()
        jobs: set[str] = set()
        screens: set[str] = set()
        frontier: list[tuple[str, str]] = [(seed_type
                                            if seed_type != "transaction"
                                            else "transaction", seed_name)]
        visited: set[tuple[str, str]] = set()
        while frontier:
            kind, name = frontier.pop()
            if (kind, name) in visited:
                continue
            visited.add((kind, name))
            if kind == "program":
                programs.add(name)
            elif kind == "job":
                jobs.add(name)
            elif kind == "screen":
                screens.add(name)
            for e in edges:
                if e["sn"] == name and e["st"] == kind:
                    if e["et"] in ("calls", "triggers", "displays") and \
                            e["dt"] in ("program", "screen", "transaction",
                                        "job"):
                        frontier.append((e["dt"], e["dn"]))
                if e["dn"] == name and e["dt"] == kind:
                    if e["et"] == "calls" and e["st"] == "step":
                        frontier.append(("job", e["sn"].split(".")[0]))
                    elif e["et"] in ("calls", "triggers") and \
                            e["st"] in ("program", "transaction"):
                        frontier.append((e["st"], e["sn"]))
                    elif e["et"] == "belongs_to" and e["st"] == "step":
                        pass
        qp = ",".join("?" * len(programs)) or "''"
        rules = store.query(
            f"SELECT rule_id, category, status FROM rules"
            f" WHERE program IN ({qp})", sorted(programs)) if programs else []
        qj = sorted(jobs | programs)
        qjp = ",".join("?" * len(qj)) or "''"
        ifaces = store.query(
            f"SELECT * FROM interfaces WHERE source_job_or_program IN ({qjp})",
            qj) if qj else []
        datasets = sorted({e["dn"] for e in edges
                           if e["st"] == "program" and e["sn"] in programs
                           and e["dt"] == "dataset"} |
                          {e["dn"] for e in edges
                           if e["st"] == "step"
                           and e["sn"].split(".")[0] in jobs
                           and e["dt"] == "dataset"})
        qd = ",".join("?" * len(datasets)) or "''"
        vols = store.query(
            f"SELECT * FROM dataset_stats WHERE dataset IN ({qd})",
            datasets) if datasets else []
        recon = store.query(
            f"SELECT name, kind, status FROM reconcile"
            f" WHERE name IN ({qjp})", qj) if qj else []

        slug = "".join(c if c.isalnum() else "_" for c in capability.lower())

        def _list(items: list[str]) -> list[str]:
            return items if items else ["- (none)"]

        lines = [
            f"# Capability: {capability}",
            "",
            f"**Verdict up front:** seeded from {seed_type} {seed_name}; "
            f"{len(programs)} program(s), {len(jobs)} job(s), "
            f"{len(rules)} rule(s), {len(ifaces)} interface(s), "
            f"{len(vols)} dataset volume record(s). Reconcile status below "
            "flags anything not verifiably healthy.",
            "",
            "## Programs",
            *_list([f"- {p}" for p in sorted(programs)]),
            "", "## Jobs", *_list([f"- {j}" for j in sorted(jobs)]),
            "", "## Screens", *_list([f"- {s}" for s in sorted(screens)]),
            "", "## Rules"]
        lines += [f"- {r['rule_id']} ({r['category']}, {r['status']})"
                  for r in rules] or ["- (none mined)"]
        lines += ["", "## Interfaces"]
        lines += [f"- [{i['protocol']}] {i['dataset_or_queue']} -> "
                  f"{i['target_node'] or '(internal)'} "
                  f"({i['frequency']}, source {i['frequency_source']})"
                  f"{' EXTERNAL' if i['external'] else ''}"
                  for i in ifaces] or ["- (none)"]
        lines += ["", "## Volumes"]
        lines += [f"- {v['dataset']}: {v['avg_bytes_per_run']} bytes / "
                  f"{v['records_per_run']} records per run "
                  f"(as-of {v['as_of']})" for v in vols] or \
                 ["- (no dataset stats loaded)"]
        lines += ["", "## Reconcile status"]
        lines += [f"- {r['kind']} {r['name']}: {r['status']}"
                  for r in recon] or ["- (no reconcile findings for this "
                                      "capability's jobs/programs)"]
        (out / f"capability_{slug}.md").write_text("\n".join(lines) + "\n",
                                                   encoding="utf-8")
    return len(caps)


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        out = cfg.workspace / "flows"
        out.mkdir(parents=True, exist_ok=True)
        streams = batch_streams(store, out)
        nav = cics_navigation(store, out)
        shared = online_batch_map(store, out)
        ncaps = capability_pages(store, cfg, out)
        print(f"flows: batch_streams.mmd ({streams.count(chr(10))} lines), "
              f"cics_navigation.mmd ({nav.count(chr(10))} lines), "
              f"{len(shared)} shares_resource edge(s), "
              f"{ncaps} capability page(s) -> {out}")
        for a, b, r in shared:
            print(f"  shares_resource: {a} <-> {b} via {r}")
    return 0
