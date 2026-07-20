"""Deterministic COBOL adapter (z/OS dialects) — island parsing.

Never fails a file: unparseable lines are skipped and counted in
``parse_errors``. Emits these fact shapes (``detail`` keys in parens):

- ``program`` — PROGRAM-ID (name).
- ``paragraph`` — procedure paragraph/section (section flag).
- ``performs`` — PERFORM target (paragraph, thru).
- ``calls`` — CALL (dynamic 0/1, paragraph); MQ/IMS calls also emit
  ``mq_call`` (operation, queue, queue_confidence) / ``ims_call`` (operands).
- ``copy`` — COPY member (replacing 0/1, section).
- ``select`` — SELECT (file, ddname, organization, record_key).
- ``fd_record`` — FD record binding (file, record, via_copy).
- ``file_open`` / ``file_read`` / ``file_write`` — file usage (mode/record).
- ``sql`` — EXEC SQL table op (table, op).
- ``cics`` — EXEC CICS command (command + resource options).
- ``data_item`` — data-division item (level, pic, usage, value, section,
  parent, occurs, redefines).
- ``condition_name`` — 88-level (parent, value).
- ``condition`` — decision point for metrics (kind, paragraph).
- ``goto`` — GO TO (target, paragraph).
- ``data_ref`` — field read/write (access, paragraph, verb, stmt_id,
  cond_fields) — the raw material for --where-used and slicing.
- ``enter_tal`` — ENTER TAL divergence marker (routine).
- ``entry`` — ENTRY point (literal).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult

_KEYWORDS = frozenset("""
ACCEPT ACCESS ADD ADVANCING AFTER ALL ALSO AND ARE AREA AS ASSIGN AT BEFORE
BINARY BY CALL CLOSE COMP COMP-3 COMP-4 COMP-5 COMPUTE CONTINUE CORRESPONDING
DATA DECLARE DELETE DELIMITED DISPLAY DIVIDE DIVISION DYNAMIC ELSE END
END-COMPUTE END-EVALUATE END-EXEC END-IF END-PERFORM END-READ END-SEARCH
END-STRING END-WRITE ENTER ENTRY ENVIRONMENT EQUAL ERROR EVALUATE EXEC EXIT
EXTEND FALSE FD FILE FILE-CONTROL FILLER FIRST FROM FUNCTION GIVING GO GOBACK
GREATER HIGH-VALUE HIGH-VALUES IDENTIFICATION IF IN INDEXED INITIALIZE INPUT
INPUT-OUTPUT INSPECT INTO INVALID I-O IS JUST JUSTIFIED KEY LEADING LESS
LINKAGE LOW-VALUE LOW-VALUES MOVE MULTIPLY NEGATIVE NEXT NOT NUMERIC OCCURS
OF OMITTED ON OPEN OR ORGANIZATION OTHER OUTPUT OVERFLOW PACKED-DECIMAL
PERFORM PIC PICTURE POSITIVE PROCEDURE PROGRAM-ID RANDOM READ RECORD
REDEFINES RELEASE REMAINDER REPLACING RETURN REWRITE ROUNDED RUN SEARCH
SECTION SELECT SENTENCE SEQUENTIAL SET SIGN SIZE SORT SPACE SPACES STANDARD
START STOP STRING SUBTRACT TALLYING TEST THAN THEN THROUGH THRU TIMES TO
TRAILING TRUE UNSTRING UNTIL UPON USAGE USING VALUE VALUES VARYING WHEN WITH
WORKING-STORAGE WRITE ZERO ZEROES ZEROS SQL CICS SQLCODE SQLSTATE RESP
ERASE FREEKB MAP MAPSET FILE RIDFLD TRANSID QUEUE LENGTH PROGRAM COMMAREA
""".split())

_ID = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|[A-Z][A-Z0-9]{2,}")
_VERBS = ("MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "IF",
          "ELSE", "READ", "WRITE", "REWRITE", "DELETE", "OPEN", "CLOSE",
          "PERFORM", "CALL", "GOBACK", "GO", "EXEC", "DISPLAY", "ACCEPT",
          "EVALUATE", "WHEN", "SET", "INITIALIZE", "STRING", "UNSTRING",
          "INSPECT", "SEARCH", "CONTINUE", "EXIT", "STOP", "END-IF",
          "END-READ", "END-EXEC", "END-EVALUATE", "ENTRY", "ENTER", "NOT",
          "AT", "SELECT", "FD", "COPY")


def _strip_literals(code: str) -> str:
    return re.sub(r"'[^']*'|\"[^\"]*\"", " ", code)


def _identifiers(code: str) -> list[str]:
    out = []
    for tok in _ID.findall(_strip_literals(code).upper()):
        if tok not in _KEYWORDS and not tok.replace("-", "").isdigit():
            out.append(tok)
    return out


def source_lines(text: str):
    """Yield (lineno, code_area, is_comment, bad_seq, odd_quotes)."""
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.expandtabs().rstrip("\n")
        seq, ind, code = line[:6], line[6:7], line[7:72]
        bad_seq = bool(seq.strip()) and not seq.strip().isdigit()
        if ind in ("*", "/"):
            yield no, "", True, False, False
            continue
        if bad_seq:
            # Treat the whole line as unusable, count it, move on (island).
            yield no, "", False, True, False
            continue
        stripped = code.rstrip()
        odd = (stripped.count("'") % 2 == 1) or (stripped.count('"') % 2 == 1)
        yield no, stripped, False, False, odd


class CobolAdapter:
    name = "cobol"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "cobol"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_cobol(text)


def parse_cobol(text: str, divergence_scan: bool = False) -> ParseResult:
    """Shared COBOL parser; ``divergence_scan`` adds HPNS marking (Phase 6)."""
    res = ParseResult()
    facts = res.facts
    division = ""
    data_section = ""
    in_file_control = False
    current_fd: str | None = None
    fd_records: dict[str, str] = {}      # record -> file
    file_modes: dict[str, str] = {}      # file -> open mode
    current_select: dict | None = None
    paragraph = ""
    data_items: list[dict] = []
    level_stack: list[tuple[int, str]] = []
    stmt_id = 0
    pending_stmt = False
    cond_stack: list[list[str]] = []
    nesting_max = 0
    exec_block: dict | None = None
    last_code_line = ""

    def emit(fact: Fact) -> None:
        facts.append(fact)

    def data_ref(name_: str, access: str, verb: str, line: int) -> None:
        cond_fields = sorted({f for grp in cond_stack for f in grp})
        emit(Fact("data_ref", name_, {
            "access": access, "paragraph": paragraph, "verb": verb,
            "stmt_id": stmt_id, "cond_fields": cond_fields}, line, line))

    def refs_from(code: str, line: int) -> None:
        """Verb-aware read/write extraction for one code line."""
        nonlocal stmt_id, pending_stmt
        up = code.strip()
        first = up.split(None, 1)[0].rstrip(".") if up else ""
        starts_new = first in _VERBS or not pending_stmt
        if starts_new and first not in ("ELSE", "END-IF", "END-READ", "NOT", "AT"):
            stmt_id += 1
        handled = False
        for m in re.finditer(r"\bMOVE\s+(.+?)\s+TO\s+([A-Z0-9-]+)", up):
            # Single-target MOVE only; multi-target MOVEs record the first.
            handled = True
            for f in _identifiers(m.group(1)):
                data_ref(f, "read", "MOVE", line)
            for f in _identifiers(m.group(2)):
                data_ref(f, "write", "MOVE", line)
        m = re.search(r"\bCOMPUTE\s+([A-Z0-9-]+)(?:\s+ROUNDED)?\s*=(.*)", up)
        if m:
            handled = True
            data_ref(m.group(1), "write", "COMPUTE", line)
            for f in _identifiers(m.group(2)):
                data_ref(f, "read", "COMPUTE", line)
        m = re.search(r"\b(ADD|SUBTRACT)\s+(.+?)\s+(?:TO|FROM)\s+([A-Z0-9-]+)(?:\s+GIVING\s+([A-Z0-9-]+))?", up)
        if m:
            handled = True
            for f in _identifiers(m.group(2)):
                data_ref(f, "read", m.group(1), line)
            if m.group(4):
                data_ref(m.group(3), "read", m.group(1), line)
                data_ref(m.group(4), "write", m.group(1), line)
            else:
                data_ref(m.group(3), "read", m.group(1), line)
                data_ref(m.group(3), "write", m.group(1), line)
        m = re.search(r"\b(MULTIPLY|DIVIDE)\s+(.+?)\s+(?:BY|INTO)\s+([A-Z0-9-]+)(?:\s+GIVING\s+([A-Z0-9-]+))?", up)
        if m:
            handled = True
            for f in _identifiers(m.group(2)):
                data_ref(f, "read", m.group(1), line)
            data_ref(m.group(3), "read", m.group(1), line)
            data_ref(m.group(4) or m.group(3), "write", m.group(1), line)
        for m in re.finditer(r"\b(?:IF|UNTIL|WHEN)\s+(.+?)(?:\bTHEN\b|$)", up):
            handled = True
            for f in _identifiers(m.group(1)):
                data_ref(f, "read", "condition", line)
        m = re.search(r"\bREAD\s+([A-Z0-9-]+)(?:\s+INTO\s+([A-Z0-9-]+))?", up)
        if m:
            handled = True
            if m.group(2):
                data_ref(m.group(2), "write", "READ", line)
        m = re.search(r"\bWRITE\s+([A-Z0-9-]+)(?:\s+FROM\s+([A-Z0-9-]+))?", up)
        if m and m.group(2):
            handled = True
            data_ref(m.group(2), "read", "WRITE", line)
            data_ref(m.group(1), "write", "WRITE", line)
        if not handled and pending_stmt:
            # Continuation of the previous statement: identifiers are reads.
            for f in _identifiers(up):
                data_ref(f, "read", "continuation", line)
        pending_stmt = not up.endswith(".")

    for no, code, is_comment, bad_seq, odd_quotes in source_lines(text):
        if is_comment:
            continue
        if bad_seq or odd_quotes:
            res.parse_errors += 1
            if bad_seq:
                continue
        up = code.upper().strip()
        if not up:
            continue
        last_code_line = up

        if exec_block is not None:
            exec_block["text"].append(up)
            if "END-EXEC" in up:
                _close_exec(exec_block, no, emit, data_ref, paragraph)
                exec_block = None
            continue
        m = re.search(r"\bEXEC\s+(SQL|CICS)\b(.*)", up)
        if m:
            exec_block = {"kind": m.group(1), "start": no,
                          "text": [m.group(2)]}
            if "END-EXEC" in up:
                _close_exec(exec_block, no, emit, data_ref, paragraph)
                exec_block = None
            continue

        if "IDENTIFICATION DIVISION" in up:
            division = "ID"
            continue
        if "ENVIRONMENT DIVISION" in up:
            division = "ENV"
            continue
        if "DATA DIVISION" in up:
            division = "DATA"
            continue
        if "PROCEDURE DIVISION" in up:
            division = "PROC"
            paragraph = ""
            continue

        m = re.match(r"PROGRAM-ID\.?\s+([A-Z0-9-]+)", up)
        if m:
            emit(Fact("program", m.group(1), {}, no, no))
            continue

        if division == "ENV":
            if "FILE-CONTROL" in up:
                in_file_control = True
                continue
            if in_file_control:
                m = re.match(r"SELECT\s+([A-Z0-9-]+)\s+ASSIGN\s+TO\s+([A-Z0-9-]+)", up)
                if m:
                    current_select = {"file": m.group(1), "ddname": m.group(2),
                                      "organization": "", "record_key": "",
                                      "line": no}
                    emit(Fact("select", m.group(1), current_select, no, no))
                    continue
                if current_select:
                    m = re.search(r"ORGANIZATION\s+(?:IS\s+)?([A-Z]+)", up)
                    if m:
                        current_select["organization"] = m.group(1)
                    m = re.search(r"RECORD\s+KEY\s+(?:IS\s+)?([A-Z0-9-]+)", up)
                    if m:
                        current_select["record_key"] = m.group(1)
                continue

        if division == "DATA":
            if "FILE SECTION" in up:
                data_section = "FILE"
                continue
            if "WORKING-STORAGE SECTION" in up:
                data_section = "WS"
                current_fd = None
                continue
            if "LINKAGE SECTION" in up:
                data_section = "LINKAGE"
                current_fd = None
                continue
            m = re.match(r"FD\s+([A-Z0-9-]+)", up)
            if m:
                current_fd = m.group(1)
                continue
            m = re.match(r"COPY\s+([A-Z0-9-]+)(\s+REPLACING)?", up)
            if m:
                emit(Fact("copy", m.group(1),
                          {"replacing": 1 if m.group(2) else 0,
                           "section": data_section, "fd": current_fd or ""},
                          no, no))
                if current_fd:
                    emit(Fact("fd_record", current_fd,
                              {"file": current_fd, "record": "",
                               "via_copy": m.group(1)}, no, no))
                    current_fd = None
                continue
            m = re.match(r"(\d{1,2})\s+([A-Z0-9-]+)(.*)", up)
            if m:
                level = int(m.group(1))
                name_, rest = m.group(2), m.group(3)
                if level == 88:
                    parent = level_stack[-1][1] if level_stack else ""
                    vm = re.search(r"VALUE(?:S)?\s+(?:IS\s+)?(.+?)\.?\s*$", rest)
                    emit(Fact("condition_name", name_,
                              {"parent": parent,
                               "value": (vm.group(1).strip() if vm else ""),
                               "section": data_section}, no, no))
                    continue
                while level_stack and level_stack[-1][0] >= level:
                    level_stack.pop()
                parent = level_stack[-1][1] if level_stack else ""
                level_stack.append((level, name_))
                pm = re.search(r"PIC(?:TURE)?\s+(?:IS\s+)?([-+A-Z0-9().SVXZ*$,/]+)", rest)
                um = re.search(r"\b(COMP-3|COMP-5|COMP-4|COMP-1|COMP-2|COMP|BINARY|PACKED-DECIMAL|DISPLAY)\b", rest)
                vm = re.search(r"VALUE\s+(?:IS\s+)?('[^']*'|\S+)", rest)
                value = ""
                if vm:
                    raw_v = vm.group(1)
                    value = raw_v.strip("'") if raw_v.startswith("'") \
                        else raw_v.rstrip(".")
                om = re.search(r"OCCURS\s+(\d+)", rest)
                rm = re.search(r"REDEFINES\s+([A-Z0-9-]+)", rest)
                detail = {"level": level, "pic": pm.group(1).rstrip(".") if pm else "",
                          "usage": um.group(1) if um else ("DISPLAY" if pm else "GROUP"),
                          "value": value,
                          "occurs": int(om.group(1)) if om else 0,
                          "redefines": rm.group(1) if rm else "",
                          "section": data_section, "parent": parent,
                          "fd": current_fd or ""}
                data_items.append(detail | {"name": name_})
                emit(Fact("data_item", name_, detail, no, no))
                if current_fd and level == 1:
                    fd_records[name_] = current_fd
                    emit(Fact("fd_record", current_fd,
                              {"file": current_fd, "record": name_,
                               "via_copy": ""}, no, no))
                    current_fd = None
                continue
            continue

        if division == "PROC":
            m = re.match(r"([A-Z0-9][A-Z0-9-]*)\s*(SECTION\s*)?\.\s*$", up)
            if m and m.group(1) not in _KEYWORDS and not pending_stmt:
                paragraph = m.group(1)
                emit(Fact("paragraph", paragraph,
                          {"section": 1 if m.group(2) else 0}, no, no))
                cond_stack.clear()
                continue

            m = re.match(r"ENTRY\s+'([A-Z0-9-]+)'", up)
            if m:
                emit(Fact("entry", m.group(1), {"paragraph": paragraph}, no, no))

            m = re.search(r"\bENTER\s+TAL\b(?:\s+\"([^\"]+)\")?", up)
            if m:
                emit(Fact("enter_tal", m.group(1) or "",
                          {"paragraph": paragraph}, no, no,
                          confidence=1.0))

            for m in re.finditer(r"\bPERFORM\s+([A-Z0-9-]+)(?:\s+(?:THRU|THROUGH)\s+([A-Z0-9-]+))?", up):
                target = m.group(1)
                if target not in _KEYWORDS:
                    emit(Fact("performs", target,
                              {"paragraph": paragraph,
                               "thru": m.group(2) or ""}, no, no))

            m = re.search(r"\bCALL\s+'([A-Z0-9$#@-]+)'", up)
            if m:
                callee = m.group(1)
                if callee.startswith("MQ"):
                    emit(Fact("mq_call", callee, _mq_detail(callee, data_items, paragraph), no, no))
                elif callee in ("CBLTDLI", "AIBTDLI"):
                    emit(Fact("ims_call", callee,
                              {"paragraph": paragraph,
                               "operands": _identifiers(up.split("USING", 1)[-1])
                               if "USING" in up else []}, no, no))
                else:
                    emit(Fact("calls", callee,
                              {"dynamic": 0, "paragraph": paragraph}, no, no))
            else:
                m = re.search(r"\bCALL\s+([A-Z0-9-]+)", up)
                if m and m.group(1) not in _KEYWORDS:
                    emit(Fact("calls", m.group(1),
                              {"dynamic": 1, "paragraph": paragraph}, no, no,
                              needs_review=1, confidence=0.7))

            for m in re.finditer(r"\bGO\s+TO\s+([A-Z0-9-]+)", up):
                emit(Fact("goto", m.group(1), {"paragraph": paragraph}, no, no))

            m = re.search(r"\bOPEN\s+(.+)", up)
            if m:
                mode = ""
                for tok in m.group(1).replace(".", " ").split():
                    if tok in ("INPUT", "OUTPUT", "I-O", "EXTEND"):
                        mode = tok
                    elif tok not in _KEYWORDS:
                        file_modes[tok] = mode
                        emit(Fact("file_open", tok, {"mode": mode,
                                  "paragraph": paragraph}, no, no))
            m = re.search(r"\bREAD\s+([A-Z0-9-]+)", up)
            if m:
                emit(Fact("file_read", m.group(1), {"paragraph": paragraph}, no, no))
            m = re.search(r"\b(WRITE|REWRITE)\s+([A-Z0-9-]+)", up)
            if m:
                rec = m.group(2)
                emit(Fact("file_write", fd_records.get(rec, rec),
                          {"record": rec, "verb": m.group(1),
                           "paragraph": paragraph}, no, no))

            # decision points for metrics + nesting/condition context
            for kind, pat in (("IF", r"\bIF\b"), ("WHEN", r"\bWHEN\b"),
                              ("UNTIL", r"\bUNTIL\b"), ("AT END", r"\bAT\s+END\b"),
                              ("ON", r"\bON\s+(?:SIZE|OVERFLOW|EXCEPTION)\b"),
                              ("INVALID KEY", r"\bINVALID\s+KEY\b")):
                for _ in re.finditer(pat, up):
                    emit(Fact("condition", kind, {"paragraph": paragraph}, no, no))
            if re.search(r"\bIF\b", up):
                cm = re.search(r"\bIF\s+(.+?)(?:\bTHEN\b|$)", up)
                cond_stack.append(_identifiers(cm.group(1)) if cm else [])
                nesting_max = max(nesting_max, len(cond_stack))
            refs_from(code, no)
            if "END-IF" in up and cond_stack:
                cond_stack.pop()
            if up.endswith("."):
                cond_stack.clear()
            continue

    if exec_block is not None:
        res.parse_errors += 1  # unterminated EXEC block
    if division == "PROC" and last_code_line and not last_code_line.endswith("."):
        res.parse_errors += 1  # source ends mid-sentence
    facts.append(Fact("metrics_hint", "nesting", {"nesting_max": nesting_max}))
    return res


def _mq_detail(callee: str, data_items: list[dict], paragraph: str) -> dict:
    """Resolve the queue name for an MQ call from MQOD literals when possible."""
    candidates = [d["value"] for d in data_items
                  if d.get("value") and "." in d.get("value", "")
                  and re.fullmatch(r"[A-Z0-9][A-Z0-9._]*", d["value"])
                  and d.get("usage") == "DISPLAY"
                  and ("OBJECT" in d["name"] or "QNAME" in d["name"]
                       or "QUEUE" in d["name"])]
    op = {"MQPUT": "put", "MQPUT1": "put", "MQGET": "get"}.get(callee, callee.lower())
    if len(candidates) == 1:
        return {"operation": op, "queue": candidates[0],
                "queue_confidence": 0.9, "paragraph": paragraph}
    return {"operation": op, "queue": "", "queue_confidence": 0.0,
            "paragraph": paragraph, "needs_review": 1}


def _close_exec(block: dict, end_line: int, emit, data_ref, paragraph: str) -> None:
    text = " ".join(block["text"])
    text = text.split("END-EXEC")[0].strip()
    start = block["start"]
    if block["kind"] == "SQL":
        ops = []
        for pat, op in ((r"INSERT\s+INTO\s+([A-Z0-9_.]+)", "INSERT"),
                        (r"UPDATE\s+([A-Z0-9_.]+)", "UPDATE"),
                        (r"DELETE\s+FROM\s+([A-Z0-9_.]+)", "DELETE"),
                        (r"DECLARE\s+([A-Z0-9_.]+)\s+CURSOR", "DECLARE CURSOR")):
            for m in re.finditer(pat, text):
                ops.append((m.group(1), op))
        if "SELECT" in text and not any(o == "DECLARE CURSOR" for _, o in ops):
            for m in re.finditer(r"\bFROM\s+([A-Z0-9_.]+)", text):
                ops.append((m.group(1), "SELECT"))
        for table, op in ops:
            emit(Fact("sql", table.rstrip(","),
                      {"op": op, "paragraph": paragraph}, start, end_line))
        for m in re.finditer(r":([A-Z0-9-]+)", text):
            access = ("write" if re.search(rf"\bINTO\s+:{m.group(1)}\b", text)
                      else "read")
            data_ref(m.group(1), access, "SQL", start)
    else:  # CICS
        words = text.split()
        command = words[0] if words else ""
        if len(words) > 1 and words[1] in ("MAP", "TEXT", "FILE", "TD", "TS"):
            command += " " + words[1]
        detail: dict = {"command": command, "paragraph": paragraph}
        for opt in ("MAP", "MAPSET", "FILE", "PROGRAM", "TRANSID", "QUEUE"):
            m = re.search(rf"\b{opt}\s*\(\s*'([^']+)'\s*\)", text)
            if m:
                detail[opt.lower()] = m.group(1)
        for opt, access in (("INTO", "write"), ("RIDFLD", "read"), ("FROM", "read")):
            m = re.search(rf"\b{opt}\s*\(\s*([A-Z0-9-]+)\s*\)", text)
            if m:
                detail[opt.lower()] = m.group(1)
                data_ref(m.group(1), access, "CICS", start)
        emit(Fact("cics", command, detail, start, end_line))


ADAPTER: Adapter = CobolAdapter()
