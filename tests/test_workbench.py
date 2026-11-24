import json
import sqlite3
import unittest

from bindery_gather import binding, fulfillment, materials, workbench
from bindery_gather.db import Rejected, connect, one


def op(event, action, **fields):
    return {"id": event, "op": action, **fields}


class BinderyTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(connect(":memory:"))
        self.work = workbench.load(workbench.FIXTURES / "workshop.json")
        self.variants = workbench.load(workbench.FIXTURES / "variants.json")
        self.late = workbench.load(workbench.FIXTURES / "late-sample.json")
        self.failures = json.loads((workbench.FIXTURES / "rejections.json").read_text(encoding="utf-8"))

    def apply(self, *events):
        return workbench.apply(self.db, events)

    def before(self, event):
        index = next(i for i, record in enumerate(self.work) if record["id"] == event)
        self.apply(*self.work[:index])
        return index

    def reject(self, case, reason):
        with self.assertRaisesRegex(Rejected, reason):
            self.apply(*self.failures[case])

    def test_seal_requires_contiguous_pages_and_freezes_manifest(self):
        records = self.work[:9]
        wrong = [{**row, "first": 8} if row["id"] == "b1-s12" else row for row in records]
        with self.assertRaisesRegex(Rejected, "page gap or overlap"):
            self.apply(*wrong)
        self.assertIsNone(one(self.db, "SELECT * FROM editions"))
        with self.assertRaisesRegex(Rejected, "missing a volume"):
            self.apply(records[0], records[-1])
        self.assertEqual(self.apply(*records)[-1]["decision"], "edition-sealed")
        with self.assertRaisesRegex(Rejected, "already sealed"):
            self.apply(op("extra", "accessory", volume="b1-v1", sku="new-tab", qty=1))

    def test_paper_approval_scope_and_format(self):
        self.before("permit-heavy")
        with self.assertRaisesRegex(Rejected, "missing substitution permit"):
            self.apply(op("unapproved", "issue", signature="b1-s11", lot="paper-heavy", qty=1))
        self.apply(*self.work[11:15], *self.variants)
        self.reject("wrong-paper-format", "different reading format")
        self.reject("wrong-paper-cap", "substitution cap exceeded")
        self.assertEqual(materials.stock(self.db, "paper-heavy")["unissued"], 2)
        self.assertEqual(materials.issue_report(self.db, "issue-11")["permit"], "permit-heavy")
        self.apply(op("cross-grain", "paper", format="braille", grade="emboss-150", grain="short", qty=2))
        with self.assertRaisesRegex(Rejected, "grain incompatible"):
            self.apply(op("grain-issue", "issue", signature="b1-s11", lot="cross-grain", qty=1))

    def test_standard_paper_stock_cannot_be_double_issued(self):
        self.before("fold-11")
        self.apply(op("reserve-last", "issue", signature="b1-s11", lot="paper-emboss", qty=4))
        with self.assertRaisesRegex(Rejected, "stock exhausted"):
            self.apply(op("double-issue", "issue", signature="b1-s12", lot="paper-emboss", qty=1))
        self.assertEqual(materials.stock(self.db, "paper-emboss"),
                         {"lot": "paper-emboss", "received": 6, "issued": 6, "unissued": 0})

    def test_imposition_uses_physical_sheet_positions_and_charges_scrap(self):
        idx = self.before("fold-11")
        good, damaged = self.apply(*self.work[idx:idx + 2])
        self.assertEqual(good["decision"]["status"], "folded")
        self.assertEqual(damaged["decision"]["faults"], ["Q-outer:direction", "Q-outer:page-pair"])
        stock = materials.issue_report(self.db, "issue-11")
        self.assertEqual([stock[k] for k in ("folded_sheets", "scrap_sheets", "remaining")], [2, 2, 0])
        with self.assertRaisesRegex(Rejected, "unscanned sheets"):
            self.apply({**self.work[idx], "id": "extra-fold", "piece": "piece-too-many"})
        self.assertEqual(self.apply(self.work[idx])[0]["decision"], "duplicate")

    def test_sheet_serial_cannot_be_reused_even_on_another_issue(self):
        self.before("book-b1v1")
        orig = next(x for x in self.work if x["id"] == "fold-12")
        self.apply(op("issue-retry", "issue", signature="b1-s12", lot="paper-emboss", qty=1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply({**orig, "id": "repeat-sheet", "issue": "issue-retry", "piece": "relabel"})
        self.assertIsNone(one(self.db, "SELECT id FROM pieces WHERE id='relabel'"))
        self.assertEqual(materials.issue_report(self.db, "issue-retry")["remaining"], 1)

    def test_gather_exact_slot_identity_and_excludes_scrap_and_other_revision(self):
        idx = self.before("book-b1v1")
        self.apply(*self.variants)
        self.reject("scrap-in-book", "quarantined piece")
        self.reject("wrong-edition-piece", "wrong edition, volume or slot")
        with self.assertRaisesRegex(Rejected, "wrong edition, volume or slot"):
            self.apply({**self.work[idx], "id": "reverse-sigs", "pieces": ["piece-12", "piece-11"]})
        self.assertEqual(self.apply(self.work[idx])[0]["decision"], "book-gathered")
        with self.assertRaisesRegex(Rejected, "piece already gathered"):
            self.apply({**self.work[idx], "id": "overused-piece"})

    def test_sampling_and_certificate_membership(self):
        idx = self.before("sample-v1")
        self.reject("uncertified", "sampling-incomplete")
        self.assertEqual(self.apply(self.work[idx])[0]["decision"], "spine-pass")
        self.assertEqual(binding.run_report(self.db, "run-b1")["status"], "awaiting-certificate")
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply({**self.work[idx], "id": "duplicate-physical-sample"})
        self.apply(self.work[idx + 1])
        self.assertEqual(binding.run_report(self.db, "run-b1")["status"], "run-released")
        with self.assertRaisesRegex(Rejected, "membership is frozen"):
            self.apply(op("late-bind", "bind", run="run-b1", book="book-b1v2"))

    def test_kit_staging_enforces_exact_bound_book_and_order_quota(self):
        idx = self.before("stage-v1")
        self.apply(*self.variants)
        self.reject("wrong-edition-book", "edition cannot be mixed")
        with self.assertRaisesRegex(Rejected, "edition cannot be mixed"):
            self.apply(op("print-into-braille", "attach", kit="kit-atlas", book="book-l1"))
        with self.assertRaisesRegex(Rejected, "quota exhausted"):
            self.apply(op("extra-kit", "kit", order="order-atlas"))
        self.apply(self.work[idx])
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply({**self.work[idx], "id": "duplicate-volume"})
        self.apply(op("unstage", "detach", attachment="stage-v1"), {**self.work[idx], "id": "restage"})
        self.assertEqual(fulfillment.kit_report(self.db, "kit-atlas", 49)["volumes"][0]["book"]["attachment"], "restage")

    def test_temporary_permit_requires_exact_scope_grant_and_cap(self):
        idx = self.before("alloc-v2-cross")
        candidate = self.work[idx]
        self.reject("unapproved-cross-volume", "requires a permit")
        with self.assertRaisesRegex(Rejected, "invalid at allocation minute"):
            self.apply({**candidate, "id": "early-alloc", "minute": 39})
        with self.assertRaisesRegex(Rejected, "permit quantity exceeded"):
            self.apply({**candidate, "id": "beyond-cap", "qty": 2})
        self.assertEqual(self.apply(candidate)[0]["decision"], "accessory-allocated")
        self.assertEqual(fulfillment.kit_report(self.db, "kit-atlas", 60)["status"], "kit-ready")
        self.reject("expired-delivery", "allocation-outside-handover-window")
        self.apply(op("return-expired", "return", allocation="alloc-v2-cross"),
                   op("native-replacement", "accessory-lot", requirement="tabs-v2", qty=1),
                   op("replace-tab", "allocate", kit="kit-atlas", lot="native-replacement", requirement="tabs-v2", qty=1, minute=62))
        self.assertEqual(self.apply(op("ship-repacked", "deliver", kit="kit-atlas", minute=62))[0]["decision"], "kit-delivered")

    def test_per_volume_counts_and_lot_balance_are_distinct(self):
        self.before("alloc-v1")
        self.apply(op("all-local", "allocate", kit="kit-atlas", lot="lot-v1", requirement="tabs-v1", qty=1, minute=50))
        with self.assertRaisesRegex(Rejected, "volume accessory requirement exceeded"):
            self.apply(op("overfill-v1", "allocate", kit="kit-atlas", lot="lot-v1", requirement="tabs-v1", qty=1, minute=50))
        with self.assertRaisesRegex(Rejected, "accessory lot exhausted"):
            self.apply(op("overspend-v2", "allocate", kit="kit-atlas", lot="lot-v2", requirement="tabs-v2", qty=2, minute=50))
        self.assertIn("accessory-count:b1-v2:tactile-tab", fulfillment.kit_report(self.db, "kit-atlas", 50)["blockers"])
        self.assertNotIn("accessory-count:b1-v1:tactile-tab", fulfillment.kit_report(self.db, "kit-atlas", 50)["blockers"])

    def test_late_spine_sample_changes_handover_eligibility(self):
        self.apply(*self.work)
        historical = fulfillment.kit_report(self.db, "kit-atlas", 99)
        self.assertEqual((historical["minute"], historical["status"]), (60, "kit-delivered"))
        self.apply(*self.late)
        state = fulfillment.kit_report(self.db, "kit-atlas", 99)
        self.assertEqual(state["status"], "handover-review-required")
        self.assertEqual(state["blockers"], ["b1-v1:run-quarantined", "b1-v2:run-quarantined"])
        self.assertEqual(one(self.db, "SELECT COUNT(*) FROM deliveries")[0], 1)
        with self.assertRaisesRegex(Rejected, "delivered kit is frozen"):
            self.apply(op("reopen-delivery", "return", allocation="alloc-v2-cross"))
        with connect(":memory:") as before_handover:
            workbench.apply(before_handover, self.work[:-1] + self.late)
            with self.assertRaisesRegex(Rejected, "run-quarantined"):
                workbench.apply(before_handover, self.work[-1:])
            self.assertEqual(one(before_handover, "SELECT COUNT(*) FROM deliveries")[0], 0)

    def test_replay_rebuilds_tampered_paper_and_frozen_handover(self):
        self.apply(*self.work, *self.late)
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")
        with self.assertRaisesRegex(Rejected, "event ID conflict"):
            self.apply(op("orphan-lot", "paper", format="braille", grade="new", grain="long", qty=1),
                       {**self.work[-1], "minute": 61})
        self.assertIsNone(one(self.db, "SELECT id FROM paper_lots WHERE id='orphan-lot'"))
        self.db.execute("UPDATE allocations SET qty=99 WHERE id='alloc-v2-cross'")
        self.db.execute("UPDATE deliveries SET evidence='{}'")
        replay = workbench.audit(self.db)
        self.assertEqual(replay["drift"], ["allocations", "deliveries"])
        self.assertEqual(replay["handovers"][0]["evidence"]["status"], "kit-ready")
