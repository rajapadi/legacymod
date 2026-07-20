"""Phase 4b acceptance: datamig - DDL conversion, proposal, round trip."""

from __future__ import annotations

import subprocess
import sys

import pytest

from legacymod.datamig import map_db2_type

from conftest import run_cli


@pytest.fixture(scope="module")
def datamig_out(workspace):
    # depends on the Phase 4 flow having approved the payroll unit;
    # re-establish it here so the module is order-independent
    (workspace / "domains.seed.csv").write_text(
        "domain,program\npayroll,PAYCALC\n", encoding="utf-8")
    run_cli(workspace, "assess")
    run_cli(workspace, "decompose")
    import csv
    units = list(csv.DictReader(open(workspace / "units.csv", newline="",
                                     encoding="utf-8")))
    for u in units:
        if u["name"] == "payroll" and u["status"] == "proposed":
            u["status"] = "approved"
    with open(workspace / "units.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=units[0].keys())
        w.writeheader()
        w.writerows(units)
    assert run_cli(workspace, "datamig", "payroll") == 0
    return workspace / "datamig" / "payroll"


def test_type_mapping():
    assert map_db2_type("DECIMAL(9,2)") == ("NUMERIC(9,2)", True)
    assert map_db2_type("CHAR(6)") == ("CHAR(6)", True)
    assert map_db2_type("TIMESTAMP") == ("TIMESTAMP", True)
    assert map_db2_type("CLOB") == ("TEXT", True)
    pg, clean = map_db2_type("DECFLOAT(16)")
    assert not clean          # flagged, not silently mapped
    _, clean = map_db2_type("WEIRDTYPE")
    assert not clean


def test_target_ddl(datamig_out):
    ddl = (datamig_out / "target_ddl_postgres.sql").read_text()
    assert "CREATE TABLE payroll_audit" in ddl
    assert "gross NUMERIC(9,2)" in ddl
    assert "PRIMARY KEY (emp_id, audit_ts)" in ddl
    assert "FOREIGN KEY (emp_id, audit_ts) REFERENCES payroll_audit" in ddl


def test_proposed_schema(datamig_out):
    prop = (datamig_out / "proposed_schema.sql").read_text()
    assert "CREATE TABLE emp_record" in prop
    assert "emp_hours NUMERIC(5,2)" in prop     # COMP-3 -> NUMERIC(p,s)
    assert "PRIMARY KEY (emp_id)" in prop       # from SELECT RECORD KEY
    assert "pay_gross NUMERIC(9,2)" in prop
    # no dangling comma before the closing paren
    for stmt in prop.split(";"):
        body = [l for l in stmt.splitlines() if l.strip()
                and not l.strip().startswith("--")]
        if body and body[-1].strip() == ")":
            last_def = body[-2].split("--")[0].rstrip()
            assert not last_def.endswith(","), stmt


def test_mapping_doc(datamig_out):
    doc = (datamig_out / "datamig.md").read_text()
    assert "DB2 -> PostgreSQL type mapping" in doc
    assert "REDEFINES" in doc
    assert "PAYROLL_AUDIT" in doc
    assert "EMP-ID" in doc      # similarity report links EMP_ID <-> EMP-ID


def test_roundtrip_scripts_pass_when_executed(datamig_out):
    for script in ("convert_emp_record.py", "convert_pay_record.py"):
        proc = subprocess.run(
            [sys.executable, str(datamig_out / script), "--self-test"],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ROUND-TRIP PASS" in proc.stdout


def test_converter_handles_real_ebcdic_file(datamig_out, tmp_path):
    """End-to-end: pack records with the generated packer, convert the
    binary file, and check the CSV output."""
    import csv
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "conv", datamig_out / "convert_emp_record.py")
    conv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conv)
    rec = {"EMP-ID": "E00042", "EMP-NAME": "GRACE HOPPER",
           "EMP-HOURS": "42.50", "EMP-RATE": "125.00",
           "EMP-STATE": "VA", "EMP-STATUS": "A"}
    src = tmp_path / "emp.dat"
    src.write_bytes(conv.pack_record(rec) * 3)
    dst = tmp_path / "emp.csv"
    conv.convert_file(str(src), str(dst))
    rows = list(csv.DictReader(open(dst, newline="", encoding="utf-8")))
    assert len(rows) == 3
    assert rows[0]["EMP-ID"] == "E00042"
    assert rows[0]["EMP-HOURS"] == "42.50"
    assert rows[0]["EMP-NAME"] == "GRACE HOPPER"
