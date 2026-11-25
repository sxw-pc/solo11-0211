"""Import offline spine gauge exports and recover unsampled bindings."""
import csv
import json
import re
from pathlib import Path
from urllib.parse import quote

from . import binding, imposition
from .db import Rejected, choice, entity, identity, integer, one, require


READING_FIELDS = ("reading", "binding", "minute", "fixture", "jaw_mm", "pull", "skew")
MEASURES = ("minute", "jaw_mm", "pull", "skew")
EXPORT_FIELDS = ("device", "batch", *READING_FIELDS)


def reading_id(device, batch, reading):
    tokens = (quote(identity(token), safe="") for token in (device, batch, reading))
    return "qc-reading/" + "/".join(tokens)


def measure(value, field):
    integer(value, field)
    require(value <= 2**63 - 1, f"{field} exceeds SQLite integer range")
    return value


def record_reading(db, event, device, batch, reading, binding_id, minute, fixture, jaw_mm, pull, skew):
    require(event == reading_id(device, batch, reading), "reading event ID does not match its source")
    entity(db, "bindings", binding_id)
    identity(fixture)
    for value, field in zip((minute, jaw_mm, pull, skew), MEASURES):
        measure(value, field)
    if one(db, "SELECT 1 FROM samples WHERE binding=?", binding_id) is None:
        result = binding.sample(db, event, binding_id, pull, skew)
        sample_id = event
    else:
        result = "reading-retained"
        sample_id = None
    db.execute("""INSERT INTO qc_readings
                  (id,device,batch,reading,binding,minute,fixture,jaw_mm,pull,skew,sample)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (event, device, batch, reading, binding_id, minute, fixture, jaw_mm, pull, skew, sample_id))
    return result


def object_pairs(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "duplicate JSON field")
    return result


def load_json(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=object_pairs)
    require(isinstance(data, dict) and data.keys() == {"schema", "device", "batch", "readings"},
            "invalid JSON export envelope")
    require(data["schema"] == "spine-readings-v1", "unsupported JSON export schema")
    device, batch = identity(data["device"]), identity(data["batch"])
    require(isinstance(data["readings"], list), "readings must be an array")
    for row in data["readings"]:
        require(isinstance(row, dict) and row.keys() == set(READING_FIELDS), "invalid JSON reading fields")
        yield {"device": device, "batch": batch, **row}


def load_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream, strict=True)
        require(rows.fieldnames is not None and len(rows.fieldnames) == len(EXPORT_FIELDS)
                and set(rows.fieldnames) == set(EXPORT_FIELDS), "invalid CSV export header")
        for row in rows:
            require(None not in row and None not in row.values(), f"wrong column count at CSV line {rows.line_num}")
            for field in MEASURES:
                require(re.fullmatch(r"[0-9]+", row[field]), f"invalid {field} at CSV line {rows.line_num}")
                row[field] = int(row[field])
            yield row


READERS = {".json": load_json, ".csv": load_csv}

SHIFT_SCHEMA = "qc-shift-v1"
HALF_MM_PER_MM = 2
MAX_PENDING = 6
MAX_WINDOWS = 6
MAX_SHIFT_DEVICES = 2
MAX_FIXTURES_PER_DEVICE = 2
MAX_LEVELS_PER_FIXTURE = 4


def load_export(path):
    path = Path(path)
    records = []
    row_ids = set()
    try:
        suffix = choice(path.suffix.lower(), READERS, "export extension")
        for index, row in enumerate(READERS[suffix](path), 1):
            try:
                key = reading_id(row["device"], row["batch"], row["reading"])
                require(key not in row_ids, "duplicate source reading")
                row_ids.add(key)
                binding_id = identity(row.pop("binding"))
                identity(row["fixture"])
                for field in MEASURES:
                    measure(row[field], field)
                records.append({"id": key, "op": "qc-reading", "binding_id": binding_id, **row})
            except ValueError as exc:
                raise Rejected(f"reading {index}: {exc}") from exc
        require(records, "empty export")
    except (ValueError, OSError, csv.Error) as exc:
        raise Rejected(f"{path}: {exc}") from exc
    return records


def missing_bindings(db, run_id):
    entity(db, "runs", run_id)
    return {row["id"] for row in db.execute("""
        SELECT b.id FROM bindings b LEFT JOIN samples s ON s.binding=b.id
        WHERE b.run=? AND s.id IS NULL
    """, (run_id,))}


def recovery_records(db, run_id, records):
    # workbench.apply starts the write transaction before advancing this iterator.
    missing = missing_bindings(db, run_id)
    candidates = (record for record in records if record["binding_id"] in missing)
    yield from sorted(candidates, key=lambda record: (record["minute"], record["id"]))


def load_stations(path):
    stations = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=object_pairs)
    require(isinstance(stations, list), "stations must be an array")
    seen = set()
    for station in stations:
        require(isinstance(station, dict) and station.keys() == {"device", "fixture", "formats", "max_jaw_mm"},
                "invalid station fields")
        key = identity(station["device"]), identity(station["fixture"])
        require(key not in seen, "duplicate station fixture")
        seen.add(key)
        integer(station["max_jaw_mm"], "maximum jaw opening", 1)
        formats = station["formats"]
        require(isinstance(formats, list) and formats, "station needs at least one reader format")
        for fmt in formats:
            choice(fmt, imposition.FORMATS, "station reader format")
    return sorted(stations, key=lambda station: (station["device"], station["fixture"]))


def load_shift(path):
    """Load a shift plan: grade calibration, enterable windows, jaw levels and the opening setup."""
    shift = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=object_pairs)
    require(isinstance(shift, dict) and shift.keys() == {"schema", "grade_thickness_half_mm", "windows", "stations", "ready"},
            "invalid shift fields")
    require(shift["schema"] == SHIFT_SCHEMA, "unsupported shift schema")
    calibration = shift["grade_thickness_half_mm"]
    require(isinstance(calibration, dict) and calibration, "shift needs at least one grade calibration")
    for grade, thickness in calibration.items():
        identity(grade)
        integer(thickness, "calibrated sheet thickness", 1)
    windows = shift["windows"]
    require(isinstance(windows, list) and 1 <= len(windows) <= MAX_WINDOWS, "shift needs one to six windows")
    seen_windows = set()
    for window in windows:
        require(isinstance(window, dict) and window.keys() == {"window", "bindings"}, "invalid shift window fields")
        key = identity(window["window"])
        require(key not in seen_windows, "duplicate shift window")
        seen_windows.add(key)
        bindings = window["bindings"]
        require(isinstance(bindings, list) and bindings, "shift window needs at least one enterable binding")
        for binding_id in bindings:
            identity(binding_id)
        require(len(set(bindings)) == len(bindings), f"duplicate binding in window {key}")
    stations = shift["stations"]
    require(isinstance(stations, list) and 1 <= len(stations) <= MAX_SHIFT_DEVICES, "shift needs one or two devices")
    seen_devices, seen_fixtures = set(), set()
    for station in stations:
        require(isinstance(station, dict)
                and station.keys() == {"device", "fixture", "margin_half_mm", "levels_half_mm"},
                "invalid shift station fields")
        device, fixture = identity(station["device"]), identity(station["fixture"])
        key = device, fixture
        require(key not in seen_fixtures, "duplicate shift fixture")
        seen_fixtures.add(key)
        seen_devices.add(device)
        require(len(seen_devices) <= MAX_SHIFT_DEVICES, "shift exceeds two devices")
        margin = station["margin_half_mm"]
        integer(margin, "fixture crush margin")
        levels = station["levels_half_mm"]
        require(isinstance(levels, list) and 1 <= len(levels) <= MAX_LEVELS_PER_FIXTURE,
                "fixture needs one to four jaw levels")
        for level in levels:
            integer(level, "fixture jaw level", 1)
        require(len(set(levels)) == len(levels), f"duplicate jaw level on {device}/{fixture}")
    device_fixtures = {}
    for device, fixture in seen_fixtures:
        device_fixtures.setdefault(device, set()).add(fixture)
    require(all(len(fixtures) <= MAX_FIXTURES_PER_DEVICE for fixtures in device_fixtures.values()),
            "a shift device can carry at most two fixtures")
    ready = shift["ready"]
    require(isinstance(ready, dict) and ready.keys() == {"device", "fixture", "jaw_half_mm"},
            "invalid shift ready combination")
    identity(ready["device"])
    identity(ready["fixture"])
    integer(ready["jaw_half_mm"], "ready jaw opening", 1)
    return {
        "schema": SHIFT_SCHEMA,
        "grade_thickness_half_mm": dict(sorted(calibration.items())),
        "windows": [{"window": w["window"], "bindings": list(w["bindings"])} for w in windows],
        "stations": sorted((dict(station) for station in stations),
                           key=lambda station: (station["device"], station["fixture"])),
        "ready": dict(ready),
    }


def _book_materials(db, run_id):
    """Physical paper actually gathered into each book: real lot, grade and counted sheets."""
    materials = {}
    for row in db.execute("""
        SELECT bp.book,bp.slot,bp.piece,l.id AS lot,l.grade,l.grain,COUNT(s.serial) AS sheets
        FROM bindings b JOIN book_pieces bp ON bp.book=b.book JOIN pieces p ON p.id=bp.piece
        JOIN issues i ON p.issue=i.id JOIN paper_lots l ON i.lot=l.id
        JOIN sheets s ON s.piece=p.id WHERE b.run=? GROUP BY bp.book,bp.slot ORDER BY bp.book,bp.slot
    """, (run_id,)):
        entry = dict(row)
        materials.setdefault(entry.pop("book"), []).append(entry)
    return materials


def _grade_sheets(materials):
    grades = {}
    for entry in materials:
        grades[entry["grade"]] = grades.get(entry["grade"], 0) + entry["sheets"]
    return dict(sorted(grades.items()))


def build_shift_plan(db, run_id, stations, shift):
    """Enumerate the binding sets this shift can physically capture, window by window."""
    run = binding.run_report(db, run_id)
    fmt = entity(db, "editions", run["edition"])["format"]
    pending = [member for member in run["members"] if member["sample"] is None]
    require(len(pending) <= MAX_PENDING, "shift planning supports at most six pending bindings")
    require(stations, "shift planning needs station fixtures")

    station_by_key = {(s["device"], s["fixture"]): s for s in stations}
    fixture_levels, fixture_margin = {}, {}
    for station in shift["stations"]:
        key = station["device"], station["fixture"]
        base = station_by_key.get(key)
        require(base is not None, f"shift station has no fixture record: {key[0]}/{key[1]}")
        require(fmt in base["formats"], f"shift fixture does not support the run reader format: {key[0]}/{key[1]}")
        levels = sorted(station["levels_half_mm"])
        require(all(level <= base["max_jaw_mm"] * HALF_MM_PER_MM for level in levels),
                f"shift jaw level exceeds rated opening: {key[0]}/{key[1]}")
        fixture_levels[key] = levels
        fixture_margin[key] = station["margin_half_mm"]

    ready_key = shift["ready"]["device"], shift["ready"]["fixture"]
    require(ready_key in fixture_levels, "shift ready combination names an unknown fixture")
    require(shift["ready"]["jaw_half_mm"] in fixture_levels[ready_key],
            "shift ready combination uses an unlisted jaw level")
    ready = (ready_key[0], ready_key[1], shift["ready"]["jaw_half_mm"])
    # Every alternative (device, fixture, jaw level) needs its own empty-fixture check before first use.
    combos = sorted((device, fixture, level)
                    for (device, fixture), levels in fixture_levels.items() for level in levels)

    window_order = [window["window"] for window in shift["windows"]]
    enterable = {window["window"]: set(window["bindings"]) for window in shift["windows"]}
    pending_ids = [member["binding"] for member in pending]
    pending_set = set(pending_ids)
    for window, bindings in enterable.items():
        require(bindings <= pending_set, f"window {window} lists a binding that is not pending in this run")

    materials = _book_materials(db, run_id)
    calibration = shift["grade_thickness_half_mm"]
    profiles = {}
    for member in pending:
        binding_id, book = member["binding"], member["book"]
        paper = materials.get(book, [])
        grade_sheets = _grade_sheets(paper)
        uncalibrated = sorted(grade for grade in grade_sheets if grade not in calibration)
        if uncalibrated:
            thickness = None
        else:
            thickness = sum(calibration[grade] * sheets for grade, sheets in grade_sheets.items())
        jaws = []
        if thickness is not None:
            for (device, fixture), levels in fixture_levels.items():
                margin = fixture_margin[(device, fixture)]
                for level in levels:
                    if thickness <= level <= thickness + margin:
                        jaws.append({"device": device, "fixture": fixture, "jaw_half_mm": level})
        windows_for_binding = sorted((window for window in window_order if binding_id in enterable[window]),
                                     key=window_order.index)
        profiles[binding_id] = {
            "grade_sheets": grade_sheets,
            "thickness_half_mm": thickness,
            "uncalibrated_grades": uncalibrated,
            "jaw_options": jaws,
            "windows": windows_for_binding,
        }

    feasible = {binding_id: {(jaw["device"], jaw["fixture"], jaw["jaw_half_mm"])
                             for jaw in profiles[binding_id]["jaw_options"]}
                for binding_id in pending_ids}
    combo_index = {combo: index for index, combo in enumerate(combos)}

    # Exhaustive read-only enumeration, bounded by six bindings, six windows, two devices and
    # eight combinations. Each window captures one enterable binding under the current
    # combination, spends itself on one empty-fixture check, or idles; the checked combination
    # carries across windows until the next check, and a window used for a check cannot capture.
    all_sets = set()

    def enumerate_sets(position, current, captured):
        all_sets.add(captured)
        if position == len(window_order):
            return
        window = window_order[position]
        # A window captures at most one binding, and only under the combination already checked.
        for binding_id in pending_ids:
            if binding_id in captured or binding_id not in enterable[window]:
                continue
            if current in feasible[binding_id]:
                enumerate_sets(position + 1, current, captured | frozenset({binding_id}))
        # Idle: the checked combination carries into the next window.
        enumerate_sets(position + 1, current, captured)
        # Or spend the whole window on one empty-fixture check; no reading can be taken here.
        for combo in combos:
            if combo != current:
                enumerate_sets(position + 1, combo, captured)

    enumerate_sets(0, ready, frozenset())
    maximal_sets = sorted((plan for plan in all_sets if not any(plan < other for other in all_sets)),
                          key=lambda plan: (-len(plan), sorted(plan)))

    def omission_reasons(plan):
        reasons = []
        for binding_id in pending_ids:
            if binding_id in plan:
                continue
            profile = profiles[binding_id]
            if profile["uncalibrated_grades"]:
                reason = "uncalibrated-grade"
                detail = ("paper grades without shift thickness calibration: "
                          + ",".join(profile["uncalibrated_grades"]))
            elif not profile["jaw_options"]:
                reason = "no-physical-jaw-level"
                detail = (f"clamping thickness {profile['thickness_half_mm']} half-mm fits no listed "
                          "fixture level within its crush margin and rated opening")
            else:
                reason = "cannot-fit-in-windows"
                detail = ("a jaw level exists but every enterable window is needed for checks or other "
                          "captures before the combination is ready")
            reasons.append({"binding": binding_id, "reason": reason, "detail": detail})
        return reasons

    def witness_for(plan):
        if not plan:
            return [{"window": window, "action": "idle"} for window in window_order]
        path = []

        def walk(position, current, captured):
            if captured == plan:
                path.extend({"window": window_order[i], "action": "idle"} for i in range(position, len(window_order)))
                return True
            if position == len(window_order):
                return False
            window = window_order[position]
            for binding_id in pending_ids:
                if binding_id in captured or binding_id not in plan or binding_id not in enterable[window]:
                    continue
                for combo in sorted(feasible[binding_id], key=lambda c: combo_index[c]):
                    if combo == current and walk(
                            position + 1, current, captured | frozenset({binding_id})):
                        path.append({"window": window, "action": "capture", "binding": binding_id,
                                     "device": combo[0], "fixture": combo[1], "jaw_half_mm": combo[2]})
                        return True
            if walk(position + 1, current, captured):
                path.append({"window": window, "action": "idle"})
                return True
            for combo in combos:
                if combo != current and walk(position + 1, combo, captured):
                    path.append({"window": window, "action": "empty-fixture-check",
                                 "device": combo[0], "fixture": combo[1], "jaw_half_mm": combo[2]})
                    return True
            return False

        walk(0, ready, frozenset())
        path.reverse()
        return path

    plans = [{
        "captures": sorted(plan),
        "schedule": witness_for(plan),
        "omitted": omission_reasons(plan),
    } for plan in maximal_sets]
    return {
        "scope": pending_ids,
        "window_order": window_order,
        "ready_combination": {"device": ready[0], "fixture": ready[1], "jaw_half_mm": ready[2]},
        "binding_profiles": profiles,
        "plans": plans,
        "result": ("no-capturable-binding" if not maximal_sets[0] else "plans-enumerated"),
    }


def report(db, run_id, stations=(), shift=None):
    run = binding.run_report(db, run_id)
    fmt = entity(db, "editions", run["edition"])["format"]
    readings = {}
    for row in db.execute("""
        SELECT q.* FROM qc_readings q JOIN bindings b ON b.id=q.binding
        WHERE b.run=? ORDER BY q.binding,q.minute,q.id
    """, (run_id,)):
        readings.setdefault(row["binding"], []).append(dict(row))
    materials = _book_materials(db, run_id)
    available = [station for station in stations if fmt in station["formats"]]
    for member in run["members"]:
        member["readings"] = readings.get(member["binding"], [])
        member["paper"] = materials.get(member["book"], [])
        member["station_candidates"] = available if member["sample"] is None else []
    missing = [member["binding"] for member in run["members"] if member["sample"] is None]
    result = {**run, "format": fmt, "missing": missing}
    if shift is not None:
        plan = build_shift_plan(db, run_id, stations, shift)
        profiles = plan.pop("binding_profiles")
        for member in run["members"]:
            profile = profiles.get(member["binding"])
            if profile is not None:
                member["grade_sheets"] = profile["grade_sheets"]
                member["thickness_half_mm"] = profile["thickness_half_mm"]
                member["uncalibrated_grades"] = profile["uncalibrated_grades"]
                member["jaw_options"] = profile["jaw_options"]
                member["enterable_windows"] = profile["windows"]
        result["shift_plan"] = plan
    return result
