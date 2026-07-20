"""HPNS (HP NonStop / Tandem) COBOL adapter.

Deterministic tier where z/OS COBOL rules apply — reuses the COBOL island
parser. NonStop divergences are mined by regex and marked:

- ``enter_tal`` — ENTER TAL statement (emitted by the shared parser).
- ``hpns_divergence`` — SERVERCLASS / Pathway markers (kind, evidence).

Constructs the deterministic pass cannot resolve are left to the
llm_assisted flow in Phase 6 (facts marked ``origin=llm, needs_review``).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, ParseContext, ParseResult, Fact
from .cobol import parse_cobol


class HpnsCobolAdapter:
    name = "hpns_cobol"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "hpns_cobol"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        res = parse_cobol(text)
        for no, raw in enumerate(text.splitlines(), 1):
            m = re.search(r"SERVERCLASS\s+([A-Z0-9-]+)", raw.upper())
            if m:
                res.facts.append(Fact(
                    "hpns_divergence", m.group(1),
                    {"kind": "pathway_serverclass", "evidence": raw.strip()},
                    no, no, confidence=0.8, needs_review=1))
        return res


ADAPTER: Adapter = HpnsCobolAdapter()
