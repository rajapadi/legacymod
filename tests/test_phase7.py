"""Phase 7 hardening: coverage for JCL utility paths, validate flows,
report, config, claude_cli provider guard."""

from __future__ import annotations

import json
import sys

import pytest

from legacymod.adapters.jcl import parse_jcl
from legacymod.config import Config, load_config

from conftest import run_cli


def _facts(res, ftype):
    return [f for f in res.facts if f.fact_type == ftype]


def test_jcl_iebgener_and_repro_lineage():
    res = parse_jcl("""//COPYJOB  JOB (ACCT),'COPY'
//STEP010  EXEC PGM=IEBGENER
//SYSUT1   DD DSN=IN.FILE,DISP=SHR
//SYSUT2   DD DSN=OUT.FILE,DISP=(NEW,CATLG)
//STEP020  EXEC PGM=IDCAMS
//SYSIN    DD *
  REPRO INDATASET(A.B.C) OUTDATASET(D.E.F)
/*
""")
    lineage = _facts(res, "lineage")
    assert {"IEBGENER", "IDCAMS REPRO"} == {f.detail["via"] for f in lineage}
    geners = next(f for f in lineage if f.detail["via"] == "IEBGENER")
    assert geners.detail["from_dsn"] == "IN.FILE"
    assert geners.detail["to_dsn"] == "OUT.FILE"


def test_jcl_bpxbatch_and_xcom_and_symbols():
    res = parse_jcl("""//USSJOB   JOB (ACCT),'SFTP'
//         SET ENV=PROD
//STEP010  EXEC PGM=BPXBATCH,PARM='SH sftp -b bat.txt user@files.example.org'
//STEP020  EXEC PGM=XCOMJOB
//SYSIN01  DD *
  REMOTE_SYSTEM=xcom.example.org
  LOCAL_FILE=PROD.XFER.OUT
/*
//STEP030  EXEC PGM=FTP
//INPUT    DD *
  open plain.example.org
  get remote.dat 'PROD.IN.FILE'
/*
""")
    transfers = {f.detail["protocol"]: f.detail for f in
                 _facts(res, "transfer")}
    assert transfers["sftp"]["node"] == "files.example.org"
    assert transfers["xcom"]["node"] == "xcom.example.org"
    assert transfers["ftp"]["direction"] == "inbound"
    sym = _facts(res, "symbolic")[0]
    assert sym.name == "ENV" and sym.detail["value"] == "PROD"


def test_jcl_include_and_bad_lines():
    res = parse_jcl("//J JOB X\n//   INCLUDE MEMBER=COMMON\nGARBAGE LINE\n")
    assert _facts(res, "include")[0].name == "COMMON"
    assert res.parse_errors >= 1


def test_validate_csv_case_and_missing_files(workspace, capsys):
    fx = workspace / "fixtures" / "payroll"
    csvcase = fx / "case_csvdemo"
    csvcase.mkdir(parents=True, exist_ok=True)
    (csvcase / "case.json").write_text(json.dumps({
        "format": "csv",
        "legacy_cmd": [sys.executable, "-c", "pass"],
        "modern_cmd": [sys.executable, "-c", "pass"]}), encoding="utf-8")
    (csvcase / "expected.csv").write_text("id,amt\n1,10\n", encoding="utf-8")
    (csvcase / "actual.csv").write_text("id,amt\n1,10\n", encoding="utf-8")
    empty = fx / "case_empty"
    empty.mkdir(exist_ok=True)
    (empty / "case.json").write_text("{}", encoding="utf-8")
    try:
        rc = run_cli(workspace, "validate", "payroll")
        out = capsys.readouterr().out
        assert rc == 2                       # shipped case_fail still fails
        assert "case_csvdemo: PASS" in out
        assert "case_empty: FAIL - missing expected" in out
        report = (workspace / "validation" / "payroll" /
                  "report.md").read_text()
        assert "case_csvdemo" in report
    finally:
        import shutil
        shutil.rmtree(csvcase)
        shutil.rmtree(empty)


def test_validate_oracle_skips_without_cobc(workspace, monkeypatch):
    import legacymod.validate as v
    monkeypatch.setattr(v.shutil, "which", lambda *_: None)
    from legacymod.store import Store
    cfg = Config(workspace=workspace)
    with Store(cfg) as store:
        assert v._oracle(workspace, "cobol/PAYCALC.cbl", store) is None


def test_report_command(workspace, capsys):
    assert run_cli(workspace, "report") == 0
    out = capsys.readouterr().out
    assert "report.md" in out
    text = (workspace / "report.md").read_text()
    assert "Verdict up front" in text
    assert "Parse coverage" in text
    assert (workspace / "report_summary.csv").exists()


def test_config_loading(tmp_path):
    toml = tmp_path / "legacymod.toml"
    toml.write_text('workspace = "./ws2"\n[llm]\nprovider = "claude_cli"\n'
                    'model = "m1"\n', encoding="utf-8")
    cfg = load_config(toml)
    assert cfg.workspace.name == "ws2"
    assert cfg.llm_provider == "claude_cli" and cfg.llm_model == "m1"
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.toml")
    assert load_config(None, tmp_path / "ov").workspace == tmp_path / "ov"


def test_claude_cli_requires_binary(monkeypatch):
    from legacymod.llm import claude_cli
    monkeypatch.setattr(claude_cli.shutil, "which", lambda *_: None)
    with pytest.raises(RuntimeError, match="claude"):
        claude_cli.ClaudeCliProvider().complete("hi", "test")


def test_claude_cli_invokes_subprocess(monkeypatch):
    from legacymod.llm import claude_cli

    class FakeProc:
        returncode = 0
        stdout = "analysis text"
        stderr = ""

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(claude_cli.shutil, "which", lambda *_: "claude.exe")
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    result = claude_cli.ClaudeCliProvider("modelx").complete("prompt", "t")
    assert result.text == "analysis text"
    assert captured["cmd"][0] == "claude.exe"
    assert "--model" in captured["cmd"]
