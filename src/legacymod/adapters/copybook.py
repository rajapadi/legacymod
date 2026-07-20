"""Copybook adapter — the data-model backbone.

Parses 01–49 level items and 88-level condition names into a normalized
field table. Fact shapes:

- ``field`` — (level, pic, usage, length, decimals, digits, signed,
  occurs, redefines, parent, offset, value). ``offset`` is the byte
  offset within the record for simple sequential layouts; REDEFINES
  entries reuse the redefined field's offset and are flagged for human
  decision downstream.
- ``condition_name`` — 88-level (parent, value).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult

_LEVEL = re.compile(r"^\s*(\d{1,2})\s+([A-Z0-9-]+|FILLER)(.*)$")


def pic_meta(pic: str, usage: str) -> dict:
    """Compute storage length / digits / decimals from a PIC + USAGE."""
    if not pic:
        return {"length": 0, "digits": 0, "decimals": 0, "signed": 0}
    expanded = re.sub(r"([X9AZB0/])\((\d+)\)",
                      lambda m: m.group(1) * int(m.group(2)), pic.upper())
    signed = 1 if expanded.startswith("S") else 0
    expanded = expanded.lstrip("S")
    if "V" in expanded:
        left, right = expanded.split("V", 1)
    else:
        left, right = expanded, ""
    digits = sum(1 for c in expanded if c == "9")
    decimals = sum(1 for c in right if c == "9")
    alnum = sum(1 for c in expanded if c in "XA")
    usage = (usage or "DISPLAY").upper()
    if usage in ("COMP-3", "PACKED-DECIMAL"):
        length = (digits + 2) // 2  # ceil((digits+1)/2)
    elif usage in ("COMP", "COMP-4", "COMP-5", "BINARY"):
        length = 2 if digits <= 4 else 4 if digits <= 9 else 8
    else:
        length = alnum + digits + sum(1 for c in expanded if c in "ZB0/*$,.+-")
    return {"length": length, "digits": digits, "decimals": decimals,
            "signed": signed}


def parse_copybook(text: str) -> ParseResult:
    res = ParseResult()
    stack: list[tuple[int, str]] = []
    offsets: dict[str, int] = {}
    cursor = 0
    pending = ""
    pending_line = 0
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.expandtabs()
        if line[6:7] in ("*", "/"):
            continue
        code = line[7:72].strip() if len(line) > 7 else line.strip()
        if not code:
            continue
        if pending:
            code = pending + " " + code
            start_line = pending_line
        else:
            start_line = no
        if not code.endswith("."):
            pending, pending_line = code, start_line
            continue
        pending = ""
        sentence = code.rstrip(".")
        m = _LEVEL.match(sentence.upper())
        if not m:
            res.parse_errors += 1
            continue
        level, name, rest = int(m.group(1)), m.group(2), m.group(3)
        if level == 88:
            parent = stack[-1][1] if stack else ""
            vm = re.search(r"VALUE(?:S)?\s+(?:IS\s+)?(.+?)\s*$", rest)
            res.facts.append(Fact("condition_name", name,
                                  {"parent": parent,
                                   "value": vm.group(1).strip() if vm else ""},
                                  start_line, no))
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else ""
        stack.append((level, name))
        pm = re.search(r"PIC(?:TURE)?\s+(?:IS\s+)?([-+A-Z0-9().SVXZ*$,/]+)", rest)
        um = re.search(r"\b(COMP-3|COMP-5|COMP-4|COMP-1|COMP-2|COMP|BINARY"
                       r"|PACKED-DECIMAL|DISPLAY)\b", rest)
        om = re.search(r"OCCURS\s+(\d+)", rest)
        rm = re.search(r"REDEFINES\s+([A-Z0-9-]+)", rest)
        vm = re.search(r"VALUE\s+(?:IS\s+)?('[^']*'|\S+)", rest)
        pic = pm.group(1).rstrip(".") if pm else ""
        usage = um.group(1) if um else ("DISPLAY" if pic else "GROUP")
        meta = pic_meta(pic, usage)
        if rm:
            offset = offsets.get(rm.group(1), cursor)
        else:
            offset = cursor
        occurs = int(om.group(1)) if om else 0
        detail = {"level": level, "pic": pic, "usage": usage,
                  "occurs": occurs, "redefines": rm.group(1) if rm else "",
                  "parent": parent, "offset": offset,
                  "value": vm.group(1).strip("'") if vm else "", **meta}
        res.facts.append(Fact("field", name, detail, start_line, no))
        offsets[name] = offset
        if pic and not rm:
            cursor = offset + meta["length"] * max(occurs, 1)
    if pending:
        res.parse_errors += 1
    return res


class CopybookAdapter:
    name = "copybook"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "copybook"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_copybook(text)


ADAPTER: Adapter = CopybookAdapter()
