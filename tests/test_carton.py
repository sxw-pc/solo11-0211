import unittest
from contextlib import ExitStack

from bindery_gather import carton, fulfillment, workbench
from bindery_gather.db import Rejected, connect


def event(event_id, op, **payload):
    return {"id": event_id, "op": op, **payload}


class CartonTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.db = stack.enter_context(connect(":memory:"))
        self.base = workbench.load(workbench.FIXTURES / "workshop.json")[:-1]
        self.scan = workbench.load(workbench.FIXTURES / "carton-scan.json")
        self.late = workbench.load(workbench.FIXTURES / "late-sample.json")
        workbench.apply(self.db, self.base)

    def test_carton_dispatch_and_replay_keep_the_original_capture(self):
        workbench.apply(self.db, self.scan)
        view = carton.report(self.db, "box-atlas", 60)
        self.assertEqual(view["kit_status"], "kit-delivered")
        self.assertFalse(view["missing"])
        self.assertFalse(view["unexpected"])
        self.assertEqual(view["seal"]["evidence"]["kit_status"], "kit-ready")
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")
        self.assertTrue(all(item["decision"] == "duplicate" for item in workbench.apply(self.db, self.scan)))

    def test_receiving_two_reads_of_one_label_does_not_complete_the_carton(self):
        workbench.apply(self.db, self.scan[:5])
        workbench.apply(self.db, [{**self.scan[4], "id": "duplicate-physical-label", "device": "left-bench"}])
        view = carton.report(self.db, "box-atlas", 54)
        self.assertEqual(view["missing"], [("allocation", "alloc-v2-cross")])
        with self.assertRaisesRegex(Rejected, "captured labels"):
            workbench.apply(self.db, [self.scan[-2]])
        self.assertIsNone(carton.report(self.db, "box-atlas", 54)["seal"])

    def test_late_failed_sample_blocks_dispatch_but_preserves_seal(self):
        workbench.apply(self.db, self.scan[:-1])
        workbench.apply(self.db, self.late)
        with self.assertRaisesRegex(Rejected, "run-quarantined"):
            workbench.apply(self.db, [self.scan[-1]])
        after = carton.report(self.db, "box-atlas", 60)
        self.assertEqual(after["kit_status"], "kit-held")
        self.assertEqual(after["seal"]["evidence"]["kit_status"], "kit-ready")
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")

    def test_reallocation_invalidates_the_components_of_a_sealed_carton(self):
        workbench.apply(self.db, self.scan[:-1])
        workbench.apply(self.db, [event("remove-tab", "return", allocation="alloc-v2-cross"),
                                 event("replace-tab", "allocate", kit="kit-atlas", lot="lot-v1",
                                       requirement="tabs-v2", qty=1, minute=57, bridge="temp-v2")])
        self.assertEqual(fulfillment.kit_report(self.db, "kit-atlas", 60)["status"], "kit-ready")
        with self.assertRaisesRegex(Rejected, "components changed"):
            workbench.apply(self.db, [self.scan[-1]])

    def test_wrong_carton_content_and_invalid_time_fail_without_sealing(self):
        workbench.apply(self.db, self.scan[:-2])
        with self.assertRaisesRegex(Rejected, "captured labels"):
            workbench.apply(self.db, [{**self.scan[-2], "minute": 51}])
        wrong = event("other-book-label", "carton-read", carton="box-atlas", device="left-bench",
                      minute=51, kind="book", reference="elsewhere")
        with self.assertRaisesRegex(Rejected, "captured labels"):
            workbench.apply(self.db, [wrong, self.scan[-2]])
        view = carton.report(self.db, "box-atlas", 54)
        self.assertFalse(view["unexpected"])
        self.assertIsNone(view["seal"])
