"""Phase 2b acceptance: assess, slice, where-used."""

from __future__ import annotations

import csv
import sqlite3

from conftest import run_cli


def _csv(ws, name):
    with open(ws / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_assess_acceptance(workspace):
    assert run_cli(workspace, "assess") == 0

    metrics = {r["program"]: r for r in _csv(workspace, "metrics.csv")}
    assert int(metrics["PAYCALC"]["cyclomatic"]) >= 4

    clones = _csv(workspace, "clones.csv")
    assert any({"cobol/PAYCALC.cbl", "cobol/ORPHAN.cbl"} ==
               {c["file_a"], c["file_b"]} for c in clones), \
        "PAYCALC/ORPHAN clone not detected"

    missing = {r["name"] for r in _csv(workspace, "missing_artifacts.csv")}
    assert {"PAYAUDIT", "PAYRPT"} <= missing
    # vendor utilities must NOT be reported missing
    assert "IKJEFT01" not in missing and "IDCAMS" not in missing

    blockers = _csv(workspace, "blockers.csv")
    assert any(b["program_or_job"] == "ORPHAN"
               and b["blocker_type"] == "assembler_call"
               and "ASMXIT01" in b["detail"] for b in blockers)
    assert any(b["program_or_job"] == "PAYSRV"
               and b["blocker_type"] == "enter_tal" for b in blockers)

    sizes = {r[0] for r in
             sqlite3.connect(workspace / "legacymod.db")
             .execute("SELECT DISTINCT program FROM metrics")}
    assert "PAYCALC" in sizes


def test_where_used_emp_hours(workspace, capsys):
    run_cli(workspace, "graph", "--where-used", "EMP-HOURS")
    out = capsys.readouterr().out
    assert "EMPREC" in out and "defines" in out
    assert "PAYCALC" in out and "reads at line 36" in out


def test_slice_pay_gross(workspace, capsys):
    rc = run_cli(workspace, "slice", "PAYCALC", "--seed", "PAY-GROSS")
    assert rc == 0
    out = capsys.readouterr().out
    assert "CALC-PAY" in out
    for field in ("WS-GROSS", "WS-RATE", "WS-HOURS", "EMP-HOURS", "EMP-RATE"):
        assert field in out, field
    # SQL audit branch exclusives must be excluded
    for excluded in ("PAY-STATUS", "PAY-EMP-ID", "EMP-ID,", "WS-EOF"):
        assert excluded not in out, excluded


def test_slice_unknown_program(workspace, capsys):
    assert run_cli(workspace, "slice", "NOPE", "--seed", "X") == 1
