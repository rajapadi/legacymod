"""Phase 6b acceptance: interfaces, runs, reconcile, flows."""

from __future__ import annotations

import csv
import shutil

import pytest

from conftest import OPS, run_cli


@pytest.fixture(scope="module")
def ops_workspace(workspace):
    shutil.copy(OPS / "capabilities.csv", workspace / "capabilities.csv")
    assert run_cli(workspace, "interfaces") == 0
    assert run_cli(workspace, "runs", str(OPS / "job_runs.csv"),
                   "--dataset-stats", str(OPS / "dataset_stats.csv")) == 0
    assert run_cli(workspace, "interfaces") == 0   # refresh frequencies
    assert run_cli(workspace, "reconcile") == 0
    assert run_cli(workspace, "flows") == 0
    return workspace


def _csv(ws, name):
    with open(ws / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_interfaces_catalog(ops_workspace):
    rows = _csv(ops_workspace, "interfaces.csv")
    ndm = next(r for r in rows if r["protocol"] == "ndm")
    assert ndm["dataset_or_queue"] == "PROD.PAY.OUT"
    assert ndm["target_node"] == "BANKNODE1"
    assert ndm["external"] == "1"
    sftp = next(r for r in rows if r["protocol"] == "sftp")
    assert sftp["target_node"] == "sftp.vendorx.com"
    assert sftp["external"] == "1"
    mq = next(r for r in rows if r["protocol"] == "mq")
    assert mq["source_job_or_program"] == "PAYNOTIF"
    assert mq["dataset_or_queue"] == "BANK.ACK.QUEUE"
    assert mq["target_node"] == "BANKQM@bankmq.example.com"
    assert mq["external"] == "1"


def test_runs_analytics(ops_workspace):
    summary = {r["job"]: r for r in _csv(ops_workspace, "runs_summary.csv")}
    assert summary["PAYRUN"]["frequency"] == "weekly"
    assert summary["PAYRUN"]["typical_start"].startswith("02:00")
    assert summary["PAYRPT"]["abends"] == "1"
    assert "0008" in summary["PAYRPT"]["abend_detail"]
    vols = _csv(ops_workspace, "interface_volumes.csv")
    bank = next(v for v in vols if v["target_node"] == "BANKNODE1")
    assert bank["avg_bytes_per_run"] == "10485760"
    assert bank["records_per_run"] == "52000"
    assert bank["as_of"] == "2026-07-07"


def test_reconcile_categories(ops_workspace):
    rows = {(r["name"], r["kind"]): r
            for r in _csv(ops_workspace, "reconcile.csv")}
    old = rows[("OLDPURGE", "job")]
    assert old["status"] == "decommission_candidate"
    assert old["needs_review"] == "1"
    assert rows[("PAYRPT", "schedule_job")]["status"] == "broken_reference"
    vsam = rows[("VSAMDEF", "job")]
    assert vsam["status"] == "on_request"
    assert "2026-05-14" in vsam["evidence_json"]
    assert rows[("PAYRUN", "job")]["status"] == "healthy"
    assert rows[("XFERJOB", "job")]["status"] == "healthy"
    assert rows[("OLDPURGE", "program")]["status"] == "transitively_dead"
    # healthy jobs are never review-flagged
    assert rows[("PAYRUN", "job")]["needs_review"] == "0"


def test_batch_stream_mermaid(ops_workspace):
    mmd = (ops_workspace / "flows" / "batch_streams.mmd").read_text()
    assert "jo_PAYRUN -->|precedes| jo_PAYRPT" in mmd
    assert "jo_PAYRUN -->|precedes| jo_XFERJOB" in mmd
    assert "TIME=0200" in mmd and "TIME=0300" in mmd
    assert 'ex_EXT_BANKACK(("external: EXT.BANKACK"))' in mmd


def test_cics_navigation_mermaid(ops_workspace):
    mmd = (ops_workspace / "flows" / "cics_navigation.mmd").read_text()
    assert "tr_PYIQ" in mmd and "pr_PAYINQ" in mmd
    assert "sc_PAYMAP_PAYMAP" in mmd
    assert "RETURN TRANSID" in mmd


def test_shares_resource_via_csd(ops_workspace):
    import sqlite3
    con = sqlite3.connect(ops_workspace / "legacymod.db")
    rows = con.execute(
        "SELECT s.name, d.name, e.detail_json FROM edges e"
        " JOIN nodes s ON s.id=e.src_node JOIN nodes d ON d.id=e.dst_node"
        " WHERE e.edge_type='shares_resource'").fetchall()
    con.close()
    pair = next((r for r in rows
                 if {r[0], r[1]} == {"PAYCALC", "PAYINQ"}), None)
    assert pair, f"no PAYCALC/PAYINQ shares_resource edge in {rows}"
    assert "PROD.EMP.MASTER" in pair[2]
    assert "CSD FILE" in pair[2]


def test_capability_pages(ops_workspace):
    flows = ops_workspace / "flows"
    pages = list(flows.glob("capability_*.md"))
    assert len(pages) == 3
    payroll = (flows / "capability_payroll_calculation.md").read_text()
    for needle in ("PAYCALC", "PAYRUN", "R948a8abca6", "PROD.PAY.OUT",
                   "healthy"):
        assert needle in payroll, needle
    inquiry = (flows / "capability_employee_inquiry.md").read_text()
    assert "PAYINQ" in inquiry
