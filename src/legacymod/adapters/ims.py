"""IMS adapter: DBD (segments, fields, hierarchy) and PSB (PCBs, intent).

Fact shapes:

- ``ims_dbd`` — DBD NAME= (access).
- ``ims_segment`` — SEGM (dbd, parent, bytes).
- ``ims_field`` — FIELD (dbd, segment, seq 0/1, bytes, start, type).
- ``ims_psb`` — PSBGEN PSBNAME= (lang).
- ``ims_pcb`` — PCB TYPE=DB (psb file stem until PSBGEN seen, dbdname,
  procopt, intent read/write derived from PROCOPT: A/I/R/D imply write).

CBLTDLI/AIBTDLI call extraction lives in the COBOL adapter; the derived
program->PSB link is added by the analyze post-pass (see
docs/decisions.md — name-stem match, else sole-PSB heuristic with
needs_review=1).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def parse_ims(text: str, kind: str) -> ParseResult:
    res = ParseResult()
    dbd = ""
    segment = ""
    pcbs = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        m = re.match(r"DBD\s+NAME=(\w+)(?:,ACCESS=\(?([\w,]+)\)?)?", line)
        if m:
            dbd = m.group(1)
            res.facts.append(Fact("ims_dbd", dbd,
                                  {"access": m.group(2) or ""}, no, no))
            continue
        m = re.match(r"SEGM\s+NAME=(\w+)(.*)", line)
        if m:
            segment = m.group(1)
            parent = re.search(r"PARENT=(\w+)", m.group(2))
            nbytes = re.search(r"BYTES=(\d+)", m.group(2))
            res.facts.append(Fact("ims_segment", segment,
                                  {"dbd": dbd,
                                   "parent": parent.group(1) if parent else "0",
                                   "bytes": int(nbytes.group(1)) if nbytes
                                   else 0}, no, no))
            continue
        m = re.match(r"FIELD\s+NAME=(\(?[\w,]+\)?)(.*)", line)
        if m:
            nm = m.group(1).strip("()").split(",")
            nbytes = re.search(r"BYTES=(\d+)", m.group(2))
            start = re.search(r"START=(\d+)", m.group(2))
            ftype = re.search(r"TYPE=(\w)", m.group(2))
            res.facts.append(Fact("ims_field", nm[0],
                                  {"dbd": dbd, "segment": segment,
                                   "seq": 1 if "SEQ" in nm else 0,
                                   "bytes": int(nbytes.group(1)) if nbytes else 0,
                                   "start": int(start.group(1)) if start else 0,
                                   "type": ftype.group(1) if ftype else ""},
                                  no, no))
            continue
        m = re.match(r"PCB\s+TYPE=(\w+)(.*)", line)
        if m:
            dbdname = re.search(r"DBDNAME=(\w+)", m.group(2))
            procopt = re.search(r"PROCOPT=(\w+)", m.group(2))
            po = procopt.group(1).upper() if procopt else ""
            pcbs.append(Fact("ims_pcb", dbdname.group(1) if dbdname else "",
                             {"type": m.group(1),
                              "dbdname": dbdname.group(1) if dbdname else "",
                              "procopt": po,
                              "intent": "write" if any(c in po for c in "AIRD")
                              else "read"}, no, no))
            continue
        m = re.match(r"PSBGEN\s+(.*)", line)
        if m:
            psbname = re.search(r"PSBNAME=(\w+)", m.group(1))
            lang = re.search(r"LANG=(\w+)", m.group(1))
            res.facts.append(Fact("ims_psb",
                                  psbname.group(1) if psbname else "",
                                  {"lang": lang.group(1) if lang else ""},
                                  no, no))
    res.facts.extend(pcbs)
    return res


class ImsAdapter:
    name = "ims"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type in ("ims_dbd", "ims_psb")

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_ims(text, artifact.artifact_type)


ADAPTER: Adapter = ImsAdapter()
