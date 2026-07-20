"""DB2 DDL adapter.

Fact shapes:

- ``table`` — CREATE TABLE (columns: [{name, type, nullable}], pk: [...]).
- ``fk`` — FOREIGN KEY (table, columns, ref_table, ref_columns).
- ``index`` — CREATE INDEX (table, columns, unique 0/1).
- ``ddl_column_link`` — column linked to a copybook field by name
  similarity (column, field, similarity) — reported, never assumed
  (confidence < 1, needs_review).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def parse_ddl(text: str) -> ParseResult:
    res = ParseResult()
    clean = re.sub(r"--[^\n]*", "", text)
    for m in re.finditer(
            r"CREATE\s+TABLE\s+([A-Z0-9_.\"]+)\s*\((.*?)\)\s*(?:;|$)",
            clean, re.I | re.S):
        tname = m.group(1).strip('"').upper()
        body = m.group(2)
        cols = []
        pk: list[str] = []
        depth = 0
        piece = ""
        pieces = []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                pieces.append(piece)
                piece = ""
            else:
                piece += ch
        if piece.strip():
            pieces.append(piece)
        for p in pieces:
            p = " ".join(p.split())
            pm = re.match(r"PRIMARY\s+KEY\s*\(([^)]*)\)", p, re.I)
            if pm:
                pk = [c.strip().upper() for c in pm.group(1).split(",")]
                continue
            fm = re.match(r"(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*"
                          r"\(([^)]*)\)\s*REFERENCES\s+([A-Z0-9_.\"]+)\s*"
                          r"\(([^)]*)\)", p, re.I)
            if fm:
                res.facts.append(Fact(
                    "fk", tname,
                    {"table": tname,
                     "columns": [c.strip().upper()
                                 for c in fm.group(1).split(",")],
                     "ref_table": fm.group(2).strip('"').upper(),
                     "ref_columns": [c.strip().upper()
                                     for c in fm.group(3).split(",")]},
                    _line_of(text, m.start()), _line_of(text, m.end())))
                continue
            cm = re.match(r"([A-Z0-9_\"]+)\s+([A-Z]+[A-Z0-9]*"
                          r"(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)"
                          r"(.*)", p, re.I)
            if cm:
                cols.append({"name": cm.group(1).strip('"').upper(),
                             "type": " ".join(cm.group(2).upper().split()),
                             "nullable": "NOT NULL" not in cm.group(3).upper()})
        res.facts.append(Fact("table", tname, {"columns": cols, "pk": pk},
                              _line_of(text, m.start()),
                              _line_of(text, m.end())))
    for m in re.finditer(
            r"CREATE\s+(UNIQUE\s+)?INDEX\s+([A-Z0-9_.\"]+)\s+ON\s+"
            r"([A-Z0-9_.\"]+)\s*\(([^)]*)\)", clean, re.I):
        res.facts.append(Fact(
            "index", m.group(2).strip('"').upper(),
            {"table": m.group(3).strip('"').upper(),
             "columns": [c.strip().upper() for c in m.group(4).split(",")],
             "unique": 1 if m.group(1) else 0},
            _line_of(text, m.start()), _line_of(text, m.start())))
    return res


class Db2DdlAdapter:
    name = "db2ddl"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "db2ddl"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_ddl(text)


ADAPTER: Adapter = Db2DdlAdapter()
