"""MQSC definitions adapter (runmqsc-style DEFINE listing).

Resolves MQ queue names found in code to their destination queue manager
and host: QREMOTE -> RQMNAME + XMITQ -> sender CHANNEL -> CONNAME host.
Fact shapes:

- ``mq_qlocal`` — DEFINE QLOCAL (descr, usage; usage XMITQ marks a
  transmission queue).
- ``mq_qremote`` — DEFINE QREMOTE (rname, rqmname, xmitq).
- ``mq_channel`` — DEFINE CHANNEL (chltype, conname, xmitq).
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def _opt(line: str, name: str) -> str:
    m = re.search(rf"\b{name}\s*\(\s*'?([^)']*)'?\s*\)", line, re.I)
    return m.group(1).strip() if m else ""


def parse_mqsc(text: str) -> ParseResult:
    res = ParseResult()
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        m = re.match(r"DEFINE\s+(QLOCAL|QREMOTE|QALIAS|CHANNEL)\s*"
                     r"\(\s*'?([^)']+)'?\s*\)", line, re.I)
        if not m:
            res.parse_errors += 1
            continue
        kind, name = m.group(1).upper(), m.group(2).strip()
        if kind == "QLOCAL":
            res.facts.append(Fact("mq_qlocal", name,
                                  {"descr": _opt(line, "DESCR"),
                                   "usage": _opt(line, "USAGE").upper()},
                                  no, no))
        elif kind == "QREMOTE":
            res.facts.append(Fact("mq_qremote", name,
                                  {"rname": _opt(line, "RNAME"),
                                   "rqmname": _opt(line, "RQMNAME"),
                                   "xmitq": _opt(line, "XMITQ")}, no, no))
        elif kind == "CHANNEL":
            res.facts.append(Fact("mq_channel", name,
                                  {"chltype": _opt(line, "CHLTYPE").upper(),
                                   "conname": _opt(line, "CONNAME"),
                                   "xmitq": _opt(line, "XMITQ")}, no, no))
        else:
            res.facts.append(Fact("mq_qalias", name,
                                  {"target": _opt(line, "TARGET")}, no, no))
    return res


class MqscAdapter:
    name = "mqsc"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "mqsc"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_mqsc(text)


ADAPTER: Adapter = MqscAdapter()
