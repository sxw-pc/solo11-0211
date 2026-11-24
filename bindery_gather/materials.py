"""Paper lot substitution, piece counts and folding evidence."""
from . import imposition
from .db import choice, encode, entity, identity, integer, one, require


def receive(db, event, format, grade, grain, qty):
    choice(format, imposition.FORMATS, "paper format")
    identity(grade)
    choice(grain, imposition.GRAINS, "paper grain")
    integer(qty, "paper sheet quantity", 1)
    db.execute("INSERT INTO paper_lots VALUES (?,?,?,?,?)", (event, format, grade, grain, qty))
    return "paper-received"


def material(db, lot_id, signature):
    lot = entity(db, "paper_lots", lot_id)
    sig = entity(db, "signatures", signature)
    volume = entity(db, "volumes", sig["volume"])
    spec = imposition.sealed(db, volume["edition"])
    require(lot["format"] == spec["format"], "paper belongs to a different reading format")
    require(lot["grain"] == sig["grain"], "paper grain incompatible with signature")
    return lot, sig


def permit(db, event, signature, lot, cap):
    paper, sig = material(db, lot, signature)
    require(paper["grade"] != sig["grade"], "standard paper needs no substitution permit")
    require(integer(cap, "paper permit cap", 1) <= paper["qty"], "permit exceeds received stock")
    db.execute("INSERT INTO permits VALUES (?,?,?,?)", (event, signature, lot, cap))
    return "substitution-approved"


def stock(db, lot_id):
    lot = entity(db, "paper_lots", lot_id)
    issued = one(db, "SELECT COALESCE(SUM(qty),0) FROM issues WHERE lot=?", lot_id)[0]
    return {"lot": lot_id, "received": lot["qty"], "issued": issued, "unissued": lot["qty"] - issued}


def issue(db, event, signature, lot, qty):
    paper, sig = material(db, lot, signature)
    integer(qty, "issued sheets", 1)
    require(qty <= stock(db, lot)["unissued"], "paper stock exhausted")
    approval = None
    if paper["grade"] != sig["grade"]:
        approval = one(db, "SELECT * FROM permits WHERE signature=? AND lot=?", signature, lot)
        require(approval is not None, "missing substitution permit for this signature and lot")
        used = one(db, "SELECT COALESCE(SUM(qty),0) FROM issues WHERE permit=?", approval["id"])[0]
        require(used + qty <= approval["cap"], "substitution cap exceeded")
    db.execute("INSERT INTO issues VALUES (?,?,?,?,?)", (event, signature, lot, qty, approval["id"] if approval else None))
    return "substitute-paper-issued" if approval else "standard-paper-issued"


def issue_report(db, issue_id):
    issued = entity(db, "issues", issue_id)
    copies = db.execute("SELECT p.id,p.status,p.faults,COUNT(s.serial) AS sheets FROM pieces p JOIN sheets s ON s.piece=p.id "
                        "WHERE p.issue=? GROUP BY p.id ORDER BY p.id", (issue_id,)).fetchall()
    folded = sum(row["sheets"] for row in copies if row["status"] == "folded")
    scrap = sum(row["sheets"] for row in copies if row["status"] == "scrap")
    return {"issue": issue_id, "lot": issued["lot"], "signature": issued["signature"], "permit": issued["permit"],
            "folded_sheets": folded, "scrap_sheets": scrap, "remaining": issued["qty"] - folded - scrap,
            "pieces": [dict(row) for row in copies]}


def fold(db, event, issue, piece, scans):
    identity(piece)
    issued = entity(db, "issues", issue)
    sig = entity(db, "signatures", issued["signature"])
    faults = imposition.verify_layout(sig, scans)
    require(sig["sheets"] <= issue_report(db, issue)["remaining"], "issue does not have enough unscanned sheets")
    status = "scrap" if faults else "folded"
    db.execute("INSERT INTO pieces VALUES (?,?,?,?,?)", (piece, event, issue, status, encode(faults)))
    for scan in scans:
        db.execute("INSERT INTO sheets VALUES (?,?,?,?,?,?,?)", (scan["serial"], piece, scan["position"],
                   encode(scan["front"]), encode(scan["back"]), scan["turn"], scan["head"]))
    return {"status": status, "piece": piece, "faults": faults}
