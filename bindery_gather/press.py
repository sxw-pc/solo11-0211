"""Track physical embossing plates and the sheets pressed between services."""
from . import imposition, materials
from .db import entity, integer, one, require


def register(db, event, signature, capacity, minute):
    spec = entity(db, "signatures", signature)
    volume = entity(db, "volumes", spec["volume"])
    imposition.sealed(db, volume["edition"])
    integer(capacity, "plate cycle capacity", spec["sheets"])
    integer(minute, "plate registration minute")
    db.execute("INSERT INTO press_dies VALUES (?,?,?,?)", (event, signature, capacity, minute))
    return "die-registered"


def report(db, die):
    plate = entity(db, "press_dies", die)
    services = [dict(row) for row in db.execute(
        "SELECT id,minute FROM die_services WHERE die=? ORDER BY rowid", (die,))]
    service = services[-1] if services else None
    uses = [dict(row) for row in db.execute(
        """SELECT u.id,u.piece,u.minute,u.sheets,u.service,p.status
           FROM die_uses u JOIN pieces p ON p.id=u.piece WHERE u.die=? ORDER BY u.rowid""", (die,))]
    cycle = service["id"] if service else None
    used = sum(use["sheets"] for use in uses if use["service"] == cycle)
    last = max([plate["minute"]] + [item["minute"] for item in services + uses])
    return {"die": die, "signature": plate["signature"], "capacity": plate["capacity"],
            "remaining": plate["capacity"] - used, "service": service, "services": services,
            "uses": uses, "last_minute": last}


def overhaul(db, event, die, minute):
    current = report(db, die)
    require(integer(minute, "overhaul minute") >= current["last_minute"], "out-of-order die service")
    db.execute("INSERT INTO die_services VALUES (?,?,?)", (event, die, minute))
    return "die-overhauled"


def fold(db, event, die, issue, piece, scans, minute):
    current = report(db, die)
    require(integer(minute, "press minute") >= current["last_minute"], "out-of-order die use")
    issued = entity(db, "issues", issue)
    require(issued["signature"] == current["signature"], "wrong die for issued signature")
    sheets = entity(db, "signatures", issued["signature"])["sheets"]
    require(sheets <= current["remaining"], "die service required before pressing")
    result = materials.fold(db, event, issue, piece, scans)
    service = current["service"]
    db.execute("INSERT INTO die_uses VALUES (?,?,?,?,?,?)",
               (event, die, piece, minute, sheets, service["id"] if service else None))
    return result
