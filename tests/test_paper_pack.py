import unittest

from bindery_gather import materials, workbench
from bindery_gather.db import Rejected, connect, one


def op(event, action, **fields):
    return {"id": event, "op": action, **fields}


SCAN_P = op("scan-p", "paper-scan", manifest="pack-b1", bundle="pack-p", format="braille", qty=3,
            grain="long", candidates=["emboss-150", "emboss-170"])
SCAN_Q = op("scan-q", "paper-scan", manifest="pack-b1", bundle="pack-q", format="braille", qty=3,
            grain="long", candidates=["emboss-150", "emboss-170"])


class PackedPaperTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(connect(":memory:"))
        self.work = workbench.load(workbench.FIXTURES / "workshop.json")
        # Everything up to and including the sealed edition.
        self.apply(*self.work[:9])

    def apply(self, *events):
        return workbench.apply(self.db, events)

    def pack_in(self):
        self.apply(SCAN_P, SCAN_Q, op("permit-p", "permit", signature="b1-s11", lot="pack-p", cap=2))

    def test_both_bundles_jointly_explain_one_manifest(self):
        self.pack_in()
        report = materials.packing_list(self.db, "pack-b1")
        self.assertEqual(report["tiers"], ["emboss-150", "emboss-170"])
        self.assertEqual(len(report["assignments"]), 2)
        # Two bundles that each claim the same grade cannot explain a one-of-each list.
        with self.assertRaisesRegex(Rejected, "cannot explain the packing manifest"):
            self.apply(op("scan-r", "paper-scan", manifest="pack-same", bundle="pack-r", format="braille",
                          qty=3, grain="long", candidates=["emboss-150"]),
                       op("scan-s", "paper-scan", manifest="pack-same", bundle="pack-s",
                          format="braille", qty=3, grain="long", candidates=["emboss-150"]))

    def test_roles_and_caps_stay_split_across_assignments(self):
        self.pack_in()
        p = materials.availability(self.db, "pack-p", "b1-s11")
        q = materials.availability(self.db, "pack-q", "b1-s11")
        self.assertEqual([r["role"] for r in p["assignments"]], ["standard", "substitute"])
        self.assertEqual([r["role"] for r in q["assignments"]], ["substitute", "standard"])
        # P's substitute branch is capped at 2; its standard branch is bounded only by stock.
        self.assertEqual(p["tentative_remaining"], 2)
        # Q has no permit, so as a substitute it can draw nothing; the shared
        # receipt is limited by the assumption under which Q would be heavy.
        self.assertEqual(q["tentative_remaining"], 0)
        review = materials.review(self.db, "b1-s11", ["pack-p", "pack-q"])
        self.assertFalse(review["pinned"])
        self.assertEqual(review["signature_grade"], "emboss-150")

    def test_tentative_receipt_is_legal_under_every_assignment(self):
        self.pack_in()
        self.apply(op("t1", "issue", signature="b1-s11", lot="pack-p", qty=1, basis="tentative"),
                   op("t2", "issue", signature="b1-s11", lot="pack-p", qty=1, basis="tentative"))
        # The third sheet is still physically present, but under the assignment
        # that makes P heavy the two-sheet cap is spent.  It cannot be issued.
        with self.assertRaisesRegex(Rejected, "tentative receipt exceeds a compatible grade assignment"):
            self.apply(op("t3", "issue", signature="b1-s11", lot="pack-p", qty=1, basis="tentative"))
        stock = materials.stock(self.db, "pack-p")
        self.assertEqual(stock["unissued"], 1)  # physically there, but not issuable
        # Nor may Q lean on the standard-role permission of a different hypothesis.
        with self.assertRaisesRegex(Rejected, "tentative receipt exceeds a compatible grade assignment"):
            self.apply(op("tq", "issue", signature="b1-s11", lot="pack-q", qty=1, basis="tentative"))

    def test_definite_issue_waits_for_an_unambiguous_grade(self):
        self.pack_in()
        with self.assertRaisesRegex(Rejected, "grade is still ambiguous"):
            self.apply(op("hard-issue", "issue", signature="b1-s11", lot="pack-p", qty=1))

    def test_label_narrows_assignments_and_keeps_existing_facts(self):
        self.pack_in()
        self.apply(op("t1", "issue", signature="b1-s11", lot="pack-p", qty=2, basis="tentative"))
        with self.assertRaisesRegex(Rejected, "grade is still ambiguous"):
            self.apply(op("confirm-early", "confirm", issue_id="t1"))
        scans = [
            {"serial": "P-outer", "position": 1, "front": [8, 1], "back": [2, 7], "turn": "long-edge", "head": "head-up"},
            {"serial": "P-inner", "position": 2, "front": [6, 3], "back": [4, 5], "turn": "long-edge", "head": "head-up"},
        ]
        self.apply(op("fold-p", "fold", issue="t1", piece="piece-p", scans=scans))
        self.apply(op("label-p", "label", bundle="pack-p", grade="emboss-170"))
        self.assertEqual(materials.packing_list(self.db, "pack-b1")["assignments"],
                         [{"pack-p": "emboss-170", "pack-q": "emboss-150"}])
        # Existing tentative receipt and the folded piece are unchanged by the label.
        self.assertEqual(materials.issue_report(self.db, "t1")["basis"], "tentative")
        self.assertEqual(materials.issue_report(self.db, "t1")["pieces"][0]["status"], "folded")
        self.assertEqual(self.apply(op("confirm-t1", "confirm", issue_id="t1"))[0]["decision"],
                         "tentative-receipt-confirmed")
        report = materials.issue_report(self.db, "t1")
        self.assertEqual((report["basis"], report["permit"]), ("definite", "permit-p"))
        self.assertEqual(report["folded_sheets"], 2)
        # P is heavy and its cap is exhausted; a third sheet needs the cap that
        # no longer exists, even though one sheet is physically left.
        with self.assertRaisesRegex(Rejected, "substitution cap exceeded"):
            self.apply(op("p-third", "issue", signature="b1-s11", lot="pack-p", qty=1))
        # Q is now the standard bundle and may be issued normally.
        self.assertEqual(self.apply(op("q-std", "issue", signature="b1-s11", lot="pack-q", qty=3))[0]["decision"],
                         "standard-paper-issued")

    def test_label_must_be_a_scanned_candidate(self):
        self.pack_in()
        with self.assertRaisesRegex(Rejected, "not a scanned candidate"):
            self.apply(op("bad-label", "label", bundle="pack-p", grade="emboss-999"))

    def test_incomplete_manifest_cannot_issue_until_both_bundles_are_scanned(self):
        self.apply(SCAN_P, op("permit-p", "permit", signature="b1-s11", lot="pack-p", cap=2))
        self.assertFalse(materials.packing_list(self.db, "pack-b1")["complete"])
        with self.assertRaisesRegex(Rejected, "packing manifest is incomplete"):
            self.apply(op("t-early", "issue", signature="b1-s11", lot="pack-p", qty=1, basis="tentative"))
        self.apply(SCAN_Q)
        self.assertEqual(materials.packing_list(self.db, "pack-b1")["complete"], True)

    def test_misdirected_sheet_quarantines_and_never_restores_paper(self):
        self.pack_in()
        self.apply(op("label-p", "label", bundle="pack-p", grade="emboss-170"),
                   op("q-std", "issue", signature="b1-s11", lot="pack-q", qty=3))
        scans = [
            {"serial": "X-outer", "position": 1, "front": [8, 1], "back": [2, 7], "turn": "short-edge", "head": "head-up"},
            {"serial": "X-inner", "position": 2, "front": [6, 3], "back": [4, 5], "turn": "long-edge", "head": "head-up"},
        ]
        result = self.apply(op("fold-x", "fold", issue="q-std", piece="piece-x", scans=scans))
        self.assertEqual(result[0]["decision"]["status"], "scrap")
        self.assertEqual(result[0]["decision"]["faults"], ["X-outer:direction"])
        report = materials.issue_report(self.db, "q-std")
        self.assertEqual([report[k] for k in ("folded_sheets", "scrap_sheets", "remaining")], [0, 2, 1])
        # The scrapped sheets stay consumed; unissued stock is not replenished.
        self.assertEqual(materials.stock(self.db, "pack-q")["unissued"], 0)
        # And this offline projection is reproducible from the journal alone.
        audit = workbench.audit(self.db)
        self.assertEqual(audit["audit"], "verified")
        self.assertEqual(audit["drift"], [])

    def test_confirmed_receipts_count_once_on_replay(self):
        self.pack_in()
        events = [op("t1", "issue", signature="b1-s11", lot="pack-p", qty=2, basis="tentative"),
                  op("label-p", "label", bundle="pack-p", grade="emboss-170"),
                  op("confirm-t1", "confirm", issue_id="t1")]
        self.apply(*events)
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")
        # Replaying the exact batch yields duplicates without spending again.
        self.assertEqual([d["decision"] for d in self.apply(*events)], ["duplicate"] * 3)
        self.assertEqual(materials.stock(self.db, "pack-p")["issued"], 2)

    def test_fixture_walks_both_valid_operator_choices(self):
        packed = workbench.load(workbench.FIXTURES / "packed-paper.json")
        self.apply(*packed)
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")
        review = materials.review(self.db, "b1-s11", ["pack-p", "pack-q"])
        self.assertTrue(review["pinned"])
        self.assertEqual(review["assignments"], [{"pack-p": "emboss-170", "pack-q": "emboss-150"}])
        report = materials.issue_report(self.db, "pack-t1")
        self.assertEqual((report["basis"], report["permit"], report["folded_sheets"]),
                         ("definite", "pack-permit-p", 2))
        # P is heavy: one physical sheet remains but the cap is spent.
        self.assertEqual(materials.stock(self.db, "pack-p")["unissued"], 1)
        self.assertEqual(materials.availability(self.db, "pack-p", "b1-s11")["tentative_remaining"], 0)
        # Q is standard; the misread fold is quarantined and its sheets consumed.
        self.assertEqual(materials.issue_report(self.db, "pack-q-issue")["scrap_sheets"], 2)
        self.assertEqual(materials.stock(self.db, "pack-q")["unissued"], 0)


if __name__ == "__main__":
    unittest.main()
