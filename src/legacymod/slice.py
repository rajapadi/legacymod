"""Static backward program slicing (paragraph granularity).

Walks backward from the seed's writers through data dependencies (MOVE /
arithmetic chains) and control dependencies (the fields tested by every
enclosing IF/UNTIL of a contributing statement), and reports the minimal
set of paragraphs + data items that produce the seed — a candidate
service-extraction report.

Honest caveat, stated in the output: this is a deterministic
over-approximation at paragraph granularity. It does not model subscript
aliasing, REDEFINES overlays, or inter-program flow through CALL
parameters; treat the result as an upper bound on what must move
together, not a proof of independence.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict

from .config import Config
from .store import Store

log = logging.getLogger(__name__)


def compute_slice(store: Store, program: str, seed: str) -> dict:
    program = program.upper()
    seed = seed.upper()
    rows = store.query(
        "SELECT f.* FROM facts f JOIN facts p ON p.artifact_id=f.artifact_id"
        " AND p.fact_type='program' AND p.name=?"
        " WHERE f.fact_type IN ('data_ref', 'paragraph')", (program,))
    if not rows:
        return {"error": f"no parsed facts for program {program!r}"}
    paragraphs = {r["name"] for r in rows if r["fact_type"] == "paragraph"}
    stmts: dict[int, dict] = {}
    for r in rows:
        if r["fact_type"] != "data_ref":
            continue
        d = json.loads(r["detail_json"] or "{}")
        s = stmts.setdefault(d.get("stmt_id", 0), {
            "reads": set(), "writes": set(), "conds": set(),
            "paragraph": d.get("paragraph", ""), "lines": set()})
        s[d["access"] + "s"].add(r["name"])
        s["conds"].update(d.get("cond_fields", []))
        s["lines"].add(r["source_line_start"])

    if seed in paragraphs:
        needed = set().union(*(s["writes"] for s in stmts.values()
                               if s["paragraph"] == seed)) or set()
        seed_kind = "paragraph"
    else:
        needed = {seed}
        seed_kind = "field"

    included_fields = set(needed)
    contributing: list[dict] = []
    frontier = set(needed)
    while frontier:
        field = frontier.pop()
        for s in stmts.values():
            if field not in s["writes"]:
                continue
            if s not in contributing:
                contributing.append(s)
            new = (s["reads"] | s["conds"]) - included_fields
            included_fields |= new | s["writes"] & {field}
            frontier |= new
    slice_paragraphs = sorted({s["paragraph"] for s in contributing
                               if s["paragraph"]})
    lines = sorted({l for s in contributing for l in s["lines"]})
    return {"program": program, "seed": seed, "seed_kind": seed_kind,
            "paragraphs": slice_paragraphs,
            "fields": sorted(included_fields),
            "statement_lines": lines}


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        result = compute_slice(store, args.program, args.seed)
        if "error" in result:
            print(result["error"])
            return 1
        out = cfg.workspace / f"slice_{result['program']}_{result['seed']}.md"
        body = [
            f"# Slice: {result['program']} <- {result['seed']} "
            f"({result['seed_kind']})",
            "",
            "**Verdict up front:** producing "
            f"`{result['seed']}` requires {len(result['paragraphs'])} "
            f"paragraph(s) and {len(result['fields'])} data item(s) (listed "
            "below). This is a deterministic over-approximation (paragraph "
            "granularity; no REDEFINES/subscript aliasing, no inter-program "
            "flow) — an upper bound for service extraction, not a proof of "
            "independence.",
            "",
            "## Paragraphs",
            *[f"- {p}" for p in result["paragraphs"]],
            "",
            "## Data items",
            *[f"- {f}" for f in result["fields"]],
            "",
            "## Contributing statement lines",
            "- " + ", ".join(str(l) for l in result["statement_lines"]),
            "",
        ]
        out.write_text("\n".join(body), encoding="utf-8")
        print(f"slice of {result['program']} from {result['seed']} "
              f"({result['seed_kind']}):")
        print("  paragraphs: " + (", ".join(result["paragraphs"]) or "(none)"))
        print("  data items: " + ", ".join(result["fields"]))
        print(f"  report -> {out}")
    return 0
