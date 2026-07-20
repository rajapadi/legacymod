"""CA7 batch-schedule adapter.

CA7 export formats vary by shop; this parser targets exactly the
documented keyword shape used in samples/estate/scheduler/::

    JOB=<name>,SCHID=<nnn>,[CALENDAR=<cal>,][TIME=<hhmm>]
    DEP=(<pred>[,<pred>...])

Parsed fields (all of them): JOB (job name), SCHID (schedule id),
CALENDAR (calendar name), TIME (scheduled hhmm), DEP list — each DEP is
a predecessor job; a ``EXT.`` prefix marks a cross-system (external)
dependency and produces an external_node in the graph.

Fact shapes:

- ``sched_job`` — (schid, calendar, time, scheduler='ca7').
- ``sched_dep`` — name = predecessor, detail (job = successor,
  external 0/1, scheduler='ca7').
"""

from __future__ import annotations

import re

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult


def parse_ca7(text: str) -> ParseResult:
    res = ParseResult()
    current_job = ""
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"JOB=([\w.$#@]+)(.*)", line, re.I)
        if m:
            current_job = m.group(1).upper()
            rest = m.group(2)
            schid = re.search(r"SCHID=(\w+)", rest, re.I)
            cal = re.search(r"CALENDAR=([\w.$#@]+)", rest, re.I)
            time_ = re.search(r"TIME=(\d{4})", rest, re.I)
            res.facts.append(Fact("sched_job", current_job,
                                  {"schid": schid.group(1) if schid else "",
                                   "calendar": cal.group(1).upper() if cal else "",
                                   "time": time_.group(1) if time_ else "",
                                   "scheduler": "ca7"}, no, no))
            continue
        m = re.match(r"DEP=\(([^)]*)\)", line, re.I)
        if m and current_job:
            for dep in m.group(1).split(","):
                dep = dep.strip().upper()
                if not dep:
                    continue
                res.facts.append(Fact("sched_dep", dep,
                                      {"job": current_job,
                                       "external": 1 if dep.startswith("EXT.")
                                       else 0, "scheduler": "ca7"}, no, no))
            continue
        res.parse_errors += 1
    return res


class Ca7Adapter:
    name = "scheduler_ca7"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "schedule_ca7"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_ca7(text)


ADAPTER: Adapter = Ca7Adapter()
