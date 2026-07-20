"""Shared fixtures: a fully analyzed workspace over samples/estate."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacymod.cli import main

REPO = Path(__file__).resolve().parents[1]
ESTATE = REPO / "samples" / "estate"
OPS = REPO / "samples" / "ops"


@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> Path:
    """Workspace with ingest + analyze + graph already run."""
    ws = tmp_path_factory.mktemp("ws")
    for cmd in (["ingest", str(ESTATE)], ["analyze"], ["graph"]):
        rc = main(["--workspace", str(ws)] + cmd)
        assert rc == 0, f"{cmd} failed"
    return ws


def run_cli(ws: Path, *argv: str) -> int:
    return main(["--workspace", str(ws)] + list(argv))
