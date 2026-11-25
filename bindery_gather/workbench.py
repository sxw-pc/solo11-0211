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
    "proof-intake": binding.intake, "defect-intake": binding.intake, "intake": binding.intake,
    "proof-capture": binding.intake, "defect-capture": binding.intake, "capture": binding.intake,
    "defect-read": binding.record_reading, "defect-reading": binding.record_reading,
    "proof-read": binding.record_reading, "proof-reading": binding.record_reading,
    "reading": binding.record_reading, "defect": binding.record_reading,
    "braille-defect": binding.record_reading,
    "proof-ruler": binding.record_ruler, "ruler": binding.record_ruler,
    "calibrate": binding.record_ruler, "calibration": binding.record_ruler,
    "proof-calibrate": binding.record_ruler, "proof-calibration": binding.record_ruler,
    "ruler-read": binding.record_ruler,
    "proof-task": binding.open_task, "proof-order": binding.open_task,
    "open-proof": binding.open_task, "proof-open": binding.open_task,
    "proof-triage": binding.open_task, "triage": binding.open_task,
    "route-proof": binding.open_task, "submit-proof": binding.open_task,
    "repair": lambda db, event, **kw: binding.open_task(db, event, forced_kind="page-repair", **kw),
    "repair-order": lambda db, event, **kw: binding.open_task(db, event, forced_kind="page-repair", **kw),
    "proof-repair": lambda db, event, **kw: binding.open_task(db, event, forced_kind="page-repair", **kw),
    "page-repair": lambda db, event, **kw: binding.open_task(db, event, forced_kind="page-repair", **kw),
    "retest": lambda db, event, **kw: binding.open_task(db, event, forced_kind="spread-retest", **kw),
    "proof-retest": lambda db, event, **kw: binding.open_task(db, event, forced_kind="spread-retest", **kw),
    "retest-order": lambda db, event, **kw: binding.open_task(db, event, forced_kind="spread-retest", **kw),
    "spread-retest": lambda db, event, **kw: binding.open_task(db, event, forced_kind="spread-retest", **kw),
    "pair-retest": lambda db, event, **kw: binding.open_task(db, event, forced_kind="spread-retest", **kw),
    "proof-complete": binding.complete_task, "complete-proof": binding.complete_task,
    "complete-task": binding.complete_task, "proof-done": binding.complete_task,
    "finish-proof": binding.complete_task, "complete-order": binding.complete_task,
    "proof-withdraw": binding.withdraw_task, "withdraw-proof": binding.withdraw_task,
    "withdraw-task": binding.withdraw_task, "cancel-proof": binding.withdraw_task,
    "withdraw-order": binding.withdraw_task,
    "proof-review": binding.review_task, "review-task": binding.review_task,
    "proof-resolve": binding.resolve_segment, "ruler-resolve": binding.resolve_segment,
    "resolve-segment": binding.resolve_segment,
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
