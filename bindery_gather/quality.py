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


def report(db, run_id, stations=()):
    run = binding.run_report(db, run_id)
    fmt = entity(db, "editions", run["edition"])["format"]
    readings = {}
    for row in db.execute("""
        SELECT q.* FROM qc_readings q JOIN bindings b ON b.id=q.binding
        WHERE b.run=? ORDER BY q.binding,q.minute,q.id
    """, (run_id,)):
        readings.setdefault(row["binding"], []).append(dict(row))
    materials = {}
    for row in db.execute("""
        SELECT bp.book,bp.slot,bp.piece,l.id AS lot,l.grade,l.grain,COUNT(s.serial) AS sheets
        FROM bindings b JOIN book_pieces bp ON bp.book=b.book JOIN pieces p ON p.id=bp.piece
        JOIN issues i ON p.issue=i.id JOIN paper_lots l ON i.lot=l.id
        JOIN sheets s ON s.piece=p.id WHERE b.run=? GROUP BY bp.book,bp.slot ORDER BY bp.book,bp.slot
    """, (run_id,)):
        entry = dict(row)
        materials.setdefault(entry.pop("book"), []).append(entry)
    available = [station for station in stations if fmt in station["formats"]]
    for member in run["members"]:
        member["readings"] = readings.get(member["binding"], [])
        member["paper"] = materials.get(member["book"], [])
        member["station_candidates"] = available if member["sample"] is None else []
    missing = [member["binding"] for member in run["members"] if member["sample"] is None]
    return {**run, "format": fmt, "missing": missing}
