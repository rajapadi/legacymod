"""Human-in-the-loop review queue — CSV out, CSV in.

``legacymod review`` exports everything awaiting a human decision to
``workspace/review_queue.csv``: rule candidates/explanations and any
fact marked ``needs_review=1`` (LLM-origin facts, dynamic calls, ...).

A human edits the ``decision`` column (``approve`` / ``reject`` /
``accept``) and runs ``legacymod review --apply`` to write decisions
back: rules become approved/rejected, facts get needs_review cleared,
and matching llm_log rows are stamped accepted_by_human.
"""

from __future__ import annotations

import argparse
import csv
import logging

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_QUEUE = "review_queue.csv"


def export_queue(store: Store, cfg: Config) -> int:
    path = cfg.workspace / _QUEUE
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["item_type", "id", "where", "what", "content",
                    "status", "decision"])
        for r in store.query(
                "SELECT rule_id, program, category, source_lines, snippet,"
                " plain_english, status FROM rules"
                " WHERE status IN ('candidate', 'explained')"
                " ORDER BY program, source_lines"):
            w.writerow(["rule", r["rule_id"],
                        f"{r['program']} {r['source_lines']}", r["category"],
                        r["plain_english"] or r["snippet"], r["status"], ""])
            n += 1
        for r in store.query(
                "SELECT f.id, a.path, f.fact_type, f.name, f.detail_json,"
                " f.origin FROM facts f JOIN artifacts a ON a.id=f.artifact_id"
                " WHERE f.needs_review=1 ORDER BY a.path"):
            w.writerow(["fact", r["id"], r["path"],
                        f"{r['fact_type']}:{r['name']}", r["detail_json"],
                        f"origin={r['origin']}", ""])
            n += 1
    return n


def apply_queue(store: Store, cfg: Config, path: str) -> tuple[int, int]:
    src = path or (cfg.workspace / _QUEUE)
    applied = skipped = 0
    with open(src, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            decision = (row.get("decision") or "").strip().lower()
            if not decision:
                skipped += 1
                continue
            if row["item_type"] == "rule" and decision in ("approve", "reject"):
                store.execute(
                    "UPDATE rules SET status=?, needs_review=0 WHERE rule_id=?",
                    ("approved" if decision == "approve" else "rejected",
                     row["id"]))
                store.execute(
                    "UPDATE llm_log SET accepted_by_human=1 WHERE artifact=?",
                    (f"rule:{row['id']}",))
                applied += 1
            elif row["item_type"] == "fact" and decision in ("accept",):
                store.execute("UPDATE facts SET needs_review=0 WHERE id=?",
                              (row["id"],))
                applied += 1
            else:
                skipped += 1
    store.commit()
    from .llm.provider import export_log
    export_log(store, cfg)
    store.export_csv(
        "SELECT rule_id, program, source_lines, category, snippet,"
        " plain_english, origin, confidence, status FROM rules"
        " ORDER BY program, source_lines", cfg.workspace / "rules.csv")
    return applied, skipped


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        if getattr(args, "apply", None) is not None:
            applied, skipped = apply_queue(store, cfg, args.apply)
            print(f"review: applied {applied} decision(s), "
                  f"{skipped} row(s) without a decision left untouched")
        else:
            n = export_queue(store, cfg)
            print(f"review: {n} item(s) awaiting human decision -> "
                  f"{cfg.workspace / _QUEUE}")
            print("  edit the 'decision' column (approve/reject/accept), "
                  "then run: legacymod review --apply")
    return 0
