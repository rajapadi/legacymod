"""CICS BMS mapset adapter.

Fact shapes:

- ``mapset`` — DFHMSD (mode, lang).
- ``map`` — DFHMDI (mapset, size).
- ``screen_field`` — DFHMDF (mapset, map, pos [row, col], length, attrs,
  initial, picout).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def parse_bms(text: str) -> ParseResult:
    res = ParseResult()
    # join macro continuations: a non-blank in column 72 continues the line
    logical: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        cont = len(line) >= 72 and line[71] != " "
        body = line[:71] if cont else line
        if pending:
            pending += body.strip()
        else:
            pending = body
            start = no
        if not cont:
            logical.append((start, pending))
            pending = ""
    if pending:
        logical.append((start, pending))

    mapset = ""
    current_map = ""
    for no, line in logical:
        m = re.match(r"(\S+)?\s+DFHMSD\s+(.*)", line)
        if m:
            if "TYPE=FINAL" in (m.group(2) or ""):
                continue
            mapset = m.group(1) or mapset
            mode = re.search(r"MODE=(\w+)", m.group(2) or "")
            lang = re.search(r"LANG=(\w+)", m.group(2) or "")
            res.facts.append(Fact("mapset", mapset,
                                  {"mode": mode.group(1) if mode else "",
                                   "lang": lang.group(1) if lang else ""},
                                  no, no))
            continue
        m = re.match(r"(\S+)\s+DFHMDI\s+(.*)", line)
        if m:
            current_map = m.group(1)
            size = re.search(r"SIZE=\((\d+),(\d+)\)", m.group(2))
            res.facts.append(Fact("map", current_map,
                                  {"mapset": mapset,
                                   "size": list(size.groups()) if size else []},
                                  no, no))
            continue
        m = re.match(r"(\S+)?\s+DFHMDF\s+(.*)", line)
        if m:
            body = m.group(2)
            pos = re.search(r"POS=\((\d+),(\d+)\)", body)
            length = re.search(r"LENGTH=(\d+)", body)
            attrb = re.search(r"ATTRB=(\([^)]*\)|\w+)", body)
            init = re.search(r"INITIAL='([^']*)'", body)
            picout = re.search(r"PICOUT='([^']*)'", body)
            res.facts.append(Fact(
                "screen_field", m.group(1) or "(anon)",
                {"mapset": mapset, "map": current_map,
                 "pos": list(pos.groups()) if pos else [],
                 "length": int(length.group(1)) if length else 0,
                 "attrs": attrb.group(1).strip("()") if attrb else "",
                 "initial": init.group(1) if init else "",
                 "picout": picout.group(1) if picout else ""}, no, no))
    return res


class CicsBmsAdapter:
    name = "cics_bms"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "cics_bms"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_bms(text)


ADAPTER: Adapter = CicsBmsAdapter()
