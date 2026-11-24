"""Scan exact signature copies into books and qualify a spine-binding run."""
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
