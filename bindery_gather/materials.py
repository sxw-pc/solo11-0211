"""Paper lot substitution, piece counts and folding evidence.

The plain ``paper`` / ``permit`` / ``issue`` / ``fold`` operations keep their
original behaviour.  On top of them this module captures a *packed pair* of
paper bundles that share one packing list: the list promises exactly one
standard-grade bundle and one heavier-grade bundle, while a hand scanner can
only narrow each bundle to a set of candidate grade characters.

Because the two candidate sets must explain the packing list *jointly*, the
surviving grade assignments are enumerated together (see :func:`manifest`).
An issue drawn before the true grade is known must be valid under **every**
surviving assignment.  Each assignment is charged independently: in one
assignment the same physical sheets are standard stock, in another they are a
permit-capped substitution.  The two balances are reported separately and are
never pooled into a single generic remainder.  A receipt only becomes a
definite ledger entry once a reliable label (or otherwise a single surviving
assignment) pins the grade.
"""
import json

from . import imposition
from .db import choice, encode, entity, identity, integer, one, require


def _candidates(grades):
    require(isinstance(grades, list) and grades, "invalid candidate grades")
    seen = []
    for grade in grades:
        identity(grade)
        require(grade not in seen, "duplicate candidate grade")
        seen.append(grade)
    return seen


def _candidates_of(lot):
    raw = lot["candidates"]
    return json.loads(raw) if raw is not None else [lot["grade"]]


def receive(db, event, format, grade, grain, qty):
    choice(format, imposition.FORMATS, "paper format")
    identity(grade)
    choice(grain, imposition.GRAINS, "paper grain")
    integer(qty, "paper sheet quantity", 1)
    db.execute("INSERT INTO paper_lots VALUES (?,?,?,?,?,NULL)", (event, format, grade, grain, qty))
    return "paper-received"


def scan(db, event, manifest, bundle, format, qty, grain, candidates):
    """Capture one physical bundle from a shared two-bundle packing list.

    ``manifest`` names the shared list, ``bundle`` the physical bundle number,
    ``qty`` the physically recounted sheets, ``grain`` the observed grain and
    ``candidates`` the grade characters the scanner could not tell apart.  The
    two bundles are captured independently; the list's tiers are the union of
    the characters actually seen, which must be exactly two.
    """
    identity(manifest)
    choice(format, imposition.FORMATS, "packing-list format")
    integer(qty, "scanned sheet quantity", 1)
    choice(grain, imposition.GRAINS, "scanned grain")
    grades = _candidates(candidates)
    header = one(db, "SELECT id,grades FROM manifests WHERE id=?", manifest)
    if header is None:
        tiers = list(grades)
        db.execute("INSERT INTO manifests VALUES (?,?)", (manifest, encode(sorted(tiers))))
    else:
        tiers = sorted(set(json.loads(header["grades"])) | set(grades))
        require(len(tiers) <= 2, "packing manifest promises exactly two grade tiers")
        existing = db.execute("SELECT b.format,b.grain FROM manifest_bundles m JOIN paper_lots b ON b.id=m.bundle "
                              "WHERE m.manifest=?", (manifest,)).fetchall()
        require(len(existing) < 2, "packing manifest already holds two bundles")
        for prior in existing:
            require((prior["format"], prior["grain"]) == (format, grain), "bundle differs from its packing manifest")
        db.execute("UPDATE manifests SET grades=? WHERE id=?", (encode(tiers), manifest))
    require(one(db, "SELECT id FROM paper_lots WHERE id=?", identity(bundle)) is None, "bundle already scanned")
    # Provisional grade is the first candidate; it is never trusted for issue.
    db.execute("INSERT INTO paper_lots VALUES (?,?,?,?,?,?)",
               (bundle, format, grades[0], grain, qty, encode(grades)))
    db.execute("INSERT INTO manifest_bundles VALUES (?,?)", (manifest, bundle))
    if header is not None:
        packing_list(db, manifest)  # jointly reject candidate sets that cannot explain the list
    return "bundle-scanned"


def _label_grade(db, bundle):
    row = one(db, "SELECT grade FROM paper_labels WHERE bundle=?", bundle)
    return row["grade"] if row else None


def packing_list(db, manifest_id):
    """Enumerate the grade assignments compatible with one packing list.

    A bundle is fixed by a reliable label when one exists, otherwise limited to
    its own candidates.  Only assignments that give each bundle a different
    grade from the list's two tiers survive.  Both bundles are resolved
    *together*; a bundle never gets to pick its own most convenient grade.

    An incomplete list (only one bundle scanned so far) is reported with
    ``complete`` false and no assignments; a complete list whose candidates
    cannot explain the tiers is a contradiction and raises.
    """
    identity(manifest_id)
    header = entity(db, "manifests", manifest_id)
    tiers = json.loads(header["grades"])
    bundles = db.execute("SELECT b.* FROM manifest_bundles m JOIN paper_lots b ON b.id=m.bundle "
                         "WHERE m.manifest=? ORDER BY b.id", (manifest_id,)).fetchall()
    require(len(bundles) <= 2, "packing manifest holds more than two bundles")
    options = {}
    for bundle in bundles:
        labelled = _label_grade(db, bundle["id"])
        if labelled is not None:
            require(labelled in tiers and labelled in _candidates_of(bundle),
                    "bundle label contradicts the packing manifest")
            options[bundle["id"]] = [labelled]
        else:
            options[bundle["id"]] = [grade for grade in _candidates_of(bundle) if grade in tiers]
    assignments = []
    if len(bundles) == 2:
        first, second = bundles[0]["id"], bundles[1]["id"]
        assignments = [{first: ga, second: gb}
                       for ga in options[first] for gb in options[second]
                       if ga != gb and {ga, gb} == set(tiers)]
        require(bool(assignments), "scanned grades cannot explain the packing manifest")
    return {"manifest": manifest_id, "tiers": tiers, "complete": len(bundles) == 2,
            "bundles": [{**dict(bundle), "candidates": _candidates_of(bundle)} for bundle in bundles],
            "assignments": assignments}


def _assignments(db, lot):
    """Grade assignments still open for the bundle ``lot``.

    An ordinary (unscanned) lot has exactly one assignment: its recorded grade.
    """
    row = one(db, "SELECT manifest FROM manifest_bundles WHERE bundle=?", lot)
    if row is None:
        return [{lot: _label_grade(db, lot) or entity(db, "paper_lots", lot)["grade"]}]
    report = packing_list(db, row["manifest"])
    require(report["complete"], "packing manifest is incomplete")
    return report["assignments"]


def label(db, event, bundle, grade):
    """Record a reliable physical grade tag for one scanned bundle."""
    paper = entity(db, "paper_lots", bundle)
    identity(grade)
    require(grade in _candidates_of(paper), "label is not a scanned candidate")
    existing = _label_grade(db, bundle)
    if existing is not None:
        require(existing == grade, "bundle already carries a different label")
        return "bundle-labelled"
    db.execute("INSERT INTO paper_labels VALUES (?,?,?)", (event, bundle, grade))
    manifest_row = one(db, "SELECT manifest FROM manifest_bundles WHERE bundle=?", bundle)
    if manifest_row is not None:
        packing_list(db, manifest_row["manifest"])  # reject a label that leaves no assignment
    return "bundle-labelled"


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
    require(any(grade != sig["grade"] for grade in _candidates_of(paper)),
            "standard paper needs no substitution permit")
    require(integer(cap, "paper permit cap", 1) <= paper["qty"], "permit exceeds received stock")
    db.execute("INSERT INTO permits VALUES (?,?,?,?)", (event, signature, lot, cap))
    return "substitution-approved"


def stock(db, lot_id):
    lot = entity(db, "paper_lots", lot_id)
    issued = one(db, "SELECT COALESCE(SUM(qty),0) FROM issues WHERE lot=?", lot_id)[0]
    return {"lot": lot_id, "received": lot["qty"], "issued": issued, "unissued": lot["qty"] - issued}


def _issued(db, lot, signature=None):
    query, args = "SELECT COALESCE(SUM(qty),0) FROM issues WHERE lot=?", [lot]
    if signature is not None:
        query, args = query + " AND signature=?", args + [signature]
    return one(db, query, *args)[0]


def availability(db, lot, signature):
    """Remaining sheets a receipt may draw, one entry per grade assignment.

    For every surviving assignment the bundle sits wholly in one grade role for
    this signature.  A standard role is only limited by the physical sheets; a
    substitute role is additionally limited by this signature's permit cap.
    The same issued receipt is therefore charged to standard stock in one
    assignment and to the substitution cap in another -- never to a merged
    generic balance.
    """
    paper = entity(db, "paper_lots", lot)
    sig = entity(db, "signatures", signature)
    physical = paper["qty"] - _issued(db, lot)
    entries = []
    for assignment in _assignments(db, lot):
        grade = assignment[lot]
        if grade == sig["grade"]:
            entries.append({"grades": assignment, "role": "standard", "issued": _issued(db, lot),
                            "physical_remaining": physical, "cap_remaining": None,
                            "remaining": physical})
            continue
        approval = one(db, "SELECT * FROM permits WHERE signature=? AND lot=?", signature, lot)
        used = _issued(db, lot, signature)
        cap_remaining = approval["cap"] - used if approval else 0
        entries.append({"grades": assignment, "role": "substitute", "issued": _issued(db, lot),
                        "physical_remaining": physical, "cap_remaining": cap_remaining,
                        "remaining": max(0, min(physical, cap_remaining))})
    return {"lot": lot, "signature": signature, "candidates": _candidates_of(paper),
            "labelled": _label_grade(db, lot), "assignments": entries,
            "tentative_remaining": min(entry["remaining"] for entry in entries)}


def _resolved_grade(db, lot):
    """The single grade of ``lot`` when its assignment is unambiguous."""
    assignments = _assignments(db, lot)
    require(len(assignments) == 1, "bundle grade is still ambiguous; use a tentative issue")
    return assignments[0][lot]


def issue(db, event, signature, lot, qty, basis="definite"):
    """Issue paper; ``basis='tentative'`` covers an unscanned-grade bundle."""
    paper, sig = material(db, lot, signature)
    integer(qty, "issued sheets", 1)
    require(basis in ("definite", "tentative"), "invalid issue basis")
    if basis == "tentative":
        require(paper["candidates"] is not None, "tentative receipts only apply to scanned bundles")
        room = availability(db, lot, signature)["tentative_remaining"]
        require(qty <= room, "tentative receipt exceeds a compatible grade assignment")
        db.execute("INSERT INTO issues VALUES (?,?,?,?,?,?)", (event, signature, lot, qty, None, basis))
        return "tentative-paper-issued"
    require(qty <= stock(db, lot)["unissued"], "paper stock exhausted")
    grade = _resolved_grade(db, lot)
    if grade != sig["grade"]:
        approval = one(db, "SELECT * FROM permits WHERE signature=? AND lot=?", signature, lot)
        require(approval is not None, "missing substitution permit for this signature and lot")
        used = _issued(db, lot, signature)
        require(used + qty <= approval["cap"], "substitution cap exceeded")
    else:
        approval = None
    db.execute("INSERT INTO issues VALUES (?,?,?,?,?,?)",
               (event, signature, lot, qty, approval["id"] if approval else None, basis))
    return "substitute-paper-issued" if approval else "standard-paper-issued"


def confirm(db, event, issue_id):
    """Merge a tentative receipt into the final ledger once the grade is pinned.

    This is the point where the capture facts (scan/label) and the issue fact
    become one definite bookkeeping entry.  It fails while more than one grade
    assignment still survives, so a receipt is never hardened against an
    assumption the evidence no longer supports.
    """
    issued = entity(db, "issues", issue_id)
    require(issued["basis"] == "tentative", "only tentative receipts can be confirmed")
    grade = _resolved_grade(db, issued["lot"])
    sig = entity(db, "signatures", issued["signature"])
    approval = None
    if grade != sig["grade"]:
        approval = one(db, "SELECT * FROM permits WHERE signature=? AND lot=?",
                       issued["signature"], issued["lot"])
        require(approval is not None, "confirmed substitute receipt needs a permit")
        require(_issued(db, issued["lot"], issued["signature"]) <= approval["cap"],
                "substitution cap exceeded")
    db.execute("UPDATE issues SET basis='definite', permit=? WHERE id=?",
               (approval["id"] if approval else None, issue_id))
    return "tentative-receipt-confirmed"


def review(db, signature, lots):
    """Operator-facing check of which receipts are executable right now.

    ``lots`` is the pair of bundle IDs sharing a packing list.  For each bundle
    the report shows every surviving grade assignment, the role it would take,
    and the sheets a receipt could still draw in that role.  A receipt is only
    offered when it is legal under *all* assignments (``tentative_remaining``);
    the two role balances stay separate.
    """
    require(isinstance(lots, list) and len(lots) == 2, "review needs the two packed bundles")
    sig = entity(db, "signatures", signature)
    manifests = []
    for lot in lots:
        row = one(db, "SELECT manifest FROM manifest_bundles WHERE bundle=?", lot)
        manifests.append(row["manifest"] if row else None)
    require(None not in manifests and manifests[0] == manifests[1],
            "review needs two bundles from one packing list")
    entries, assignments = [], None
    for lot in lots:
        available = availability(db, lot, signature)
        if assignments is None:
            assignments = [entry["grades"] for entry in available["assignments"]]
        entries.append({"lot": lot, "candidates": available["candidates"], "labelled": available["labelled"],
                        "roles": available["assignments"],
                        "tentative_remaining": available["tentative_remaining"]})
    pinned = len(assignments) == 1
    return {"signature": signature, "signature_grade": sig["grade"],
            "assignments": assignments, "pinned": pinned,
            "bundles": entries,
            # What the operator may legally do now: formalise once pinned,
            # otherwise only tentative receipts that survive every assignment.
            "can_formalise": pinned,
            "tentative_available": {entry["lot"]: entry["tentative_remaining"]
                                    for entry in entries if entry["tentative_remaining"] > 0}}


def issue_report(db, issue_id):
    issued = entity(db, "issues", issue_id)
    copies = db.execute("SELECT p.id,p.status,p.faults,COUNT(s.serial) AS sheets FROM pieces p JOIN sheets s ON s.piece=p.id "
                        "WHERE p.issue=? GROUP BY p.id ORDER BY p.id", (issue_id,)).fetchall()
    folded = sum(row["sheets"] for row in copies if row["status"] == "folded")
    scrap = sum(row["sheets"] for row in copies if row["status"] == "scrap")
    return {"issue": issue_id, "lot": issued["lot"], "signature": issued["signature"], "permit": issued["permit"],
            "basis": issued["basis"], "folded_sheets": folded, "scrap_sheets": scrap,
            "remaining": issued["qty"] - folded - scrap, "pieces": [dict(row) for row in copies]}


def fold(db, event, issue, piece, scans):
    identity(piece)
    issued = entity(db, "issues", issue)
    sig = entity(db, "signatures", issued["signature"])
    faults = imposition.verify_layout(sig, scans)
    require(sig["sheets"] <= issue_report(db, issue)["remaining"], "issue does not have enough unscanned sheets")
    status = "scrap" if faults else "folded"
    # Pages and direction are judged against the original sealed layout.  Even
    # a quarantined piece has consumed its sheets: folding never returns paper
    # to unissued stock.
    db.execute("INSERT INTO pieces VALUES (?,?,?,?,?)", (piece, event, issue, status, encode(faults)))
    for scan in scans:
        db.execute("INSERT INTO sheets VALUES (?,?,?,?,?,?,?)", (scan["serial"], piece, scan["position"],
                   encode(scan["front"]), encode(scan["back"]), scan["turn"], scan["head"]))
    return {"status": status, "piece": piece, "faults": faults}
