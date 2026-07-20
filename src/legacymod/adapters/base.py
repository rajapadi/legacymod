"""Adapter protocol and shared types.

Every technology adapter implements :class:`Adapter`:

- ``name``: adapter id recorded on the artifact row.
- ``tier``: ``"deterministic"`` (regex / island parsing — must tolerate
  anything and never crash) or ``"llm_assisted"`` (structure proposed via
  the LLM provider; every fact marked ``origin='llm', needs_review=1``).
- ``applicable(artifact)``: whether it wants the artifact.
- ``parse(artifact, text, ctx)``: emit Facts.

Facts are typed rows (see ``migrations/001_initial.sql``); ``detail`` is a
schemaless dict — each adapter documents its shapes in its module
docstring. Registering a new technology = one new module exposing an
``ADAPTER`` object; see README "Adding an adapter".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ArtifactRef:
    """Inventory row handed to adapters."""

    id: int
    path: str          # relative to the estate root
    artifact_type: str
    language: str
    encoding: str
    loc: int


@dataclass
class Fact:
    fact_type: str
    name: str
    detail: dict[str, Any] = field(default_factory=dict)
    line_start: int = 0
    line_end: int = 0
    origin: str = "parser"
    confidence: float = 1.0
    needs_review: int = 0


@dataclass
class ParseResult:
    facts: list[Fact] = field(default_factory=list)
    parse_errors: int = 0


class ParseContext:
    """Cross-cutting services available to adapters during analyze."""

    def __init__(self, cfg: Any, store: Any = None):
        self.cfg = cfg
        # Store handle for llm_assisted adapters (cache + llm_log live
        # there). None in unit tests that exercise pure parsing.
        self.store = store


class Adapter(Protocol):
    name: str
    tier: str

    def applicable(self, artifact: ArtifactRef) -> bool: ...

    def parse(self, artifact: ArtifactRef, text: str,
              ctx: ParseContext) -> ParseResult: ...
