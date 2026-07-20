"""Phase 4 acceptance: decompose, HITL gate, spec, codegen targets."""

from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from conftest import run_cli


@pytest.fixture(scope="module")
def decomposed(workspace):
    (workspace / "domains.seed.csv").write_text(
        "domain,program\npayroll,PAYCALC\nnonstop,PAYSRV\n", encoding="utf-8")
    run_cli(workspace, "assess")
    assert run_cli(workspace, "decompose") == 0
    return workspace


def _units(ws):
    with open(ws / "units.csv", newline="", encoding="utf-8") as fh:
        return {r["name"]: r for r in csv.DictReader(fh)}


def test_decompose_dispositions(decomposed):
    units = _units(decomposed)
    assert units["payroll"]["disposition"] == "refactor"
    ev = json.loads(units["payroll"]["disposition_evidence_json"])
    assert "ASMXIT01" in ev["reason"]          # ASMXIT01-free evidence cited
    assert not ev["blockers"]
    ev_ns = json.loads(units["nonstop"]["disposition_evidence_json"])
    assert any(b["blocker_type"] == "enter_tal" for b in ev_ns["blockers"])
    assert (decomposed / "waves.csv").exists()


def test_spec_requires_approval(decomposed, capsys):
    # payroll may already be approved by a previous run; nonstop is not
    rc = run_cli(decomposed, "spec", "nonstop")
    assert rc == 1
    assert "HITL gate" in capsys.readouterr().out


def test_generate_requires_spec_first(decomposed, capsys):
    rc = run_cli(decomposed, "generate", "nonstop", "--target", "java-spring")
    assert rc == 1
    out = capsys.readouterr().out
    assert "spec-first" in out or "status=proposed" in out


@pytest.fixture(scope="module")
def payroll_generated(decomposed):
    ws = decomposed
    units = list(_units(ws).values())
    for u in units:
        if u["name"] == "payroll":
            u["status"] = "approved"
    with open(ws / "units.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=units[0].keys())
        w.writeheader()
        w.writerows(units)
    con = sqlite3.connect(ws / "legacymod.db")
    con.execute("UPDATE rules SET status='approved' WHERE program='PAYCALC'")
    con.commit()
    con.close()
    assert run_cli(ws, "spec", "payroll") == 0
    assert run_cli(ws, "generate", "payroll", "--target", "java-spring") == 0
    assert run_cli(ws, "generate", "payroll", "--target", "openapi") == 0
    assert run_cli(ws, "generate", "payroll", "--target", "airflow-dag") == 0
    return ws


def test_spec_lists_approved_rules(payroll_generated):
    spec = (payroll_generated / "specs" / "payroll.md").read_text()
    assert "COMPUTE WS-GROSS = (40 * WS-RATE)" in spec   # verbatim snippet
    assert "9999.99" in spec
    assert "refactor" in spec
    assert "EMP-HOURS" in spec                            # data model table


def test_java_tree_shape_and_traceability(payroll_generated):
    base = payroll_generated / "generated" / "payroll" / "java-spring"
    java = base / "src" / "main" / "java" / "com" / "legacymod" / "payroll"
    entity = (java / "entity" / "EmpRecord.java").read_text()
    assert "package com.legacymod.payroll.entity;" in entity
    assert "private BigDecimal empHours;" in entity
    assert "import java.math.BigDecimal;" in entity
    service = (java / "service" / "PayrollService.java").read_text()
    assert "UnsupportedOperationException" in service
    assert "PAYCALC lines 39-40" in service
    readme = (base / "README.md").read_text()
    assert "EmpRecord.java" in readme and "EMPREC" in readme.upper()
    assert "PayrollService.java" in readme
    test_file = base / "src" / "test" / "java" / "com" / "legacymod" / \
        "payroll" / "PayrollCharacterizationTest.java"
    assert "fail(" in test_file.read_text()               # failing by design


def test_openapi_valid_and_traced(payroll_generated):
    text = (payroll_generated / "generated" / "payroll" / "openapi" /
            "openapi.yaml").read_text()
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(text)
    assert doc["openapi"].startswith("3.")
    schemas = doc["components"]["schemas"]
    assert {"EmpRecord", "PayRecord"} <= set(schemas)
    src = schemas["EmpRecord"]["properties"]["empHours"]["x-legacy-source"]
    assert "EMP-HOURS" in src and "EMPREC" in src
    src2 = schemas["PayRecord"]["properties"]["payGross"]["x-legacy-source"]
    assert "PAY-GROSS" in src2


def test_airflow_dag_generated(payroll_generated):
    dag = (payroll_generated / "generated" / "payroll" / "airflow-dag" /
           "payroll_dag.py").read_text()
    assert "payrun" in dag
    compile(dag, "payroll_dag.py", "exec")   # syntactically valid Python
