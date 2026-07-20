"""Stage 3b - pre-migration assessment.

- Complexity metrics per program (paragraph detail in the report):
  approximate cyclomatic complexity = 1 + count of IF / EVALUATE WHEN /
  PERFORM UNTIL / AT END / ON / INVALID KEY conditions; LOC; GO TO count;
  max IF-nesting depth.
- Clone detection: normalized line-window hashing across COBOL sources,
  identifiers/literals/numbers canonicalized, matches extended to maximal
  ranges. Window default is 5 normalized lines (see docs/decisions.md:
  the 30-line default from large-estate practice would miss the known
  clone in the sample estate; window is a parameter of ``find_clones``).
- Completeness: referenced-but-absent artifacts, with a known-utilities
  allowlist (unresolved != error for vendor utilities).
- Migration-blocker scan: assembler CALLs, ENTER TAL, dynamic CALL
  targets, sort exits (E15/E35), EXCP/SVC access, macro-level CICS.
- Effort: t-shirt size per program from LOC + cyclomatic + fan-in/out +
  blockers. Formula (documented in assess.md): score = LOC/100 +
  cyclomatic/5 + (fan_in + fan_out)/4 + 2*blockers; S<2, M<5, L<10,
  else XL. The t-shirt is a *realistic* relative size; the report also
  shows the upper bound (one size up) per the honest-numbers convention.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from .config import Config
from .store import Store
from .adapters.cobol import _ID, _KEYWORDS, source_lines

log = logging.getLogger(__name__)

_TSHIRT = ((2, "S"), (5, "M"), (10, "L"), (float("inf"), "XL"))
_UP = {"S": "M", "M": "L", "L": "XL", "XL": "XL"}


# -- clones -------------------------------------------------------------

def _normalize(text: str) -> list[tuple[int, str]]:
    """(lineno, canonical form) for each substantive code line."""
    out = []
    for no, code, is_comment, bad_seq, _ in source_lines(text):
        if is_comment or bad_seq or not code.strip():
            continue
        up = code.upper()
        up = re.sub(r"'[^']*'|\"[^\"]*\"", "L", up)
        up = _ID.sub(lambda m: m.group(0) if m.group(0) in _KEYWORDS else "ID", up)
        up = re.sub(r"\b\d+(\.\d+)?\b", "N", up)
        up = " ".join(up.split())
        if up:
            out.append((no, up))
    return out


def find_clones(sources: dict[str, str], window: int = 5,
                min_tokens: int = 20) -> list[dict]:
    """Cross-file (and disjoint same-file) duplicated logic blocks."""
    norm = {path: _normalize(text) for path, text in sources.items()}
    hashes: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path, lines in norm.items():
        for i in range(len(lines) - window + 1):
            chunk = "\n".join(l for _, l in lines[i:i + window])
            hashes[hashlib.sha1(chunk.encode()).hexdigest()].append((path, i))
    pair_hits: dict[tuple, set[tuple[int, int]]] = defaultdict(set)
    for locs in hashes.values():
        if len(locs) < 2:
            continue
        for a in range(len(locs)):
            for b in range(a + 1, len(locs)):
                (pa, ia), (pb, ib) = sorted((locs[a], locs[b]))
                if pa == pb and abs(ia - ib) < window:
                    continue  # self-overlap
                pair_hits[(pa, pb)].add((ia, ib))
    clones = []
    for (pa, pb), starts in pair_hits.items():
        # merge consecutive window hits into maximal ranges
        merged: list[list[int]] = []
        for ia, ib in sorted(starts):
            if merged and ia == merged[-1][1] + 1 and ib == merged[-1][3] + 1:
                merged[-1][1], merged[-1][3] = ia, ib
            else:
                merged.append([ia, ia, ib, ib])
        for a0, a1, b0, b1 in merged:
            la = norm[pa]
            lb = norm[pb]
            lines_a = (la[a0][0], la[a1 + window - 1][0])
            lines_b = (lb[b0][0], lb[b1 + window - 1][0])
            tokens = sum(len(l.split()) for _, l in la[a0:a1 + window])
            if tokens < min_tokens:
                continue
            clones.append({"file_a": pa, "lines_a": f"{lines_a[0]}-{lines_a[1]}",
                           "file_b": pb, "lines_b": f"{lines_b[0]}-{lines_b[1]}",
                           "token_count": tokens})
    return clones


# -- assessment driver --------------------------------------------------

def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        root = store.source_root()
        for table in ("metrics", "clones", "blockers", "missing_artifacts"):
            store.execute(f"DELETE FROM {table}")

        artifacts = {r["id"]: dict(r) for r in store.query(
            "SELECT * FROM artifacts")}
        facts = []
        for f in store.query(
                "SELECT f.*, a.path apath, a.artifact_type FROM facts f "
                "JOIN artifacts a ON a.id=f.artifact_id"):
            d = dict(f)
            d["detail"] = json.loads(f["detail_json"] or "{}")
            facts.append(d)
        by_type = defaultdict(list)
        for f in facts:
            by_type[f["fact_type"]].append(f)
        prog_of_artifact = {f["artifact_id"]: f["name"]
                            for f in by_type["program"]}

        # ---- metrics
        cyclo: dict[str, int] = defaultdict(int)
        goto: dict[str, int] = defaultdict(int)
        nesting: dict[str, int] = {}
        para_cyclo: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for f in by_type["condition"]:
            prog = prog_of_artifact.get(f["artifact_id"])
            if prog:
                cyclo[prog] += 1
                para_cyclo[prog][f["detail"].get("paragraph", "")] += 1
        for f in by_type["goto"]:
            prog = prog_of_artifact.get(f["artifact_id"])
            if prog:
                goto[prog] += 1
        for f in by_type["metrics_hint"]:
            prog = prog_of_artifact.get(f["artifact_id"])
            if prog:
                nesting[prog] = f["detail"].get("nesting_max", 0)
        fan_in: dict[str, int] = defaultdict(int)
        fan_out: dict[str, int] = defaultdict(int)
        for e in store.query(
                "SELECT s.name sn, s.node_type st, d.name dn, d.node_type dt"
                " FROM edges e JOIN nodes s ON s.id=e.src_node"
                " JOIN nodes d ON d.id=e.dst_node WHERE e.edge_type='calls'"):
            if e["dt"] == "program":
                fan_in[e["dn"]] += 1
            if e["st"] == "program":
                fan_out[e["sn"]] += 1

        for aid, prog in prog_of_artifact.items():
            a = artifacts[aid]
            store.execute(
                "INSERT INTO metrics (artifact_id, program, cyclomatic, loc,"
                " goto_count, nesting_max) VALUES (?,?,?,?,?,?)",
                (aid, prog, 1 + cyclo[prog], a["loc"], goto[prog],
                 nesting.get(prog, 0)))

        # ---- clones (COBOL family sources)
        sources = {}
        for a in artifacts.values():
            if a["artifact_type"] in ("cobol", "hpns_cobol") \
                    and a["encoding"] == "ascii":
                sources[a["path"]] = (root / a["path"]).read_text(
                    encoding="utf-8", errors="replace")
        clones = find_clones(sources)
        for i, c in enumerate(clones, 1):
            store.execute(
                "INSERT OR REPLACE INTO clones VALUES (?,?,?,?,?,?)",
                (f"CL{i:04d}", c["file_a"], c["lines_a"], c["file_b"],
                 c["lines_b"], c["token_count"]))

        # ---- completeness: referenced but absent
        have_programs = {p.upper() for p in prog_of_artifact.values()}
        have_members = {Path(a["path"]).stem.upper() for a in artifacts.values()}
        known = set(cfg.known_utilities)
        missing: dict[tuple[str, str], tuple[str, int]] = {}

        def ref(name: str, kind: str, by: str, line: int) -> None:
            name = name.upper()
            if name in known or not name:
                return
            exists = name in have_programs if kind == "program" \
                else name in have_members
            if not exists:
                missing.setdefault((name, kind), (by, line))

        for f in by_type["calls"]:
            prog = prog_of_artifact.get(f["artifact_id"], f["apath"])
            ref(f["name"], "program", prog, f["source_line_start"])
        for f in by_type["runs_program"]:
            ref(f["name"], "program", f["detail"].get("job", ""),
                f["source_line_start"])
        for f in by_type["step"]:
            d = f["detail"]
            if d.get("pgm"):
                ref(d["pgm"], "program", f["name"], f["source_line_start"])
            if d.get("proc"):
                ref(d["proc"], "proc", f["name"], f["source_line_start"])
        for f in by_type["copy"]:
            prog = prog_of_artifact.get(f["artifact_id"], f["apath"])
            ref(f["name"], "copybook", prog, f["source_line_start"])
        for f in by_type["include"]:
            ref(f["name"], "include", f["detail"].get("job", ""),
                f["source_line_start"])
        for f in by_type["cics"]:
            if f["detail"].get("mapset"):
                prog = prog_of_artifact.get(f["artifact_id"], f["apath"])
                ref(f["detail"]["mapset"], "mapset", prog,
                    f["source_line_start"])
        for f in by_type["rexx_call"]:
            ref(f["name"], "program", f["apath"], f["source_line_start"])
        for (name, kind), (by, line) in sorted(missing.items()):
            store.execute(
                "INSERT INTO missing_artifacts VALUES (?,?,?,?)",
                (name, kind, by, line))

        # ---- migration blockers
        def blocker(who: str, btype: str, line: int, detail: str) -> None:
            store.execute(
                "INSERT INTO blockers (program_or_job, blocker_type,"
                " evidence_line, detail) VALUES (?,?,?,?)",
                (who, btype, line, detail))

        for f in by_type["calls"]:
            prog = prog_of_artifact.get(f["artifact_id"], f["apath"])
            if f["name"].upper().startswith("ASM"):
                blocker(prog, "assembler_call", f["source_line_start"],
                        f"CALL '{f['name']}' - assembler member "
                        "(name heuristic; verify)")
            elif f["detail"].get("dynamic"):
                blocker(prog, "dynamic_call", f["source_line_start"],
                        f"dynamic CALL {f['name']} - target resolved at run "
                        "time")
        for f in by_type["enter_tal"]:
            prog = prog_of_artifact.get(f["artifact_id"], f["apath"])
            blocker(prog, "enter_tal", f["source_line_start"],
                    f"ENTER TAL {f['name'] or ''} - NonStop TAL interop")
        for f in by_type["hpns_divergence"]:
            prog = prog_of_artifact.get(f["artifact_id"], f["apath"])
            blocker(prog, "pathway_serverclass", f["source_line_start"],
                    f["detail"].get("evidence", ""))
        # source-level scans: sort exits, EXCP, macro-level CICS
        for a in artifacts.values():
            if a["encoding"] != "ascii":
                continue
            who = prog_of_artifact.get(a["id"], Path(a["path"]).stem.upper())
            if a["artifact_type"] in ("cobol", "hpns_cobol", "jcl"):
                text = (root / a["path"]).read_text(encoding="utf-8",
                                                    errors="replace")
                for no, line in enumerate(text.splitlines(), 1):
                    up = line.upper()
                    if re.search(r"\bMODS=\(?E(15|35)\b|\bE(15|35)=", up):
                        blocker(who, "sort_exit", no, line.strip())
                    if re.search(r"\bEXCP\b|\bSVC\s+\d", up):
                        blocker(who, "low_level_io", no, line.strip())
                    if re.search(r"\bDFH(KC|IC|TC|FC)\s+TYPE=", up):
                        blocker(who, "cics_macro_level", no, line.strip())

        store.commit()

        # ---- effort t-shirts
        blocker_count: dict[str, int] = defaultdict(int)
        for r in store.query("SELECT program_or_job, COUNT(*) c FROM blockers"
                             " GROUP BY program_or_job"):
            blocker_count[r["program_or_job"]] = r["c"]
        efforts = []
        for r in store.query("SELECT * FROM metrics ORDER BY program"):
            prog = r["program"]
            score = (r["loc"] or 0) / 100 + r["cyclomatic"] / 5 \
                + (fan_in[prog] + fan_out[prog]) / 4 + 2 * blocker_count[prog]
            size = next(s for lim, s in _TSHIRT if score < lim)
            efforts.append((prog, r["cyclomatic"], r["loc"], r["goto_count"],
                            r["nesting_max"], fan_in[prog], fan_out[prog],
                            blocker_count[prog], round(score, 2), size))

        # ---- exports + report
        ws = cfg.workspace
        store.export_csv("SELECT program, cyclomatic, loc, goto_count,"
                         " nesting_max FROM metrics ORDER BY program",
                         ws / "metrics.csv")
        store.export_csv("SELECT * FROM clones ORDER BY clone_id",
                         ws / "clones.csv")
        store.export_csv("SELECT program_or_job, blocker_type, evidence_line,"
                         " detail FROM blockers ORDER BY program_or_job",
                         ws / "blockers.csv")
        store.export_csv("SELECT * FROM missing_artifacts ORDER BY name",
                         ws / "missing_artifacts.csv")

        nclones = len(clones)
        nmissing = len(missing)
        nblock = sum(blocker_count.values())
        lines = [
            "# Assessment",
            "",
            f"**Verdict up front:** {len(efforts)} programs measured; "
            f"{nclones} clone pair(s); {nmissing} referenced-but-absent "
            f"artifact(s); {nblock} migration blocker(s). Effort sizes below "
            "are realistic relative t-shirts; the upper-bound column is one "
            "size up. Formula: score = LOC/100 + cyclomatic/5 + "
            "(fan_in+fan_out)/4 + 2*blockers; S<2, M<5, L<10, else XL.",
            "",
            "| program | cyclomatic | LOC | GO TOs | nesting | fan-in |"
            " fan-out | blockers | score | size (realistic) | upper bound |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
        for e in efforts:
            lines.append("| " + " | ".join(str(x) for x in e) +
                         f" | {_UP[e[-1]]} |")
        lines += ["", "## Paragraph-level cyclomatic detail", ""]
        for prog in sorted(para_cyclo):
            for para, c in sorted(para_cyclo[prog].items()):
                lines.append(f"- {prog}.{para or '(main)'}: {1 + c}")
        (ws / "assess.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        print(f"assess: {len(efforts)} programs, {nclones} clone pair(s), "
              f"{nmissing} missing artifact(s), {nblock} blocker(s) -> "
              f"{ws / 'assess.md'} (+ metrics/clones/blockers/"
              "missing_artifacts CSVs)")
        for e in efforts:
            print(f"  {e[0]:10s} cyclomatic={e[1]:<3d} loc={e[2]:<4d} "
                  f"blockers={e[7]} size={e[9]}")
    return 0

