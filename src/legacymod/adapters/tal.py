"""TAL (Tandem Application Language) adapter — llm_assisted by design.

No open-source TAL parser exists (verified 2026-07-12), so this adapter
deliberately does NOT attempt a grammar. A regex pass extracts the
deterministic skeleton (PROC / SUBPROC / INT / STRING declarations and
CALL sites); the LLM provider proposes structure/semantics on top.
**Every fact from this adapter is marked origin='llm', needs_review=1**
(enforced by the analyze driver for the whole llm_assisted tier) — the
skeleton is regex-derived, but nothing TAL leaves this adapter with
parser-grade trust.

Fact shapes:

- ``tal_proc`` — PROC/SUBPROC declaration (kind, return_type).
- ``calls`` — CALL sites (dynamic 0).
- ``tal_analysis`` — the provider's structure proposal (provider, model,
  cached), clearly a proposal, never consumed downstream without human
  review.
"""

from __future__ import annotations

import logging
import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult

log = logging.getLogger(__name__)


def parse_tal_skeleton(text: str) -> ParseResult:
    res = ParseResult()
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("!"):
            continue
        m = re.match(r"(?:(INT|STRING|FIXED|REAL)\s+)?(PROC|SUBPROC)\s+"
                     r"([A-Za-z^_]\w*)", line, re.I)
        if m:
            res.facts.append(Fact("tal_proc", m.group(3).lower(),
                                  {"kind": m.group(2).upper(),
                                   "return_type": (m.group(1) or "").upper()},
                                  no, no))
            continue
        m = re.match(r"CALL\s+([A-Za-z^_]\w*)", line, re.I)
        if m:
            res.facts.append(Fact("calls", m.group(1).lower(),
                                  {"dynamic": 0, "paragraph": ""}, no, no))
    return res


class TalAdapter:
    name = "tal"
    tier = "llm_assisted"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "tal"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        res = parse_tal_skeleton(text)
        if ctx.store is not None:
            from ..llm.provider import complete_cached
            prompt = ("Summarize the structure and purpose of this TAL "
                      f"source ({artifact.path}) in 3 sentences for a "
                      f"modernization inventory:\n{text[:4000]}\n")
            result, _ = complete_cached(ctx.store, ctx.cfg, prompt,
                                        purpose="tal_structure",
                                        artifact=artifact.path)
            res.facts.append(Fact("tal_analysis", artifact.path,
                                  {"text": result.text, "model": result.model},
                                  1, text.count("\n") + 1,
                                  origin="llm", confidence=result.confidence,
                                  needs_review=1))
        return res


ADAPTER: Adapter = TalAdapter()
