"""Stage 1 — ingest: walk a source tree, classify every artifact.

Classification is extension + content sniffing. Unknown types are kept
with ``artifact_type=unknown``, never dropped. Encoding is detected via a
byte histogram: suspected EBCDIC files are flagged (``ebcdic?``) and left
unconverted. The estate directory is strictly read-only — all output goes
to the workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
from pathlib import Path

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

# extension -> (artifact_type, language) first guess
_EXT_MAP: dict[str, tuple[str, str]] = {
    ".cbl": ("cobol", "COBOL"),
    ".cob": ("cobol", "COBOL"),
    ".cpy": ("copybook", "COBOL"),
    ".jcl": ("jcl", "JCL"),
    ".proc": ("jcl", "JCL"),
    ".prc": ("jcl", "JCL"),
    ".ctl": ("utility_ctl", "CTL"),
    ".sql": ("db2ddl", "SQL"),
    ".ddl": ("db2ddl", "SQL"),
    ".rexx": ("rexx", "REXX"),
    ".rex": ("rexx", "REXX"),
    ".ezt": ("easytrieve", "Easytrieve"),
    ".bms": ("cics_bms", "BMS"),
    ".dbd": ("ims_dbd", "IMS"),
    ".psb": ("ims_psb", "IMS"),
    ".tal": ("tal", "TAL"),
    ".mqsc": ("mqsc", "MQSC"),
    ".xml": ("schedule_controlm", "XML"),
    ".csd": ("cics_csd", "CSD"),
}


def _sniff(text: str) -> tuple[str, str] | None:
    """Classify by content alone. Returns (artifact_type, language) or None."""
    up = text.upper()
    if "/* REXX */" in up[:200]:
        return "rexx", "REXX"
    if re.search(r"^\s*//\w+\s+JOB\s", up, re.M):
        return "jcl", "JCL"
    if re.search(r"^\s*//\w+\s+PROC\b", up, re.M):
        return "jcl", "JCL"
    if "DFHMSD" in up:
        return "cics_bms", "BMS"
    if re.search(r"\bDBD\s+NAME=", up):
        return "ims_dbd", "IMS"
    if "PSBGEN" in up or re.search(r"\bPCB\s+TYPE=", up):
        return "ims_psb", "IMS"
    if re.search(r"DEFINE\s+(TRANSACTION|PROGRAM|FILE)\s*\(", up):
        return "cics_csd", "CSD"
    if re.search(r"DEFINE\s+(QLOCAL|QREMOTE|QALIAS|CHANNEL)\s*\(", up):
        return "mqsc", "MQSC"
    if "JOB=" in up and "SCHID=" in up:
        return "schedule_ca7", "CA7"
    if "<DEFTABLE" in up:
        return "schedule_controlm", "XML"
    if "CREATE TABLE" in up:
        return "db2ddl", "SQL"
    if "IDENTIFICATION DIVISION" in up or "PROGRAM-ID" in up:
        if "ENTER TAL" in up or "SERVERCLASS" in up:
            return "hpns_cobol", "COBOL"
        return "cobol", "COBOL"
    if re.search(r"^\s*(INT|STRING|FIXED)?\s*PROC\s+\w+", up, re.M) and (
            "SUBPROC" in up or "BEGIN" in up):
        return "tal", "TAL"
    if re.search(r"^\s*FILE\s+\w+", up, re.M) and "JOB INPUT" in up:
        return "easytrieve", "Easytrieve"
    if re.search(r"^\s*\d\d\s+[\w-]+.*PIC\s", up, re.M):
        return "copybook", "COBOL"
    return None


def detect_encoding(data: bytes) -> str:
    """Byte-histogram EBCDIC detection. Flags, never converts.

    Heuristic: EBCDIC text is dominated by bytes >= 0x40 with very few
    bytes in the ASCII printable range 0x20-0x7E other than those that
    happen to overlap; ASCII/UTF-8 text is overwhelmingly < 0x80.
    """
    if not data:
        return "ascii"
    sample = data[:65536]
    high = sum(1 for b in sample if b >= 0x80)
    ratio_high = high / len(sample)
    if ratio_high < 0.10:
        return "ascii"
    # Try cp037: if it decodes to mostly printable text, call it EBCDIC.
    try:
        decoded = sample.decode("cp037")
        printable = sum(1 for c in decoded if c.isprintable() or c in "\r\n\t")
        if printable / len(decoded) > 0.95:
            return "ebcdic-cp037?"
    except UnicodeDecodeError:
        pass
    return "binary?"


def classify(path: Path, data: bytes) -> tuple[str, str, str, float, str]:
    """Return (artifact_type, language, encoding, confidence, text)."""
    encoding = detect_encoding(data)
    if encoding != "ascii":
        # Flagged, not converted: classify on extension only.
        atype, lang = _EXT_MAP.get(path.suffix.lower(), ("unknown", ""))
        return atype, lang, encoding, 0.5 if atype != "unknown" else 0.3, ""
    text = data.decode("utf-8", errors="replace")
    ext_guess = _EXT_MAP.get(path.suffix.lower())
    sniffed = _sniff(text)
    if ext_guess and sniffed:
        if sniffed[0] == ext_guess[0]:
            return sniffed[0], sniffed[1], encoding, 1.0, text
        # Content wins over extension (e.g. .cob file that is HPNS COBOL,
        # .txt that is a CA7 export) but confidence drops.
        return sniffed[0], sniffed[1], encoding, 0.8, text
    if sniffed:
        return sniffed[0], sniffed[1], encoding, 0.7, text
    if ext_guess:
        return ext_guess[0], ext_guess[1], encoding, 0.6, text
    return "unknown", "", encoding, 0.3, text


def run(args: argparse.Namespace, cfg: Config) -> int:
    src = Path(args.src_dir)
    if not src.is_dir():
        print(f"error: source directory not found: {src}")
        return 1
    with Store(cfg) as store:
        # Full re-ingest: replace artifacts and everything derived from them.
        store.execute("DELETE FROM facts")
        store.execute("DELETE FROM edges")
        store.execute("DELETE FROM nodes")
        store.execute("DELETE FROM artifacts")
        store.set_meta("source_root", str(src.resolve()))
        count = 0
        by_type: dict[str, int] = {}
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            atype, lang, encoding, confidence, text = classify(path, data)
            loc = text.count("\n") + (1 if text and not text.endswith("\n") else 0) \
                if text else 0
            rel = path.relative_to(src).as_posix()
            store.execute(
                "INSERT INTO artifacts (path, artifact_type, language, encoding,"
                " loc, sha256, confidence) VALUES (?,?,?,?,?,?,?)",
                (rel, atype, lang, encoding, loc,
                 hashlib.sha256(data).hexdigest(), confidence))
            by_type[atype] = by_type.get(atype, 0) + 1
            count += 1
        store.commit()
        n = store.export_csv(
            "SELECT path, artifact_type, language, encoding, loc, sha256,"
            " parse_errors, confidence FROM artifacts ORDER BY path",
            cfg.workspace / "inventory.csv")
        print(f"ingested {count} artifacts from {src} -> "
              f"{cfg.workspace / 'inventory.csv'} ({n} rows)")
        for atype in sorted(by_type):
            print(f"  {atype:18s} {by_type[atype]:>4d}")
    return 0
