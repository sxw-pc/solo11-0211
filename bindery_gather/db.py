"""Transactional capture log and bindery records."""
import json
import sqlite3
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, id TEXT UNIQUE, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS editions(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, format TEXT NOT NULL, revision INTEGER NOT NULL,
 volume_count INTEGER NOT NULL, min_samples INTEGER NOT NULL, min_pull INTEGER NOT NULL,
 max_skew INTEGER NOT NULL, seal TEXT, UNIQUE(title,format,revision));
CREATE TABLE IF NOT EXISTS volumes(
 id TEXT PRIMARY KEY, edition TEXT NOT NULL REFERENCES editions, ordinal INTEGER NOT NULL,
 slots INTEGER NOT NULL, UNIQUE(edition,ordinal));
CREATE TABLE IF NOT EXISTS signatures(
 id TEXT PRIMARY KEY, volume TEXT NOT NULL REFERENCES volumes, slot INTEGER NOT NULL,
 first INTEGER NOT NULL, sheets INTEGER NOT NULL, grade TEXT NOT NULL, grain TEXT NOT NULL,
 turn TEXT NOT NULL, head TEXT NOT NULL, UNIQUE(volume,slot));
CREATE TABLE IF NOT EXISTS requirements(
 id TEXT PRIMARY KEY, volume TEXT NOT NULL REFERENCES volumes, sku TEXT NOT NULL,
 qty INTEGER NOT NULL, UNIQUE(volume,sku));
CREATE TABLE IF NOT EXISTS paper_lots(
 id TEXT PRIMARY KEY, format TEXT NOT NULL, grade TEXT NOT NULL, grain TEXT NOT NULL, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS permits(
 id TEXT PRIMARY KEY, signature TEXT NOT NULL REFERENCES signatures,
 lot TEXT NOT NULL REFERENCES paper_lots, cap INTEGER NOT NULL, UNIQUE(signature,lot));
CREATE TABLE IF NOT EXISTS issues(
 id TEXT PRIMARY KEY, signature TEXT NOT NULL REFERENCES signatures,
 lot TEXT NOT NULL REFERENCES paper_lots, qty INTEGER NOT NULL, permit TEXT REFERENCES permits);
CREATE TABLE IF NOT EXISTS pieces(
 id TEXT PRIMARY KEY, event TEXT UNIQUE NOT NULL, issue TEXT NOT NULL REFERENCES issues,
 status TEXT NOT NULL, faults TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sheets(
 serial TEXT PRIMARY KEY, piece TEXT NOT NULL REFERENCES pieces, position INTEGER NOT NULL,
 front TEXT NOT NULL, back TEXT NOT NULL, turn TEXT NOT NULL, head TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY, volume TEXT NOT NULL REFERENCES volumes);
CREATE TABLE IF NOT EXISTS book_pieces(
 book TEXT NOT NULL REFERENCES books, slot INTEGER NOT NULL,
 piece TEXT NOT NULL UNIQUE REFERENCES pieces, PRIMARY KEY(book,slot));
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, edition TEXT NOT NULL REFERENCES editions);
CREATE TABLE IF NOT EXISTS bindings(
 id TEXT PRIMARY KEY, book TEXT NOT NULL UNIQUE REFERENCES books, run TEXT NOT NULL REFERENCES runs);
CREATE TABLE IF NOT EXISTS samples(
 id TEXT PRIMARY KEY, binding TEXT NOT NULL UNIQUE REFERENCES bindings, pull INTEGER NOT NULL,
 skew INTEGER NOT NULL, passed INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS certificates(
 id TEXT PRIMARY KEY, run TEXT NOT NULL UNIQUE REFERENCES runs, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, edition TEXT NOT NULL REFERENCES editions, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kits(id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders);
CREATE TABLE IF NOT EXISTS kit_books(
 id TEXT PRIMARY KEY, kit TEXT NOT NULL REFERENCES kits, volume TEXT NOT NULL REFERENCES volumes,
 book TEXT NOT NULL REFERENCES books, removed TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS current_book ON kit_books(book) WHERE removed IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS current_volume ON kit_books(kit,volume) WHERE removed IS NULL;
CREATE TABLE IF NOT EXISTS accessory_lots(
 id TEXT PRIMARY KEY, requirement TEXT NOT NULL REFERENCES requirements, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bridges(
 id TEXT PRIMARY KEY, kit TEXT NOT NULL REFERENCES kits, lot TEXT NOT NULL REFERENCES accessory_lots,
 requirement TEXT NOT NULL REFERENCES requirements, cap INTEGER NOT NULL, granted INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(
 id TEXT PRIMARY KEY, kit TEXT NOT NULL REFERENCES kits, lot TEXT NOT NULL REFERENCES accessory_lots,
 requirement TEXT NOT NULL REFERENCES requirements, qty INTEGER NOT NULL, minute INTEGER NOT NULL,
 bridge TEXT REFERENCES bridges, returned TEXT);
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY, kit TEXT NOT NULL UNIQUE REFERENCES kits, minute INTEGER NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proof_intakes(
 id TEXT PRIMARY KEY, book TEXT NOT NULL REFERENCES books);
CREATE TABLE IF NOT EXISTS proof_segments(
 id TEXT PRIMARY KEY, intake TEXT REFERENCES proof_intakes, book TEXT REFERENCES books,
 ordinal INTEGER NOT NULL, mirrored INTEGER, ruler_event TEXT, version INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'open', conflict TEXT);
CREATE TABLE IF NOT EXISTS proof_rulers(
 id TEXT PRIMARY KEY, segment TEXT NOT NULL REFERENCES proof_segments, mirrored INTEGER,
 serial TEXT, side TEXT, left_page INTEGER, right_page INTEGER,
 agrees INTEGER, conflicts INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS proof_readings(
 id TEXT PRIMARY KEY, intake TEXT, segment TEXT NOT NULL REFERENCES proof_segments, book TEXT REFERENCES books,
 serial TEXT NOT NULL, side TEXT NOT NULL, half TEXT NOT NULL, kind TEXT NOT NULL, defect_key TEXT NOT NULL,
 piece TEXT, position INTEGER, face TEXT, eligible INTEGER NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
 duplicate_of TEXT, source_file TEXT, mirrored INTEGER, located INTEGER NOT NULL,
 page INTEGER, spread TEXT);
CREATE TABLE IF NOT EXISTS proof_handling(
 id TEXT PRIMARY KEY, reading TEXT NOT NULL, segment TEXT, book TEXT,
 decision TEXT NOT NULL, kind TEXT, order_id TEXT, detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS proof_orders(
 id TEXT PRIMARY KEY, event TEXT UNIQUE NOT NULL, book TEXT NOT NULL REFERENCES books,
 segment TEXT NOT NULL REFERENCES proof_segments, reading TEXT NOT NULL, defect_key TEXT NOT NULL,
 kind TEXT NOT NULL, pages TEXT NOT NULL, status TEXT NOT NULL, review TEXT NOT NULL DEFAULT 'ok',
 evidence TEXT NOT NULL, result TEXT, complete_event TEXT, withdraw_event TEXT);
CREATE INDEX IF NOT EXISTS proof_readings_defect ON proof_readings(defect_key);
CREATE INDEX IF NOT EXISTS proof_orders_defect ON proof_orders(defect_key, kind);
CREATE UNIQUE INDEX IF NOT EXISTS proof_orders_unique_valid
 ON proof_orders(defect_key, kind) WHERE status IN ('pending','completed');
"""


class Rejected(ValueError):
    """An operation violated a workshop contract."""


def require(condition, message):
    if not condition:
        raise Rejected(message)


def identity(value):
    require(isinstance(value, str) and bool(value.strip()) and value == value.strip(), "invalid identifier")
    return value


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f"invalid {label}")
    return value


def choice(value, choices, label):
    require(isinstance(value, str) and value in choices, f"invalid {label}")
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


@contextmanager
def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        yield db
    finally:
        db.close()


def one(db, sql, *args):
    return db.execute(sql, args).fetchone()


def entity(db, table, key):
    identity(key)
    row = one(db, f"SELECT * FROM {table} WHERE id=?", key)
    require(row is not None, f"unknown {table}: {key}")
    return row


def ingest(db, records, actions):
    decisions = []
    db.execute("BEGIN IMMEDIATE")
    try:
        for record in records:
            require(isinstance(record, dict), "event must be an object")
            event = identity(record.get("id"))
            action = record.get("op")
            require(isinstance(action, str) and action in actions, "unknown operation")
            body = encode(record)
            old = one(db, "SELECT body FROM events WHERE id=?", event)
            if old:
                require(old["body"] == body, f"event ID conflict: {event}")
                decisions.append({"id": event, "decision": "duplicate"})
                continue
            args = {k: v for k, v in record.items() if k not in ("id", "op")}
            try:
                decision = actions[action](db, event, **args)
            except TypeError as exc:
                raise Rejected(f"invalid arguments for {action}: {exc}") from exc
            db.execute("INSERT INTO events(id,body) VALUES (?,?)", (event, body))
            decisions.append({"id": event, "decision": decision})
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return decisions
