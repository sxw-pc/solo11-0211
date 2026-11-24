"""Run the bindery workbench from a local Python checkout."""
import argparse
from pathlib import Path
import sqlite3
import sys

from . import binding, fulfillment, materials, quality, workbench
from .db import connect, encode, require


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "apply", "demo", "issue-report", "paper-stock", "run-status", "kit-status", "audit",
                 "qc-intake", "qc-recover", "qc-status"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--db", required=True)
        if name == "apply":
            cmd.add_argument("file")
        elif name in {"issue-report", "paper-stock", "run-status", "kit-status"}:
            cmd.add_argument("key")
        if name == "kit-status":
            cmd.add_argument("minute", type=int)
        if name == "qc-intake":
            cmd.add_argument("exports", nargs="+")
        if name in {"qc-recover", "qc-status"}:
            cmd.add_argument("run")
        if name == "qc-recover":
            cmd.add_argument("inbox")
        if name == "qc-status":
            cmd.add_argument("--stations")
        if name == "audit":
            cmd.add_argument("--minute", type=int)
    opts = parser.parse_args(argv)
    try:
        query = opts.command not in {"init", "apply", "demo", "qc-intake", "qc-recover"}
        if query or opts.command in {"qc-intake", "qc-recover"}:
            require(Path(opts.db).is_file(), "database does not exist")
        with connect(opts.db) as db:
            if query and opts.command != "audit":
                db.execute("BEGIN")
            if opts.command == "init":
                output = {"database": "ready"}
            elif opts.command == "apply":
                output = workbench.apply(db, workbench.load(opts.file))
            elif opts.command == "qc-intake":
                output = workbench.intake_quality(db, opts.exports)
            elif opts.command == "qc-recover":
                output = workbench.recover_quality(db, opts.run, opts.inbox)
            elif opts.command == "qc-status":
                stations = quality.load_stations(opts.stations) if opts.stations else ()
                output = quality.report(db, opts.run, stations)
            elif opts.command == "demo":
                output = {"decisions": workbench.demo(db), **workbench.read_state(db)}
            elif opts.command == "issue-report":
                output = materials.issue_report(db, opts.key)
            elif opts.command == "paper-stock":
                output = materials.stock(db, opts.key)
            elif opts.command == "run-status":
                output = binding.run_report(db, opts.key)
            elif opts.command == "kit-status":
                output = fulfillment.kit_report(db, opts.key, opts.minute)
            else:
                output = workbench.audit(db, opts.minute)
        print(encode(output))
        return 1 if opts.command == "audit" and output["drift"] else 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
