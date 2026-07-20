"""CICS CSD extract adapter (DFHCSDUP-style DEFINE listing).

The bridge that lets online FILE names resolve to real datasets for the
online<->batch dependency map. Fact shapes:

- ``csd_transaction`` — DEFINE TRANSACTION (program, group).
- ``csd_program`` — DEFINE PROGRAM (group, language).
- ``csd_file`` — DEFINE FILE (dsname, group).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def _opt(line: str, name: str) -> str:
    m = re.search(rf"{name}\s*\(\s*([^)]*)\s*\)", line, re.I)
    return m.group(1).strip() if m else ""


def parse_csd(text: str) -> ParseResult:
    res = ParseResult()
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        m = re.match(r"DEFINE\s+(TRANSACTION|PROGRAM|FILE)\s*\(([^)]*)\)",
                     line, re.I)
        if not m:
            if line and not line.startswith("*"):
                res.parse_errors += 1
            continue
        kind, name = m.group(1).upper(), m.group(2).strip()
        if kind == "TRANSACTION":
            res.facts.append(Fact("csd_transaction", name,
                                  {"program": _opt(line, "PROGRAM"),
                                   "group": _opt(line, "GROUP")}, no, no))
        elif kind == "PROGRAM":
            res.facts.append(Fact("csd_program", name,
                                  {"group": _opt(line, "GROUP"),
                                   "language": _opt(line, "LANGUAGE")}, no, no))
        else:
            res.facts.append(Fact("csd_file", name,
                                  {"dsname": _opt(line, "DSNAME"),
                                   "group": _opt(line, "GROUP")}, no, no))
    return res


class CicsCsdAdapter:
    name = "cics_csd"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "cics_csd"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_csd(text)


ADAPTER: Adapter = CicsCsdAdapter()
