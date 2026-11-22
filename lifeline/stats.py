"""Network statistics and guest-candidate queries.

    python -m lifeline.stats --rpc <chain>=<url> [--rpc <chain>=<url> ...] [--heights <file>] [--out <dir>]

A first run pins the heights and writes `data.json`, `table.md` and
`heights.json`. A run with `--heights <file>` reproduces `data.json` at the
saved heights. The command sends read requests only and needs no key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_config, open_readers, parse_rpc
from .queries import QUERIES, QueryError, Scan, missing_key
from .reader import (
    CallReverted,
    ChainMismatch,
    ReaderError,
    Snapshot,
    SnapshotError,
    StateUnavailable,
    first_block_at,
)

# The specification gives its dates in EET. `from` is the first block at or after this time.
WINDOW_START = "2026-11-05T00:00:00+02:00"

DEFAULT_PARAMS = {
    "windowStart": WINDOW_START,
    "windowStartTimestamp": int(datetime.fromisoformat(WINDOW_START).timestamp()),
    # Start value: the largest deposit-to-fill time in specification section 1 row 10.
    "latencyMarginSeconds": 5,
    # "A few runs" of reserve. The fees are samples, not protocol constants (specification F8).
    "guestFewRuns": 3,
    "sampleFeeWei": {"reactive": "12500000000000000", "destination": "700000000000"},
}


def parse_arguments(argv):
    parser = argparse.ArgumentParser(prog="python -m lifeline.stats", description=__doc__.split("\n\n")[0])
    parser.add_argument("--rpc", action="append", metavar="<chain>=<url>",
                        help="RPC URL for a chain; repeat it for more chains or for an ordered list of URLs")
    parser.add_argument("--heights", metavar="<file>", help="reproduce the run at the heights of a saved snapshot")
    parser.add_argument("--out", default="evidence/network", metavar="<dir>", help="output directory")
    parser.add_argument("--config", metavar="<file>", help="configuration file (default: config/network.json)")
    parser.add_argument("--cache", default=".cache/chain", metavar="<dir>", help="cache directory")
    parser.add_argument("--retries", type=int, default=8, help="retries of one request before the run fails")
    parser.add_argument("--backoff", type=float, default=1.0, help="first retry delay in seconds; it doubles")
    return parser.parse_args(argv)


def run_queries(scan):
    """Run each query. Return the data-file records, the table rows and the report lines."""
    records, rows, notes = {}, [], []
    for query in QUERIES:
        missing = missing_key(scan.config, query.requires)
        if missing:
            records[query.name] = {
                "chains": [],
                "definition": query.definition,
                "status": "not_configured",
                "result": {"missing": missing},
            }
            notes.append(f"not configured: {query.name} did not run; {missing} is missing")
            continue
        print(f"query {query.name}", file=sys.stderr)
        before = {name: reader.sent.copy() for name, reader in scan.readers.items()}
        result, query_rows = query.run(scan)
        sent = Counter()
        for name, reader in scan.readers.items():
            sent += reader.sent - before[name]
        used = [chain for chain in scan.chains if any(chain in row_chains for _, _, row_chains in query_rows)]
        records[query.name] = {"chains": used, "definition": query.definition, "status": "ok", "result": result}
        rows += [(query.name, label, value, chains) for label, value, chains in query_rows]
        methods = ", ".join(f"{method} x{count}" for method, count in sorted(sent.items())) or "none (cache)"
        notes.append(f"rpc requests of {query.name}: {methods}")
    return records, rows, notes


def format_heights(snapshot, chains, separator):
    start = separator.join(str(snapshot.chains[chain]["from"]) for chain in chains)
    end = separator.join(str(snapshot.chains[chain]["to"]) for chain in chains)
    return start, end


def render_table(snapshot, chains, records, rows) -> str:
    lines = ["# Network statistics", ""]
    lines += ["| Chain | Chain ID | From | To |", "|---|---|---|---|"]
    for name in chains:
        heights = snapshot.chains[name]
        lines.append(f"| {name} | {heights['chainId']} | {heights['from']} | {heights['to']} |")
    lines += ["", "Parameters: `" + json.dumps(snapshot.params, sort_keys=True) + "`", ""]
    lines += ["| Query | Number | Value | Chain | From | To |", "|---|---|---|---|---|---|"]
    for name, label, value, row_chains in rows:
        start, end = format_heights(snapshot, row_chains, "<br>")
        shown = "n/a" if value is None else value
        lines.append(f"| `{name}` | {label} | {shown} | {'<br>'.join(row_chains)} | {start} | {end} |")
    for name, record in records.items():
        if record["status"] == "not_configured":
            lines.append(f"| `{name}` | not configured: `{record['result']['missing']}` | n/a | | | |")
    lines += ["", "## Definitions", ""]
    lines += [f"- `{name}`: {record['definition']}" for name, record in records.items()]
    return "\n".join(lines) + "\n"


def report(snapshot, chains, rows, notes) -> list:
    lines = []
    for name in chains:
        heights = snapshot.chains[name]
        lines.append(f"heights {name} chainId {heights['chainId']} from {heights['from']} to {heights['to']}")
    for name, label, value, row_chains in rows:
        start, end = format_heights(snapshot, row_chains, ",")
        shown = "n/a" if value is None else value
        lines.append(f"{name} | {','.join(row_chains)} | from {start} to {end} | {label} = {shown}")
    lines += notes
    lines += [f"from snapshot (the node has no state at the old block): {name}" for name in snapshot.from_saved]
    lines += [f"differs from snapshot (the saved value is used): {value}" for value in snapshot.differs]
    return lines


def main(argv=None) -> int:
    arguments = parse_arguments(argv)
    try:
        config = load_config(arguments.config)
        readers = open_readers(config, parse_rpc(arguments.rpc), arguments.cache,
                               retries=arguments.retries, backoff=arguments.backoff)
        if arguments.heights:
            snapshot = Snapshot.load(arguments.heights, readers)
            absent = sorted(DEFAULT_PARAMS.keys() - snapshot.params.keys())
            if absent:
                raise SnapshotError(f"{arguments.heights}: params has no {', '.join(absent)}")
        else:
            start = DEFAULT_PARAMS["windowStartTimestamp"]
            snapshot = Snapshot.pin(readers, lambda reader, to: first_block_at(reader, start, to), DEFAULT_PARAMS)
        scan = Scan(config, readers, snapshot)
        records, rows, notes = run_queries(scan)
    except (ConfigError, ChainMismatch, QueryError, SnapshotError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (ReaderError, StateUnavailable, CallReverted) as error:
        # No number is printed and no file is written. The cache keeps the finished chunks.
        print(f"error: {error}", file=sys.stderr)
        return 1

    out = Path(arguments.out)
    data = {"version": 1, "heights": snapshot.chains, "params": snapshot.params, "queries": records}
    files = {
        "data.json": json.dumps(data, indent=1, sort_keys=True) + "\n",
        "table.md": render_table(snapshot, scan.chains, records, rows),
        "heights.json": snapshot.text(),
    }
    # All three files or none: write each one beside its final name, then move them.
    staged = [out / (name + ".tmp") for name in files]
    try:
        out.mkdir(parents=True, exist_ok=True)
        for path, text in zip(staged, files.values()):
            path.write_text(text)
        for path, name in zip(staged, files):
            os.replace(path, out / name)
    except OSError as error:
        for path in staged:
            if path.is_file():
                path.unlink()
        print(f"error: cannot write the output files: {error}", file=sys.stderr)
        return 1

    print("\n".join(report(snapshot, scan.chains, rows, notes)))
    print(f"wrote {out / 'data.json'}, {out / 'table.md'}, {out / 'heights.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
