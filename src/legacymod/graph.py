"""Stage 3 — knowledge graph: build, query, export.

Nodes/edges are derived deterministically from facts (schema in
migrations/001_initial.sql). Derived edges include resolved
program->dataset access (SELECT...ASSIGN ddname joined to the DD cards of
every step that executes the program) — the chain behind impact and
lineage answers. Queries: --impact, --lineage, --dead, --cycles,
--where-used. Exports: workspace/graph.json and workspace/graph.mmd
(whole estate; per-domain subgraphs once decompose has produced units).
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


def _facts(store: Store, *types: str) -> list[dict]:
    q = ",".join("?" for _ in types)
    rows = store.query(
        f"SELECT f.*, a.path AS apath, a.artifact_type FROM facts f "
        f"JOIN artifacts a ON a.id = f.artifact_id "
        f"WHERE f.fact_type IN ({q}) ORDER BY f.artifact_id, f.source_line_start",
        types)
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(r["detail_json"] or "{}")
        out.append(d)
    return out


def build(store: Store) -> None:
    store.execute("DELETE FROM edges")
    store.execute("DELETE FROM nodes")

    prog_artifact: dict[str, int] = {}
    for f in _facts(store, "program"):
        prog_artifact[f["name"]] = f["artifact_id"]
        store.node_id("program", f["name"], f["artifact_id"])

    for f in _facts(store, "paragraph"):
        prog = _program_of(store, f["artifact_id"])
        if not prog:
            continue
        pid = store.node_id("paragraph", f"{prog}.{f['name']}")
        store.add_edge(pid, store.node_id("program", prog), "belongs_to")

    for f in _facts(store, "performs"):
        prog = _program_of(store, f["artifact_id"])
        if not prog:
            continue
        src = store.node_id("paragraph", f"{prog}.{f['detail'].get('paragraph', '')}")
        dst = store.node_id("paragraph", f"{prog}.{f['name']}")
        store.add_edge(src, dst, "performs", {"line": f["source_line_start"]})

    for f in _facts(store, "calls"):
        prog = _program_of(store, f["artifact_id"])
        if not prog:
            continue
        store.add_edge(store.node_id("program", prog),
                       store.node_id("program", f["name"]),
                       "calls",
                       {"dynamic": f["detail"].get("dynamic", 0),
                        "line": f["source_line_start"]},
                       origin=f["origin"])

    for f in _facts(store, "copy"):
        prog = _program_of(store, f["artifact_id"])
        if not prog:
            continue
        store.add_edge(store.node_id("program", prog),
                       store.node_id("copybook", f["name"]),
                       "includes", {"line": f["source_line_start"]})

    # copybook artifacts attach to their nodes by member name
    for row in store.query(
            "SELECT id, path FROM artifacts WHERE artifact_type='copybook'"):
        member = Path(row["path"]).stem.upper()
        store.node_id("copybook", member, row["id"])

    # JCL: jobs, steps, DD bindings, executed programs
    step_pgm: dict[str, str] = {}
    step_dds: dict[str, dict[str, dict]] = defaultdict(dict)
    for f in _facts(store, "job"):
        store.node_id("job", f["name"], f["artifact_id"])
    for f in _facts(store, "step"):
        d = f["detail"]
        sid = store.node_id("step", f["name"], f["artifact_id"])
        store.add_edge(sid, store.node_id("job", d["job"]), "belongs_to")
        if d.get("pgm"):
            step_pgm[f["name"]] = d["pgm"]
            store.add_edge(sid, store.node_id("program", d["pgm"]), "calls",
                           {"via": "EXEC PGM", "cond": d.get("cond", "")})
    for f in _facts(store, "runs_program"):
        d = f["detail"]
        sid = store.node_id("step", f"{d['job']}.{d['step']}")
        store.add_edge(sid, store.node_id("program", f["name"]), "calls",
                       {"via": d.get("via", ""), "plan": d.get("plan", "")})
        step_pgm.setdefault(f"{d['job']}.{d['step']}", f["name"])
    for f in _facts(store, "dd"):
        d = f["detail"]
        if not d.get("dsn"):
            continue
        step_key = f"{d['job']}.{d['step']}"
        step_dds[step_key][d["ddname"]] = d
        sid = store.node_id("step", step_key)
        store.add_edge(sid, store.node_id("dataset", d["dsn"]), "binds",
                       {"ddname": d["ddname"], "disp": d.get("disp", "")})

    # program file access resolved through ddname -> DSN
    selects = defaultdict(list)   # program -> select details
    for f in _facts(store, "select"):
        prog = _program_of(store, f["artifact_id"])
        if prog:
            selects[prog].append(f["detail"])
    modes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for f in _facts(store, "file_open"):
        prog = _program_of(store, f["artifact_id"])
        mode = f["detail"].get("mode", "")
        access = {"INPUT": {"reads"}, "OUTPUT": {"writes"},
                  "EXTEND": {"writes"}, "I-O": {"reads", "writes"}}.get(mode, set())
        if prog:
            modes[(prog, f["name"])] |= access
    for f in _facts(store, "file_read"):
        prog = _program_of(store, f["artifact_id"])
        if prog:
            modes[(prog, f["name"])].add("reads")
    for f in _facts(store, "file_write"):
        prog = _program_of(store, f["artifact_id"])
        if prog:
            modes[(prog, f["name"])].add("writes")
    for prog, sels in selects.items():
        pid = store.node_id("program", prog)
        for sel in sels:
            logical, ddname = sel["file"], sel["ddname"]
            access = modes.get((prog, logical), {"reads"})
            bound = False
            for step_key, pgm in step_pgm.items():
                if pgm != prog:
                    continue
                dd = step_dds.get(step_key, {}).get(ddname)
                if not dd:
                    continue
                bound = True
                for acc in access:
                    store.add_edge(pid, store.node_id("dataset", dd["dsn"]), acc,
                                   {"via": f"{ddname} DD -> SELECT {logical} "
                                           f"ASSIGN {ddname}",
                                    "job": dd["job"], "step": dd["step"]})
            if not bound:
                for acc in access:
                    store.add_edge(pid, store.node_id("file", ddname), acc,
                                   {"via": f"SELECT {logical} ASSIGN {ddname} "
                                           "(no JCL binding found)"})

    # SQL table access
    for f in _facts(store, "sql"):
        prog = _program_of(store, f["artifact_id"])
        if not prog:
            continue
        op = f["detail"].get("op", "")
        acc = "writes" if op in ("INSERT", "UPDATE", "DELETE") else "reads"
        store.add_edge(store.node_id("program", prog),
                       store.node_id("table", f["name"]), acc,
                       {"op": op, "line": f["source_line_start"]})

    # CICS: screens, files, transactions
    for f in _facts(store, "cics"):
        prog = _program_of(store, f["artifact_id"])
        if not prog:
            continue
        d = f["detail"]
        pid = store.node_id("program", prog)
        cmd = d.get("command", "")
        if d.get("map"):
            screen = f"{d.get('mapset', d['map'])}.{d['map']}" \
                if d.get("mapset") else d["map"]
            store.add_edge(pid, store.node_id("screen", screen), "displays",
                           {"command": f"{cmd} MAP"})
        if d.get("file"):
            acc = "writes" if cmd.startswith(("WRITE", "REWRITE", "DELETE")) \
                else "reads"
            store.add_edge(pid, store.node_id("file", d["file"]), acc,
                           {"command": cmd, "context": "cics"})
        if d.get("program"):
            store.add_edge(pid, store.node_id("program", d["program"]), "calls",
                           {"via": cmd})
        if cmd == "RETURN" and d.get("transid"):
            store.add_edge(pid, store.node_id("transaction", d["transid"]),
                           "triggers", {"via": "RETURN TRANSID"})

    # MQ queue access
    for f in _facts(store, "mq_call"):
        prog = _program_of(store, f["artifact_id"])
        d = f["detail"]
        if not prog or not d.get("queue"):
            continue
        acc = "writes" if d.get("operation") == "put" else "reads"
        store.add_edge(store.node_id("program", prog),
                       store.node_id("queue", d["queue"]), acc,
                       {"operation": d.get("operation", "")})

    # utility lineage: step reads/writes datasets
    for f in _facts(store, "lineage"):
        d = f["detail"]
        sid = store.node_id("step", f"{d['job']}.{d['step']}")
        if d.get("from_dsn"):
            store.add_edge(sid, store.node_id("dataset", d["from_dsn"]), "reads",
                           {"via": d.get("via", "")})
        if d.get("to_dsn"):
            store.add_edge(sid, store.node_id("dataset", d["to_dsn"]), "writes",
                           {"via": d.get("via", "")})

    # scheduler facts (Phase 6 adapters): schedule_job nodes + precedes edges
    job_names = {r["name"] for r in store.query(
        "SELECT name FROM nodes WHERE node_type='job'")}
    for f in _facts(store, "sched_job"):
        sid = store.node_id("schedule_job", f["name"], f["artifact_id"])
        if f["name"] in job_names:   # schedule entry -> JCL job of same name
            store.add_edge(sid, store.node_id("job", f["name"]), "triggers",
                           {"via": f["detail"].get("scheduler", "")})
    for f in _facts(store, "sched_dep"):
        d = f["detail"]
        pred_type = "external_node" if d.get("external") else "schedule_job"
        pred = store.node_id(pred_type, f["name"])
        store.add_edge(pred, store.node_id("schedule_job", d["job"]), "precedes",
                       {"external": d.get("external", 0),
                        "scheduler": d.get("scheduler", "")})

    # BMS screens
    for f in _facts(store, "map"):
        mapset = f["detail"].get("mapset", "") or f["name"]
        store.node_id("screen", f"{mapset}.{f['name']}", f["artifact_id"])

    # MQSC-defined queues
    for f in _facts(store, "mq_qlocal", "mq_qremote", "mq_qalias"):
        store.node_id("queue", f["name"], f["artifact_id"])

    # REXX dataset access
    for f in _facts(store, "rexx_dataset"):
        prog = _program_of(store, f["artifact_id"])
        if prog:
            acc = "writes" if f["detail"].get("access") == "write" else "reads"
            store.add_edge(store.node_id("program", prog),
                           store.node_id("dataset", f["name"]), acc,
                           {"via": f["detail"].get("via", "")})

    # Easytrieve file access (ddname only; no JCL binding in the estate)
    for f in _facts(store, "ezt_file"):
        prog = _program_of(store, f["artifact_id"])
        if prog and f["detail"].get("ddname"):
            store.add_edge(store.node_id("program", prog),
                           store.node_id("file", f["detail"]["ddname"]),
                           "reads", {"via": "Easytrieve FILE"})

    # CSD facts (Phase 6): transaction -> program, file -> dataset
    for f in _facts(store, "csd_transaction"):
        store.add_edge(store.node_id("transaction", f["name"]),
                       store.node_id("program", f["detail"].get("program", "")),
                       "triggers", {"via": "CSD DEFINE TRANSACTION"})
    for f in _facts(store, "csd_file"):
        if f["detail"].get("dsname"):
            store.add_edge(store.node_id("file", f["name"]),
                           store.node_id("dataset", f["detail"]["dsname"]),
                           "binds", {"via": "CSD DSNAME"})
    store.commit()


def _program_of(store: Store, artifact_id: int) -> str:
    row = store.query(
        "SELECT name FROM facts WHERE artifact_id=? AND fact_type='program' "
        "LIMIT 1", (artifact_id,))
    if row:
        return row[0]["name"]
    # copybooks/JCL have no program; fall back to artifact stem for refs
    return ""


def _load(store: Store):
    nodes = {r["id"]: dict(r) for r in store.query("SELECT * FROM nodes")}
    edges = [dict(r) | {"detail": json.loads(r["detail_json"] or "{}")}
             for r in store.query("SELECT * FROM edges")]
    return nodes, edges


def _find_node(nodes: dict, name: str):
    name = name.upper()
    matches = [n for n in nodes.values() if n["name"].upper() == name]
    order = {"program": 0, "dataset": 1, "table": 2, "job": 3}
    matches.sort(key=lambda n: order.get(n["node_type"], 9))
    return matches[0] if matches else None


def query_impact(store: Store, name: str) -> list[str]:
    nodes, edges = _load(store)
    node = _find_node(nodes, name)
    out: list[str] = []
    if not node:
        return [f"no node named {name!r} in the graph"]
    nid = node["id"]
    out.append(f"impact analysis for {node['node_type']} {node['name']}:")
    by_dst = defaultdict(list)
    by_src = defaultdict(list)
    for e in edges:
        by_dst[e["dst_node"]].append(e)
        by_src[e["src_node"]].append(e)

    def nn(i):
        return nodes[i]

    # upstream executors/callers (and their jobs, transitively)
    seen_jobs = set()
    for e in by_dst[nid]:
        src = nn(e["src_node"])
        if e["edge_type"] == "calls":
            via = e["detail"].get("via", "")
            out.append(f"  called/run by {src['node_type']} {src['name']}"
                       + (f" (via {via})" if via else ""))
            if src["node_type"] == "step":
                for je in by_src[src["id"]]:
                    if je["edge_type"] == "belongs_to":
                        job = nn(je["dst_node"])
                        if job["name"] not in seen_jobs:
                            seen_jobs.add(job["name"])
                            step = src["name"].split(".", 1)[-1]
                            out.append(f"  job {job['name']} depends on it "
                                       f"(via step {step})")
        elif e["edge_type"] == "triggers":
            out.append(f"  triggered by {src['node_type']} {src['name']}")
    # what it touches
    for e in by_src[nid]:
        dst = nn(e["dst_node"])
        et = e["edge_type"]
        if et in ("reads", "writes", "displays", "includes", "calls", "triggers"):
            via = e["detail"].get("via", "") or e["detail"].get("op", "") \
                or e["detail"].get("command", "")
            out.append(f"  {et} {dst['node_type']} {dst['name']}"
                       + (f" (via {via})" if via else ""))
    # downstream consumers of what it writes
    for e in by_src[nid]:
        if e["edge_type"] != "writes":
            continue
        ds = e["dst_node"]
        for r in by_dst[ds]:
            if r["edge_type"] == "reads" and r["src_node"] != nid:
                src = nn(r["src_node"])
                out.append(f"  downstream: {src['node_type']} {src['name']} "
                           f"reads {nn(ds)['name']}")
    if node["node_type"] in ("dataset", "table", "file"):
        for e in by_dst[nid]:
            src = nn(e["src_node"])
            via = e["detail"].get("via", "") or e["detail"].get("ddname", "")
            out.append(f"  {e['edge_type']} by {src['node_type']} {src['name']}"
                       + (f" (via {via})" if via else ""))
    return out


def query_lineage(store: Store, dataset: str) -> list[str]:
    nodes, edges = _load(store)
    node = _find_node(nodes, dataset)
    if not node:
        return [f"no node named {dataset!r} in the graph"]
    out = [f"lineage for {node['node_type']} {node['name']}:"]
    by_dst = defaultdict(list)
    by_src = defaultdict(list)
    for e in edges:
        by_dst[e["dst_node"]].append(e)
        by_src[e["src_node"]].append(e)

    def walk(nid: int, direction: str, depth: int, seen: set):
        pad = "    " * depth
        rel_in = "writes" if direction == "up" else "reads"
        for e in by_dst[nid]:
            if e["edge_type"] != rel_in or e["src_node"] in seen:
                continue
            actor = nodes[e["src_node"]]
            via = e["detail"].get("via", "")
            out.append(f"{pad}  {'<-' if direction == 'up' else '->'} "
                       f"{rel_in} by {actor['node_type']} {actor['name']}"
                       + (f" (via {via})" if via else ""))
            seen.add(e["src_node"])
            other = "reads" if direction == "up" else "writes"
            for e2 in by_src[e["src_node"]]:
                if e2["edge_type"] == other and \
                        nodes[e2["dst_node"]]["node_type"] in ("dataset", "table", "file"):
                    d2 = nodes[e2["dst_node"]]
                    out.append(f"{pad}      {other} {d2['node_type']} {d2['name']}")
                    walk(e2["dst_node"], direction, depth + 1, seen)

    out.append("  upstream (what produces it):")
    walk(node["id"], "up", 0, {node["id"]})
    out.append("  downstream (what consumes it):")
    walk(node["id"], "down", 0, {node["id"]})
    return out


def query_dead(store: Store) -> list[str]:
    nodes, edges = _load(store)
    referenced = {e["dst_node"] for e in edges
                  if e["edge_type"] in ("calls", "triggers")}
    out = ["programs with source in the estate and no static reference "
           "(no caller, no job step, no transaction):"]
    found = False
    for n in nodes.values():
        if n["node_type"] != "program" or not n["artifact_id"]:
            continue
        if n["id"] not in referenced:
            found = True
            path = store.query("SELECT path FROM artifacts WHERE id=?",
                               (n["artifact_id"],))
            out.append(f"  {n['name']}  ({path[0]['path'] if path else '?'})")
    if not found:
        out.append("  (none)")
    return out


def query_cycles(store: Store) -> list[str]:
    nodes, edges = _load(store)
    adj = defaultdict(set)
    for e in edges:
        if e["edge_type"] in ("calls", "performs"):
            adj[e["src_node"]].add(e["dst_node"])
    # iterative Tarjan SCC
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    counter = [0]
    sccs: list[list[int]] = []

    def strongconnect(v0: int) -> None:
        work = [(v0, iter(sorted(adj[v0])))]
        index[v0] = low[v0] = counter[0]
        counter[0] += 1
        stack.append(v0)
        on_stack.add(v0)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(adj[w]))))
                    advanced = True
                    break
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                pv = work[-1][0]
                low[pv] = min(low[pv], low[v])
            if low[v] == index[v]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(scc)

    for v in list(adj):
        if v not in index:
            strongconnect(v)
    if not sccs:
        return ["no call/perform cycles found"]
    out = ["cycles found:"]
    for scc in sccs:
        out.append("  " + " <-> ".join(nodes[i]["name"] for i in scc))
    return out


def query_where_used(store: Store, field: str) -> list[str]:
    field = field.upper()
    out = [f"where-used for data element {field}:"]
    rows = store.query(
        "SELECT a.path, a.artifact_type, f.fact_type, f.detail_json,"
        " f.source_line_start FROM facts f JOIN artifacts a ON a.id=f.artifact_id"
        " WHERE f.name=? AND f.fact_type IN"
        " ('field', 'data_item', 'condition_name', 'data_ref')"
        " ORDER BY a.path, f.source_line_start", (field,))
    if not rows:
        out.append("  (no definitions or references found)")
        return out
    by_artifact: dict[str, list] = defaultdict(list)
    for r in rows:
        by_artifact[r["path"]].append(r)
    for path, items in by_artifact.items():
        prog = ""
        prow = store.query(
            "SELECT f.name FROM facts f JOIN artifacts a ON a.id=f.artifact_id "
            "WHERE a.path=? AND f.fact_type='program' LIMIT 1", (path,))
        if prow:
            prog = prow[0]["name"]
        label = prog or Path(path).stem.upper()
        for r in items:
            d = json.loads(r["detail_json"] or "{}")
            if r["fact_type"] in ("field", "data_item"):
                out.append(f"  {label} ({path}): defines at line "
                           f"{r['source_line_start']} (level {d.get('level')}, "
                           f"PIC {d.get('pic') or '-'})")
            elif r["fact_type"] == "condition_name":
                out.append(f"  {label} ({path}): 88-level under "
                           f"{d.get('parent', '?')} at line {r['source_line_start']}")
            else:
                para = d.get("paragraph", "")
                out.append(f"  {label} ({path}): {d.get('access')}s at line "
                           f"{r['source_line_start']}"
                           + (f" in {para}" if para else "")
                           + f" ({d.get('verb', '')})")
    return out


_SHAPES = {"program": ("[", "]"), "paragraph": ("([", "])"),
           "dataset": ("[(", ")]"), "table": ("[(", ")]"),
           "job": ("{{", "}}"), "step": ("{{", "}}"),
           "screen": ("[/", "/]"), "queue": (">", "]"),
           "external_node": ("((", "))"), "schedule_job": ("{{", "}}")}


def _mid(node: dict) -> str:
    return f"{node['node_type'][:2]}_" + \
        "".join(c if c.isalnum() else "_" for c in node["name"])


def export(store: Store, workspace: Path) -> tuple[Path, Path]:
    nodes, edges = _load(store)
    gj = {"nodes": [{"id": n["id"], "type": n["node_type"], "name": n["name"],
                     "artifact_id": n["artifact_id"]} for n in nodes.values()],
          "edges": [{"src": e["src_node"], "dst": e["dst_node"],
                     "type": e["edge_type"], "detail": e["detail"],
                     "origin": e["origin"]} for e in edges]}
    json_path = workspace / "graph.json"
    json_path.write_text(json.dumps(gj, indent=1), encoding="utf-8")

    lines = ["flowchart LR"]
    # group programs into domain subgraphs once units exist
    domain_of: dict[str, str] = {}
    for u in store.query("SELECT domain, programs_json FROM units"):
        for p in json.loads(u["programs_json"] or "[]"):
            domain_of[p] = u["domain"]
    shown = [n for n in nodes.values() if n["node_type"] != "paragraph"]
    by_domain: dict[str, list] = defaultdict(list)
    for n in shown:
        dom = domain_of.get(n["name"], "") if n["node_type"] == "program" else ""
        by_domain[dom].append(n)
    for dom, members in sorted(by_domain.items()):
        indent = "  "
        if dom:
            lines.append(f"  subgraph {dom}")
            indent = "    "
        for n in members:
            o, c = _SHAPES.get(n["node_type"], ("[", "]"))
            lines.append(f'{indent}{_mid(n)}{o}"{n["node_type"]}: {n["name"]}"{c}')
        if dom:
            lines.append("  end")
    seen = set()
    for e in edges:
        s, d = nodes[e["src_node"]], nodes[e["dst_node"]]
        if s["node_type"] == "paragraph" or d["node_type"] == "paragraph":
            continue
        key = (e["src_node"], e["dst_node"], e["edge_type"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {_mid(s)} -->|{e['edge_type']}| {_mid(d)}")
    mmd_path = workspace / "graph.mmd"
    mmd_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, mmd_path


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        build(store)
        if args.impact:
            print("\n".join(query_impact(store, args.impact)))
        elif args.lineage:
            print("\n".join(query_lineage(store, args.lineage)))
        elif args.dead:
            print("\n".join(query_dead(store)))
        elif args.cycles:
            print("\n".join(query_cycles(store)))
        elif args.where_used:
            print("\n".join(query_where_used(store, args.where_used)))
        else:
            jp, mp = export(store, cfg.workspace)
            counts = store.query(
                "SELECT node_type, COUNT(*) c FROM nodes GROUP BY node_type "
                "ORDER BY node_type")
            ec = store.query("SELECT COUNT(*) c FROM edges")[0]["c"]
            print(f"graph built: {sum(r['c'] for r in counts)} nodes, "
                  f"{ec} edges -> {jp}, {mp}")
            for r in counts:
                print(f"  {r['node_type']:14s} {r['c']:>4d}")
    return 0
