import copy
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bindery_gather import binding, fulfillment, quality, workbench
from bindery_gather.db import Rejected, connect, one


FIXTURES = workbench.FIXTURES
BENCH = FIXTURES / "qc-bench.json"
INBOX = FIXTURES / "qc-inbox"
COMPACT = INBOX / "02-compact.csv"
STATIONS = FIXTURES / "qc-stations.json"
NIGHT_SHIFT = FIXTURES / "qc-shift-night.json"
SINGLE_SHIFT = FIXTURES / "qc-shift-single.json"
DELAYED_SHIFT = FIXTURES / "qc-shift-delayed.json"


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(connect(":memory:"))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workshop = workbench.load(FIXTURES / "workshop.json")
        self.variants = workbench.load(FIXTURES / "variants.json")

    def setup_workshop(self):
        workbench.apply(self.db, self.workshop + self.variants)

    def write_export(self, document, filename="export.json"):
        path = self.directory / filename
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_batch_intake_retries_and_keeps_legacy_sample(self):
        self.setup_workshop()
        result = workbench.intake_quality(self.db, [BENCH])
        self.assertEqual([r["decision"] for r in result], ["reading-retained", "spine-pass"])
        clone = self.directory / "renamed.json"
        clone.write_bytes(BENCH.read_bytes())
        self.assertEqual([r["decision"] for r in workbench.intake_quality(self.db, [clone])], ["duplicate"] * 2)
        report = quality.report(self.db, "run-b1")
        member = report["members"][0]
        self.assertEqual(member["sample"], "sample-v1")
        self.assertIsNone(member["readings"][0]["sample"])
        self.assertEqual(report["missing"], ["bind-v2"])
        self.assertEqual(binding.run_report(self.db, "run-l1")["status"], "awaiting-certificate")

    def test_source_conflict_rolls_back_other_files_in_the_same_intake(self):
        self.setup_workshop()
        workbench.intake_quality(self.db, [BENCH])
        changed = json.loads(BENCH.read_text(encoding="utf-8"))
        changed["readings"][0]["jaw_mm"] += 1
        with self.assertRaisesRegex(Rejected, "event ID conflict"):
            workbench.intake_quality(self.db, [COMPACT, self.write_export(changed)])
        self.assertEqual(quality.report(self.db, "run-b1")["missing"], ["bind-v2"])
        self.assertEqual(quality.report(self.db, "run-b2")["missing"], ["bind-b2"])
        self.assertEqual(one(self.db, "SELECT COUNT(*) FROM qc_readings")[0], 2)

    def test_invalid_readings_and_unknown_binding_leave_no_partial_intake(self):
        self.setup_workshop()
        original = json.loads(BENCH.read_text(encoding="utf-8"))
        for field, value, reason in [
            ("pull", True, "invalid pull"), ("skew", 2**63, "integer range"),
            ("reading", "01", "duplicate source reading"), ("binding", "unknown", "unknown bindings"),
        ]:
            with self.subTest(field=field):
                data = copy.deepcopy(original)
                data["readings"][1][field] = value
                with self.assertRaisesRegex(ValueError, reason):
                    workbench.intake_quality(self.db, [COMPACT, self.write_export(data)])
                self.assertEqual(one(self.db, "SELECT COUNT(*) FROM qc_readings")[0], 0)
                self.assertEqual(one(self.db, "SELECT COUNT(*) FROM samples")[0], 1)

    def test_recovery_orders_mixed_exports_and_updates_delivered_kit(self):
        self.setup_workshop()
        before = fulfillment.kit_report(self.db, "kit-atlas", 99)
        frozen = one(self.db, "SELECT evidence FROM deliveries")[0]
        self.assertEqual(before["status"], "kit-delivered")
        recovered = workbench.recover_quality(self.db, "run-b1", INBOX)
        self.assertEqual(recovered["exports_scanned"], 2)
        self.assertEqual([r["decision"] for r in recovered["decisions"]], ["spine-fail", "reading-retained"])
        state = quality.report(self.db, "run-b1")
        self.assertEqual((state["status"], state["certificate"], state["missing"]), ("run-quarantined", "cert-b1", []))
        readings = state["members"][1]["readings"]
        self.assertEqual([(row["device"], row["pull"], row["skew"]) for row in readings],
                         [("compact-gauge", 6, 3), ("wide-gauge", 8, 1)])
        self.assertEqual(readings[0]["id"], readings[0]["sample"])
        self.assertIsNone(readings[1]["sample"])
        after = fulfillment.kit_report(self.db, "kit-atlas", 99)
        self.assertEqual((after["status"], after["minute"]), ("handover-review-required", 60))
        self.assertEqual(after["blockers"], ["b1-v1:run-quarantined", "b1-v2:run-quarantined"])
        self.assertEqual(one(self.db, "SELECT evidence FROM deliveries")[0], frozen)
        self.assertEqual(workbench.recover_quality(self.db, "run-b1", INBOX)["decisions"], [])
        self.assertEqual(quality.report(self.db, "run-b2")["missing"], ["bind-b2"])
        other = workbench.recover_quality(self.db, "run-b2", INBOX)
        self.assertEqual([r["decision"] for r in other["decisions"]], ["spine-pass"])

    def test_available_source_changes_certification_and_pending_kit(self):
        staged = [r for r in self.workshop if r["op"] not in {"certify", "deliver"}]
        certify = [r for r in self.workshop if r["op"] == "certify"]
        for source, expected in [(COMPACT, "run-quarantined"), (INBOX / "01-wide.json", "awaiting-certificate")]:
            with self.subTest(source=source.name), connect(":memory:") as db:
                inbox = self.directory / source.stem
                inbox.mkdir()
                (inbox / source.name).write_bytes(source.read_bytes())
                workbench.apply(db, staged)
                workbench.recover_quality(db, "run-b1", inbox)
                self.assertEqual(binding.run_report(db, "run-b1")["status"], expected)
                if source == COMPACT:
                    with self.assertRaisesRegex(Rejected, "run cannot be certified: run-quarantined"):
                        workbench.apply(db, certify)
                    self.assertEqual(fulfillment.kit_report(db, "kit-atlas", 60)["status"], "kit-held")
                else:
                    self.assertEqual(workbench.apply(db, certify)[0]["decision"], "run-released")
                    self.assertEqual(fulfillment.kit_report(db, "kit-atlas", 60)["status"], "kit-ready")

    def test_existing_database_picks_up_new_capture_table(self):
        path = self.directory / "existing.sqlite"
        with connect(path) as db:
            workbench.apply(db, self.workshop)
            db.execute("DROP TABLE qc_readings")
        with connect(path) as db:
            self.assertEqual(quality.report(db, "run-b1")["missing"], ["bind-v2"])
            workbench.recover_quality(db, "run-b1", INBOX)
            self.assertEqual(workbench.audit(db)["audit"], "verified")

    def test_recovered_failure_blocks_pending_delivery_and_is_audited(self):
        workbench.apply(self.db, self.workshop[:-1])
        self.assertEqual(fulfillment.kit_report(self.db, "kit-atlas", 60)["status"], "kit-ready")
        workbench.recover_quality(self.db, "run-b1", INBOX)
        with self.assertRaisesRegex(Rejected, "kit held: .*run-quarantined"):
            workbench.apply(self.db, self.workshop[-1:])
        self.assertEqual(workbench.audit(self.db)["audit"], "verified")
        self.db.execute("UPDATE qc_readings SET jaw_mm=jaw_mm+1")
        self.assertEqual(workbench.audit(self.db)["drift"], ["qc_readings"])

    def test_pending_books_report_actual_paper_and_available_fixtures(self):
        staged = [r for r in self.workshop if r["op"] not in {"sample", "certify", "deliver"}]
        workbench.apply(self.db, staged + self.variants)
        stations = quality.load_stations(STATIONS)
        first, second = quality.report(self.db, "run-b1", stations)["members"]
        self.assertEqual([(p["lot"], p["sheets"]) for p in first["paper"]], [("paper-heavy", 2), ("paper-emboss", 1)])
        self.assertEqual([(p["lot"], p["sheets"]) for p in second["paper"]], [("paper-emboss", 1)])
        self.assertEqual([(s["fixture"], s["max_jaw_mm"]) for s in first["station_candidates"]],
                         [("compact-jaws", 2), ("wide-jaws", 12)])
        large = quality.report(self.db, "run-l1", stations)["members"][0]
        self.assertEqual([s["fixture"] for s in large["station_candidates"]], ["wide-jaws"])
        workbench.intake_quality(self.db, [BENCH])
        workbench.apply(self.db, [r for r in self.workshop if r["op"] == "certify"])
        self.assertEqual(binding.run_report(self.db, "run-b1")["status"], "run-released")
        self.assertEqual(quality.report(self.db, "run-b1", stations)["members"][0]["station_candidates"], [])

    def test_recovery_rejects_malformed_csv_even_with_a_good_export_present(self):
        self.setup_workshop()
        (self.directory / "02-compact.csv").write_bytes(COMPACT.read_bytes())
        with (self.directory / "01-broken.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(quality.EXPORT_FIELDS)
            writer.writerow(["device", "batch", "reading"])
        with self.assertRaisesRegex(ValueError, "01-broken.csv.*wrong column count"):
            workbench.recover_quality(self.db, "run-b1", self.directory)
        self.assertEqual(quality.report(self.db, "run-b1")["missing"], ["bind-v2"])

    def test_cli_round_trip_with_a_persistent_database(self):
        db = self.directory / "workshop.sqlite"

        def cli(command, *args):
            proc = subprocess.run([sys.executable, "-B", "-m", "bindery_gather", command, "--db", str(db), *map(str, args)],
                                  cwd=FIXTURES.parent, capture_output=True, text=True, check=True)
            return json.loads(proc.stdout)

        cli("apply", FIXTURES / "workshop.json")
        cli("apply", FIXTURES / "variants.json")
        self.assertEqual(len(cli("qc-intake", BENCH)), 2)
        self.assertEqual(cli("qc-status", "run-b1", "--stations", STATIONS)["missing"], ["bind-v2"])
        self.assertEqual(cli("qc-recover", "run-b1", INBOX)["exports_scanned"], 2)
        self.assertEqual(cli("run-status", "run-b1")["status"], "run-quarantined")
        self.assertEqual(cli("kit-status", "kit-atlas", "60")["status"], "handover-review-required")
        self.assertEqual(cli("audit")["audit"], "verified")

    def stage_pending_workshop(self, mutate=None):
        staged = [r for r in self.workshop if r["op"] not in {"sample", "certify", "deliver"}]
        if mutate:
            staged = mutate(copy.deepcopy(staged))
        workbench.apply(self.db, staged)
        return quality.load_stations(STATIONS)

    def write_shift(self, document):
        path = self.directory / "shift.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_status_reproduction_lists_both_fixtures_before_shift_planning(self):
        stations = self.stage_pending_workshop()
        report = quality.report(self.db, "run-b1", stations)
        self.assertEqual(report["missing"], ["bind-v1", "bind-v2"])
        for member in report["members"]:
            self.assertEqual([s["fixture"] for s in member["station_candidates"]],
                             ["compact-jaws", "wide-jaws"])
            self.assertNotIn("shift_plan", report)
            self.assertNotIn("thickness_half_mm", member)
        # Actual paper: heavy lot contributes only the two folded sheets that were gathered.
        first = report["members"][0]
        self.assertEqual([(p["lot"], p["sheets"]) for p in first["paper"]],
                         [("paper-heavy", 2), ("paper-emboss", 1)])

    def test_night_shift_enumerates_two_maximal_plans_with_window_witnesses(self):
        stations = self.stage_pending_workshop()
        report = quality.report(self.db, "run-b1", stations, quality.load_shift(NIGHT_SHIFT))
        first, second = report["members"]
        # 2 heavy sheets at 1.5 mm plus 1 standard sheet at 1 mm => 4 mm; volume 2 is 1 mm.
        self.assertEqual((first["thickness_half_mm"], second["thickness_half_mm"]), (8, 2))
        self.assertEqual(first["grade_sheets"], {"emboss-150": 1, "emboss-170": 2})
        self.assertEqual([(j["fixture"], j["jaw_half_mm"]) for j in first["jaw_options"]], [("wide-jaws", 8)])
        self.assertEqual([(j["fixture"], j["jaw_half_mm"]) for j in second["jaw_options"]],
                         [("compact-jaws", 4), ("wide-jaws", 4)])
        plan = report["shift_plan"]
        self.assertEqual(plan["result"], "plans-enumerated")
        self.assertEqual([p["captures"] for p in plan["plans"]], [["bind-v1"], ["bind-v2"]])
        volume_one, volume_two = plan["plans"]
        self.assertEqual([(step["window"], step["action"]) for step in volume_one["schedule"]],
                         [("window-1", "empty-fixture-check"), ("window-2", "capture")])
        self.assertEqual(volume_one["schedule"][0]["jaw_half_mm"], 8)
        self.assertEqual(volume_one["schedule"][1]["binding"], "bind-v1")
        self.assertEqual([(step["window"], step["action"]) for step in volume_two["schedule"]],
                         [("window-1", "capture"), ("window-2", "idle")])
        self.assertEqual(volume_two["schedule"][0]["fixture"], "compact-jaws")
        # The wide 4 mm level can never capture volume 1: it is not among its physical jaw options.
        self.assertEqual(volume_one["omitted"][0]["reason"], "cannot-fit-in-windows")
        self.assertEqual(volume_two["omitted"][0]["reason"], "cannot-fit-in-windows")
        # Re-running returns the identical deterministic witness for the same capture set.
        again = quality.report(self.db, "run-b1", stations, quality.load_shift(NIGHT_SHIFT))
        self.assertEqual(again["shift_plan"], plan)

    def test_single_window_loses_volume_one_to_a_changeover_then_standard_paper_fits(self):
        stations = self.stage_pending_workshop()
        blocked = quality.report(self.db, "run-b1", stations, quality.load_shift(SINGLE_SHIFT))["shift_plan"]
        self.assertEqual(blocked["result"], "no-capturable-binding")
        self.assertEqual(blocked["plans"][0]["captures"], [])
        self.assertEqual(blocked["plans"][0]["schedule"], [{"window": "window-1", "action": "idle"}])
        reasons = {row["binding"]: row["reason"] for row in blocked["plans"][0]["omitted"]}
        self.assertEqual(reasons, {"bind-v1": "cannot-fit-in-windows", "bind-v2": "no-physical-jaw-level"})

        def standard_issue(records):
            for record in records:
                if record.get("id") == "issue-11":
                    record["lot"] = "paper-emboss"
            return records

        with connect(":memory:") as db:
            self.db = db
            stations = self.stage_pending_workshop(standard_issue)
            fixed = quality.report(db, "run-b1", stations, quality.load_shift(SINGLE_SHIFT))["shift_plan"]
        self.assertEqual(fixed["plans"][0]["captures"], ["bind-v1"])
        step = fixed["plans"][0]["schedule"][0]
        self.assertEqual((step["action"], step["binding"], step["jaw_half_mm"]),
                         ("capture", "bind-v1", 6))
        self.assertEqual(fixed["plans"][0]["omitted"][0]["reason"], "no-physical-jaw-level")

    def test_delayed_window_uses_idle_and_early_check_as_distinct_witnesses(self):
        stations = self.stage_pending_workshop()
        plan = quality.report(self.db, "run-b1", stations, quality.load_shift(DELAYED_SHIFT))["shift_plan"]
        self.assertEqual([p["captures"] for p in plan["plans"]], [["bind-v1"], ["bind-v2"]])
        volume_one, volume_two = plan["plans"]
        # Volume 1 is not enterable until window 2: the check has to be banked in window 1.
        self.assertEqual([(s["window"], s["action"]) for s in volume_one["schedule"]],
                         [("window-1", "empty-fixture-check"), ("window-2", "capture")])
        # Volume 2 stays on the ready compact combination and idles through its unavailable window.
        self.assertEqual([(s["window"], s["action"]) for s in volume_two["schedule"]],
                         [("window-1", "capture"), ("window-2", "idle")])

    def test_uncalibrated_grade_is_reported_instead_of_using_nominal_paper(self):
        stations = self.stage_pending_workshop()
        shift = quality.load_shift(NIGHT_SHIFT)
        del shift["grade_thickness_half_mm"]["emboss-170"]
        report = quality.report(self.db, "run-b1", stations, shift)
        first = report["members"][0]
        self.assertIsNone(first["thickness_half_mm"])
        self.assertEqual(first["uncalibrated_grades"], ["emboss-170"])
        self.assertEqual(first["jaw_options"], [])
        plan = report["shift_plan"]
        self.assertEqual([p["captures"] for p in plan["plans"]], [["bind-v2"]])
        omitted = {row["binding"]: row["reason"] for row in plan["plans"][0]["omitted"]}
        self.assertEqual(omitted, {"bind-v1": "uncalibrated-grade"})

    def test_no_physical_level_and_empty_shift_still_return_explicit_results(self):
        stations = self.stage_pending_workshop()
        shift = quality.load_shift(NIGHT_SHIFT)
        for station in shift["stations"]:
            station["levels_half_mm"] = [level for level in station["levels_half_mm"] if level != 8]
        report = quality.report(self.db, "run-b1", stations, shift)
        plan = report["shift_plan"]
        self.assertEqual(plan["result"], "plans-enumerated")
        self.assertEqual([p["captures"] for p in plan["plans"]], [["bind-v2"]])
        self.assertEqual(plan["plans"][0]["omitted"][0]["reason"], "no-physical-jaw-level")

        idle = quality.load_shift(self.write_shift({
            "schema": quality.SHIFT_SCHEMA,
            "grade_thickness_half_mm": {"emboss-150": 2, "emboss-170": 3},
            "windows": [{"window": "window-1", "bindings": ["bind-v1", "bind-v2"]}],
            "stations": [{"device": "wide-gauge", "fixture": "wide-jaws",
                          "margin_half_mm": 0, "levels_half_mm": [8, 16]}],
            "ready": {"device": "wide-gauge", "fixture": "wide-jaws", "jaw_half_mm": 16},
        }))
        empty = quality.report(self.db, "run-b1", stations, idle)["shift_plan"]
        self.assertEqual(empty["result"], "no-capturable-binding")
        self.assertEqual(empty["plans"][0]["captures"], [])
        self.assertEqual(empty["plans"][0]["schedule"], [{"window": "window-1", "action": "idle"}])
        reasons = {row["binding"]: row["reason"] for row in empty["plans"][0]["omitted"]}
        self.assertEqual(reasons, {"bind-v1": "cannot-fit-in-windows", "bind-v2": "no-physical-jaw-level"})

    def test_shift_input_is_validated_against_stations_windows_and_limits(self):
        self.stage_pending_workshop()
        with self.assertRaisesRegex(Rejected, "shift planning needs station fixtures"):
            quality.build_shift_plan(self.db, "run-b1", (), quality.load_shift(NIGHT_SHIFT))
        unknown_binding = quality.load_shift(NIGHT_SHIFT)
        unknown_binding["windows"][0]["bindings"].append("bind-ghost")
        with self.assertRaisesRegex(Rejected, "not pending in this run"):
            quality.report(self.db, "run-b1", quality.load_stations(STATIONS), unknown_binding)
        over_levels = quality.load_shift(NIGHT_SHIFT)
        over_levels["stations"][1]["levels_half_mm"] = [2, 4, 6, 8, 10]
        with self.assertRaisesRegex(Rejected, "one to four jaw levels"):
            quality.load_shift(self.write_shift(over_levels))
        bad_ready = quality.load_shift(NIGHT_SHIFT)
        bad_ready["ready"]["jaw_half_mm"] = 6
        with self.assertRaisesRegex(Rejected, "unlisted jaw level"):
            quality.report(self.db, "run-b1", quality.load_stations(STATIONS), bad_ready)
        over_rated = quality.load_shift(NIGHT_SHIFT)
        over_rated["stations"][0]["levels_half_mm"] = [24]
        with self.assertRaisesRegex(Rejected, "rated opening"):
            quality.report(self.db, "run-b1", quality.load_stations(STATIONS), over_rated)

    def test_cli_shift_planning_round_trip(self):
        db = self.directory / "shift.sqlite"

        def cli(command, *args):
            proc = subprocess.run([sys.executable, "-B", "-m", "bindery_gather", command, "--db", str(db), *map(str, args)],
                                  cwd=FIXTURES.parent, capture_output=True, text=True, check=True)
            return json.loads(proc.stdout)

        staged = [r for r in self.workshop if r["op"] not in {"sample", "certify", "deliver"}]
        path = self.directory / "staged.json"
        path.write_text(json.dumps(staged), encoding="utf-8")
        cli("apply", path)
        plan = cli("qc-status", "run-b1", "--stations", STATIONS, "--shift", NIGHT_SHIFT)["shift_plan"]
        self.assertEqual([p["captures"] for p in plan["plans"]], [["bind-v1"], ["bind-v2"]])
        proc = subprocess.run([sys.executable, "-B", "-m", "bindery_gather", "qc-status", "--db", str(db),
                               "run-b1", "--shift", NIGHT_SHIFT],
                              cwd=FIXTURES.parent, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("shift planning requires --stations", proc.stderr)
