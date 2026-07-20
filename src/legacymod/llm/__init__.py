"""LLM subsystem: bounded enrichment flows used by docs and rules.

The LLM never gets the final word: every output lands as clearly marked
text with ``origin=llm``/``needs_review=1``, cached by prompt hash and
logged to llm_log. Enrichment only runs behind explicit ``--enrich`` /
``--llm-impl`` flags.
"""

from __future__ import annotations

import json
import logging

from ..config import Config
from ..store import Store
from .provider import ai_block, complete_cached, get_provider

log = logging.getLogger(__name__)

__all__ = ["enrich_rules", "enrich_program_doc", "propose_impl",
           "complete_cached", "get_provider", "ai_block"]


def enrich_rules(store: Store, cfg: Config) -> int:
    """Fill plain_english for un-explained rule candidates."""
    provider = get_provider(cfg)
    n = hits = 0
    # candidates get explained; explained-but-unapproved rules re-run
    # deterministically through the cache. Human-decided rules untouched.
    for r in store.query("SELECT rule_id, program, category, snippet FROM rules"
                         " WHERE status='candidate'"
                         " OR (status='explained' AND needs_review=1)"):
        prompt = (
            "Explain this COBOL business-rule candidate in one plain-English "
            f"sentence for a business analyst.\nProgram: {r['program']}\n"
            f"Category: {r['category']}\nSnippet:\n{r['snippet']}\n")
        result, hit = complete_cached(store, cfg, prompt,
                                      purpose="rule_explanation",
                                      artifact=f"rule:{r['rule_id']}")
        store.execute(
            "UPDATE rules SET plain_english=?, origin='llm', confidence=?,"
            " status='explained', needs_review=1 WHERE rule_id=?",
            (result.text, result.confidence, r["rule_id"]))
        n += 1
        hits += 1 if hit else 0
    store.commit()
    print(f"  enriched {n} rule(s) via {provider.name} "
          f"({hits} cache hit(s)); all marked origin=llm, needs_review=1")
    return n


def enrich_program_doc(store: Store, cfg: Config, program: str) -> str:
    """Marked AI narrative block for a program page, or '' if no facts."""
    rows = store.query(
        "SELECT f.fact_type, f.name, f.detail_json FROM facts f"
        " JOIN facts p ON p.artifact_id=f.artifact_id"
        "  AND p.fact_type='program' AND p.name=?"
        " WHERE f.fact_type IN ('calls', 'sql', 'cics', 'select', 'performs')"
        " ORDER BY f.id", (program,))
    if not rows:
        return ""
    summary = "\n".join(f"{r['fact_type']} {r['name']} "
                        f"{r['detail_json']}" for r in rows[:50])
    prompt = ("Write a short narrative summary (3-5 sentences) of what this "
              f"COBOL program does, for a modernization spec.\n"
              f"Program: {program}\nExtracted facts:\n{summary}\n")
    result, _ = complete_cached(store, cfg, prompt,
                                purpose="program_narrative",
                                artifact=f"program:{program}")
    provider = get_provider(cfg)
    return ai_block(result, provider.name)


def propose_impl(store: Store, cfg: Config, unit: str, method: str,
                 rule_snippets: str) -> str:
    """LLM-proposed method body draft (Phase 4 codegen, --llm-impl)."""
    prompt = ("Propose a Java method body implementing these COBOL rule "
              f"snippets. Unit: {unit}. Method: {method}.\n{rule_snippets}\n")
    result, _ = complete_cached(store, cfg, prompt, purpose="impl_draft",
                                artifact=f"unit:{unit}:{method}")
    return result.text
