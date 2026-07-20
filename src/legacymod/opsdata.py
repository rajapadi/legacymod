"""Stage 11 — operational data (runs + dataset stats) and analytics.

The platform cannot see live SMF/CA7 logs; it accepts the documented CSV
export formats::

    job_runs:      job_name, run_date, start_time, end_time, cond_code
    dataset_stats: dataset, avg_bytes_per_run, records_per_run, as_of

Derived: actual run frequency per job (from date gaps), typical start
times and durations, last-run date, abend rates (cond code > 4 or
non-numeric system abends), and data-volume analysis joining dataset
stats onto the transmission catalog. Interface frequencies are refreshed
here from run history (frequency_source says which source was used).
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
from datetime import date, datetime, time
from pathlib import Path

from .config import Config
from .store import Store

log = logging.getLogger(__name__)


def _is_abend(cond: str) -> bool:
    cond = (cond or "").strip()
    try:
        return int(cond) > 4
    except ValueError:
        return bool(cond)   # S0C7-style system abend codes


def job_frequency(store: Store, job: str) -> tuple[str, str]:
    """(frequency, source). Run history wins; scheduler calendar second."""
    dates = sorted({date.fromisoformat(r["run_date"]) for r in store.query(
        "SELECT run_date FROM job_runs WHERE job_name=?", (job,))})
    if len(dates) >= 2:
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        med = statistics.median(gaps)
        freq = ("daily" if med <= 1.5 else
                "weekly" if med <= 9 else
                "monthly" if med <= 45 else "ad-hoc")
        return freq, "run_history"
    if len(dates) == 1:
        return "ad-hoc (single recorded run)", "run_history"
    for r in store.query(
            "SELECT detail_json FROM facts WHERE fact_type='sched_job'"
            " AND name=?", (job,)):
        import json
        cal = json.loads(r["detail_json"] or "{}").get("calendar", "")
        if cal:
            mapping = {"BUSDAYS": "daily (business days)",
                       "WEEKLY": "weekly", "MONTHLY": "monthly"}
            return mapping.get(cal, f"per calendar {cal}"), \
                "scheduler_calendar"
        return "scheduled (no calendar parsed)", "scheduler_calendar"
    return "unknown", "none"


def _duration_minutes(r) -> float | None:
    try:
        t0 = time.fromisoformat(r["start_time"])
        t1 = time.fromisoformat(r["end_time"])
    except (ValueError, TypeError):
        return None
    d0 = datetime.combine(date(2000, 1, 1), t0)
    d1 = datetime.combine(date(2000, 1, 1 + (1 if t1 < t0 else 0)), t1)
    return (d1 - d0).total_seconds() / 60


def run(args: argparse.Namespace, cfg: Config) -> int:
    src = Path(args.run_history)
    if not src.is_file():
        print(f"runs: file not found: {src}")
        return 1
    with Store(cfg) as store:
        store.execute("DELETE FROM job_runs")
        with open(src, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            need = {"job_name", "run_date", "start_time", "end_time",
                    "cond_code"}
            if not need <= set(reader.fieldnames or []):
                print(f"runs: {src} must have columns {sorted(need)} "
                      f"(documented export format); got {reader.fieldnames}")
                return 1
            for row in reader:
                store.execute(
                    "INSERT INTO job_runs VALUES (?,?,?,?,?)",
                    (row["job_name"].upper(), row["run_date"],
                     row["start_time"], row["end_time"], row["cond_code"]))
        stats_path = getattr(args, "dataset_stats", None)
        if not stats_path:
            sibling = src.parent / "dataset_stats.csv"
            stats_path = sibling if sibling.is_file() else None
            if stats_path:
                print(f"runs: also loading {stats_path} (found next to "
                      "run history)")
        if stats_path:
            store.execute("DELETE FROM dataset_stats")
            with open(stats_path, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    store.execute(
                        "INSERT INTO dataset_stats VALUES (?,?,?,?)",
                        (row["dataset"].upper(),
                         int(row["avg_bytes_per_run"]),
                         int(row["records_per_run"]), row["as_of"]))
        store.commit()

        # ---- per-job analytics
        jobs = [r["job_name"] for r in store.query(
            "SELECT DISTINCT job_name FROM job_runs ORDER BY job_name")]
        summary = []
        for job in jobs:
            rows = store.query("SELECT * FROM job_runs WHERE job_name=?"
                               " ORDER BY run_date", (job,))
            freq, source = job_frequency(store, job)
            starts = sorted(r["start_time"] for r in rows)
            typical_start = starts[len(starts) // 2] if starts else ""
            durs = [d for r in rows if (d := _duration_minutes(r)) is not None]
            abends = [r for r in rows if _is_abend(r["cond_code"])]
            summary.append({
                "job": job, "runs": len(rows), "frequency": freq,
                "frequency_source": source, "typical_start": typical_start,
                "avg_duration_min": round(statistics.mean(durs), 1)
                if durs else "",
                "last_run": rows[-1]["run_date"], "abends": len(abends),
                "abend_detail": "; ".join(
                    f"{r['run_date']} cond {r['cond_code']}" for r in abends)})

        # ---- refresh interface frequencies from run history
        for r in store.query("SELECT interface_id, source_job_or_program"
                             " FROM interfaces"):
            freq, source = job_frequency(store, r["source_job_or_program"])
            store.execute(
                "UPDATE interfaces SET frequency=?, frequency_source=?"
                " WHERE interface_id=?", (freq, source, r["interface_id"]))
        store.commit()

        # ---- volume joins onto the transmission catalog
        volumes = []
        for r in store.query(
                "SELECT i.*, d.avg_bytes_per_run, d.records_per_run, d.as_of"
                " FROM interfaces i JOIN dataset_stats d"
                " ON d.dataset = i.dataset_or_queue"
                " ORDER BY d.avg_bytes_per_run DESC"):
            volumes.append(dict(r))

        ws = cfg.workspace
        with open(ws / "runs_summary.csv", "w", newline="",
                  encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(summary[0].keys())
                               if summary else ["job"])
            w.writeheader()
            w.writerows(summary)
        with open(ws / "interface_volumes.csv", "w", newline="",
                  encoding="utf-8") as fh:
            cols = ["interface_id", "protocol", "dataset_or_queue",
                    "target_node", "frequency", "frequency_source",
                    "avg_bytes_per_run", "records_per_run", "as_of"]
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(volumes)
        store.export_csv("SELECT * FROM job_runs ORDER BY job_name, run_date",
                         ws / "job_runs.csv")

        print(f"runs: {sum(s['runs'] for s in summary)} run record(s) for "
              f"{len(summary)} job(s) -> runs_summary.csv, "
              "interface_volumes.csv")
        for s in summary:
            note = f", {s['abends']} abend(s): {s['abend_detail']}" \
                if s["abends"] else ""
            print(f"  {s['job']:10s} {s['frequency']:8s} "
                  f"typical start {s['typical_start']} "
                  f"avg {s['avg_duration_min']} min, last {s['last_run']}"
                  f"{note}")
        for v in volumes:
            mb = v["avg_bytes_per_run"] / 1048576
            print(f"  volume: {v['dataset_or_queue']} ~{mb:.0f} MB / "
                  f"{v['records_per_run']} records per run "
                  f"(as-of {v['as_of']}) -> {v['protocol']} interface to "
                  f"{v['target_node']}")
    return 0
