"""REXX adapter.

Fact shapes:

- ``program`` — the exec itself (name = artifact stem) so calls/data
  edges attach in the graph.
- ``rexx_proc`` — internal procedure labels (name:).
- ``rexx_address`` — ADDRESS environment switches (TSO/ISPEXEC/...).
- ``calls`` — CALL name (external unless a local label; local ones are
  emitted as rexx_proc_call).
- ``rexx_dataset`` — datasets touched via ALLOC DA(...) / EXECIO
  (dsn, access read/write, via).
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def parse_rexx(text: str, name: str) -> ParseResult:
    res = ParseResult()
    res.facts.append(Fact("program", name.upper(), {"language": "REXX"}, 1, 1))
    labels = set()
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        m = re.match(r"([A-Za-z_][\w.]*):\s*$", line)
        if m:
            labels.add(m.group(1).upper())
            res.facts.append(Fact("rexx_proc", m.group(1).upper(), {}, no, no))
    alloc_dd_dsn: dict[str, str] = {}
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("/*"):
            continue
        m = re.search(r"ADDRESS\s+(\w+)", line, re.I)
        if m:
            res.facts.append(Fact("rexx_address", m.group(1).upper(), {},
                                  no, no))
        m = re.search(r"ALLOC\s+F(?:I(?:LE)?)?\((\w+)\)\s+DA(?:TASET)?"
                      r"\('([^']+)'\)", line, re.I)
        if m:
            alloc_dd_dsn[m.group(1).upper()] = m.group(2).upper()
            res.facts.append(Fact("rexx_dataset", m.group(2).upper(),
                                  {"dsn": m.group(2).upper(),
                                   "ddname": m.group(1).upper(),
                                   "access": "read", "via": "ALLOC"}, no, no))
        m = re.search(r"EXECIO\s+\S+\s+DISK(R|W)U?\s+(\w+)", line, re.I)
        if m:
            dd = m.group(2).upper()
            dsn = alloc_dd_dsn.get(dd, dd)
            res.facts.append(Fact("rexx_dataset", dsn,
                                  {"dsn": dsn, "ddname": dd,
                                   "access": "read" if m.group(1).upper() == "R"
                                   else "write", "via": "EXECIO"}, no, no))
        m = re.match(r"CALL\s+([A-Za-z_][\w.]*)", line, re.I)
        if m:
            target = m.group(1).upper()
            if target in labels:
                res.facts.append(Fact("rexx_proc_call", target, {}, no, no))
            else:
                res.facts.append(Fact("calls", target,
                                      {"dynamic": 0, "paragraph": ""}, no, no))
    return res


class RexxAdapter:
    name = "rexx"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "rexx"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_rexx(text, Path(artifact.path).stem)


ADAPTER: Adapter = RexxAdapter()
