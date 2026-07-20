"""Easytrieve adapter — coarse by design (file/job sections, fields,
called programs, report names).

Fact shapes:

- ``program`` — the job name (JOB INPUT ... NAME x) or artifact stem.
- ``ezt_file`` — FILE section (ddname).
- ``ezt_field`` — field definition (file, start, length, type, decimals).
- ``ezt_job`` — JOB INPUT (input file).
- ``ezt_report`` — REPORT section.
- ``calls`` — CALL statements.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def parse_ezt(text: str, stem: str) -> ParseResult:
    res = ParseResult()
    current_file = ""
    job_name = ""
    facts = res.facts
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        s = line.strip()
        if not s or s.startswith("*"):
            continue
        m = re.match(r"FILE\s+(\w+)(?:\s+DD\((\w+)\))?", s, re.I)
        if m:
            current_file = m.group(1).upper()
            facts.append(Fact("ezt_file", current_file,
                              {"ddname": (m.group(2) or "").upper()}, no, no))
            continue
        m = re.match(r"JOB\s+INPUT\s+(\w+)(?:\s+NAME\s+(\w+))?", s, re.I)
        if m:
            job_name = (m.group(2) or "").upper()
            facts.append(Fact("ezt_job", job_name or stem.upper(),
                              {"input": m.group(1).upper()}, no, no))
            continue
        m = re.match(r"REPORT\s+(\w+)", s, re.I)
        if m:
            facts.append(Fact("ezt_report", m.group(1).upper(), {}, no, no))
            continue
        m = re.match(r"CALL\s+(\w+)", s, re.I)
        if m:
            facts.append(Fact("calls", m.group(1).upper(),
                              {"dynamic": 0, "paragraph": ""}, no, no))
            continue
        m = re.match(r"([\w-]+)\s+(\d+)\s+(\d+)\s+([ABNPU])(?:\s+(\d+))?", s,
                     re.I)
        if m and current_file:
            facts.append(Fact("ezt_field", m.group(1).upper(),
                              {"file": current_file, "start": int(m.group(2)),
                               "length": int(m.group(3)),
                               "type": m.group(4).upper(),
                               "decimals": int(m.group(5) or 0)}, no, no))
    facts.insert(0, Fact("program", job_name or stem.upper(),
                         {"language": "Easytrieve"}, 1, 1))
    return res


class EasytrieveAdapter:
    name = "easytrieve"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "easytrieve"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_ezt(text, Path(artifact.path).stem)


ADAPTER: Adapter = EasytrieveAdapter()
