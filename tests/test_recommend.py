"""Recommend stage: evidence-based future-state architecture (HITL gate)."""

from __future__ import annotations

import csv

import pytest

from legacymod.recommend import recommend_rows

from conftest import run_cli


def _ev(**overrides):
    base = {"programs": ["P1"], "transactions": [], "screens": [], "jobs": [],
            "multi_step_jobs": [], "outside_callers": [], "datasets": [],
            "tables": [], "queues": [], "keyed_files": [], "occurs_fields": 0,
            "cics_stmts": 0, "mq_calls": 0, "ims_calls": 0, "sql_stmts": 0,
            "blockers": [], "transfers": []}
    return base | overrides


def test_rules_batch_unit_targets_pipeline_not_service():
    rows = recommend_rows(_ev(jobs=["PAYRUN"], multi_step_jobs=["PAYRUN"],
                              keyed_files=["EMP-FILE"]))
    by = {r["concern"]: r for r in rows}
    assert "pipeline" in by["execution_style"]["recommendation"]
    assert by["compute"]["generate_target"] == "airflow-dag"
    assert "PostgreSQL" in by["data"]["recommendation"]
    assert "ui" not in by


def test_rules_single_job_batch_prefers_spring_batch():
    rows = recommend_rows(_ev(jobs=["ONEJOB"]))
    by = {r["concern"]: r for r in rows}
    assert by["compute"]["generate_target"] == "java-spring"
    assert "Spring Batch" in by["compute"]["recommendation"]


def test_rules_online_unit_targets_service_api_and_ui():
    rows = recommend_rows(_ev(transactions=["PAY1"], screens=["PAYMAP"],
                              cics_stmts=12, tables=["PAYROLL_AUDIT"]))
    by = {r["concern"]: [x for x in rows if x["concern"] == r["concern"]]
          for r in rows}
    assert "service" in by["execution_style"][0]["recommendation"]
    targets = {r["generate_target"] for rs in by.values() for r in rs}
    assert {"java-spring", "openapi"} <= targets
    assert "Angular" in by["ui"][0]["recommendation"]


def test_rules_mixed_unit_recommends_split():
    rows = recommend_rows(_ev(transactions=["PAY1"], cics_stmts=3,
                              jobs=["PAYRUN"]))
    style = next(r for r in rows if r["concern"] == "execution_style")
    assert "split" in style["recommendation"]
    targets = {r["generate_target"] for r in rows if r["generate_target"]}
    assert "java-spring" in targets            # both paths get a compute row


def test_rules_every_row_carries_evidence():
    for rows in (recommend_rows(_ev(jobs=["J1"], queues=["Q1"],
                                    transfers=[{"protocol": "ndm"}])),
                 recommend_rows(_ev(outside_callers=["CALLER1"]))):
        for r in rows:
            assert r["evidence"], r["concern"]
            assert 0 < r["confidence"] <= 1


@pytest.fixture(scope="module")
def recommended(workspace):
    (workspace / "domains.seed.csv").write_text(
        "domain,program\npayroll,PAYCALC\nnonstop,PAYSRV\n", encoding="utf-8")
    run_cli(workspace, "assess")
    assert run_cli(workspace, "decompose") == 0
    assert run_cli(workspace, "recommend") == 0
    return workspace


def _arch_rows(ws):
    with open(ws / "architecture.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_recommend_writes_gated_csv_and_report(recommended):
    rows = _arch_rows(recommended)
    assert rows and all(r["status"] == "proposed" for r in rows)
    assert {"execution_style"} <= {r["concern"] for r in rows}
    report = (recommended / "architecture.md").read_text(encoding="utf-8")
    assert "Verdict up front" in report
    assert "architecture.csv" in report


def test_generate_uses_approved_recommendation(recommended, capsys):
    ws = recommended
    # approve the payroll unit and its recommendations
    with open(ws / "units.csv", newline="", encoding="utf-8") as fh:
        units = list(csv.DictReader(fh))
    for u in units:
        if u["name"] == "payroll":
            u["status"] = "approved"
    with open(ws / "units.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=units[0].keys())
        w.writeheader()
        w.writerows(units)
    payroll_id = next(u["unit_id"] for u in units if u["name"] == "payroll")
    rows = _arch_rows(ws)
    for r in rows:
        if r["unit_id"] == payroll_id:
            r["status"] = "approved"
    with open(ws / "architecture.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    assert run_cli(ws, "spec", "payroll") == 0
    capsys.readouterr()
    assert run_cli(ws, "generate", "payroll") == 0      # no --target
    out = capsys.readouterr().out
    assert "generate[" in out                           # rendered something
    # re-running recommend must not clobber the human-approved rows
    assert run_cli(ws, "recommend") == 0
    approved_after = [r for r in _arch_rows(ws)
                      if r["unit_id"] == payroll_id
                      and r["status"] == "approved"]
    assert len(approved_after) == len(
        [r for r in rows if r["unit_id"] == payroll_id])


def test_generate_without_target_or_approval_fails_with_guidance(
        recommended, capsys):
    rc = run_cli(recommended, "generate", "nonstop")
    assert rc == 1
    out = capsys.readouterr().out
    assert "recommend" in out or "spec-first" in out
