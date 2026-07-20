"""Stage 9 — behavior-first equivalence harness.

Fixture layout: ``workspace/fixtures/<unit>/case_*/`` with
``expected.*`` (legacy baseline), ``actual.*`` (modernized output) and a
``case.json``::

    {"record": "PAY-RECORD",       # layout for flat comparison
     "format": "flat" | "csv",
     "legacy_cmd": [...],          # optional: produce expected.* locally
     "modern_cmd": [...],          # optional: produce actual.* locally
     "oracle_source": "cobol/X.cbl"  # optional GnuCOBOL oracle
    }

Fixtures are captured from the real system when available; when the
dialect allows, ``oracle_source`` lets GnuCOBOL (``cobc``, feature-
detected on PATH — skipped gracefully when absent) act as a local
execution oracle to (re)generate ``expected.*``. SELECT...ASSIGN ddnames
are mapped to fixture files via ``DD_<ddname>`` environment variables,
the GnuCOBOL convention.

The comparator diffs expected vs actual **field by field** — EBCDIC
(cp037) and packed-decimal aware for flat files via the record layout;
column-aware for CSV. Per-case elapsed time for both sides is recorded
when both are executed locally (a Dual Run-style parity report with a
performance column; timing is informational, never pass/fail at fixture
scale). A unit is ``equivalence: PASS`` only when 100% of cases pass;
anything less lists the exact mismatching fields.

Shipped demo fixtures live in ``samples/fixtures/<unit>/`` and are
copied into the workspace on first run.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config
from .store import Store
from .decompose import sync_units_from_csv
from .specgen import find_unit, spec_data
from .datamig import _layout

log = logging.getLogger(__name__)


def _find_record_layout(store: Store, cfg: Config, unit: dict,
                        record: str) -> tuple[list[dict], int]:
    data = spec_data(store, cfg, unit)
    for rec in data["records"]:
        if rec["legacy_name"].upper() == record.upper():
            layout, length, _ = _layout(rec)
            return layout, length
    raise ValueError(f"record {record!r} not found in unit "
                     f"{unit['name']}'s data model")


def _unpack(layout: list[dict], length: int, data: bytes) -> list[dict]:
    """EBCDIC/packed-aware record split, reusing the datamig field kinds."""
    records = []
    for base in range(0, len(data) - length + 1, length):
        rec = {}
        for f in layout:
            chunk = data[base + f["offset"]: base + f["offset"] + f["length"]]
            if f["kind"] == "display_char":
                rec[f["name"]] = chunk.decode("cp037").rstrip()
            elif f["kind"] == "comp3":
                h = chunk.hex()
                body, sign = h[:-1], h[-1]
                if f["decimals"]:
                    body = body[:-f["decimals"]] + "." + body[-f["decimals"]:]
                v = body.lstrip("0") or "0"
                if v.startswith("."):
                    v = "0" + v
                rec[f["name"]] = ("-" if sign in ("d", "b") else "") + v
            elif f["kind"] == "comp":
                rec[f["name"]] = str(int.from_bytes(chunk, "big", signed=True))
            else:
                s = chunk.decode("cp037")
                if f["decimals"]:
                    s = s[:-f["decimals"]] + "." + s[-f["decimals"]:]
                rec[f["name"]] = s.strip()
        records.append(rec)
    return records


def compare_flat(layout: list[dict], length: int, expected: bytes,
                 actual: bytes) -> list[str]:
    mismatches = []
    exp = _unpack(layout, length, expected)
    act = _unpack(layout, length, actual)
    if len(exp) != len(act):
        mismatches.append(f"record-count expected={len(exp)} actual={len(act)}")
    for i, (e, a) in enumerate(zip(exp, act), 1):
        for f in layout:
            if e[f["name"]] != a[f["name"]]:
                mismatches.append(
                    f"record {i} field {f['name']}: expected "
                    f"{e[f['name']]!r} actual {a[f['name']]!r}")
    return mismatches


def compare_csv(expected: Path, actual: Path) -> list[str]:
    with open(expected, newline="", encoding="utf-8-sig") as fh:
        exp = list(csv.DictReader(fh))
    with open(actual, newline="", encoding="utf-8-sig") as fh:
        act = list(csv.DictReader(fh))
    mismatches = []
    if len(exp) != len(act):
        mismatches.append(f"row-count expected={len(exp)} actual={len(act)}")
    for i, (e, a) in enumerate(zip(exp, act), 1):
        for col in e:
            if e.get(col, "") != a.get(col, ""):
                mismatches.append(f"row {i} column {col}: expected "
                                  f"{e.get(col)!r} actual {a.get(col)!r}")
    return mismatches


def _run_side(cmd: list[str], cwd: Path) -> float:
    t0 = time.perf_counter()
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, timeout=300)
    return (time.perf_counter() - t0) * 1000


def _oracle(case_dir: Path, source_rel: str, store: Store) -> float | None:
    """GnuCOBOL execution oracle: compile + run to produce expected.dat."""
    cobc = shutil.which("cobc")
    if not cobc:
        log.info("cobc not on PATH - GnuCOBOL oracle skipped for %s",
                 case_dir.name)
        return None
    src = store.source_root() / source_rel
    exe = case_dir / "oracle.exe"
    subprocess.run([cobc, "-x", "-free" if src.suffix == ".cob" else "-fixed",
                    "-o", str(exe), str(src)], check=True,
                   capture_output=True, timeout=300)
    env = dict(**__import__("os").environ)
    for f in case_dir.glob("input.*"):
        # map every input file to its ddname (input.EMPMAST -> DD_EMPMAST)
        dd = f.suffix.lstrip(".").upper()
        env[f"DD_{dd}"] = str(f)
        env[dd] = str(f)
    t0 = time.perf_counter()
    subprocess.run([str(exe)], cwd=case_dir, env=env, check=True,
                   capture_output=True, timeout=300)
    return (time.perf_counter() - t0) * 1000


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        sync_units_from_csv(store, cfg)
        unit = find_unit(store, args.unit)
        if not unit:
            print(f"validate: no unit {args.unit!r}")
            return 1
        fixtures = cfg.workspace / "fixtures" / unit["name"]
        if not fixtures.is_dir():
            shipped = Path(__file__).resolve().parents[2] / "samples" / \
                "fixtures" / unit["name"]
            if shipped.is_dir():
                shutil.copytree(shipped, fixtures)
                print(f"validate: copied shipped fixtures {shipped} -> "
                      f"{fixtures}")
            else:
                print(f"validate: no fixtures at {fixtures} - create "
                      "case_* directories (see docs/architecture.md)")
                return 1
        cases = sorted(p for p in fixtures.glob("case_*") if p.is_dir())
        if not cases:
            print(f"validate: no case_* directories under {fixtures}")
            return 1

        store.execute("DELETE FROM validation_results WHERE unit=?",
                      (unit["name"],))
        outdir = cfg.workspace / "validation" / unit["name"]
        outdir.mkdir(parents=True, exist_ok=True)
        results = []
        for case_dir in cases:
            meta = json.loads((case_dir / "case.json").read_text(
                encoding="utf-8")) if (case_dir / "case.json").is_file() else {}
            legacy_ms = modern_ms = None
            if meta.get("oracle_source"):
                try:
                    legacy_ms = _oracle(case_dir, meta["oracle_source"], store)
                except subprocess.CalledProcessError as exc:
                    log.warning("oracle failed for %s: %s", case_dir.name, exc)
            if meta.get("legacy_cmd"):
                legacy_ms = _run_side(meta["legacy_cmd"], case_dir)
            if meta.get("modern_cmd"):
                modern_ms = _run_side(meta["modern_cmd"], case_dir)
            expected = next(iter(case_dir.glob("expected.*")), None)
            actual = next(iter(case_dir.glob("actual.*")), None)
            if not expected or not actual:
                results.append((case_dir.name, 0,
                                ["missing expected.*/actual.* file"],
                                legacy_ms, modern_ms))
                continue
            if meta.get("format", "flat") == "csv":
                mismatches = compare_csv(expected, actual)
            else:
                layout, length = _find_record_layout(
                    store, cfg, unit, meta.get("record", ""))
                mismatches = compare_flat(layout, length,
                                          expected.read_bytes(),
                                          actual.read_bytes())
            results.append((case_dir.name, 0 if mismatches else 1,
                            mismatches, legacy_ms, modern_ms))

        passed = sum(r[1] for r in results)
        equivalence = "PASS" if passed == len(results) else "FAIL"
        for name, ok, mism, lms, mms in results:
            store.execute(
                "INSERT INTO validation_results VALUES (?,?,?,?,?,?)",
                (unit["name"], name, ok, "; ".join(mism), lms, mms))
        store.commit()
        store.export_csv(
            "SELECT unit, case_name, passed, mismatched_fields,"
            " legacy_elapsed_ms, modern_elapsed_ms FROM validation_results"
            " WHERE unit=? ORDER BY case_name",
            outdir / "results.csv", (unit["name"],))

        lines = [
            f"# Validation report - unit {unit['name']}",
            "",
            f"**Equivalence: {equivalence}** - {passed}/{len(results)} "
            "case(s) passed (a unit passes only at 100%). Timing columns "
            "are informational (fixture-scale, never a pass/fail "
            "criterion).",
            "",
            "| case | result | legacy ms | modern ms | mismatched fields |",
            "|---|---|---:|---:|---|",
        ]
        for name, ok, mism, lms, mms in results:
            lines.append(
                f"| {name} | {'PASS' if ok else 'FAIL'} |"
                f" {f'{lms:.1f}' if lms is not None else 'n/a'} |"
                f" {f'{mms:.1f}' if mms is not None else 'n/a'} |"
                f" {'; '.join(mism) if mism else '-'} |")
        if shutil.which("cobc") is None:
            lines += ["", "GnuCOBOL oracle: `cobc` not found on PATH - "
                          "oracle regeneration skipped (expected.* files "
                          "used as shipped)."]
        (outdir / "report.md").write_text("\n".join(lines) + "\n",
                                          encoding="utf-8")
        if equivalence == "PASS":
            store.execute(
                "UPDATE units SET status='validated' WHERE unit_id=?"
                " AND status='generated'", (unit["unit_id"],))
            store.commit()
        print(f"validate[{unit['name']}]: equivalence {equivalence} "
              f"({passed}/{len(results)}) -> {outdir / 'report.md'}")
        for name, ok, mism, *_ in results:
            head = mism[0] if mism else ""
            print(f"  {name}: {'PASS' if ok else 'FAIL'}"
                  + (f" - {head}" if head else ""))
    return 0 if equivalence == "PASS" else 2
