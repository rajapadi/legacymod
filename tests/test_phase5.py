"""Phase 5 acceptance: behavior-first validation harness."""

from __future__ import annotations

import csv

from legacymod.validate import compare_csv, compare_flat

from conftest import run_cli


def test_validate_payroll_fixtures(workspace, capsys):
    rc = run_cli(workspace, "validate", "payroll")
    out = capsys.readouterr().out
    assert rc == 2                       # one deliberate failure -> FAIL
    assert "case_pass: PASS" in out
    assert "case_fail: FAIL" in out
    assert "PAY-GROSS" in out            # names the exact field
    report = (workspace / "validation" / "payroll" / "report.md").read_text()
    assert "expected '1234.50' actual '1234.99'" in report
    with open(workspace / "validation" / "payroll" / "results.csv",
              newline="", encoding="utf-8") as fh:
        rows = {r["case_name"]: r for r in csv.DictReader(fh)}
    assert rows["case_pass"]["passed"] == "1"
    assert rows["case_fail"]["passed"] == "0"
    assert "PAY-GROSS" in rows["case_fail"]["mismatched_fields"]


def test_compare_flat_packed_decimal_aware():
    layout = [
        {"name": "ID", "offset": 0, "length": 3, "kind": "display_char",
         "digits": 0, "decimals": 0, "signed": 0},
        {"name": "AMT", "offset": 3, "length": 3, "kind": "comp3",
         "digits": 5, "decimals": 2, "signed": 1},
    ]
    rec_a = "AB1".encode("cp037") + bytes.fromhex("12345c")
    rec_b = "AB1".encode("cp037") + bytes.fromhex("12345d")   # negative
    assert compare_flat(layout, 6, rec_a, rec_a) == []
    mism = compare_flat(layout, 6, rec_a, rec_b)
    assert len(mism) == 1 and "AMT" in mism[0]
    assert "'123.45'" in mism[0] and "'-123.45'" in mism[0]


def test_compare_flat_record_count():
    layout = [{"name": "X", "offset": 0, "length": 1, "kind": "display_char",
               "digits": 0, "decimals": 0, "signed": 0}]
    mism = compare_flat(layout, 1, b"\xc1\xc2", b"\xc1")
    assert any("record-count" in m for m in mism)


def test_compare_flat_ascii_encoding():
    layout = [{"name": "F1", "offset": 0, "length": 3, "kind": "display_char",
               "digits": 0, "decimals": 0, "signed": 0}]
    mism = compare_flat(layout, 3, b"ABC", b"ABD", "ascii")
    assert mism == ["record 1 field F1: expected 'ABC' actual 'ABD'"]
    assert compare_flat(layout, 3, b"ABC", b"ABC", "ascii") == []


def test_oracle_cmd_env_construction(tmp_path, monkeypatch):
    """The oracle must honor dialect, includes, and output-DD mapping
    (shapes taken from the CardDemo CBACT01C fixture)."""
    from legacymod import validate as v

    calls = []
    monkeypatch.setattr(v.shutil, "which", lambda name: "cobc")
    monkeypatch.setattr(v.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw.get("env"))))

    class FakeStore:
        def source_root(self):
            return tmp_path / "estate"

    case = tmp_path / "case_x"
    case.mkdir()
    (case / "input.ACCTFILE").write_bytes(b"x")
    meta = {"oracle_source": "cbl/CBACT01C.cbl", "oracle_std": "ibm",
            "oracle_includes": ["cpy"],
            "oracle_outputs": {"OUTFILE": "expected.dat"}}
    assert v._oracle(case, meta, FakeStore()) is not None

    compile_cmd = calls[0][0]
    assert "-std" in compile_cmd and "ibm" in compile_cmd
    inc = compile_cmd[compile_cmd.index("-I") + 1]
    assert inc.endswith("cpy")
    run_env = calls[1][1]
    assert run_env["DD_ACCTFILE"].endswith("input.ACCTFILE")
    assert run_env["DD_OUTFILE"].endswith("expected.dat")


def test_compare_csv_column_aware(tmp_path):
    a = tmp_path / "e.csv"
    b = tmp_path / "a.csv"
    a.write_text("id,amt\n1,10.00\n2,20.00\n", encoding="utf-8")
    b.write_text("id,amt\n1,10.00\n2,20.01\n", encoding="utf-8")
    mism = compare_csv(a, b)
    assert mism == ["row 2 column amt: expected '20.00' actual '20.01'"]


def test_validate_unknown_unit(workspace, capsys):
    assert run_cli(workspace, "validate", "nope") == 1
