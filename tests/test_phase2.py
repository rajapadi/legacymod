"""Phase 2 acceptance: rule mining + docs generation."""

from __future__ import annotations

import csv

from conftest import run_cli


def _rules(ws):
    run_cli(ws, "rules")
    with open(ws / "rules.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_rules_finds_required_candidates(workspace):
    rules = _rules(workspace)
    assert len(rules) >= 3
    paycalc = [r for r in rules if r["program"] == "PAYCALC"]

    comp = [r for r in paycalc if r["category"] == "computation"]
    assert any("1.5" in r["snippet"] and "COMPUTE WS-GROSS" in r["snippet"]
               for r in comp), "overtime computation not mined"

    val = [r for r in paycalc if r["category"] == "validation"]
    assert any("9999.99" in r["snippet"] for r in val), "pay-cap check not mined"

    st = [r for r in paycalc if r["category"] in ("state_transition", "routing")]
    assert any("'H'" in r["snippet"] and "'P'" in r["snippet"] for r in st), \
        "H/P status assignment not mined"


def test_rules_line_ranges_match_source(workspace):
    from conftest import ESTATE
    src = (ESTATE / "cobol" / "PAYCALC.cbl").read_text().splitlines()
    for r in _rules(workspace):
        if r["program"] != "PAYCALC":
            continue
        lo, hi = (int(x) for x in r["source_lines"].split("-"))
        window = "\n".join(l.strip() for l in src[lo - 1:hi])
        first_snip = r["snippet"].splitlines()[0].strip()
        assert first_snip in window, \
            f"{r['rule_id']} lines {r['source_lines']} do not contain snippet"


def test_rules_remine_preserves_status(workspace):
    import sqlite3
    rules = _rules(workspace)
    rid = rules[0]["rule_id"]
    con = sqlite3.connect(workspace / "legacymod.db")
    con.execute("UPDATE rules SET status='approved' WHERE rule_id=?", (rid,))
    con.commit()
    con.close()
    rules2 = {r["rule_id"]: r for r in _rules(workspace)}
    assert rules2[rid]["status"] == "approved"


def test_docs_generated(workspace):
    run_cli(workspace, "docs")
    docs = workspace / "docs"
    crud = (docs / "crud.md").read_text()
    assert "PAYCALC" in crud and "PAYROLL_AUDIT" in crud
    page = (docs / "programs" / "PAYCALC.md").read_text()
    assert "PAYROLL_AUDIT | C" in page
    assert "PROD.EMP.MASTER" in page
    assert (docs / "index.md").read_text().startswith("# System overview")
    assert (docs / "jobs" / "PAYRUN.md").exists()
