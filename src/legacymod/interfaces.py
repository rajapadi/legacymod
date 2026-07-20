"""Stage 10 — external-interface and transmission catalog.

Answers "what leaves the system, by what mechanism, to which server,
how often". Sources:

- Connect:Direct / NDM (both names for the same product) DMBATCH /
  DGADBATC SYSIN blocks — SNODE=, COPY FROM/TO datasets.
- Batch FTP/FTPS (PGM=FTP SYSIN: open host, put/get) and USS SFTP
  (BPXBATCH sftp/scp), XCOM — all parsed by the JCL adapter into
  ``transfer`` facts consumed here.
- IBM MQ: MQPUT/MQPUT1/MQGET calls in COBOL give queue names; the MQSC
  definitions extract resolves where a queue actually goes
  (QREMOTE -> RQMNAME + XMITQ -> sender CHANNEL -> CONNAME host).
- Dataset handoffs between jobs (writer job -> reader job) as internal
  interfaces.

Output: ``workspace/interfaces.csv`` + ``transfers_to`` graph edges
pointing at external_node nodes. Frequency uses run history when
present, else scheduler calendars (frequency_source records which).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections import defaultdict

from .config import Config
from .store import Store
from .opsdata import job_frequency

log = logging.getLogger(__name__)


def _iface_id(*parts: str) -> str:
    return "IF" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def _facts(store: Store, *types: str) -> list[dict]:
    q = ",".join("?" for _ in types)
    return [dict(r) | {"detail": json.loads(r["detail_json"] or "{}")}
            for r in store.query(
                f"SELECT f.*, a.path FROM facts f"
                f" JOIN artifacts a ON a.id=f.artifact_id"
                f" WHERE f.fact_type IN ({q})", types)]


def resolve_mq_queue(store: Store, queue: str) -> tuple[str, int]:
    """Follow QREMOTE -> XMITQ -> SDR channel -> CONNAME. Returns
    (target description, external 0/1)."""
    for f in _facts(store, "mq_qremote"):
        if f["name"] != queue:
            continue
        rqm = f["detail"].get("rqmname", "")
        xmitq = f["detail"].get("xmitq", "")
        host = ""
        for ch in _facts(store, "mq_channel"):
            if ch["detail"].get("xmitq") == xmitq and \
                    ch["detail"].get("chltype", "").startswith("S"):
                host = ch["detail"].get("conname", "").split("(")[0]
        target = rqm + (f"@{host}" if host else "")
        return target or queue, 1
    return "", 0   # local queue: internal


def run(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg) as store:
        store.execute("DELETE FROM interfaces")
        rows: list[dict] = []

        # 1) file transmissions from JCL transfer facts
        for f in _facts(store, "transfer"):
            d = f["detail"]
            dataset = d.get("from_dsn") or d.get("to_dsn") or f["name"]
            target = d.get("node", "")
            rows.append({
                "interface_id": _iface_id(d.get("protocol", ""), d.get("job", ""),
                                          dataset, target),
                "direction": d.get("direction", "outbound"),
                "protocol": d.get("protocol", "other"),
                "source_job_or_program": d.get("job", ""),
                "dataset_or_queue": dataset,
                "target_node": target,
                "external": 1})

        # 2) MQ interfaces resolved through the MQSC chain
        for f in _facts(store, "mq_call"):
            d = f["detail"]
            queue = d.get("queue", "")
            if not queue:
                continue
            prog = store.query(
                "SELECT name FROM facts WHERE artifact_id=?"
                " AND fact_type='program'", (f["artifact_id"],))
            source = prog[0]["name"] if prog else f["path"]
            target, external = resolve_mq_queue(store, queue)
            rows.append({
                "interface_id": _iface_id("mq", source, queue, target),
                "direction": "outbound" if d.get("operation") == "put"
                else "inbound",
                "protocol": "mq",
                "source_job_or_program": source,
                "dataset_or_queue": queue,
                "target_node": target,
                "external": external})

        # 3) dataset handoffs between jobs (internal interfaces)
        writers: dict[str, set[str]] = defaultdict(set)
        readers: dict[str, set[str]] = defaultdict(set)
        for e in store.query(
                "SELECT s.name sn, s.node_type st, d.name dn, d.node_type dt,"
                " e.edge_type et, e.detail_json FROM edges e"
                " JOIN nodes s ON s.id=e.src_node"
                " JOIN nodes d ON d.id=e.dst_node"
                " WHERE e.edge_type IN ('reads', 'writes')"
                " AND d.node_type='dataset'"):
            detail = json.loads(e["detail_json"] or "{}")
            job = detail.get("job") or (e["sn"].split(".")[0]
                                        if e["st"] == "step" else "")
            if not job:
                continue
            (writers if e["et"] == "writes" else readers)[e["dn"]].add(job)
        for ds in sorted(writers):
            for w in sorted(writers[ds]):
                for r in sorted(readers.get(ds, set()) - {w}):
                    rows.append({
                        "interface_id": _iface_id("dataset", w, ds, r),
                        "direction": "internal",
                        "protocol": "dataset",
                        "source_job_or_program": w,
                        "dataset_or_queue": ds,
                        "target_node": r,
                        "external": 0})

        # frequency + persist + graph edges
        seen = set()
        for row in rows:
            if row["interface_id"] in seen:
                continue
            seen.add(row["interface_id"])
            freq, source = job_frequency(store, row["source_job_or_program"])
            store.execute(
                "INSERT INTO interfaces VALUES (?,?,?,?,?,?,?,?,?)",
                (row["interface_id"], row["direction"], row["protocol"],
                 row["source_job_or_program"], row["dataset_or_queue"],
                 row["target_node"], freq, source, row["external"]))
        store.execute("DELETE FROM edges WHERE edge_type='transfers_to'")
        for row in rows:
            if not row["external"] or not row["target_node"]:
                continue
            kind = "queue" if row["protocol"] == "mq" else "dataset"
            src_node = store.node_id(kind, row["dataset_or_queue"])
            dst = store.node_id("external_node", row["target_node"])
            store.add_edge(src_node, dst, "transfers_to",
                           {"protocol": row["protocol"],
                            "interface": row["interface_id"]})
        store.commit()
        n = store.export_csv(
            "SELECT * FROM interfaces ORDER BY external DESC, protocol,"
            " interface_id", cfg.workspace / "interfaces.csv")
        ext = store.query("SELECT COUNT(*) c FROM interfaces WHERE external=1")
        print(f"interfaces: {n} interface(s), {ext[0]['c']} external -> "
              f"{cfg.workspace / 'interfaces.csv'}")
        for r in store.query("SELECT * FROM interfaces"
                             " ORDER BY external DESC, protocol"):
            print(f"  [{r['protocol']:7s}] {r['source_job_or_program']:10s} "
                  f"{r['dataset_or_queue']:18s} -> "
                  f"{r['target_node'] or '(internal)':28s} "
                  f"{r['frequency']} ({r['frequency_source']})"
                  f"{'  EXTERNAL' if r['external'] else ''}")
    return 0
