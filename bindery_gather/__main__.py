"""Run the bindery workbench from a local Python checkout."""
import argparse
from pathlib import Path
import sqlite3
import sys

from . import binding, fulfillment, materials, workbench
from .db import connect, encode, require


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "apply", "demo", "issue-report", "paper-stock", "run-status", "kit-status", "audit"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--db", required=True)
        if name == "apply":
            cmd.add_argument("file")
        elif name in {"issue-report", "paper-stock", "run-status", "kit-status"}:
            cmd.add_argument("key")
        if name == "kit-status":
            cmd.add_argument("minute", type=int)
        if name == "audit":
            cmd.add_argument("--minute", type=int)
    opts = parser.parse_args(argv)
    try:
        query = opts.command not in {"init", "apply", "demo"}
        if query:
            require(Path(opts.db).is_file(), "database does not exist")
        with connect(opts.db) as db:
            if query and opts.command != "audit":
                db.execute("BEGIN")
            if opts.command == "init":
                output = {"database": "ready"}
            elif opts.command == "apply":
                output = workbench.apply(db, workbench.load(opts.file))
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
