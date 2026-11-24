"""Fixture dispatch, persisted reports and independent database reconstruction."""
import json
from pathlib import Path

from . import binding, fulfillment, imposition, materials
from .db import connect, ingest, integer, require


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ACTIONS = {
    "edition": imposition.edition, "volume": imposition.volume, "signature": imposition.signature,
    "accessory": imposition.accessory, "seal": imposition.seal,
    "paper": materials.receive, "permit": materials.permit, "issue": materials.issue, "fold": materials.fold,
    "gather": binding.gather, "run": binding.run, "bind": binding.bind, "sample": binding.sample, "certify": binding.certify,
    "order": fulfillment.order, "kit": fulfillment.open_kit, "attach": fulfillment.attach, "detach": fulfillment.detach,
    "accessory-lot": fulfillment.accessory_lot, "bridge": fulfillment.bridge, "allocate": fulfillment.allocate,
    "return": fulfillment.return_allocation, "deliver": fulfillment.deliver,
}


def load(path):
    records = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(records, list), "fixture must be an array of events")
    return records


def apply(db, records):
    return ingest(db, records, ACTIONS)


def demo(db):
    return apply(db, load(FIXTURES / "workshop.json") + load(FIXTURES / "late-sample.json"))


def read_state(db, minute=None):
    if minute is None:
        minute = db.execute("""SELECT COALESCE(MAX(minute),0) FROM (
             SELECT minute FROM allocations UNION ALL SELECT granted FROM bridges UNION ALL SELECT minute FROM deliveries)""").fetchone()[0]
    integer(minute, "report minute")

    def ids(table):
        return [row[0] for row in db.execute(f"SELECT id FROM {table} ORDER BY id")]
    return {"as_of": minute, "paper": [materials.stock(db, key) for key in ids("paper_lots")],
            "issues": [materials.issue_report(db, key) for key in ids("issues")],
            "runs": [binding.run_report(db, key) for key in ids("runs")],
            "kits": [fulfillment.kit_report(db, key, minute) for key in ids("kits")],
            "handovers": [{**dict(row), "evidence": json.loads(row["evidence"])} for row in
                          db.execute("SELECT * FROM deliveries ORDER BY id")]}


def audit(db, minute=None):
    db.execute("BEGIN")
    try:
        rows = db.execute("SELECT id,body FROM events ORDER BY seq").fetchall()
        records = [json.loads(row["body"]) for row in rows]
        require(all(row["id"] == body.get("id") for row, body in zip(rows, records)), "journal identity mismatch")
        with connect(":memory:") as rebuilt:
            apply(rebuilt, records)
            drift = []
            tables = rebuilt.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            for table, in tables:
                query = f'SELECT * FROM "{table}" ORDER BY rowid'
                if [tuple(row) for row in db.execute(query)] != [tuple(row) for row in rebuilt.execute(query)]:
                    drift.append(table)
            return {"audit": "verified" if not drift else "projection-drift", "events": len(records),
                    "drift": drift, **read_state(rebuilt, minute)}
    finally:
        db.rollback()
