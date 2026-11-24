import unittest
from contextlib import ExitStack

from bindery_gather import materials, press, workbench
from bindery_gather.db import Rejected, connect


def event(event_id, op, **payload):
    return {"id": event_id, "op": op, **payload}


class PressTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.db = stack.enter_context(connect(":memory:"))
        base = workbench.load(workbench.FIXTURES / "workshop.json")
        workbench.apply(self.db, base[:next(i for i, row in enumerate(base) if row["op"] == "fold")])
        self.first = next(row for row in base if row["id"] == "fold-11")
        self.scrap = next(row for row in base if row["id"] == "fold-scrap")
        self.other = next(row for row in base if row["id"] == "fold-12")
        workbench.apply(self.db, [event("die-11", "die-register", signature="b1-s11", capacity=2, minute=10)])

    @staticmethod
    def tracked(record, minute):
        return {**record, "op": "die-fold", "die": "die-11", "minute": minute}

    def test_scrap_spends_die_and_paper_capacity(self):
        one = workbench.apply(self.db, [self.tracked(self.scrap, 11)])[0]
        self.assertEqual(one["decision"]["status"], "scrap")
        self.assertEqual(press.report(self.db, "die-11")["remaining"], 0)
        with self.assertRaisesRegex(Rejected, "service required"):
            workbench.apply(self.db, [self.tracked(self.first, 12)])
        self.assertEqual(materials.issue_report(self.db, "issue-11")["remaining"], 2)
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")

    def test_overhaul_starts_a_new_cycle_with_old_use_retained(self):
        first = self.tracked(self.first, 11)
        reset = event("overhaul-11", "die-overhaul", die="die-11", minute=12)
        second = self.tracked(self.scrap, 13)
        workbench.apply(self.db, [first, reset, second])
        view = press.report(self.db, "die-11")
        self.assertEqual(view["remaining"], 0)
        self.assertEqual([use["service"] for use in view["uses"]], [None, "overhaul-11"])
        self.assertTrue(all(result["decision"] == "duplicate" for result in workbench.apply(self.db, [first, reset, second])))
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")

    def test_wrong_signature_rolls_back_plate_service_and_fold(self):
        with self.assertRaisesRegex(Rejected, "wrong die"):
            workbench.apply(self.db, [event("overhaul-11", "die-overhaul", die="die-11", minute=12),
                                      self.tracked(self.other, 13)])
        view = press.report(self.db, "die-11")
        self.assertEqual(view["remaining"], 2)
        self.assertEqual(view["services"], [])
        self.assertEqual(view["uses"], [])
        self.assertEqual(materials.issue_report(self.db, "issue-12")["pieces"], [])

    def test_service_minute_cannot_precede_prior_press_use(self):
        workbench.apply(self.db, [self.tracked(self.first, 11)])
        with self.assertRaisesRegex(Rejected, "out-of-order die service"):
            workbench.apply(self.db, [event("overhaul-11", "die-overhaul", die="die-11", minute=10)])
