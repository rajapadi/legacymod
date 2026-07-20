"""CLI smoke tests: parser wiring, status command, stub dispatch."""

from __future__ import annotations

from legacymod.cli import build_parser, main


def test_parser_has_all_subcommands():
    parser = build_parser()
    text = parser.format_help()
    for cmd in ("ingest", "analyze", "graph", "assess", "slice", "docs",
                "rules", "interfaces", "runs", "reconcile", "flows",
                "decompose", "spec", "datamig", "generate", "validate",
                "review", "report", "status"):
        assert cmd in text


def test_status_runs_without_workspace(tmp_path, capsys):
    rc = main(["--workspace", str(tmp_path / "nope"), "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no store yet" in out


def test_no_command_prints_help(capsys):
    rc = main([])
    assert rc == 2
    assert "usage" in capsys.readouterr().out.lower()
