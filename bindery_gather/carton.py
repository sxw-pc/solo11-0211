"""Capture physical carton labels before sealing and handing over a kit."""
import json

from . import fulfillment
from .db import choice, encode, entity, identity, integer, one, require



def open_carton(db, event, kit, label):
    fulfillment.pending(db, kit)
    db.execute("INSERT INTO cartons VALUES (?,?,?)", (event, kit, identity(label)))
    return "carton-opened"


def unsealed(db, carton):
    row = entity(db, "cartons", carton)
    require(one(db, "SELECT id FROM carton_seals WHERE carton=?", carton) is None, "carton is sealed")
    fulfillment.pending(db, row["kit"])
    return row


def observe(db, event, carton, device, minute, kind, reference):
    unsealed(db, carton)
    choice(kind, ("book", "allocation"), "carton label kind")
    db.execute("INSERT INTO carton_observations VALUES (?,?,?,?,?,?)",
               (event, carton, identity(device), integer(minute, "observation minute"), kind, identity(reference)))
    return "carton-label-captured"


def expected_components(kit):
    return sorted(
        [("book", volume["book"]["book"]) for volume in kit["volumes"] if volume["book"] is not None]
        + [("allocation", entry["id"]) for volume in kit["volumes"]
           for accessory in volume["accessories"] for entry in accessory["allocations"]]
    )


def report(db, carton, minute):
    row = entity(db, "cartons", carton)
    integer(minute, "report minute")
    kit = fulfillment.kit_report(db, row["kit"], minute)
    expected = expected_components(kit)
    observations = [dict(value) for value in db.execute(
        "SELECT id,device,minute,kind,reference FROM carton_observations WHERE carton=? ORDER BY id", (carton,))]
    observed = {(value["kind"], value["reference"]) for value in observations if value["minute"] <= minute}
    missing = sorted(set(expected) - observed)
    unexpected = sorted(observed - set(expected))
    recorded = one(db, "SELECT id,minute,evidence FROM carton_seals WHERE carton=?", carton)
    sealed = None if recorded is None else {**dict(recorded), "evidence": json.loads(recorded["evidence"])}
    return {"carton": carton, "label": row["label"], "kit": row["kit"], "minute": minute,
            "kit_status": kit["status"], "kit_blockers": kit["blockers"], "expected": expected,
            "observations": observations, "missing": missing, "unexpected": unexpected, "seal": sealed}


def seal(db, event, carton, minute):
    row = unsealed(db, carton)
    require(one(db, "SELECT id FROM carton_seals WHERE kit=?", row["kit"]) is None, "kit already has a sealed carton")
    current = report(db, carton, minute)
    require(not current["kit_blockers"], "kit is not ready for carton sealing")
    require(not current["missing"] and not current["unexpected"], "captured labels do not match current kit components")
    current.pop("seal")
    current["observations"] = [value for value in current["observations"] if value["minute"] <= minute]
    db.execute("INSERT INTO carton_seals VALUES (?,?,?,?,?)", (event, carton, row["kit"], minute, encode(current)))
    return "carton-sealed"


def dispatch(db, event, carton, minute):
    current = report(db, carton, minute)
    sealed = current["seal"]
    require(sealed is not None and sealed["minute"] <= minute, "carton was not sealed at dispatch")
    require(sealed["evidence"]["expected"] == [list(component) for component in current["expected"]],
            "kit components changed since carton sealing")
    return fulfillment.deliver(db, event, current["kit"], minute)
