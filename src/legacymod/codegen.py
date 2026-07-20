"""Stage 8 — target skeleton generation (spec-first, pluggable targets).

Targets:

- ``java-spring`` — entities from the data model, a service class with
  one stub per rule, REST controller stubs per interface, failing-by-
  design characterization tests, and a README mapping every generated
  element back to legacy artifacts.
- ``airflow-dag`` — DAG skeleton for the unit's batch jobs with
  dependencies from scheduler edges + dataset handoffs.
- ``openapi`` — OpenAPI 3 draft whose schemas are PIC-derived from the
  unit's record layouts, each property tagged ``x-legacy-source``.

Codegen consumes the assembled spec (``specgen.spec_data``), never raw
legacy source, and refuses units that have not produced a spec.
``--llm-impl`` adds LLM-proposed method bodies as clearly marked draft
comments (origin=llm, needs_review) — never as live code.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .config import Config
from .store import Store
from .decompose import sync_units_from_csv
from .specgen import find_unit, jinja_env, spec_data, _java_name

log = logging.getLogger(__name__)


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if s == "" or any(c in s for c in ":{}[],&*#?|-<>=!%@`'\"") \
            or s.strip() != s:
        return json.dumps(s)
    return s


def to_yaml(obj, indent: int = 0) -> list[str]:
    """Tiny YAML emitter for plain dict/list/scalar trees (always valid)."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{_yaml_scalar(k)}:")
                lines.extend(to_yaml(v, indent + 1))
            else:
                v = v if not isinstance(v, (dict, list)) else "{}"
                lines.append(f"{pad}{_yaml_scalar(k)}: {_yaml_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                sub = to_yaml(item, indent + 1)
                first = sub[0].strip()
                lines.append(f"{pad}- {first}")
                lines.extend(sub[1:])
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def _methods(store: Store, data: dict, llm_impl: bool, cfg: Config) -> list[dict]:
    methods = []
    rows = store.query(
        "SELECT * FROM rules WHERE program IN (%s) AND status<>'rejected'"
        " ORDER BY program, source_lines"
        % ",".join("?" * len(data["programs"])), data["programs"]) \
        if data["programs"] else []
    for r in rows:
        m = {"rule_id": r["rule_id"], "category": r["category"],
             "program": r["program"], "lines": r["source_lines"],
             "snippet": r["snippet"], "plain_english": r["plain_english"] or "",
             "status": r["status"],
             "method_name": f"{r['category'].replace('_', '')}"
                            f"{r['rule_id']}", "draft": ""}
        if llm_impl:
            from .llm import propose_impl
            m["draft"] = propose_impl(store, cfg, data["unit"]["name"],
                                      m["method_name"], r["snippet"])
        methods.append(m)
    return methods


def _gen_java(store: Store, cfg: Config, data: dict, out: Path,
              llm_impl: bool) -> list[Path]:
    env = jinja_env()
    unit = data["unit"]
    pkg = f"com.legacymod.{unit['name'].lower()}"
    class_name = _java_name(unit["name"], cap=True)
    spec_path = f"workspace/specs/{unit['name']}.md"
    methods = _methods(store, data, llm_impl, cfg)
    for i in data["interfaces"]:
        i["method_name"] = ("get" + _java_name(i["legacy"].replace(".", "-"),
                                               cap=True))
    written = []
    base = out / "src" / "main" / "java" / Path(*pkg.split("."))
    for rec in data["records"]:
        p = base / "entity" / f"{rec['class_name']}.java"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(env.get_template("java_service/Entity.java.j2").render(
            package=pkg, rec=rec), encoding="utf-8")
        written.append(p)
    p = base / "service" / f"{class_name}Service.java"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(env.get_template("java_service/Service.java.j2").render(
        package=pkg, unit=unit, class_name=class_name, methods=methods,
        spec_path=spec_path), encoding="utf-8")
    written.append(p)
    p = base / "api" / f"{class_name}Controller.java"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(env.get_template("java_service/Controller.java.j2").render(
        package=pkg, unit=unit, class_name=class_name,
        interfaces=data["interfaces"]), encoding="utf-8")
    written.append(p)
    p = out / "src" / "test" / "java" / Path(*pkg.split(".")) / \
        f"{class_name}CharacterizationTest.java"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(env.get_template(
        "java_service/CharacterizationTest.java.j2").render(
        package=pkg, unit=unit, class_name=class_name, methods=methods),
        encoding="utf-8")
    written.append(p)
    p = out / "README.md"
    p.write_text(env.get_template("java_service/README.md.j2").render(
        unit=unit, class_name=class_name, records=data["records"],
        methods=methods, interfaces=data["interfaces"], spec_path=spec_path),
        encoding="utf-8")
    written.append(p)
    return written


def _gen_airflow(store: Store, data: dict, out: Path) -> list[Path]:
    env = jinja_env()
    unit = data["unit"]
    jobs = data["jobs"]
    tasks = [{"task_id": j.lower(), "job": j, "note": ""} for j in jobs]
    deps = []
    # scheduler precedence among this unit's jobs
    for e in store.query(
            "SELECT s.name sn, d.name dn FROM edges e"
            " JOIN nodes s ON s.id=e.src_node JOIN nodes d ON d.id=e.dst_node"
            " WHERE e.edge_type='precedes'"):
        if e["sn"] in jobs and e["dn"] in jobs:
            deps.append({"up": e["sn"].lower(), "down": e["dn"].lower()})
    # dataset handoffs: writer job -> reader job
    writers: dict[str, set[str]] = {}
    readers: dict[str, set[str]] = {}
    for e in store.query(
            "SELECT s.name sn, s.node_type st, d.name dn, e.edge_type et"
            " FROM edges e JOIN nodes s ON s.id=e.src_node"
            " JOIN nodes d ON d.id=e.dst_node"
            " WHERE e.edge_type IN ('reads', 'writes')"
            " AND s.node_type='step'"):
        job = e["sn"].split(".")[0]
        target = writers if e["et"] == "writes" else readers
        target.setdefault(e["dn"], set()).add(job)
    for ds, wjobs in writers.items():
        for w in wjobs & set(jobs):
            for r in readers.get(ds, set()) & set(jobs):
                if r != w:
                    d = {"up": w.lower(), "down": r.lower()}
                    if d not in deps:
                        deps.append(d)
    # schedule from CA7 TIME= when a scheduler fact matches the first job
    schedule = None
    for f in store.query("SELECT name, detail_json FROM facts"
                         " WHERE fact_type='sched_job'"):
        if f["name"] in jobs:
            t = json.loads(f["detail_json"] or "{}").get("time", "")
            if len(t) == 4:
                schedule = f"{int(t[2:])} {int(t[:2])} * * *"
                break
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{unit['name'].lower()}_dag.py"
    p.write_text(env.get_template("airflow_dag/dag.py.j2").render(
        unit=unit, tasks=tasks, deps=deps, schedule=schedule),
        encoding="utf-8")
    return [p]


def _gen_openapi(data: dict, out: Path) -> list[Path]:
    env = jinja_env()
    unit = data["unit"]
    schemas: dict = {}
    paths: dict = {}
    for rec in data["records"]:
        props: dict = {}
        required = []
        for f in rec["fields"]:
            props[f["java_name"]] = dict(f["json"]) | {
                "description": f"PIC {f['pic']} {f['usage']}",
                "x-legacy-source": f"{rec['legacy_name']}.{f['legacy_name']} "
                                   f"({f['source']}:{f['line']})"}
            required.append(f["java_name"])
        schemas[rec["class_name"]] = {
            "type": "object",
            "description": f"From legacy record {rec['legacy_name']} "
                           f"({rec['source']})",
            "properties": props, "required": required}
        paths[f"/{unit['name'].lower()}/{rec['class_name'].lower()}"] = {
            "get": {
                "summary": f"Read {rec['legacy_name']} "
                           "(modernize-in-place API enablement draft)",
                "operationId": f"get{rec['class_name']}",
                "responses": {"200": {
                    "description": "OK",
                    "content": {"application/json": {"schema": {
                        "$ref": f"#/components/schemas/{rec['class_name']}"
                    }}}}}}}
    out.mkdir(parents=True, exist_ok=True)
    p = out / "openapi.yaml"
    p.write_text(env.get_template("openapi/openapi.yaml.j2").render(
        title=f"{unit['name']} API (draft)", unit=unit,
        paths_yaml="\n".join(to_yaml(paths, 1)),
        schemas_yaml="\n".join(to_yaml(schemas, 2))), encoding="utf-8")
    return [p]


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        sync_units_from_csv(store, cfg)
        unit = find_unit(store, args.unit)
        if not unit:
            print(f"generate: no unit {args.unit!r}")
            return 1
        if unit["status"] in ("proposed", "approved"):
            print(f"generate: unit {unit['name']} is status={unit['status']} "
                  "- spec-first is mandatory: run `legacymod spec "
                  f"{unit['name']}` (after approving in units.csv)")
            return 1
        spec_file = cfg.workspace / "specs" / f"{unit['name']}.md"
        if not spec_file.is_file():
            print(f"generate: spec missing ({spec_file}) - run "
                  f"`legacymod spec {unit['name']}` first")
            return 1
        data = spec_data(store, cfg, unit)
        out = cfg.workspace / "generated" / unit["name"] / args.target
        if args.target == "java-spring":
            written = _gen_java(store, cfg, data, out,
                                getattr(args, "llm_impl", False))
        elif args.target == "airflow-dag":
            written = _gen_airflow(store, data, out)
        else:
            written = _gen_openapi(data, out)
        store.execute(
            "UPDATE units SET status='generated' WHERE unit_id=?"
            " AND status='spec_done'", (unit["unit_id"],))
        store.commit()
        print(f"generate[{args.target}]: {len(written)} file(s) -> {out}")
        for p in written:
            print(f"  {p.relative_to(cfg.workspace)}")
    return 0
