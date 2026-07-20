"""Control-M adapter — parses the DEFTABLE XML export format.

Fact shapes:

- ``sched_job`` — JOB element (application, sub_application, cyclic,
  nodeid, from_time [WHEN FROMTIME], scheduler='controlm').
- ``sched_dep`` — derived by matching OUTCOND -> INCOND names within the
  export: name = producing job, detail (job = consuming job, condition,
  external 0/1, scheduler='controlm').
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from .base import Adapter, ArtifactRef, Fact, ParseContext, ParseResult

log = logging.getLogger(__name__)


def parse_controlm(text: str) -> ParseResult:
    res = ParseResult()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        log.warning("Control-M XML parse error: %s", exc)
        res.parse_errors = 1
        return res
    producers: dict[str, str] = {}   # condition -> producing job
    consumers: list[tuple[str, str]] = []
    for job in root.iter("JOB"):
        name = (job.get("JOBNAME") or "").upper()
        when = job.find("WHEN")
        res.facts.append(Fact("sched_job", name, {
            "application": job.get("APPLICATION") or "",
            "sub_application": job.get("SUB_APPLICATION") or "",
            "cyclic": job.get("CYCLIC") or "0",
            "nodeid": job.get("NODEID") or "",
            "from_time": (when.get("FROMTIME") if when is not None else "")
            or "",
            "time": (when.get("FROMTIME") if when is not None else "") or "",
            "scheduler": "controlm"}))
        for out in job.iter("OUTCOND"):
            if (out.get("SIGN") or "ADD").upper() != "DEL":
                producers[(out.get("NAME") or "").upper()] = name
        for inc in job.iter("INCOND"):
            consumers.append(((inc.get("NAME") or "").upper(), name))
    for cond, consumer in consumers:
        producer = producers.get(cond)
        if producer:
            res.facts.append(Fact("sched_dep", producer,
                                  {"job": consumer, "condition": cond,
                                   "external": 0, "scheduler": "controlm"}))
        else:
            res.facts.append(Fact("sched_dep", cond,
                                  {"job": consumer, "condition": cond,
                                   "external": 1, "scheduler": "controlm"},
                                  needs_review=1, confidence=0.6))
    return res


class ControlMAdapter:
    name = "scheduler_controlm"
    tier = "deterministic"

    def applicable(self, artifact: ArtifactRef) -> bool:
        return artifact.artifact_type == "schedule_controlm"

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult:
        return parse_controlm(text)


ADAPTER: Adapter = ControlMAdapter()
