"""Stage 5 — deterministic business-rule candidate mining.

Mines, with line-level traceability:

- every conditional guarding a persistent write (file WRITE/REWRITE, SQL
  INSERT/UPDATE/DELETE, IMS ISRT/REPL/DLET) — category ``validation``;
- every computation chain feeding a persisted field — ``computation``;
- every literal-to-status-field assignment inside a conditional —
  ``state_transition`` (status-like target) or ``routing``;
- every 88-level condition name — ``state_transition`` (low confidence).

Candidates keep a stable ``rule_id`` (hash of program+lines+category) so
re-mining preserves human-set statuses and explanations. ``--enrich``
(Phase 3) fills ``plain_english`` via the LLM provider, marked
``origin=llm, needs_review``. Output: ``workspace/rules.csv``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections import defaultdict

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_STATUSY = re.compile(r"STATUS|STATE|FLAG|IND$|CODE$|TYPE$")
_PERSIST_SQL = {"INSERT", "UPDATE", "DELETE"}
_PERSIST_IMS = {"ISRT", "REPL", "DLET"}


def _sentence_span(lines: list[str], start_idx: int) -> int:
    """End line index (0-based) of the IF sentence starting at start_idx."""
    depth = 0
    for i in range(start_idx, len(lines)):
        up = lines[i].upper()
        depth += len(re.findall(r"\bIF\b", up))
        depth -= len(re.findall(r"\bEND-IF\b", up))
        if up.rstrip().endswith(".") or depth <= 0 and i > start_idx:
            return i
    return len(lines) - 1


def _rule_id(program: str, lines: str, category: str) -> str:
    return "R" + hashlib.sha1(
        f"{program}|{lines}|{category}".encode()).hexdigest()[:10]


def mine_program(program: str, text: str, facts: list[dict]) -> list[dict]:
    lines = text.splitlines()
    by_type: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        by_type[f["fact_type"]].append(f)

    candidates: list[dict] = []

    def add(category: str, l0: int, l1: int, confidence: float = 1.0) -> None:
        l1 = min(l1, len(lines))
        snippet = "\n".join(x.rstrip() for x in lines[l0 - 1:l1])
        candidates.append({
            "program": program, "lines": f"{l0}-{l1}", "category": category,
            "snippet": snippet, "confidence": confidence})

    # persistent-write line numbers
    persist_lines: set[int] = set()
    for f in by_type["file_write"]:
        persist_lines.add(f["source_line_start"])
    for f in by_type["sql"]:
        if f["detail"].get("op") in _PERSIST_SQL:
            persist_lines.update(range(f["source_line_start"],
                                       f["source_line_end"] + 1))
    for f in by_type["ims_call"]:
        if any(op in json.dumps(f["detail"]).upper() for op in _PERSIST_IMS):
            persist_lines.add(f["source_line_start"])

    # fields that flow into persisted records (reverse MOVE closure)
    reach: set[str] = set()
    record_writes = {f["detail"].get("record", "") for f in by_type["file_write"]}
    parents = {f["name"]: f["detail"].get("parent", "")
               for f in by_type["data_item"]}
    for name, parent in parents.items():
        top = name
        seenp = set()
        while parents.get(top) and top not in seenp:
            seenp.add(top)
            top = parents[top]
        if top in record_writes or name in record_writes:
            reach.add(name)
    reach |= record_writes
    stmts: dict[int, dict] = {}
    for f in by_type["data_ref"]:
        s = stmts.setdefault(f["detail"].get("stmt_id", 0),
                             {"reads": set(), "writes": set(),
                              "verbs": set(), "l0": f["source_line_start"],
                              "l1": f["source_line_start"]})
        s[f["detail"]["access"] + "s"].add(f["name"])
        s["verbs"].add(f["detail"].get("verb", ""))
        s["l0"] = min(s["l0"], f["source_line_start"])
        s["l1"] = max(s["l1"], f["source_line_start"])
    for f in by_type["data_ref"]:
        if f["detail"].get("verb") == "SQL":
            reach.add(f["name"])
    changed = True
    while changed:
        changed = False
        for s in stmts.values():
            if "MOVE" in s["verbs"] and s["writes"] & reach:
                new = s["reads"] - reach
                if new:
                    reach |= new
                    changed = True

    # computation candidates: arithmetic statements writing persisted fields
    for s in stmts.values():
        if s["verbs"] & {"COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"} \
                and s["writes"] & reach:
            add("computation", s["l0"], s["l1"])

    # conditionals guarding persistent writes
    if_lines = sorted({f["source_line_start"] for f in by_type["condition"]
                       if f["name"] == "IF"})
    for l in if_lines:
        end = _sentence_span(lines, l - 1) + 1
        span = set(range(l, end + 1))
        if span & persist_lines:
            add("validation", l, end)
            # literal status assignments inside the guarded block
            for i in sorted(span):
                if i - 1 >= len(lines):
                    continue
                m = re.search(r"\bMOVE\s+'([^']*)'\s+TO\s+([A-Z0-9-]+)",
                              lines[i - 1].upper())
                if m:
                    category = ("state_transition"
                                if _STATUSY.search(m.group(2)) else "routing")
                    add(category, l, end)
                    break

    # 88-level condition names (state definitions)
    for f in by_type["condition_name"]:
        add("state_transition", f["source_line_start"],
            f["source_line_end"] or f["source_line_start"], confidence=0.5)

    return candidates


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        root = store.source_root()
        rows = store.query(
            "SELECT DISTINCT a.id, a.path, f.name AS program FROM artifacts a "
            "JOIN facts f ON f.artifact_id=a.id AND f.fact_type='program' "
            "WHERE a.artifact_type IN ('cobol', 'hpns_cobol') ORDER BY a.path")
        existing = {r["rule_id"]: dict(r) for r in
                    store.query("SELECT * FROM rules")}
        mined = 0
        kept: set[str] = set()
        for row in rows:
            facts = []
            for f in store.query("SELECT * FROM facts WHERE artifact_id=?",
                                 (row["id"],)):
                d = dict(f)
                d["detail"] = json.loads(f["detail_json"] or "{}")
                facts.append(d)
            text = (root / row["path"]).read_text(encoding="utf-8",
                                                  errors="replace")
            for c in mine_program(row["program"], text, facts):
                rid = _rule_id(c["program"], c["lines"], c["category"])
                if rid in kept:
                    continue
                kept.add(rid)
                mined += 1
                old = existing.get(rid)
                if old:
                    # preserve human decisions across re-mining
                    store.execute(
                        "UPDATE rules SET snippet=?, confidence=? WHERE rule_id=?",
                        (c["snippet"], c["confidence"], rid))
                else:
                    store.execute(
                        "INSERT INTO rules (rule_id, program, source_lines,"
                        " category, snippet, plain_english, origin, confidence,"
                        " status) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rid, c["program"], c["lines"], c["category"],
                         c["snippet"], "", "parser", c["confidence"],
                         "candidate"))
        # drop rules whose source lines no longer exist (stale re-mine)
        for rid in set(existing) - kept:
            store.execute("DELETE FROM rules WHERE rule_id=? AND status='candidate'",
                          (rid,))
        store.commit()

        if getattr(args, "enrich", False):
            from .llm import enrich_rules
            enrich_rules(store, cfg)

        store.export_csv(
            "SELECT rule_id, program, source_lines, category, snippet,"
            " plain_english, origin, confidence, status FROM rules"
            " ORDER BY program, source_lines",
            cfg.workspace / "rules.csv")
        counts = store.query(
            "SELECT category, status, COUNT(*) c FROM rules GROUP BY 1, 2")
        total = sum(r["c"] for r in counts)
        print(f"rules: {total} candidates in catalog -> "
              f"{cfg.workspace / 'rules.csv'}")
        for r in counts:
            print(f"  {r['category']:17s} {r['status']:10s} {r['c']:>4d}")
    return 0
