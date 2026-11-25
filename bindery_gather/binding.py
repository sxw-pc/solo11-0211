"""Scan exact signature copies into books and qualify a spine-binding run."""
import json

from . import imposition
from .db import encode, entity, identity, integer, one, require


def gather(db, event, volume, pieces):
    plan = entity(db, "volumes", volume)
    imposition.sealed(db, plan["edition"])
    expected = imposition.volume_signatures(db, volume)
    require(isinstance(pieces, list) and len(pieces) == len(expected), "wrong signature count")
    require(len(set(identity(p) for p in pieces)) == len(expected), "duplicate piece in gather")
    for piece_id, signature in zip(pieces, expected):
        piece = entity(db, "pieces", piece_id)
        issue = entity(db, "issues", piece["issue"])
        require(piece["status"] == "folded", f"quarantined piece: {piece_id}")
        require(issue["signature"] == signature["id"], f"wrong edition, volume or slot for piece: {piece_id}")
        require(one(db, "SELECT book FROM book_pieces WHERE piece=?", piece_id) is None, f"piece already gathered: {piece_id}")
    db.execute("INSERT INTO books VALUES (?,?)", (event, volume))
    db.executemany("INSERT INTO book_pieces VALUES (?,?,?)", [(event, i, p) for i, p in enumerate(pieces, 1)])
    return "book-gathered"


def run(db, event, edition):
    imposition.sealed(db, edition)
    db.execute("INSERT INTO runs VALUES (?,?)", (event, edition))
    return "binding-run-opened"


def bind(db, event, run, book):
    batch = entity(db, "runs", run)
    require(one(db, "SELECT id FROM certificates WHERE run=?", run) is None, "certified run membership is frozen")
    gathered = entity(db, "books", book)
    volume = entity(db, "volumes", gathered["volume"])
    require(volume["edition"] == batch["edition"], "book edition differs from binding run")
    db.execute("INSERT INTO bindings VALUES (?,?,?)", (event, book, run))
    return "book-bound"


def sample(db, event, binding, pull, skew):
    item = entity(db, "bindings", binding)
    batch = entity(db, "runs", item["run"])
    spec = entity(db, "editions", batch["edition"])
    integer(pull, "spine pull")
    integer(skew, "spine skew")
    passed = pull >= spec["min_pull"] and skew <= spec["max_skew"]
    db.execute("INSERT INTO samples VALUES (?,?,?,?,?)", (event, binding, pull, skew, passed))
    return "spine-pass" if passed else "spine-fail"


def run_report(db, run_id):
    batch = entity(db, "runs", run_id)
    spec = entity(db, "editions", batch["edition"])
    members = [dict(row) for row in db.execute("""SELECT b.id AS binding,b.book,s.id AS sample,s.pull,s.skew,s.passed
                  FROM bindings b LEFT JOIN samples s ON b.id=s.binding WHERE b.run=? ORDER BY b.id""", (run_id,))]
    sampled = [row for row in members if row["sample"] is not None]
    failed = [row["sample"] for row in sampled if not row["passed"]]
    certificate = one(db, "SELECT id FROM certificates WHERE run=?", run_id)
    if failed:
        status = "run-quarantined"
    elif len(sampled) < spec["min_samples"]:
        status = "sampling-incomplete"
    else:
        status = "run-released" if certificate else "awaiting-certificate"
    return {"run": run_id, "edition": spec["id"], "status": status, "required_samples": spec["min_samples"],
            "min_pull": spec["min_pull"], "max_skew": spec["max_skew"], "members": members,
            "failed": failed, "certificate": certificate[0] if certificate else None}


def certify(db, event, run):
    report = run_report(db, run)
    require(report["status"] == "awaiting-certificate", f"run cannot be certified: {report['status']}")
    db.execute("INSERT INTO certificates VALUES (?,?,?)", (event, run, encode(report)))
    return "run-released"


# ---------------------------------------------------------------------------
# Defect evidence capture and proofreading work orders
#
# The device export only knows a physical sheet serial, the sheet side and a
# camera half.  Page identity comes from three existing ledgers:
#   * imposition.sheet_faces for the canonical page pairs,
#   * the sheets/pieces/issues physical fold records from materials.fold,
#   * book_pieces for which gathered book block actually owns the piece.
# Camera left/right is calibrated once per setup segment ("装夹段"); the same
# correspondence applies to every reading of that segment.
# ---------------------------------------------------------------------------

MAX_SEGMENTS_PER_INTAKE = 2
MAX_READINGS_PER_SEGMENT = 6

SIDE_VALUES = {
    "front": "front", "recto": "front", "正面": "front", "正": "front",
    "front-side": "front",
    "back": "back", "verso": "back", "背面": "back", "背": "back", "back-side": "back",
}
HALF_VALUES = {
    "left": "left", "l": "left", "左": "left", "左半区": "left", "left-half": "left",
    "right": "right", "r": "right", "右": "right", "右半区": "right", "right-half": "right",
}
BOOL_TRUE = {"true", "1", "yes", "mirrored", "mirror", "on", "是", "镜装"}
BOOL_FALSE = {"false", "0", "no", "off", "not-mirrored", "standard", "否", "非镜装", "没有镜装"}

KIND_PAGE = "page-repair"
KIND_SPREAD = "spread-retest"
PAGE_KIND_WORDS = {"page", "single", "single-page", "repair", "patch", "page-repair",
                   "repair-order", "page-only", "mono"}
SPREAD_KIND_WORDS = {"spread", "pair", "two-page", "spread-retest", "pair-retest", "retest",
                     "retest-order", "spread-recheck", "pair-recheck", "double"}

TASK_PENDING = "pending"
TASK_COMPLETED = "completed"
TASK_WITHDRAWN = "withdrawn"
VALID_TASK_STATUSES = (TASK_PENDING, TASK_COMPLETED)  # withdrawn tickets are no longer valid


def _pick(fields, *names):
    for name in names:
        value = fields.get(name)
        if value is not None:
            return value
    return None


def _unwrap(fields):
    """Accept evidence nested under a single payload/reading/data object."""
    for name in ("payload", "data", "record", "reading"):
        nested = fields.get(name)
        if isinstance(nested, dict):
            merged = dict(nested)
            for key, value in fields.items():
                if key != name:
                    merged.setdefault(key, value)
            return merged
    return fields


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int,)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in BOOL_TRUE:
            return True
        if token in BOOL_FALSE:
            return False
    return None


def _as_int(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _choice_value(value, aliases, label):
    require(isinstance(value, str), f"invalid {label}")
    key = value.strip().lower()
    require(key in aliases, f"invalid {label}: {value!r}")
    return aliases[key]


def _norm_kind(value):
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in PAGE_KIND_WORDS:
        return KIND_PAGE
    if token in SPREAD_KIND_WORDS:
        return KIND_SPREAD
    if token in ("page_repair", "spread_retest"):
        return token.replace("_", "-")
    return None


def _target_book(db, fields, required=True):
    intake_ref = _pick(fields, "intake", "intake_id", "capture", "batch", "submission", "collection")
    book_ref = _pick(fields, "book", "book_id", "copy", "book_block", "block", "book_copy")
    if intake_ref is not None:
        intake = entity(db, "proof_intakes", intake_ref)
        if book_ref is not None:
            require(book_ref == intake["book"], "proof evidence book differs from its intake")
        return intake["book"], intake["id"]
    if required:
        require(book_ref is not None, "proof evidence requires a book or intake")
    if book_ref is None:
        return None, None
    return identity(book_ref), None


def intake(db, event, book=None, **extra):
    """进件: open one defect-evidence intake for a gathered braille book."""
    fields = _unwrap({"book": book, **extra}) if book is not None else _unwrap(extra)
    book_id = _pick(fields, "book", "book_id", "copy", "book_block", "block")
    require(book_id is not None, "proof intake requires a book")
    book_row = entity(db, "books", book_id)
    volume = entity(db, "volumes", book_row["volume"])
    edition = entity(db, "editions", volume["edition"])
    require(edition["format"] == imposition.FORMATS[0],
            "proof intake only accepts one gathered braille volume")
    db.execute("INSERT INTO proof_intakes VALUES (?,?)", (event, book_id))
    return "proof-intake-opened"


# Common alternative entry names for the intake.
open_intake = intake
defect_intake = intake
capture_intake = intake


def _get_segment(db, segment_ref, book_id, intake_id, create):
    require(segment_ref is not None, "proof evidence requires a setup segment")
    segment_ref = identity(str(segment_ref)) if not isinstance(segment_ref, str) else identity(segment_ref)
    seg = one(db, "SELECT * FROM proof_segments WHERE id=?", segment_ref)
    if seg is not None:
        if seg["book"] is not None and book_id is not None:
            require(seg["book"] == book_id, "setup segment belongs to another book")
        return seg
    require(create, f"unknown proof setup segment: {segment_ref}")
    if intake_id is not None:
        intake = entity(db, "proof_intakes", intake_id)
        if book_id is None:
            book_id = intake["book"]
        count = one(db, "SELECT COUNT(*) FROM proof_segments WHERE intake=?", intake_id)[0]
        require(count < MAX_SEGMENTS_PER_INTAKE, "a proof intake allows at most two setup segments")
        ordinal = count + 1
    else:
        require(book_id is not None, "setup segment requires a book or intake")
        count = one(db, "SELECT COUNT(*) FROM proof_segments WHERE book=?", book_id)[0]
        ordinal = count + 1
    db.execute("""INSERT INTO proof_segments
                  (id,intake,book,ordinal,mirrored,ruler_event,version,status,conflict)
                  VALUES (?,?,?,?,NULL,NULL,0,'open',NULL)""",
               (segment_ref, intake_id, book_id, ordinal))
    return one(db, "SELECT * FROM proof_segments WHERE id=?", segment_ref)


def sheet_attribution(db, book_id, serial, side):
    """Resolve a physical serial to this book and its canonical page pair.

    Returns eligibility plus the imposition pair and traceability rows.  A
    missing serial is malformed input; a known scrap/not-gathered sheet is
    simply ineligible to open proof work for ``book_id``.
    """
    serial = identity(serial)
    sheet = one(db, "SELECT * FROM sheets WHERE serial=?", serial)
    require(sheet is not None, f"unknown physical sheet serial: {serial}")
    piece = entity(db, "pieces", sheet["piece"])
    issue = entity(db, "issues", piece["issue"])
    sig = entity(db, "signatures", issue["signature"])
    front, back = imposition.sheet_faces(sig, sheet["position"])
    face = list(front if side == "front" else back)
    stored = json.loads(sheet[side])
    gathered = one(db, "SELECT slot FROM book_pieces WHERE book=? AND piece=?", book_id, piece["id"])
    reasons = []
    if piece["status"] != "folded":
        reasons.append(f"quarantined-piece:{piece['id']}")
    if gathered is None:
        reasons.append("sheet-not-gathered-in-book")
    return {
        "serial": serial, "sheet": dict(sheet), "piece": piece, "signature": sig,
        "face": face, "stored_face": stored,
        "gathered_slot": gathered["slot"] if gathered is not None else None,
        "eligible": not reasons, "reasons": reasons,
    }


def _effective_mirrored(seg):
    """A conflicted segment's old ruler is invalid and must not be used."""
    if seg is None or seg["status"] != "open" or seg["mirrored"] is None:
        return None
    return bool(seg["mirrored"])


def _relocate_segment_readings(db, seg):
    """Recompute live page location for every reading of a segment."""
    mirrored = _effective_mirrored(seg)
    rows = db.execute("SELECT * FROM proof_readings WHERE segment=?", (seg["id"],)).fetchall()
    for row in rows:
        face = json.loads(row["face"]) if row["face"] else None
        if mirrored is None or face is None:
            db.execute("UPDATE proof_readings SET mirrored=NULL,located=0,page=NULL WHERE id=?", (row["id"],))
        else:
            page = imposition.half_page(face, row["half"], mirrored)
            db.execute("UPDATE proof_readings SET mirrored=?,located=1,page=? WHERE id=?",
                       (1 if mirrored else 0, page, row["id"]))


def record_reading(db, event, book=None, segment=None, serial=None, side=None,
                   half=None, kind=None, **extra):
    """进件读数: record one device defect reading (event id is the 事件号)."""
    fields = {"book": book, "segment": segment, "serial": serial, "side": side,
              "half": half, "kind": kind}
    fields = {key: value for key, value in fields.items() if value is not None}
    fields.update(extra)
    fields = _unwrap(fields)
    book_id, intake_id = _target_book(db, fields)
    segment_ref = _pick(fields, "segment", "segment_id", "setup", "setup_id", "clamp", "mount")
    serial = _pick(fields, "serial", "sheet_serial", "sheet", "serial_number", "sheet_id")
    side_raw = _pick(fields, "side", "side_name", "face_side", "front_back")
    half_raw = _pick(fields, "half", "camera_half", "zone", "lens_half", "region", "half_zone")
    kind_value = _pick(fields, "kind", "category", "defect_kind", "defect_type", "type",
                       "code", "class", "defect")
    if isinstance(kind_value, dict):
        kind_value = _pick(kind_value, "kind", "category", "code", "name")
    source_file = _pick(fields, "source_file", "file", "filename", "source", "fixture", "origin_file")
    require(serial is not None, "defect reading requires a physical sheet serial")
    require(kind_value is not None, "defect reading requires a defect category")
    side = _choice_value(side_raw, SIDE_VALUES, "front/back side")
    half = _choice_value(half_raw, HALF_VALUES, "camera left/right half")
    kind = identity(str(kind_value))

    seg = _get_segment(db, segment_ref, book_id, intake_id, create=True)
    if seg["book"] is None and book_id is not None:
        db.execute("UPDATE proof_segments SET book=?, intake=COALESCE(intake,?) WHERE id=?",
                   (book_id, intake_id, seg["id"]))
        seg = one(db, "SELECT * FROM proof_segments WHERE id=?", seg["id"])
    book_id = seg["book"] or book_id
    require(book_id is not None, "defect reading requires a target book")

    count = one(db, "SELECT COUNT(*) FROM proof_readings WHERE segment=?", seg["id"])[0]
    require(count < MAX_READINGS_PER_SEGMENT, "a setup segment allows at most six readings")

    attr = sheet_attribution(db, book_id, serial, side)
    face = attr["face"]
    spread = imposition.spread_pages(face)
    mirrored = _effective_mirrored(seg)
    page = imposition.half_page(face, half, mirrored) if mirrored is not None else None
    located = page is not None

    defect_key = encode([attr["serial"], side, half, kind])
    prior = one(db, "SELECT id FROM proof_readings WHERE defect_key=? ORDER BY rowid LIMIT 1", defect_key)
    duplicate_of = prior["id"] if prior is not None and prior["id"] != event else (
        prior["duplicate_of"] if prior is not None else None)
    if prior is not None and prior["id"] == event:
        duplicate_of = None

    db.execute("""INSERT INTO proof_readings
        (id,intake,segment,book,serial,side,half,kind,defect_key,piece,position,face,
         eligible,reasons,duplicate_of,source_file,mirrored,located,page,spread)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (event, intake_id, seg["id"], book_id, attr["serial"], side, half, kind, defect_key,
                attr["piece"]["id"], attr["sheet"]["position"], encode(face),
                1 if attr["eligible"] else 0, encode(attr["reasons"]),
                duplicate_of, source_file,
                None if mirrored is None else (1 if mirrored else 0),
                1 if located else 0, page, encode(spread) if spread is not None else None))
    if duplicate_of is not None:
        return "defect-reading-duplicate"
    return "defect-reading-recorded"


# Alternative entry names for reading intake.
read_defect = record_reading
defect_reading = record_reading
add_reading = record_reading
capture_reading = record_reading


def _ruler_observation(row):
    if row is None:
        return None
    data = dict(row)
    return {key: data.get(key) for key in
            ("id", "segment", "mirrored", "serial", "side", "left_page", "right_page", "detail")}


def _freeze_segment(db, seg, new_ruler_id, new_obs):
    """Mark a segment conflicted and flag its existing proof orders."""
    original = one(db, "SELECT * FROM proof_rulers WHERE id=?", seg["ruler_event"])
    previous = json.loads(seg["conflict"]) if seg["conflict"] else None
    observations = []
    conflict_ids = []
    if previous:
        observations = previous.get("observations", [])
        conflict_ids = list(previous.get("conflicts", []))
    if not observations:
        observations = [_ruler_observation(original)] if original is not None else []
    observations.append(new_obs)
    conflict_ids.append(new_ruler_id)
    detail = {
        "segment": seg["id"],
        "ruler_event": seg["ruler_event"],
        "ruler_mirrored": seg["mirrored"],
        "conflicting_event": new_ruler_id,
        "conflicting_mirrored": new_obs.get("mirrored") if isinstance(new_obs, dict) else None,
        "conflicts": conflict_ids,
        "observations": observations,
        "message": ("segment ruler conflict: "
                    f"{seg['ruler_event']} vs {new_ruler_id} in setup {seg['id']}; "
                    "new proof tasks for this segment are frozen"),
    }
    db.execute("UPDATE proof_segments SET status='conflicted', conflict=? WHERE id=?",
               (encode(detail), seg["id"]))
    db.execute("UPDATE proof_orders SET review='review-required' WHERE segment=? AND review='ok'",
               (seg["id"],))
    refreshed = one(db, "SELECT * FROM proof_segments WHERE id=?", seg["id"])
    _relocate_segment_readings(db, refreshed)
    return detail


def record_ruler(db, event, segment=None, mirrored=None, left_page=None, right_page=None,
                 serial=None, side=None, **extra):
    """补采标尺: establish/corroborate/contradict a segment's left/right mapping."""
    fields = {"segment": segment, "mirrored": mirrored, "left_page": left_page,
              "right_page": right_page, "serial": serial, "side": side}
    fields = {key: value for key, value in fields.items() if value is not None}
    fields.update(extra)
    fields = _unwrap(fields)
    book_id, intake_id = _target_book(db, fields, required=False)
    segment_ref = _pick(fields, "segment", "segment_id", "setup", "clamp", "mount")
    seg = _get_segment(db, segment_ref, book_id, intake_id, create=True)

    mirror_value = _as_bool(_pick(fields, "mirrored", "mirror", "is_mirrored", "mirror_mount",
                                  "mirror_mounted", "inverted"))
    left = _as_int(_pick(fields, "left_page", "left", "page_left", "camera_left"))
    right = _as_int(_pick(fields, "right_page", "right", "page_right", "camera_right"))
    serial_ref = _pick(fields, "serial", "sheet_serial", "sheet")
    side_raw = _pick(fields, "side", "face_side")
    side_name = _choice_value(side_raw, SIDE_VALUES, "front/back side") if side_raw is not None else None

    pair = None
    if serial_ref is not None and side_name is not None:
        sheet = one(db, "SELECT * FROM sheets WHERE serial=?", identity(serial_ref))
        require(sheet is not None, f"unknown physical sheet serial: {serial_ref}")
        pair = json.loads(sheet[side_name])
    elif left is not None and right is not None:
        for reading in db.execute("SELECT face FROM proof_readings WHERE segment=?", (seg["id"],)):
            candidate = json.loads(reading["face"])
            if sorted(candidate) == sorted([left, right]):
                pair = candidate
                break

    if mirror_value is None and left is not None and right is not None and pair is not None:
        if [left, right] == pair:
            mirror_value = False
        elif [right, left] == pair:
            mirror_value = True

    obs = {"id": event, "segment": seg["id"], "mirrored": mirror_value,
           "serial": serial_ref, "side": side_name, "left_page": left, "right_page": right}

    agrees = None
    conflicts_flag = 0
    if mirror_value is not None and seg["mirrored"] is not None:
        agrees = 1 if bool(seg["mirrored"]) == mirror_value else 0
        conflicts_flag = 0 if agrees else 1

    db.execute("""INSERT INTO proof_rulers
                  (id,segment,mirrored,serial,side,left_page,right_page,agrees,conflicts,detail)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (event, seg["id"], None if mirror_value is None else (1 if mirror_value else 0),
                serial_ref, side_name, left, right, agrees, conflicts_flag, encode(obs)))

    if mirror_value is None:
        return "proof-ruler-unresolved"

    if seg["mirrored"] is None:
        db.execute("UPDATE proof_segments SET mirrored=?,ruler_event=?,version=version+1 WHERE id=?",
                   (1 if mirror_value else 0, event, seg["id"]))
        refreshed = one(db, "SELECT * FROM proof_segments WHERE id=?", seg["id"])
        _relocate_segment_readings(db, refreshed)
        return "proof-ruler-established"

    if bool(seg["mirrored"]) != mirror_value:
        refreshed = one(db, "SELECT * FROM proof_segments WHERE id=?", seg["id"])
        _freeze_segment(db, refreshed, event, obs)
        return "proof-ruler-conflict"

    return "proof-ruler-confirmed"


ruler = record_ruler
calibrate = record_ruler
calibration = record_ruler
add_ruler = record_ruler


def resolve_segment(db, event, segment=None, mirrored=None, ruler=None, **extra):
    """Adjudicate a conflicted segment using a later authoritative ruler."""
    fields = _unwrap({"segment": segment, "mirrored": mirrored, "ruler": ruler, **extra}
                     if any(v is not None for v in (segment, mirrored, ruler)) else _unwrap(extra))
    segment_ref = _pick(fields, "segment", "segment_id", "setup", "clamp")
    seg = entity(db, "proof_segments", segment_ref)
    ruler_ref = _pick(fields, "ruler", "ruler_event", "authoritative_ruler", "proof_ruler")
    mirror_value = _as_bool(_pick(fields, "mirrored", "is_mirrored"))
    if ruler_ref is not None:
        ruler_row = entity(db, "proof_rulers", ruler_ref)
        require(ruler_row["mirrored"] is not None, "authoritative ruler has no correspondence")
        mirror_value = bool(ruler_row["mirrored"])
    require(mirror_value is not None, "segment resolution needs mirrored or an authoritative ruler")
    db.execute("""UPDATE proof_segments
                  SET status='open',mirrored=?,ruler_event=?,version=version+1,conflict=NULL WHERE id=?""",
               (1 if mirror_value else 0, ruler_ref or event, seg["id"]))
    db.execute("""INSERT INTO proof_rulers
                  (id,segment,mirrored,serial,side,left_page,right_page,agrees,conflicts,detail)
                  VALUES (?,?,?,?,NULL,NULL,NULL,1,0,?)""",
               (event, seg["id"], 1 if mirror_value else 0,
                encode({"id": event, "resolution": ruler_ref or event, "mirrored": mirror_value})))
    refreshed = one(db, "SELECT * FROM proof_segments WHERE id=?", seg["id"])
    _relocate_segment_readings(db, refreshed)
    return "proof-segment-resolved"


def _ineligible_message(book_id, reading, attr):
    return (f"cannot open proof task for {book_id} from reading {reading}: "
            f"serial {attr['serial']} on piece {attr['piece']['id']} is not part of this gathered "
            f"book block or legal folded signature ({'; '.join(attr['reasons'])})")


def _frozen_message(seg):
    conflict = json.loads(seg["conflict"]) if seg["conflict"] else {}
    observations = conflict.get("observations", [])
    observed = ", ".join(
        f"{obs.get('id')}(mirrored={obs.get('mirrored')}"
        + (f",{obs.get('serial')} {obs.get('side')} L{obs.get('left_page')}/R{obs.get('right_page')}"
           if obs.get("serial") else "")
        + ")"
        for obs in observations if isinstance(obs, dict))
    return (f"proof setup segment {seg['id']} is frozen by a ruler conflict "
            f"({conflict.get('ruler_event')} vs {conflict.get('conflicting_event')}); "
            f"specific observations: {observed}")


def _insert_handling(db, event, reading_id, seg, book_id, decision, kind, order_id, detail):
    db.execute("INSERT INTO proof_handling VALUES (?,?,?,?,?,?,?,?)",
               (event, reading_id, seg, book_id, decision, kind, order_id, encode(detail)))


def open_task(db, event, reading=None, kind=None, forced_kind=None, **extra):
    """校对任务入口: open a page-repair ticket or an actual spread retest order.

    Without a segment ruler an unlocated reading is legally parked as
    ``pending-location`` for single-page-only benches; a spread-capable bench
    may instead open one even-first ``[even, odd]`` pair retest.
    """
    fields = _unwrap({"reading": reading, "kind": kind, **extra}
                     if reading is not None or kind is not None else _unwrap(extra))
    reading_ref = _pick(fields, "reading", "reading_id", "defect_reading", "source_reading",
                        "read", "evidence_id")
    if reading_ref is None:
        segment_ref = _pick(fields, "segment", "segment_id", "setup")
        serial_ref = _pick(fields, "serial", "sheet_serial", "serial")
        side_raw = _pick(fields, "side")
        half_raw = _pick(fields, "half", "camera_half")
        kind_raw = _pick(fields, "kind", "category", "defect_kind", "type")
        require(None not in (segment_ref, serial_ref, side_raw, half_raw, kind_raw),
                "proof task requires a reading id or the full defect coordinates")
        side_name = _choice_value(side_raw, SIDE_VALUES, "front/back side")
        half_name = _choice_value(half_raw, HALF_VALUES, "camera half")
        defect_key = encode([identity(serial_ref), side_name, half_name, identity(str(kind_raw))])
        found = one(db, "SELECT * FROM proof_readings WHERE defect_key=? ORDER BY rowid LIMIT 1", defect_key)
        require(found is not None, "no defect reading matches these coordinates")
        row = found
    else:
        row = entity(db, "proof_readings", reading_ref)

    canonical = row["duplicate_of"] or row["id"]
    if canonical != row["id"]:
        row = entity(db, "proof_readings", canonical)

    book_id = row["book"]
    side = row["side"]
    attr = sheet_attribution(db, book_id, row["serial"], side)
    require(attr["eligible"], _ineligible_message(book_id, canonical, attr))

    seg = entity(db, "proof_segments", row["segment"])
    require(seg["status"] != "conflicted", _frozen_message(seg))

    face = json.loads(row["face"])
    mirrored = _effective_mirrored(seg)
    page = imposition.half_page(face, row["half"], mirrored) if mirrored is not None else None
    spread = imposition.spread_pages(face)

    requested = forced_kind or _norm_kind(_pick(fields, "kind", "type", "order_type", "scope", "mode"))
    allow_pair = _as_bool(_pick(fields, "allow_pair", "allow_spread", "pair_retest", "spread_retest",
                                "retest_allowed", "can_retest", "pair", "spread"))
    bench = _pick(fields, "bench", "workbench", "station", "desk", "capability")
    if isinstance(bench, str):
        token = bench.lower()
        if any(word in token for word in ("retest", "pair", "spread", "对页", "复测")):
            allow_pair = True
        if any(word in token for word in ("single", "repair-only", "page-only", "单页", "修补")):
            allow_pair = False if allow_pair is None else allow_pair

    detail = {"reading": canonical, "face": face, "candidate_pages": face,
              "spread_pages": spread, "ruler_event": seg["ruler_event"],
              "mirrored": mirrored, "ruler_version": seg["version"]}

    if requested == KIND_PAGE:
        require(page is not None,
                f"defect reading {canonical} is pending location: camera half cannot be written as "
                f"page 4 or 5 yet; no single-page repair order may be issued (candidates {face})")
        order_kind, pages, basis = KIND_PAGE, [page], "segment-ruler-page"
    elif requested == KIND_SPREAD:
        require(spread is not None,
                f"face pair {face} on {row['serial']} is not a continuous even-first reading spread; "
                "cannot open a pair retest order")
        order_kind, pages, basis = KIND_SPREAD, spread, "facing-spread-pair"
    elif page is not None:
        order_kind, pages, basis = KIND_PAGE, [page], "segment-ruler-page"
    elif spread is not None and allow_pair:
        order_kind, pages, basis = KIND_SPREAD, spread, "facing-spread-pair"
    else:
        _insert_handling(db, event, canonical, seg["id"], book_id,
                         "pending-location", None, None,
                         {**detail, "reason": "single-page bench cannot choose between candidate pages",
                          "pair_allowed": bool(allow_pair)})
        return "proof-reading-pending-location"

    order_ref = _pick(fields, "order", "task", "ticket", "proof_task", "proof_order",
                      "order_id", "task_id", "ticket_id")
    order_id = identity(order_ref) if order_ref is not None else event
    require(one(db, "SELECT 1 FROM proof_orders WHERE id=?", order_id) is None,
            f"proof task id already exists: {order_id}")

    prior = one(db, """SELECT id,status FROM proof_orders
                       WHERE defect_key=? AND kind=? AND status IN (?,?) ORDER BY rowid LIMIT 1""",
                row["defect_key"], order_kind, TASK_PENDING, TASK_COMPLETED)
    if prior is not None:
        require(False,
                f"duplicate {order_kind} proof order for the same physical sheet {row['serial']} "
                f"and defect {row['kind']}: valid order {prior['id']} ({prior['status']}) already exists")

    repeat_events = [r["id"] for r in db.execute(
        "SELECT id FROM proof_readings WHERE defect_key=? ORDER BY rowid", (row["defect_key"],))]
    evidence = {
        "schema": "proof-evidence-v1",
        "book": book_id,
        "reading": canonical,
        "reading_events": repeat_events,
        "source_file": row["source_file"],
        "serial": row["serial"],
        "piece": row["piece"],
        "side": row["side"],
        "half": row["half"],
        "kind": row["kind"],
        "face": face,
        "candidate_pages": face,
        "spread_pages": spread,
        "segment": seg["id"],
        "ruler_event": seg["ruler_event"],
        "ruler_version": seg["version"],
        "mirrored": mirrored,
        "page": page,
        "pages": pages,
        "basis": basis,
        "open_event": event,
    }
    db.execute("""INSERT INTO proof_orders
        (id,event,book,segment,reading,defect_key,kind,pages,status,review,evidence,result,
         complete_event,withdraw_event)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)""",
               (order_id, event, book_id, seg["id"], canonical, row["defect_key"], order_kind,
                encode(pages), TASK_PENDING, "ok", encode(evidence)))
    _insert_handling(db, event, canonical, seg["id"], book_id,
                     f"{order_kind}-opened", order_kind, order_id, detail)
    return "proof-task-opened"


open_proof_task = open_task
create_order = open_task
create_task = open_task
triage = open_task
route_proof = open_task


def _task_ref(db, event, fields):
    ref = _pick(fields, "task", "order", "ticket", "proof_task", "proof_order",
                "task_id", "order_id", "ticket_id")
    if ref is None:
        ref = event
    return entity(db, "proof_orders", ref)


def complete_task(db, event, task=None, **extra):
    """完成校对/修补: retain the opening evidence version; conflict may flag review."""
    fields = _unwrap({"task": task, **extra} if task is not None else _unwrap(extra))
    order = _task_ref(db, event, fields)
    require(order["status"] == TASK_PENDING,
            f"proof task {order['id']} is {order['status']}, not pending")
    seg = entity(db, "proof_segments", order["segment"])
    require(seg["status"] != "conflicted", _frozen_message(seg))
    result = _pick(fields, "result", "note", "outcome", "resolution", "comment")
    if result is not None:
        require(isinstance(result, str) and result.strip(), "invalid proof result")
    actual_page = _as_int(_pick(fields, "actual_page", "found_page", "observed_page",
                                "confirmed_page", "page"))
    db.execute("UPDATE proof_orders SET status=?,result=?,complete_event=? WHERE id=?",
               (TASK_COMPLETED, result, event, order["id"]))
    if actual_page is not None:
        evidence = json.loads(order["evidence"])
        expected_page = evidence.get("page")
        if expected_page is not None and actual_page != expected_page:
            current = one(db, "SELECT * FROM proof_segments WHERE id=?", seg["id"])
            _freeze_segment(db, current, event, {
                "id": event, "kind": "page-observation", "segment": seg["id"],
                "actual_page": actual_page, "expected_page": expected_page,
                "reading": order["reading"], "order": order["id"]})
    return "proof-task-completed"


complete_proof_task = complete_task
complete_order = complete_task
finish_task = complete_task


def withdraw_task(db, event, task=None, **extra):
    """撤回工单: withdrawn tickets no longer block re-issuing valid work."""
    fields = _unwrap({"task": task, **extra} if task is not None else _unwrap(extra))
    order = _task_ref(db, event, fields)
    require(order["status"] == TASK_PENDING,
            f"proof task {order['id']} is {order['status']}, not pending")
    db.execute("UPDATE proof_orders SET status=?,withdraw_event=? WHERE id=?",
               (TASK_WITHDRAWN, event, order["id"]))
    return "proof-task-withdrawn"


withdraw_proof_task = withdraw_task
withdraw_order = withdraw_task
cancel_task = withdraw_task


def review_task(db, event, task=None, **extra):
    """Record a human re-review verdict for a review-required proof task."""
    fields = _unwrap({"task": task, **extra} if task is not None else _unwrap(extra))
    order = _task_ref(db, event, fields)
    verdict = _pick(fields, "verdict", "review", "decision", "state")
    token = str(verdict or "").strip().lower()
    if token in {"ok", "clear", "confirmed", "resolved", "passed", "close", "复核通过", "已复核"}:
        review = "ok"
    elif token in {"required", "reopen", "upheld", "problem", "failed", "需复核", "维持"}:
        review = "review-required"
    else:
        review = "reviewed"
    db.execute("UPDATE proof_orders SET review=? WHERE id=?", (review, order["id"]))
    return "proof-review-recorded"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _reading_view(db, row):
    data = dict(row)
    for col in ("face",):
        data[col] = json.loads(data[col]) if data[col] else None
    data["spread"] = json.loads(row["spread"]) if row["spread"] else None
    data["reasons"] = json.loads(row["reasons"]) if row["reasons"] else []
    seg = one(db, "SELECT * FROM proof_segments WHERE id=?", row["segment"])
    effective = _effective_mirrored(seg)
    if effective is None or data["face"] is None:
        live_page = None
    else:
        live_page = imposition.half_page(data["face"], row["half"], effective)
    data["mirrored"] = None if effective is None else effective
    data["page"] = live_page
    data["located"] = 1 if live_page is not None else 0
    data["eligible"] = bool(row["eligible"])
    data["pending_location"] = live_page is None
    data["event"] = row["id"]
    data["state"] = "located" if live_page is not None else "pending-location"
    data["status"] = data["state"]
    data["candidate_pages"] = data["face"]
    orders = [r["id"] for r in db.execute(
        "SELECT id FROM proof_orders WHERE reading=? ORDER BY rowid",
        (row["duplicate_of"] or row["id"],))]
    data["orders"] = orders
    data["tasks"] = orders
    data["tickets"] = orders
    return data


def _order_view(db, row):
    data = dict(row)
    data["pages"] = json.loads(row["pages"])
    data["evidence"] = json.loads(row["evidence"])
    data["event"] = row["event"]
    data["state"] = row["status"]
    data["type"] = row["kind"]
    data["order_type"] = row["kind"]
    data["task"] = row["id"]
    data["ticket"] = row["id"]
    data["review_required"] = row["review"] == "review-required"
    data["page_range"] = "-".join(str(p) for p in data["pages"])
    data["valid"] = row["status"] in VALID_TASK_STATUSES
    data["active"] = row["status"] == TASK_PENDING
    return data


def _segment_view(db, row):
    data = dict(row)
    data["conflict"] = json.loads(row["conflict"]) if row["conflict"] else None
    data["mirrored"] = None if row["mirrored"] is None else bool(row["mirrored"])
    data["frozen"] = row["status"] == "conflicted"
    data["ruler_valid"] = row["status"] == "open" and row["mirrored"] is not None
    data["readings"] = [r["id"] for r in db.execute(
        "SELECT id FROM proof_readings WHERE segment=? ORDER BY rowid", (row["id"],))]
    data["orders"] = [r["id"] for r in db.execute(
        "SELECT id FROM proof_orders WHERE segment=? ORDER BY rowid", (row["id"],))]
    data["tasks"] = data["orders"]
    return data


def _book_view(db, book_id):
    book_row = entity(db, "books", book_id)
    volume = entity(db, "volumes", book_row["volume"])
    edition = entity(db, "editions", volume["edition"])
    intakes = [dict(r) for r in db.execute(
        "SELECT * FROM proof_intakes WHERE book=? ORDER BY rowid", (book_id,))]
    segments = [_segment_view(db, r) for r in db.execute(
        "SELECT * FROM proof_segments WHERE book=? ORDER BY ordinal,rowid", (book_id,))]
    readings = [_reading_view(db, r) for r in db.execute(
        "SELECT * FROM proof_readings WHERE book=? ORDER BY rowid", (book_id,))]
    orders = [_order_view(db, r) for r in db.execute(
        "SELECT * FROM proof_orders WHERE book=? ORDER BY rowid", (book_id,))]
    handling = [dict(r) for r in db.execute(
        "SELECT * FROM proof_handling WHERE book=? ORDER BY rowid", (book_id,))]
    for item in handling:
        item["detail"] = json.loads(item["detail"]) if item["detail"] else {}
    canonical = {r["id"]: r for r in readings if not r["duplicate_of"]}
    pending_location = [r["id"] for r in readings
                        if not r["duplicate_of"] and r["pending_location"] and r["eligible"]]
    return {
        "book": book_id, "volume": volume["id"], "edition": edition["id"],
        "format": edition["format"], "ordinal": volume["ordinal"],
        "intakes": intakes, "segments": segments, "readings": readings,
        "defects": readings, "orders": orders, "tasks": orders, "tickets": orders,
        "handling": handling,
        "pending_location": pending_location,
        "unlocated": pending_location,
        "located": [r["id"] for r in readings if r["located"] and not r["duplicate_of"]],
        "conflicts": [s["conflict"] for s in segments if s["conflict"]],
        "counts": {"readings": len(readings), "orders": len(orders), "tasks": len(orders),
                   "pending_location": len(pending_location),
                   "open_orders": sum(o["status"] == TASK_PENDING for o in orders),
                   "review_required": sum(o["review_required"] for o in orders)},
    }


def proof_report(db, target=None):
    """Ledger view for a book, intake, segment, reading, order or the bench."""
    if target is None:
        book_ids = [r[0] for r in db.execute("""
            SELECT DISTINCT book FROM (
              SELECT book FROM proof_intakes UNION ALL
              SELECT book FROM proof_readings WHERE book IS NOT NULL UNION ALL
              SELECT book FROM proof_orders) ORDER BY book""")]
        books = [_book_view(db, book_id) for book_id in book_ids]
        return {
            "books": books,
            "readings": [r for book in books for r in book["readings"]],
            "orders": [o for book in books for o in book["orders"]],
            "segments": [s for book in books for s in book["segments"]],
            "tasks": [o for book in books for o in book["orders"]],
            "tickets": [o for book in books for o in book["orders"]],
        }

    target = identity(target)
    book_id = None
    extra = {}
    if one(db, "SELECT 1 FROM books WHERE id=?", target):
        book_id = target
    elif one(db, "SELECT 1 FROM proof_intakes WHERE id=?", target):
        row = entity(db, "proof_intakes", target)
        book_id = row["book"]
        extra["intake"] = dict(row)
    elif one(db, "SELECT 1 FROM proof_segments WHERE id=?", target):
        seg = entity(db, "proof_segments", target)
        book_id = seg["book"]
        extra["segment"] = segment_report(db, target)
    elif one(db, "SELECT 1 FROM proof_readings WHERE id=?", target):
        row = entity(db, "proof_readings", target)
        book_id = row["book"]
        extra["reading"] = reading_report(db, target)
    elif one(db, "SELECT 1 FROM proof_orders WHERE id=?", target):
        row = entity(db, "proof_orders", target)
        book_id = row["book"]
        extra["order"] = order_report(db, target)
        extra["task"] = extra["order"]
    require(book_id is not None, f"unknown proof report target: {target}")
    view = _book_view(db, book_id)
    return {**view, **extra}


proof_status = proof_report
defect_report = proof_report
proofing_report = proof_report


def reading_report(db, reading_id):
    row = entity(db, "proof_readings", reading_id)
    return _reading_view(db, row)


def order_report(db, order_id):
    row = entity(db, "proof_orders", order_id)
    return _order_view(db, row)


task_report = order_report
proof_order_report = order_report
proof_task_report = order_report


def segment_report(db, segment_id):
    row = entity(db, "proof_segments", segment_id)
    return _segment_view(db, row)


def reconcile(db, key):
    """Match a defect/reading/order to all of its proof work by physical defect."""
    key = identity(key)
    defect_key = None
    book_id = None
    if one(db, "SELECT 1 FROM proof_orders WHERE id=?", key):
        order = entity(db, "proof_orders", key)
        defect_key, book_id = order["defect_key"], order["book"]
    elif one(db, "SELECT 1 FROM proof_readings WHERE id=?", key):
        reading = entity(db, "proof_readings", key)
        canonical = reading["duplicate_of"] or reading["id"]
        reading = entity(db, "proof_readings", canonical)
        defect_key, book_id = reading["defect_key"], reading["book"]
    else:
        rows = one(db, "SELECT defect_key,book FROM proof_orders WHERE defect_key=? LIMIT 1", key)
        if rows is not None:
            defect_key, book_id = rows["defect_key"], rows["book"]
    require(defect_key is not None, f"no proof defect matches: {key}")
    readings = [reading_report(db, r["id"]) for r in db.execute(
        "SELECT id FROM proof_readings WHERE defect_key=? ORDER BY rowid", (defect_key,))]
    orders = [order_report(db, r["id"]) for r in db.execute(
        "SELECT id FROM proof_orders WHERE defect_key=? ORDER BY rowid", (defect_key,))]
    seg_id = readings[0]["segment"] if readings else None
    segment = segment_report(db, seg_id) if seg_id else None
    current_page = readings[0]["page"] if readings else None
    grouped = {state: [o for o in orders if o["status"] == state]
               for state in (TASK_PENDING, TASK_COMPLETED, TASK_WITHDRAWN)}
    return {
        "defect_key": defect_key, "book": book_id, "current_page": current_page,
        "readings": readings, "orders": orders, "tasks": orders,
        "pending": grouped[TASK_PENDING], "completed": grouped[TASK_COMPLETED],
        "withdrawn": grouped[TASK_WITHDRAWN],
        "by_status": grouped,
        "pair_orders": [o for o in orders if o["kind"] == KIND_SPREAD],
        "repair_orders": [o for o in orders if o["kind"] == KIND_PAGE],
        "segment": segment, "ruler_valid": segment["ruler_valid"] if segment else False,
    }


reconcile_defect = reconcile
defect_reconciliation = reconcile
