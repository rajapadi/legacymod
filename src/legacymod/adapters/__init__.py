"""Adapter registry and the `analyze` stage driver.

To add a technology: create one module in this package exposing an
``ADAPTER`` object implementing the protocol in ``base.py`` and add its
module name to ``_ADAPTER_MODULES``. See README "Adding an adapter" for a
worked PL/I example.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging

from ..config import Config
from ..store import Store
from .base import Adapter, ArtifactRef, ParseContext

log = logging.getLogger(__name__)

_ADAPTER_MODULES = [
    "cobol", "copybook", "jcl", "db2ddl", "cics_bms", "cics_csd", "mqsc",
    "ims", "rexx", "easytrieve", "scheduler_ca7", "scheduler_controlm",
    "tal", "hpns_cobol",
]


def registry() -> list[Adapter]:
    adapters: list[Adapter] = []
    for mod_name in _ADAPTER_MODULES:
        try:
            mod = importlib.import_module(f"{__name__}.{mod_name}")
        except ModuleNotFoundError:
            continue  # phase not built yet
        adapters.append(mod.ADAPTER)
    return adapters


def _link_ims_psbs(store) -> None:
    """Post-pass: derive program->PSB links for CBLTDLI/AIBTDLI callers.

    Real linkage lives in JCL (DFSRRC00 PARM) or online PSB scheduling,
    which a source-only estate lacks. Heuristic per docs/decisions.md:
    name-stem match first, else — when the estate has exactly one PSB —
    that PSB with confidence 0.5. Always needs_review=1.
    """
    psbs = [r["name"] for r in store.query(
        "SELECT name FROM facts WHERE fact_type='ims_psb'")]
    if not psbs:
        return
    callers = store.query(
        "SELECT DISTINCT f.artifact_id, p.name prog FROM facts f"
        " JOIN facts p ON p.artifact_id=f.artifact_id"
        " AND p.fact_type='program' WHERE f.fact_type='ims_call'")
    for c in callers:
        match = next((p for p in psbs
                      if p[:3].upper() == c["prog"][:3].upper()), None)
        psb, conf, how = (match, 0.7, "name-stem match") if match else \
            (psbs[0], 0.5, "sole PSB in estate") if len(psbs) == 1 else \
            (None, 0, "")
        if not psb:
            continue
        store.execute(
            "DELETE FROM facts WHERE artifact_id=? AND fact_type='ims_psb_link'",
            (c["artifact_id"],))
        store.execute(
            "INSERT INTO facts (artifact_id, fact_type, name, detail_json,"
            " source_line_start, source_line_end, origin, confidence,"
            " needs_review) VALUES (?,?,?,?,0,0,'parser',?,1)",
            (c["artifact_id"], "ims_psb_link", psb,
             json.dumps({"program": c["prog"], "psb": psb, "method": how}),
             conf))
    store.commit()


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        root = store.source_root()
        adapters = registry()
        ctx = ParseContext(cfg, store=store)
        store.execute("DELETE FROM facts")
        rows = store.query(
            "SELECT id, path, artifact_type, language, encoding, loc "
            "FROM artifacts ORDER BY path")
        totals: dict[str, int] = {}
        skipped = 0
        for row in rows:
            artifact = ArtifactRef(row["id"], row["path"], row["artifact_type"],
                                   row["language"] or "", row["encoding"] or "",
                                   row["loc"] or 0)
            adapter = next((a for a in adapters if a.applicable(artifact)), None)
            if adapter is None:
                skipped += 1
                continue
            if artifact.encoding not in ("ascii",):
                # EBCDIC/binary flagged at ingest — never converted silently.
                log.warning("skipping %s (encoding %s) — convert explicitly "
                            "before analysis", artifact.path, artifact.encoding)
                skipped += 1
                continue
            text = (root / artifact.path).read_text(encoding="utf-8",
                                                    errors="replace")
            try:
                result = adapter.parse(artifact, text, ctx)
            except Exception:  # island promise: an adapter bug must not kill the run
                log.exception("adapter %s crashed on %s — recorded as parse error",
                              adapter.name, artifact.path)
                store.execute(
                    "UPDATE artifacts SET parse_errors=?, adapter=?, tier=? "
                    "WHERE id=?", (999, adapter.name, adapter.tier, artifact.id))
                continue
            if adapter.tier == "llm_assisted":
                # nothing from this tier gets parser-grade trust
                for f in result.facts:
                    f.origin = "llm"
                    f.needs_review = 1
                    f.confidence = min(f.confidence, 0.6)
            store.executemany(
                "INSERT INTO facts (artifact_id, fact_type, name, detail_json,"
                " source_line_start, source_line_end, origin, confidence,"
                " needs_review) VALUES (?,?,?,?,?,?,?,?,?)",
                [(artifact.id, f.fact_type, f.name, json.dumps(f.detail),
                  f.line_start, f.line_end, f.origin, f.confidence,
                  f.needs_review) for f in result.facts])
            store.execute(
                "UPDATE artifacts SET parse_errors=?, adapter=?, tier=? WHERE id=?",
                (result.parse_errors, adapter.name, adapter.tier, artifact.id))
            totals[adapter.name] = totals.get(adapter.name, 0) + len(result.facts)
        store.commit()
        _link_ims_psbs(store)
        store.export_csv(
            "SELECT a.path, f.fact_type, f.name, f.detail_json,"
            " f.source_line_start, f.source_line_end, f.origin, f.confidence,"
            " f.needs_review FROM facts f JOIN artifacts a ON a.id=f.artifact_id"
            " ORDER BY a.path, f.source_line_start",
            cfg.workspace / "facts.csv")
        store.export_csv(
            "SELECT path, artifact_type, language, encoding, loc, sha256,"
            " parse_errors, adapter, tier, confidence FROM artifacts ORDER BY path",
            cfg.workspace / "inventory.csv")
        nfacts = store.query("SELECT COUNT(*) c FROM facts")[0]["c"]
        parsed = store.query(
            "SELECT COUNT(*) c FROM artifacts WHERE adapter IS NOT NULL")[0]["c"]
        print(f"analyze: {nfacts} facts from {parsed} artifacts "
              f"({skipped} skipped: no adapter yet or non-ASCII)")
        for name in sorted(totals):
            print(f"  {name:18s} {totals[name]:>5d} facts")
    return 0
