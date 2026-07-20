"""Phase 1 acceptance: inventory, core adapters, graph queries."""

from __future__ import annotations

import csv
import json
import sqlite3

from pathlib import Path

from legacymod.adapters.cobol import parse_cobol
from legacymod.adapters.copybook import parse_copybook, pic_meta
from legacymod.adapters.jcl import parse_jcl
from legacymod.inventory import classify, detect_encoding

from conftest import ESTATE, run_cli


def _inventory(ws):
    with open(ws / "inventory.csv", newline="", encoding="utf-8") as fh:
        return {r["path"]: r for r in csv.DictReader(fh)}


def test_inventory_covers_estate(workspace):
    inv = _inventory(workspace)
    assert len(inv) >= 20
    expect = {
        "cobol/PAYCALC.cbl": "cobol",
        "cobol/copybooks/EMPREC.cpy": "copybook",
        "jcl/PAYROLL.jcl": "jcl",
        "ddl/PAYDB.sql": "db2ddl",
        "scheduler/ca7_schedule.txt": "schedule_ca7",
        "scheduler/controlm_export.xml": "schedule_controlm",
        "rexx/CLEANUP.rexx": "rexx",
        "easytrieve/PAYLIST.ezt": "easytrieve",
        "bms/PAYMAP.bms": "cics_bms",
        "ims/EMPDBD.dbd": "ims_dbd",
        "ims/PAYPSB.psb": "ims_psb",
        "hpns/PAYSRV.cob": "hpns_cobol",
        "tal/PAYUTIL.tal": "tal",
        "cics/PAYCSD.txt": "cics_csd",
        "mq/PAYMQ.mqsc": "mqsc",
    }
    for path, atype in expect.items():
        assert inv[path]["artifact_type"] == atype, path


def test_broken_cobol_parse_errors_no_crash(workspace):
    inv = _inventory(workspace)
    assert int(inv["cobol/BROKEN.cbl"]["parse_errors"]) > 0


def test_classify_standalone_proc_and_ctl():
    # Shapes taken from AWS CardDemo, where these members were `unknown`.
    proc = b"//REPROC PROC\n//PS010 EXEC PGM=IDCAMS\n"
    assert classify(Path("REPROC.prc"), proc)[0] == "jcl"
    assert classify(Path("REPROC"), proc)[0] == "jcl"       # content sniff
    ctl = b"  REPRO INFILE(FILEIN) OUTFILE(FILEOUT)\n"
    assert classify(Path("REPROCT.ctl"), ctl)[0] == "utility_ctl"


def test_ebcdic_detection():
    text = "HELLO PAYROLL WORLD  0123456789  " * 30
    assert detect_encoding(text.encode("cp037")).startswith("ebcdic")
    assert detect_encoding(text.encode("ascii")) == "ascii"
    assert detect_encoding(b"") == "ascii"


def test_cobol_adapter_paycalc_facts():
    text = (ESTATE / "cobol" / "PAYCALC.cbl").read_text()
    res = parse_cobol(text)
    kinds = {}
    for f in res.facts:
        kinds.setdefault(f.fact_type, []).append(f)
    assert kinds["program"][0].name == "PAYCALC"
    assert {f.name for f in kinds["paragraph"]} == \
        {"MAIN-PARA", "READ-EMP", "CALC-PAY"}
    assert {f.name for f in kinds["performs"]} == {"READ-EMP", "CALC-PAY"}
    assert kinds["calls"][0].name == "PAYAUDIT"
    sql = kinds["sql"][0]
    assert sql.name == "PAYROLL_AUDIT" and sql.detail["op"] == "INSERT"
    selects = {f.name: f.detail for f in kinds["select"]}
    assert selects["EMP-FILE"]["ddname"] == "EMPMAST"
    assert selects["EMP-FILE"]["record_key"] == "EMP-ID"
    assert selects["PAY-FILE"]["ddname"] == "PAYOUT"
    assert res.parse_errors == 0
    # data refs that later drive slicing
    refs = [(f.name, f.detail["access"]) for f in kinds["data_ref"]]
    assert ("EMP-HOURS", "read") in refs
    assert ("WS-GROSS", "write") in refs
    assert ("PAY-GROSS", "write") in refs


def test_cobol_adapter_never_raises_on_broken():
    text = (ESTATE / "cobol" / "BROKEN.cbl").read_text()
    res = parse_cobol(text)
    assert res.parse_errors > 0


def test_copybook_emprec_layout():
    res = parse_copybook((ESTATE / "cobol/copybooks/EMPREC.cpy").read_text())
    fields = {f.name: f.detail for f in res.facts if f.fact_type == "field"}
    assert fields["EMP-ID"]["length"] == 6
    assert fields["EMP-HOURS"]["usage"] == "COMP-3"
    assert fields["EMP-HOURS"]["length"] == 3       # 5 digits packed
    assert fields["EMP-HOURS"]["decimals"] == 2
    assert fields["EMP-RATE"]["length"] == 4        # 6 digits packed
    assert fields["EMP-STATE"]["offset"] == 43
    conds = {f.name for f in res.facts if f.fact_type == "condition_name"}
    assert conds == {"EMP-ACTIVE", "EMP-TERMED"}
    assert pic_meta("S9(7)V99", "COMP-3")["length"] == 5


def test_jcl_adapter_payroll():
    res = parse_jcl((ESTATE / "jcl" / "PAYROLL.jcl").read_text())
    facts = {}
    for f in res.facts:
        facts.setdefault(f.fact_type, []).append(f)
    assert facts["job"][0].name == "PAYRUN"
    steps = {f.detail["step"]: f.detail for f in facts["step"]}
    assert steps["STEP010"]["pgm"] == "PAYCALC"
    assert steps["STEP020"]["pgm"] == "IKJEFT01"
    dds = {f.detail["ddname"]: f.detail for f in facts["dd"]}
    assert dds["EMPMAST"]["dsn"] == "PROD.EMP.MASTER"
    assert dds["PAYOUT"]["dsn"] == "PROD.PAY.OUT"
    runs = facts["runs_program"][0]
    assert runs.name == "PAYRPT" and runs.detail["plan"] == "PAYPLAN"
    assert res.parse_errors == 0


def test_jcl_adapter_xferjob_transfers():
    res = parse_jcl((ESTATE / "jcl" / "XFERJOB.jcl").read_text())
    transfers = [f for f in res.facts if f.fact_type == "transfer"]
    ndm = next(t for t in transfers if t.detail["protocol"] == "ndm")
    assert ndm.detail["node"] == "BANKNODE1"
    assert ndm.detail["from_dsn"] == "PROD.PAY.OUT"
    assert ndm.detail["to_dsn"] == "BANK.PAY.IN"
    sftp = next(t for t in transfers if t.detail["protocol"] == "sftp")
    assert sftp.detail["node"] == "sftp.vendorx.com"
    assert sftp.detail["from_dsn"] == "PROD.PAY.RPT"


def test_graph_impact_and_dead(workspace, capsys):
    run_cli(workspace, "graph", "--impact", "PAYCALC")
    out = capsys.readouterr().out
    assert "job PAYRUN depends on it (via step STEP010)" in out
    assert "PROD.EMP.MASTER" in out and "EMPMAST DD" in out
    assert "writes table PAYROLL_AUDIT" in out

    run_cli(workspace, "graph", "--dead")
    out = capsys.readouterr().out
    assert "ORPHAN" in out

    run_cli(workspace, "graph", "--lineage", "PROD.PAY.OUT")
    out = capsys.readouterr().out
    assert "PAYCALC" in out


def test_graph_writes_edge_in_store(workspace):
    con = sqlite3.connect(workspace / "legacymod.db")
    row = con.execute(
        "SELECT COUNT(*) FROM edges e"
        " JOIN nodes s ON s.id=e.src_node JOIN nodes d ON d.id=e.dst_node"
        " WHERE s.name='PAYCALC' AND d.name='PAYROLL_AUDIT'"
        " AND e.edge_type='writes'").fetchone()
    con.close()
    assert row[0] == 1


def test_graph_exports(workspace):
    gj = json.loads((workspace / "graph.json").read_text())
    assert gj["nodes"] and gj["edges"]
    assert (workspace / "graph.mmd").read_text().startswith("flowchart")
