"""Stage 12 — reconcile the three worlds: schedules, libraries, runs.

Categories:

(a) scheduled and running               -> healthy
(b) in library, unscheduled, no runs    -> decommission_candidate
(c) in library, unscheduled, has runs   -> on_request (manually
    submitted; flagged, NOT marked for decommission)
(d) scheduled but JCL/program missing   -> broken_reference
(e) programs reachable only from (b)    -> transitively_dead

The platform never deletes or excludes anything on its own; every
non-healthy row carries needs_review=1 — unscheduled is evidence, not
proof (on-request and DR jobs exist). Output: ``workspace/reconcile.csv``.
"""

from __future__ import annotations

import argparse
import json
import logging

from .config import Config
from .store import Store

log = logging.getLogger(__name__)


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        store.execute("DELETE FROM reconcile")
        library_jobs = {r["name"]: r["artifact_id"] for r in store.query(
            "SELECT name, artifact_id FROM nodes WHERE node_type='job'"
            " AND artifact_id IS NOT NULL")}
        scheduled = {r["name"] for r in store.query(
            "SELECT name FROM facts WHERE fact_type='sched_job'")}
        runs: dict[str, list] = {}
        for r in store.query("SELECT * FROM job_runs"):
            runs.setdefault(r["job_name"], []).append(dict(r))
        have_run_history = bool(runs)

        rows: list[tuple[str, str, str, dict]] = []
        decommission_jobs: set[str] = set()

        for job in sorted(library_jobs):
            j_sched = job in scheduled
            j_runs = runs.get(job, [])
            if j_sched:
                rows.append((job, "job", "healthy", {
                    "scheduled": True, "runs": len(j_runs),
                    "note": "" if j_runs or not have_run_history
                    else "scheduled with JCL present but no recorded runs "
                         "in the provided window"}))
            elif j_runs:
                rows.append((job, "job", "on_request", {
                    "scheduled": False, "runs": len(j_runs),
                    "evidence_runs": [f"{r['run_date']} {r['start_time']}"
                                      for r in j_runs],
                    "note": "in the library, in no schedule, but present in "
                            "run history - manually submitted / on-request"}))
            else:
                decommission_jobs.add(job)
                rows.append((job, "job", "decommission_candidate", {
                    "scheduled": False, "runs": 0,
                    "note": "in library but in no schedule and with no "
                            "recorded runs - evidence for a human decision, "
                            "not proof (DR/on-request jobs exist)"}))

        for job in sorted(scheduled):
            if job not in library_jobs:
                rows.append((job, "schedule_job", "broken_reference", {
                    "runs": len(runs.get(job, [])),
                    "note": "scheduled but no matching JCL member in the "
                            "library" + (" (job does appear in run history)"
                                         if runs.get(job) else "")}))

        # (e) programs reachable only from decommission-candidate jobs
        callers: dict[str, set[str]] = {}
        for e in store.query(
                "SELECT s.name sn, s.node_type st, d.name dn FROM edges e"
                " JOIN nodes s ON s.id=e.src_node"
                " JOIN nodes d ON d.id=e.dst_node"
                " WHERE e.edge_type IN ('calls', 'triggers')"
                " AND d.node_type='program'"):
            src_job = e["sn"].split(".")[0] if e["st"] == "step" else e["sn"]
            callers.setdefault(e["dn"], set()).add(src_job)
        for prog, srcs in sorted(callers.items()):
            has_artifact = store.query(
                "SELECT 1 FROM nodes WHERE node_type='program' AND name=?"
                " AND artifact_id IS NOT NULL", (prog,))
            if has_artifact and srcs and srcs <= decommission_jobs:
                rows.append((prog, "program", "transitively_dead", {
                    "only_reachable_from": sorted(srcs),
                    "note": "reachable only from decommission-candidate "
                            "job(s)"}))

        for name, kind, status, evidence in rows:
            store.execute(
                "INSERT INTO reconcile VALUES (?,?,?,?,?)",
                (name, kind, status, json.dumps(evidence),
                 0 if status == "healthy" else 1))
        store.commit()
        store.export_csv(
            "SELECT name, kind, status, evidence_json, needs_review"
            " FROM reconcile ORDER BY status, name",
            cfg.workspace / "reconcile.csv")
        counts = store.query("SELECT status, COUNT(*) c FROM reconcile"
                             " GROUP BY status ORDER BY status")
        print(f"reconcile: {len(rows)} finding(s) -> "
              f"{cfg.workspace / 'reconcile.csv'} (nothing is deleted or "
              "excluded; non-healthy rows carry needs_review=1)")
        for c in counts:
            print(f"  {c['status']:24s} {c['c']}")
        for name, kind, status, evidence in rows:
            if status != "healthy":
                print(f"  {status:24s} {kind} {name}: {evidence['note']}")
    return 0
