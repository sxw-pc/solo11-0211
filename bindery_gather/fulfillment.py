"""Stage version-exact books, track per-volume accessories and hand over a kit."""
from . import binding, imposition
from .db import encode, entity, integer, one, require


def order(db, event, edition, qty):
    imposition.sealed(db, edition)
    integer(qty, "order kit count", 1)
    db.execute("INSERT INTO orders VALUES (?,?,?)", (event, edition, qty))
    return "order-opened"


def open_kit(db, event, order):
    booking = entity(db, "orders", order)
    count = one(db, "SELECT COUNT(*) FROM kits WHERE order_id=?", order)[0]
    require(count < booking["qty"], "order kit quota exhausted")
    db.execute("INSERT INTO kits VALUES (?,?)", (event, order))
    return "kit-opened"


def kit_order(db, kit):
    pack = entity(db, "kits", kit)
    return entity(db, "orders", pack["order_id"])


def pending(db, kit):
    order_row = kit_order(db, kit)
    require(one(db, "SELECT id FROM deliveries WHERE kit=?", kit) is None, "delivered kit is frozen")
    return order_row


def attach(db, event, kit, book):
    booking = pending(db, kit)
    copy = entity(db, "books", book)
    volume = entity(db, "volumes", copy["volume"])
    require(volume["edition"] == booking["edition"], "book edition cannot be mixed into this kit")
    require(one(db, "SELECT id FROM bindings WHERE book=?", book) is not None, "book is not bound")
    db.execute("INSERT INTO kit_books VALUES (?,?,?,?,NULL)", (event, kit, volume["id"], book))
    return "book-staged"


def detach(db, event, attachment):
    row = entity(db, "kit_books", attachment)
    pending(db, row["kit"])
    require(row["removed"] is None, "book already unstaged")
    db.execute("UPDATE kit_books SET removed=? WHERE id=?", (event, attachment))
    return "book-unstaged"


def accessory_lot(db, event, requirement, qty):
    spec = entity(db, "requirements", requirement)
    volume = entity(db, "volumes", spec["volume"])
    imposition.sealed(db, volume["edition"])
    integer(qty, "accessory lot quantity", 1)
    db.execute("INSERT INTO accessory_lots VALUES (?,?,?)", (event, requirement, qty))
    return "accessories-received"


def item_pair(db, edition, lot_id, requirement):
    items = entity(db, "accessory_lots", lot_id)
    origin = entity(db, "requirements", items["requirement"])
    target = entity(db, "requirements", requirement)
    for spec in (origin, target):
        volume = entity(db, "volumes", spec["volume"])
        require(volume["edition"] == edition, "accessory lot or destination has wrong edition")
    require(origin["sku"] == target["sku"], "accessory SKU does not fulfill destination requirement")
    return items, origin, target


def bridge(db, event, kit, lot, requirement, cap, granted, expires):
    booking = pending(db, kit)
    items, origin, target = item_pair(db, booking["edition"], lot, requirement)
    require(origin["volume"] != target["volume"], "same-volume lot needs no cross-volume permit")
    require(integer(cap, "cross-volume cap", 1) <= min(target["qty"], items["qty"]), "cross-volume cap exceeds requirement or lot")
    integer(granted, "permit grant minute")
    require(integer(expires, "permit expiry") >= granted, "permit expires before grant")
    db.execute("INSERT INTO bridges VALUES (?,?,?,?,?,?,?)", (event, kit, lot, requirement, cap, granted, expires))
    return "cross-volume-approved"


def used(db, *, lot=None, kit=None, requirement=None, bridge=None):
    query = "SELECT COALESCE(SUM(qty),0) FROM allocations WHERE returned IS NULL"
    args = []
    for key, value in (("lot", lot), ("kit", kit), ("requirement", requirement), ("bridge", bridge)):
        if value is not None:
            query += f" AND {key}=?"
            args.append(value)
    return one(db, query, *args)[0]


def allocate(db, event, kit, lot, requirement, qty, minute, bridge=None):
    booking = pending(db, kit)
    items, origin, target = item_pair(db, booking["edition"], lot, requirement)
    integer(qty, "allocation quantity", 1)
    integer(minute, "allocation minute")
    if origin["volume"] != target["volume"]:
        require(bridge is not None, "cross-volume allocation requires a permit")
        permit = entity(db, "bridges", bridge)
        require((permit["kit"], permit["lot"], permit["requirement"]) == (kit, lot, requirement), "permit scope mismatch")
        require(permit["granted"] <= minute <= permit["expires"], "permit invalid at allocation minute")
        require(used(db, bridge=bridge) + qty <= permit["cap"], "permit quantity exceeded")
    else:
        require(bridge is None, "unexpected cross-volume permit")
    require(used(db, lot=lot) + qty <= items["qty"], "accessory lot exhausted")
    require(used(db, kit=kit, requirement=requirement) + qty <= target["qty"], "volume accessory requirement exceeded")
    db.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,NULL)",
               (event, kit, lot, requirement, qty, minute, bridge))
    return "accessory-allocated"


def return_allocation(db, event, allocation):
    row = entity(db, "allocations", allocation)
    pending(db, row["kit"])
    require(row["returned"] is None, "allocation already returned")
    db.execute("UPDATE allocations SET returned=? WHERE id=?", (event, allocation))
    return "allocation-returned"


def kit_report(db, kit, minute):
    booking = kit_order(db, kit)
    delivery = one(db, "SELECT id,minute FROM deliveries WHERE kit=?", kit)
    integer(minute, "handover minute")
    when = delivery["minute"] if delivery else minute
    volumes, blockers = [], []
    for volume in db.execute("SELECT id,ordinal FROM volumes WHERE edition=? ORDER BY ordinal", (booking["edition"],)):
        volume_id = volume["id"]
        copy = one(db, """SELECT k.id AS attachment,k.book,b.run FROM kit_books k JOIN bindings b ON k.book=b.book
                           WHERE k.kit=? AND k.volume=? AND k.removed IS NULL""", kit, volume_id)
        slot = {"volume": volume_id, "ordinal": volume["ordinal"], "book": None, "accessories": []}
        if copy is None:
            blockers.append(f"missing-book:{volume_id}")
        else:
            check = binding.run_report(db, copy["run"])
            slot["book"] = {**dict(copy), "run_status": check["status"], "certificate": check["certificate"], "failed": check["failed"]}
            if check["status"] != "run-released":
                blockers.append(f"{volume_id}:{check['status']}")
        for target in db.execute("SELECT * FROM requirements WHERE volume=? ORDER BY sku", (volume_id,)):
            records = [dict(row) for row in db.execute("""SELECT a.id,a.lot,a.qty,a.minute,a.bridge,b.expires,b.granted
                    FROM allocations a LEFT JOIN bridges b ON a.bridge=b.id WHERE a.kit=? AND a.requirement=?
                    AND a.returned IS NULL ORDER BY a.id""", (kit, target["id"]))]
            eligible = 0
            for row in records:
                if row["minute"] > when or (row["bridge"] and not row["granted"] <= when <= row["expires"]):
                    blockers.append(f"allocation-outside-handover-window:{row['id']}")
                else:
                    eligible += row["qty"]
            if eligible != target["qty"]:
                blockers.append(f"accessory-count:{volume_id}:{target['sku']}")
            slot["accessories"].append({"sku": target["sku"], "required": target["qty"], "eligible": eligible, "allocations": records})
        volumes.append(slot)
    state = ("handover-review-required" if blockers else "kit-delivered") if delivery else ("kit-held" if blockers else "kit-ready")
    return {"kit": kit, "order": booking["id"], "edition": booking["edition"], "minute": when, "status": state,
            "delivery": delivery["id"] if delivery else None, "volumes": volumes, "blockers": blockers}


def deliver(db, event, kit, minute):
    pending(db, kit)
    report = kit_report(db, kit, minute)
    require(not report["blockers"], "kit held: " + ",".join(report["blockers"]))
    db.execute("INSERT INTO deliveries VALUES (?,?,?,?)", (event, kit, minute, encode(report)))
    return "kit-delivered"
