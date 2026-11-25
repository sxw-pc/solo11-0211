"""Frozen title variants and the imposition of each physical sheet."""
from .db import choice, entity, identity, integer, require


FORMATS = ("braille", "large-print")
GRAINS = ("long", "short")
TURNS = ("long-edge", "short-edge")
HEADS = ("head-up", "head-down")


def edition(db, event, title, format, revision, volume_count, min_samples, min_pull, max_skew):
    identity(title)
    choice(format, FORMATS, "format")
    for label, number in (("revision", revision), ("volume count", volume_count),
                          ("sample target", min_samples), ("minimum spine pull", min_pull)):
        integer(number, label, 1)
    integer(max_skew, "maximum spine skew")
    db.execute("INSERT INTO editions VALUES (?,?,?,?,?,?,?,?,NULL)",
               (event, title, format, revision, volume_count, min_samples, min_pull, max_skew))
    return "edition-draft"


def draft(db, edition_id):
    spec = entity(db, "editions", edition_id)
    require(spec["seal"] is None, "edition is already sealed")
    return spec


def sealed(db, edition_id):
    spec = entity(db, "editions", edition_id)
    require(spec["seal"] is not None, "edition is not sealed")
    return spec


def volume(db, event, edition, ordinal, slots):
    spec = draft(db, edition)
    require(integer(ordinal, "volume ordinal", 1) <= spec["volume_count"], "volume outside edition")
    integer(slots, "signature slots", 1)
    db.execute("INSERT INTO volumes VALUES (?,?,?,?)", (event, edition, ordinal, slots))
    return "volume-planned"


def signature(db, event, volume, slot, first, sheets, grade, grain, turn, head):
    plan = entity(db, "volumes", volume)
    draft(db, plan["edition"])
    require(integer(slot, "signature slot", 1) <= plan["slots"], "signature outside volume")
    integer(first, "first page", 1)
    integer(sheets, "sheets per signature", 1)
    identity(grade)
    choice(grain, GRAINS, "grain")
    choice(turn, TURNS, "turn edge")
    choice(head, HEADS, "head direction")
    db.execute("INSERT INTO signatures VALUES (?,?,?,?,?,?,?,?,?)",
               (event, volume, slot, first, sheets, grade, grain, turn, head))
    return "signature-planned"


def accessory(db, event, volume, sku, qty):
    plan = entity(db, "volumes", volume)
    draft(db, plan["edition"])
    identity(sku)
    integer(qty, "per-volume accessory count", 1)
    db.execute("INSERT INTO requirements VALUES (?,?,?,?)", (event, volume, sku, qty))
    return "accessory-planned"


def volume_signatures(db, volume):
    return db.execute("SELECT * FROM signatures WHERE volume=? ORDER BY slot", (volume,)).fetchall()


def seal(db, event, edition):
    spec = draft(db, edition)
    volumes = db.execute("SELECT * FROM volumes WHERE edition=? ORDER BY ordinal", (edition,)).fetchall()
    require(len(volumes) == spec["volume_count"], "edition missing a volume")
    for volume in volumes:
        signatures = volume_signatures(db, volume["id"])
        require(len(signatures) == volume["slots"], f"volume missing signatures: {volume['id']}")
        next_page = 1
        for sig in signatures:
            require(sig["first"] == next_page, f"page gap or overlap: {sig['id']}")
            next_page += sig["sheets"] * 4
    db.execute("UPDATE editions SET seal=? WHERE id=?", (event, edition))
    return "edition-sealed"


def sheet_faces(sig, position):
    """Return outer and inner face pairs in scanner left-to-right order."""
    first = sig["first"] + (position - 1) * 2
    last = sig["first"] + sig["sheets"] * 4 - 1 - (position - 1) * 2
    return [last, first], [first + 1, last - 1]


def spread_pages(pair):
    """Return the even-first reading spread ``[even, odd]`` for a face pair.

    Device halves only identify one of the two pages of a recorded face.  Most
    face pairs (for example the outermost ``[8, 1]``) are not facing reading
    pages; only two consecutive pages beginning on an even page form a real
    spread such as ``[4, 5]``.  Returns ``None`` for every other pair.
    """
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    if any(type(page) is not int for page in pair):
        return None
    low, high = sorted(pair)
    if low >= 1 and low % 2 == 0 and high == low + 1:
        return [low, high]
    return None


# Vocabulary aliases used by the proofreading intake.
facing_pair = spread_pages
reading_pair = spread_pages
continuous_pair = spread_pages
spread_pair = spread_pages


def half_page(pair, half, mirrored):
    """Map a camera half (``left``/``right``) to its page.

    ``pair`` is the canonical left-to-right face pair from :func:`sheet_faces`.
    ``mirrored`` is the whole setup segment's calibration result.
    """
    require(isinstance(pair, (list, tuple)) and len(pair) == 2, "invalid face pair")
    choice(half, ("left", "right"), "camera half")
    require(isinstance(mirrored, bool), "segment ruler has no left/right correspondence")
    left, right = pair if not mirrored else (pair[1], pair[0])
    return left if half == "left" else right


def verify_layout(sig, scans):
    require(isinstance(scans, list) and len(scans) == sig["sheets"], "wrong physical sheet count")
    seen_positions, seen_serials, faults = set(), set(), []
    for scan in scans:
        require(isinstance(scan, dict) and set(scan) == {"serial", "position", "front", "back", "turn", "head"},
                "invalid sheet scan shape")
        serial = identity(scan["serial"])
        require(serial not in seen_serials, "duplicate sheet serial in fold")
        seen_serials.add(serial)
        position = integer(scan["position"], "sheet position", 1)
        for side in ("front", "back"):
            require(isinstance(scan[side], list) and len(scan[side]) == 2, "invalid scanned face")
            for page in scan[side]:
                integer(page, "printed page", 1)
        choice(scan["turn"], TURNS, "observed turn")
        choice(scan["head"], HEADS, "observed head")
        if position > sig["sheets"] or position in seen_positions:
            faults.append(f"{serial}:position")
        else:
            front, back = sheet_faces(sig, position)
            if scan["front"] != front or scan["back"] != back:
                faults.append(f"{serial}:page-pair")
        seen_positions.add(position)
        if scan["turn"] != sig["turn"] or scan["head"] != sig["head"]:
            faults.append(f"{serial}:direction")
    return sorted(faults)
