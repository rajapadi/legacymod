"""SQLite façade and CSV exporters.

All machine state lives in one SQLite database under the workspace.
Every human-facing catalog is additionally exported as plain CSV (the
source of truth for review edits). Excel exports are generated only if
``openpyxl`` happens to be installed — never required.

Schema is versioned via the ``schema_version`` table; migrations are the
numbered SQL files in ``legacymod/migrations/``.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Store:
    """Thin wrapper over the workspace SQLite database."""

    def __init__(self, cfg: Config, create: bool = True):
        self.cfg = cfg
        self.workspace = cfg.workspace
        if create:
            self.workspace.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(cfg.db_path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # -- schema ----------------------------------------------------------
    def _migrate(self) -> None:
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT)")
        applied = {r[0] for r in self.con.execute(
            "SELECT version FROM schema_version")}
        for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            version = int(sql_file.name.split("_")[0])
            if version in applied:
                continue
            self.con.executescript(sql_file.read_text(encoding="utf-8"))
            self.con.execute(
                "INSERT INTO schema_version VALUES (?, datetime('now'))",
                (version,))
            log.debug("applied migration %s", sql_file.name)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.con.commit()
        self.con.close()

    # -- meta ------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self.con.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def source_root(self) -> Path:
        root = self.get_meta("source_root")
        if not root:
            raise RuntimeError("no source_root recorded — run `legacymod ingest` first")
        return Path(root)

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.con.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self.con.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, params).fetchall()

    def commit(self) -> None:
        self.con.commit()

    # -- nodes / edges ---------------------------------------------------
    def node_id(self, node_type: str, name: str,
                artifact_id: int | None = None) -> int:
        """Get-or-create a graph node; returns its id."""
        row = self.con.execute(
            "SELECT id, artifact_id FROM nodes WHERE node_type=? AND name=?",
            (node_type, name)).fetchone()
        if row:
            if artifact_id is not None and row["artifact_id"] is None:
                self.con.execute("UPDATE nodes SET artifact_id=? WHERE id=?",
                                 (artifact_id, row["id"]))
            return int(row["id"])
        cur = self.con.execute(
            "INSERT INTO nodes (node_type, name, artifact_id) VALUES (?,?,?)",
            (node_type, name, artifact_id))
        return int(cur.lastrowid)

    def add_edge(self, src: int, dst: int, edge_type: str,
                 detail: dict[str, Any] | None = None,
                 origin: str = "parser") -> None:
        self.con.execute(
            "INSERT INTO edges (src_node, dst_node, edge_type, detail_json, origin) "
            "VALUES (?,?,?,?,?)",
            (src, dst, edge_type, json.dumps(detail or {}), origin))

    # -- CSV export ------------------------------------------------------
    def export_csv(self, sql: str, out_path: Path,
                   params: Sequence[Any] = ()) -> int:
        """Run a query and write the result as CSV. Returns row count."""
        rows = self.con.execute(sql, params)
        headers = [d[0] for d in rows.description]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(list(row))
                n += 1
        self._maybe_excel(out_path, headers)
        return n

    def _maybe_excel(self, csv_path: Path, headers: list[str]) -> None:
        """Optional Excel mirror of a CSV export, only if openpyxl exists."""
        try:
            import openpyxl  # type: ignore
        except ImportError:
            return
        wb = openpyxl.Workbook()
        ws = wb.active
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                ws.append(row)
        wb.save(csv_path.with_suffix(".xlsx"))
