"""Phase 3 acceptance: stub provider, cache, log, review queue."""

from __future__ import annotations

import csv
import sqlite3

from conftest import run_cli


def test_enrich_marks_and_caches(workspace):
    run_cli(workspace, "rules")
    run_cli(workspace, "rules", "--enrich")
    con = sqlite3.connect(workspace / "legacymod.db")
    # human-decided rules are skipped by enrichment; assert on the rest
    rows = con.execute("SELECT plain_english, origin, needs_review, status"
                       " FROM rules WHERE status NOT IN"
                       " ('approved', 'rejected')").fetchall()
    assert rows
    for text, origin, nr, status in rows:
        assert "[STUB PLACEHOLDER" in text
        assert origin == "llm" and nr == 1 and status == "explained"
    con.close()

    # second run must be served from cache and say so in the log
    run_cli(workspace, "rules", "--enrich")
    with open(workspace / "llm_log.csv", newline="", encoding="utf-8") as fh:
        log = list(csv.DictReader(fh))
    assert log, "llm_log.csv empty"
    assert sum(int(r["cache_hit"]) for r in log) >= len(rows)
    assert all(r["provider"] == "stub" for r in log)


def test_stub_is_deterministic():
    from legacymod.llm.stub import StubProvider
    a = StubProvider().complete("IF X > 1 MOVE Y TO Z", "test")
    b = StubProvider().complete("IF X > 1 MOVE Y TO Z", "test")
    assert a.text == b.text
    assert "[STUB PLACEHOLDER" in a.text


def test_docs_enrich_appends_marked_block(workspace):
    run_cli(workspace, "docs", "--enrich")
    page = (workspace / "docs" / "programs" / "PAYCALC.md").read_text()
    assert "> **AI-generated** (provider=stub" in page
    assert "needs_review" in page


def test_review_export_and_apply(workspace):
    run_cli(workspace, "rules", "--enrich")
    run_cli(workspace, "review")
    queue = workspace / "review_queue.csv"
    with open(queue, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    rule_rows = [r for r in rows if r["item_type"] == "rule"]
    assert rule_rows
    rule_rows[0]["decision"] = "approve"
    with open(queue, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    run_cli(workspace, "review", "--apply")
    con = sqlite3.connect(workspace / "legacymod.db")
    status = con.execute("SELECT status FROM rules WHERE rule_id=?",
                         (rule_rows[0]["id"],)).fetchone()[0]
    accepted = con.execute(
        "SELECT COUNT(*) FROM llm_log WHERE accepted_by_human=1").fetchone()[0]
    con.close()
    assert status == "approved"
    assert accepted >= 1


def test_unknown_provider_rejected(tmp_path):
    from legacymod.config import Config
    from legacymod.llm.provider import get_provider
    import pytest
    cfg = Config(workspace=tmp_path, llm_provider="nope")
    with pytest.raises(ValueError):
        get_provider(cfg)
