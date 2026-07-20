"""Phase 6 acceptance: remaining adapters over the full sample estate."""

from __future__ import annotations

import json
import sqlite3

from legacymod.adapters.cics_bms import parse_bms
from legacymod.adapters.cics_csd import parse_csd
from legacymod.adapters.ims import parse_ims
from legacymod.adapters.mqsc import parse_mqsc
from legacymod.adapters.scheduler_ca7 import parse_ca7
from legacymod.adapters.scheduler_controlm import parse_controlm
from legacymod.adapters.tal import parse_tal_skeleton

from conftest import ESTATE


def _facts(ws, sql, *params):
    con = sqlite3.connect(ws / "legacymod.db")
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, params)]
    con.close()
    return rows


def test_every_adapter_emits_facts(workspace):
    rows = _facts(workspace,
                  "SELECT DISTINCT adapter FROM artifacts"
                  " WHERE adapter IS NOT NULL")
    adapters = {r["adapter"] for r in rows}
    assert {"cobol", "copybook", "jcl", "db2ddl", "cics_bms", "cics_csd",
            "mqsc", "ims", "rexx", "easytrieve", "scheduler_ca7",
            "scheduler_controlm", "tal", "hpns_cobol"} <= adapters
    unparsed = _facts(workspace,
                      "SELECT path FROM artifacts WHERE adapter IS NULL")
    assert not unparsed, f"artifacts with no adapter: {unparsed}"


def test_cics_facts_from_payinq(workspace):
    rows = _facts(workspace,
                  "SELECT f.name FROM facts f JOIN artifacts a"
                  " ON a.id=f.artifact_id WHERE f.fact_type='cics'"
                  " AND a.path LIKE '%PAYINQ%'")
    commands = {r["name"] for r in rows}
    assert {"RECEIVE", "READ", "SEND", "RETURN"} <= {c.split()[0]
                                                     for c in commands}


def test_cbltdli_psb_link(workspace):
    rows = _facts(workspace, "SELECT * FROM facts"
                             " WHERE fact_type='ims_psb_link'")
    assert rows, "no ims_psb_link derived"
    d = json.loads(rows[0]["detail_json"])
    assert d["program"] == "PAYIMS" and d["psb"] == "PAYPSB"
    assert rows[0]["needs_review"] == 1


def test_scheduler_edges_both_schedulers(workspace):
    deps = _facts(workspace,
                  "SELECT name, detail_json FROM facts"
                  " WHERE fact_type='sched_dep'")
    pairs = {(r["name"], json.loads(r["detail_json"])["job"]) for r in deps}
    assert ("PAYRUN", "PAYRPT") in pairs          # CA7
    assert ("PAYRUN", "XFERJOB") in pairs         # CA7
    assert ("PAYRUN2", "PAYRPT2") in pairs        # Control-M
    ext = [r for r in deps if r["name"] == "EXT.BANKACK"]
    assert ext and json.loads(ext[0]["detail_json"])["external"] == 1


def test_tal_facts_all_llm_marked(workspace):
    rows = _facts(workspace,
                  "SELECT f.origin, f.needs_review FROM facts f"
                  " JOIN artifacts a ON a.id=f.artifact_id"
                  " WHERE a.artifact_type='tal'")
    assert rows
    assert all(r["origin"] == "llm" and r["needs_review"] == 1 for r in rows)


def test_enter_tal_divergence_from_paysrv(workspace):
    rows = _facts(workspace,
                  "SELECT a.path FROM facts f JOIN artifacts a"
                  " ON a.id=f.artifact_id WHERE f.fact_type='enter_tal'")
    assert any("PAYSRV" in r["path"] for r in rows)


# unit-level parser checks
def test_bms_parser():
    res = parse_bms((ESTATE / "bms" / "PAYMAP.bms").read_text())
    kinds = {f.fact_type for f in res.facts}
    assert {"mapset", "map", "screen_field"} <= kinds
    fields = [f for f in res.facts if f.fact_type == "screen_field"]
    assert len(fields) == 3
    empid = next(f for f in fields if f.name == "EMPID")
    assert empid.detail["pos"] == ["3", "10"] and empid.detail["length"] == 6


def test_csd_parser():
    res = parse_csd((ESTATE / "cics" / "PAYCSD.txt").read_text())
    tx = next(f for f in res.facts if f.fact_type == "csd_transaction")
    assert tx.name == "PYIQ" and tx.detail["program"] == "PAYINQ"
    fl = next(f for f in res.facts if f.fact_type == "csd_file")
    assert fl.name == "EMPMAST" and fl.detail["dsname"] == "PROD.EMP.MASTER"


def test_mqsc_parser():
    res = parse_mqsc((ESTATE / "mq" / "PAYMQ.mqsc").read_text())
    qr = next(f for f in res.facts if f.fact_type == "mq_qremote")
    assert qr.name == "BANK.ACK.QUEUE"
    assert qr.detail["rqmname"] == "BANKQM"
    assert qr.detail["xmitq"] == "BANK.XMITQ"
    ch = next(f for f in res.facts if f.fact_type == "mq_channel")
    assert ch.name == "TO.BANKQM"
    assert "bankmq.example.com" in ch.detail["conname"]


def test_ims_parser():
    dbd = parse_ims((ESTATE / "ims" / "EMPDBD.dbd").read_text(), "ims_dbd")
    assert next(f for f in dbd.facts if f.fact_type == "ims_dbd").name == "EMPDBD"
    seg = next(f for f in dbd.facts if f.fact_type == "ims_segment")
    assert seg.name == "EMPSEG" and seg.detail["bytes"] == 44
    psb = parse_ims((ESTATE / "ims" / "PAYPSB.psb").read_text(), "ims_psb")
    pcb = next(f for f in psb.facts if f.fact_type == "ims_pcb")
    assert pcb.detail["dbdname"] == "EMPDBD"
    assert pcb.detail["procopt"] == "A" and pcb.detail["intent"] == "write"


def test_ca7_parser():
    res = parse_ca7((ESTATE / "scheduler" / "ca7_schedule.txt").read_text())
    jobs = {f.name: f.detail for f in res.facts if f.fact_type == "sched_job"}
    assert jobs["PAYRUN"]["time"] == "0200"
    assert jobs["PAYRUN"]["calendar"] == "BUSDAYS"
    assert jobs["XFERJOB"]["calendar"] == "WEEKLY"
    assert "OLDPURGE" not in jobs                 # deliberately unscheduled


def test_controlm_parser():
    res = parse_controlm(
        (ESTATE / "scheduler" / "controlm_export.xml").read_text())
    deps = [f for f in res.facts if f.fact_type == "sched_dep"]
    assert deps[0].name == "PAYRUN2" and deps[0].detail["job"] == "PAYRPT2"
    res_bad = parse_controlm("<not-xml")
    assert res_bad.parse_errors == 1


def test_tal_skeleton_parser():
    res = parse_tal_skeleton((ESTATE / "tal" / "PAYUTIL.tal").read_text())
    procs = {f.name for f in res.facts if f.fact_type == "tal_proc"}
    assert {"pay_round", "pay_format", "digits"} <= procs
