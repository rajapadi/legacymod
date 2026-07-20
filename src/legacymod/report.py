"""Stage 14 — one-page status report across the whole pipeline.

Honest numbers only: every figure is a count or percentage computed from
the store at report time. Output: ``workspace/report.md`` +
``workspace/report_summary.csv``.
"""

from __future__ import annotations

import argparse
import csv
import logging

from .config import Config
from .store import Store

log = logging.getLogger(__name__)


def _counts(store: Store, sql: str) -> list:
    try:
        return store.query(sql)
    except Exception:
        return []


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        inv = _counts(store, "SELECT artifact_type k, COUNT(*) c FROM artifacts"
                             " GROUP BY 1 ORDER BY 1")
        total = sum(r["c"] for r in inv)
        parsed = _counts(store, "SELECT COUNT(*) c FROM artifacts"
                                " WHERE adapter IS NOT NULL")[0]["c"]
        errors = _counts(store, "SELECT COALESCE(SUM(parse_errors),0) c"
                                " FROM artifacts")[0]["c"]
        nodes = _counts(store, "SELECT COUNT(*) c FROM nodes")[0]["c"]
        edges = _counts(store, "SELECT COUNT(*) c FROM edges")[0]["c"]
        rules = _counts(store, "SELECT status k, COUNT(*) c FROM rules"
                               " GROUP BY 1 ORDER BY 1")
        units = _counts(store, "SELECT status k, COUNT(*) c FROM units"
                               " GROUP BY 1 ORDER BY 1")
        ifaces = _counts(store, "SELECT COUNT(*) c, SUM(external) e"
                                " FROM interfaces")[0]
        ext_nodes = _counts(store, "SELECT COUNT(*) c FROM nodes"
                                   " WHERE node_type='external_node'")[0]["c"]
        recon = _counts(store, "SELECT status k, COUNT(*) c FROM reconcile"
                               " GROUP BY 1 ORDER BY 1")
        vres = _counts(store, "SELECT unit, COUNT(*) n, SUM(passed) p"
                              " FROM validation_results GROUP BY unit")
        llm = _counts(store, "SELECT COUNT(*) c, SUM(cache_hit) h,"
                             " SUM(accepted_by_human) a FROM llm_log")[0]
        review = _counts(store, "SELECT COUNT(*) c FROM facts"
                                " WHERE needs_review=1")[0]["c"]

        pct = 100 * parsed // total if total else 0
        lines = [
            "# legacymod status report",
            "",
            f"**Verdict up front:** {total} artifacts, {pct}% parsed by an "
            f"adapter ({errors} parse errors recorded, not hidden); graph "
            f"{nodes} nodes / {edges} edges; "
            f"{sum(r['c'] for r in rules)} rule(s), "
            f"{sum(r['c'] for r in units)} unit(s), "
            f"{ifaces['c'] or 0} interface(s) "
            f"({ifaces['e'] or 0} external); {review} fact(s) still "
            "awaiting human review.",
            "",
            "## Inventory",
            "", "| artifact type | count |", "|---|---:|"]
        lines += [f"| {r['k']} | {r['c']} |" for r in inv]
        lines += ["", f"Parse coverage: {parsed}/{total} ({pct}%), "
                      f"{errors} parse error(s).",
                  "", "## Graph", "",
                  f"- {nodes} nodes, {edges} edges "
                  f"({ext_nodes} external node(s))",
                  "", "## Rules by status", ""]
        lines += [f"- {r['k']}: {r['c']}" for r in rules] or ["- (none mined)"]
        lines += ["", "## Units by status", ""]
        lines += [f"- {r['k']}: {r['c']}" for r in units] or \
                 ["- (decompose not run)"]
        lines += ["", "## Interfaces", "",
                  f"- {ifaces['c'] or 0} total, {ifaces['e'] or 0} external",
                  "", "## Reconcile", ""]
        lines += [f"- {r['k']}: {r['c']}" for r in recon] or \
                 ["- (reconcile not run)"]
        lines += ["", "## Validation", ""]
        for r in vres:
            state = "PASS" if r["p"] == r["n"] else "FAIL"
            lines.append(f"- {r['unit']}: {r['p']}/{r['n']} case(s) passed "
                         f"-> equivalence {state}")
        if not vres:
            lines.append("- (no validation runs recorded)")
        lines += ["", "## LLM usage", "",
                  f"- {llm['c'] or 0} call(s), {llm['h'] or 0} cache hit(s), "
                  f"{llm['a'] or 0} accepted by a human "
                  "(full log: llm_log.csv)", ""]
        (cfg.workspace / "report.md").write_text("\n".join(lines),
                                                 encoding="utf-8")
        with open(cfg.workspace / "report_summary.csv", "w", newline="",
                  encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["metric", "value"])
            for k, v in (("artifacts", total), ("parse_coverage_pct", pct),
                         ("parse_errors", errors), ("nodes", nodes),
                         ("edges", edges),
                         ("rules", sum(r["c"] for r in rules)),
                         ("units", sum(r["c"] for r in units)),
                         ("interfaces", ifaces["c"] or 0),
                         ("external_interfaces", ifaces["e"] or 0),
                         ("facts_needing_review", review),
                         ("llm_calls", llm["c"] or 0)):
                w.writerow([k, v])
        print(f"report -> {cfg.workspace / 'report.md'} "
              f"(+ report_summary.csv)")
        print("\n".join(lines[2:4]))
    return 0
