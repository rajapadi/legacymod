"""JCL + PROC adapter, including utility control-card parsing.

Fact shapes:

- ``job`` — //name JOB (class, msgclass).
- ``step`` — EXEC (job, step, pgm, proc, cond, region).
- ``dd`` — DD statement (job, step, ddname, dsn, disp, instream 0/1).
- ``runs_program`` — program actually run inside a utility step, e.g.
  IKJEFT01 ``RUN PROGRAM(X) PLAN(Y)`` (job, step, via, plan).
- ``vsam_def`` — IDCAMS DEFINE CLUSTER (dataset, keys, recordsize, kind).
- ``lineage`` — utility copy/sort producing one dataset from another
  (from_dsn, to_dsn, via, job, step, sort_fields, include_cond).
- ``transfer`` — file transmission (protocol ndm|ftp|ftps|sftp|xcom,
  direction, from_dsn, to_dsn, node/host, job, step, remote_file).
  The FTP client step is classified ``sftp`` when the target host name
  itself indicates SFTP (see docs/decisions.md).
- ``include`` — JCL INCLUDE member; ``symbolic`` — SET symbol=value.

CA7/Control-M schedule facts come from the scheduler adapters, not here.
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult

_UTILS_SORT = {"SORT", "DFSORT", "SYNCSORT", "ICEMAN"}
_UTILS_NDM = {"DMBATCH", "DGADBATC"}


def parse_jcl(text: str) -> ParseResult:
    res = ParseResult()
    job = ""
    step = ""
    step_pgm: dict[str, str] = {}
    step_params: dict[str, str] = {}
    dds: list[dict] = []
    current_dd: dict | None = None
    instream: dict | None = None
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if instream is not None:
            if line.startswith("/*"):
                instream = None
                continue
            if line.startswith("//"):
                instream = None
            else:
                instream["data"].append((no, line))
                continue
        if not line.startswith("//"):
            res.parse_errors += 1
            continue
        if line.startswith("//*"):
            continue
        body = line[2:]
        if body[:1] in (" ", "\t"):
            # JCL continues a statement when the previous card ends with a
            # comma; otherwise a leading-blank line is an operation with a
            # blank name field (e.g. "//   INCLUDE MEMBER=X").
            if current_dd is not None and \
                    current_dd["params"].rstrip().endswith(","):
                current_dd["params"] = current_dd["params"].rstrip() \
                    + body.strip()
                continue
            parts = body.split(None, 1)
            if not parts:
                continue
            name, op = "", parts[0]
            params = parts[1] if len(parts) > 1 else ""
        else:
            parts = body.split(None, 2)
            if len(parts) < 2:
                res.parse_errors += 1
                continue
            name, op = parts[0], parts[1]
            params = parts[2] if len(parts) > 2 else ""
        current_dd = None
        if op == "JOB":
            job = name
            res.facts.append(Fact("job", name, {"params": params}, no, no))
        elif op == "EXEC":
            step = name
            m = re.search(r"PGM=([A-Z0-9$#@]+)", params)
            pm = re.search(r"PROC=([A-Z0-9$#@]+)", params)
            pgm = m.group(1) if m else ""
            proc = pm.group(1) if pm else ("" if m else params.split(",")[0])
            cm = re.search(r"COND=(\([^)]*\)|\S+?)(?:,|$)", params)
            step_pgm[step] = pgm
            step_params[step] = params
            res.facts.append(Fact("step", f"{job}.{step}",
                                  {"job": job, "step": step, "pgm": pgm,
                                   "proc": proc,
                                   "cond": cm.group(1) if cm else ""}, no, no))
        elif op == "DD":
            dsn = ""
            m = re.search(r"DSN=([A-Z0-9.&()+-]+)", params)
            if m:
                dsn = m.group(1)
            dm = re.search(r"DISP=(\([^)]*\)|\w+)", params)
            entry = {"job": job, "step": step, "ddname": name, "dsn": dsn,
                     "disp": dm.group(1) if dm else "", "line": no,
                     "params": params, "data": [], "instream": 0}
            if re.match(r"\*|DATA", params.strip() or ""):
                entry["instream"] = 1
                instream = entry
            dds.append(entry)
            current_dd = entry
        elif op == "INCLUDE":
            m = re.search(r"MEMBER=([A-Z0-9$#@]+)", params)
            if m:
                res.facts.append(Fact("include", m.group(1),
                                      {"job": job}, no, no))
        elif op == "SET":
            for m in re.finditer(r"([A-Z0-9$#@]+)=([^,]*)", params):
                res.facts.append(Fact("symbolic", m.group(1),
                                      {"value": m.group(2), "job": job}, no, no))
        elif op in ("PROC", "PEND"):
            continue
        else:
            res.parse_errors += 1

    for dd in dds:
        # re-extract DSN in case it arrived on a continuation line
        if not dd["dsn"]:
            m = re.search(r"DSN=([A-Z0-9.&()+-]+)", dd["params"])
            if m:
                dd["dsn"] = m.group(1)
        res.facts.append(Fact("dd", dd["ddname"],
                              {k: dd[k] for k in
                               ("job", "step", "ddname", "dsn", "disp",
                                "instream")}, dd["line"], dd["line"]))

    _parse_utilities(res, dds, step_pgm, step_params)
    return res


def _instream_text(dds: list[dict], step: str, *ddnames: str) -> tuple[str, int]:
    for dd in dds:
        if dd["step"] == step and dd["ddname"] in ddnames and dd["data"]:
            return "\n".join(t for _, t in dd["data"]), dd["data"][0][0]
    return "", 0


def _step_dsn(dds: list[dict], step: str, ddname: str) -> str:
    for dd in dds:
        if dd["step"] == step and dd["ddname"] == ddname:
            return dd["dsn"]
    return ""


def _parse_utilities(res: ParseResult, dds: list[dict],
                     step_pgm: dict[str, str],
                     step_params: dict[str, str]) -> None:
    for step, pgm in step_pgm.items():
        job = next((d["job"] for d in dds if d["step"] == step), "")
        if pgm == "IKJEFT01":
            text, line = _instream_text(dds, step, "SYSTSIN")
            for m in re.finditer(r"RUN\s+PROGRAM\((\w+)\)(?:\s+PLAN\((\w+)\))?",
                                 text, re.I):
                res.facts.append(Fact("runs_program", m.group(1).upper(),
                                      {"job": job, "step": step,
                                       "via": "IKJEFT01",
                                       "plan": (m.group(2) or "").upper()},
                                      line, line))
        elif pgm == "IDCAMS":
            text, line = _instream_text(dds, step, "SYSIN")
            flat = re.sub(r"-\s*\n\s*", " ", text)
            for m in re.finditer(
                    r"DEFINE\s+CLUSTER\s*\(\s*NAME\((\S+?)\)(.*?)(?:\)\s*$|\Z)",
                    flat, re.I | re.S):
                body = m.group(2)
                km = re.search(r"KEYS\((\d+)\s+(\d+)\)", body, re.I)
                rm = re.search(r"RECORDSIZE\((\d+)\s+(\d+)\)", body, re.I)
                kind = "KSDS" if re.search(r"\bINDEXED\b", body, re.I) else \
                       "ESDS" if re.search(r"\bNONINDEXED\b", body, re.I) else ""
                res.facts.append(Fact("vsam_def", m.group(1).upper(),
                                      {"dataset": m.group(1).upper(),
                                       "keys": list(km.groups()) if km else [],
                                       "recordsize": list(rm.groups()) if rm else [],
                                       "kind": kind, "job": job, "step": step},
                                      line, line))
            for m in re.finditer(r"REPRO\s+(.+)", flat, re.I):
                args = m.group(1)
                src = re.search(r"INDATASET\((\S+?)\)", args, re.I)
                dst = re.search(r"OUTDATASET\((\S+?)\)", args, re.I)
                infile = re.search(r"INFILE\((\w+)\)", args, re.I)
                outfile = re.search(r"OUTFILE\((\w+)\)", args, re.I)
                from_dsn = src.group(1) if src else \
                    _step_dsn(dds, step, infile.group(1)) if infile else ""
                to_dsn = dst.group(1) if dst else \
                    _step_dsn(dds, step, outfile.group(1)) if outfile else ""
                if from_dsn or to_dsn:
                    res.facts.append(Fact("lineage", f"{from_dsn}->{to_dsn}",
                                          {"from_dsn": from_dsn, "to_dsn": to_dsn,
                                           "via": "IDCAMS REPRO", "job": job,
                                           "step": step}, line, line))
        elif pgm in _UTILS_SORT:
            text, line = _instream_text(dds, step, "SYSIN")
            sf = re.search(r"SORT\s+FIELDS=(\S+)", text, re.I)
            ic = re.search(r"(INCLUDE|OMIT)\s+COND=(\S+)", text, re.I)
            from_dsn = _step_dsn(dds, step, "SORTIN")
            to_dsn = _step_dsn(dds, step, "SORTOUT")
            if from_dsn and to_dsn:
                res.facts.append(Fact("lineage", f"{from_dsn}->{to_dsn}",
                                      {"from_dsn": from_dsn, "to_dsn": to_dsn,
                                       "via": pgm, "job": job, "step": step,
                                       "sort_fields": sf.group(1) if sf else "",
                                       "include_cond":
                                           f"{ic.group(1)} {ic.group(2)}" if ic else ""},
                                      line, line))
        elif pgm == "IEBGENER":
            from_dsn = _step_dsn(dds, step, "SYSUT1")
            to_dsn = _step_dsn(dds, step, "SYSUT2")
            if from_dsn and to_dsn:
                res.facts.append(Fact("lineage", f"{from_dsn}->{to_dsn}",
                                      {"from_dsn": from_dsn, "to_dsn": to_dsn,
                                       "via": "IEBGENER", "job": job,
                                       "step": step}, 0, 0))
        elif pgm in _UTILS_NDM:
            text, line = _instream_text(dds, step, "SYSIN")
            flat = re.sub(r"-\s*\n\s*", " ", text)
            snode = re.search(r"SNODE=(\S+)", flat, re.I)
            for m in re.finditer(
                    r"COPY\s+FROM\s*\(\s*DSN=(\S+?)[\s)].*?TO\s*\(\s*DSN=(\S+?)[\s)]",
                    flat, re.I | re.S):
                res.facts.append(Fact("transfer", m.group(1).upper(),
                                      {"protocol": "ndm", "direction": "outbound",
                                       "from_dsn": m.group(1).upper(),
                                       "to_dsn": m.group(2).upper(),
                                       "node": snode.group(1).upper() if snode else "",
                                       "job": job, "step": step}, line, line))
        elif pgm in ("FTP", "FTPS"):
            text, line = _instream_text(dds, step, "SYSIN", "INPUT")
            host = ""
            hm = re.search(r"^\s*open\s+(\S+)", text, re.I | re.M)
            if hm:
                host = hm.group(1)
            proto = "ftps" if pgm == "FTPS" else "ftp"
            if "sftp" in host.lower():
                proto = "sftp"
            for m in re.finditer(r"^\s*(put|get)\s+'?([^'\s]+)'?(?:\s+(\S+))?",
                                 text, re.I | re.M):
                verb = m.group(1).lower()
                res.facts.append(Fact("transfer", m.group(2).upper(),
                                      {"protocol": proto,
                                       "direction": "outbound" if verb == "put"
                                       else "inbound",
                                       "from_dsn": m.group(2).upper() if verb == "put" else "",
                                       "to_dsn": m.group(2).upper() if verb == "get" else "",
                                       "remote_file": m.group(3) or "",
                                       "node": host, "job": job, "step": step},
                                      line, line))
        elif pgm == "BPXBATCH":
            # the sftp/scp command line lives in EXEC PARM= or STDPARM/STDIN
            joined = step_params.get(step, "")
            stext, line = _instream_text(dds, step, "STDPARM", "STDIN")
            joined += " " + stext
            m = re.search(r"(?:sftp|scp)\b[^\n]*?(\w+)@([\w.\-]+)",
                          joined, re.I)
            if m:
                res.facts.append(Fact("transfer", m.group(2),
                                      {"protocol": "sftp", "direction": "outbound",
                                       "from_dsn": "", "to_dsn": "",
                                       "node": m.group(2), "job": job,
                                       "step": step}, line or 0, line or 0))
        elif pgm == "XCOMJOB":
            text, line = _instream_text(dds, step, "SYSIN01", "SYSIN")
            rm = re.search(r"REMOTE_SYSTEM=(\S+)", text, re.I)
            fm = re.search(r"LOCAL_FILE=(\S+)", text, re.I)
            res.facts.append(Fact("transfer", (fm.group(1) if fm else "").upper(),
                                  {"protocol": "xcom", "direction": "outbound",
                                   "from_dsn": (fm.group(1) if fm else "").upper(),
                                   "to_dsn": "",
                                   "node": rm.group(1) if rm else "",
                                   "job": job, "step": step}, line, line))


class JclAdapter:
    name = "jcl"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "jcl"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_jcl(text)


ADAPTER: Adapter = JclAdapter()
