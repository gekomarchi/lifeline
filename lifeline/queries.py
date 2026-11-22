"""The network statistics queries.

Each query uses event scans over `from..to` and state reads at `to`, both
through `Scan`. A query returns its result for the data file and a list of
rows `(label, value, chains)`: one row for each number.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

from .events import (
    BLACKLIST,
    CALLBACK_REQUEST,
    CRON_TOPICS,
    DEBT,
    RESERVES,
    SUBSCRIBE,
    WHITELIST,
    address_of,
    ledger_call,
    parse_event,
)

# Spec section 1 row 1. Used only to report the difference from the query result.
SPEC_DROPPED_CALLBACKS = 206
REPEAT_DEBT_EVENTS = 3


class QueryError(Exception):
    """A query cannot give a number from the configured subjects."""


def block_of(log) -> int:
    return int(log["blockNumber"], 16)


class Scan:
    """Log scans and state reads of one run, at the heights of the snapshot. Scans are done one time."""

    def __init__(self, config, readers, snapshot):
        self.config = config
        self.readers = readers
        self.snapshot = snapshot
        chains = config["chains"]
        self.reactive = next(name for name, chain in chains.items() if chain["role"] == "reactive")
        self.destinations = [name for name, chain in chains.items() if chain["role"] == "destination"]
        self.chains = [self.reactive] + self.destinations
        self.destination_of = {chains[name]["chainId"]: name for name in self.destinations}  # by chain ID
        self._logs = {}

    def span(self, chain):
        heights = self.snapshot.chains[chain]
        return heights["from"], heights["to"]

    def ledger(self, chain) -> str:
        return self.config["chains"][chain]["ledger"]

    def logs(self, chain, address, topics) -> list:
        key = (chain, address.lower(), json.dumps(topics).lower())
        if key not in self._logs:
            start, end = self.span(chain)
            print(f"scan {chain} {address} {start}..{end}", file=sys.stderr)
            self._logs[key] = self.readers[chain].logs(address, topics, start, end)
        return self._logs[key]

    def ledger_events(self, chain) -> list:
        """`BlacklistContract` and `WhitelistContract` logs of the ledger."""
        return self.logs(chain, self.ledger(chain), [[BLACKLIST, WHITELIST]])

    def debt_events(self, chain) -> list:
        return [log for log in self.ledger_events(chain) if log["topics"][0] == BLACKLIST]

    def subscribes(self) -> list:
        return self.logs(self.reactive, self.ledger(self.reactive), [SUBSCRIBE])

    def callback_requests(self) -> list:
        return self.logs(self.reactive, self.ledger(self.reactive), [CALLBACK_REQUEST])

    def debts(self, chain, contracts) -> dict:
        return self._ledger_values(chain, "debt", DEBT, contracts)

    def reserves(self, chain, contracts) -> dict:
        return self._ledger_values(chain, "reserves", RESERVES, contracts)

    def timestamps(self, chain, blocks) -> dict:
        return self.readers[chain].timestamps(blocks)

    def _ledger_values(self, chain, getter_name, getter, contracts) -> dict:
        ledger = self.ledger(chain)
        requests = [
            (ledger, ledger_call(getter, contract), f"{getter_name}({contract}) on {chain}")
            for contract in contracts
        ]
        results = self.snapshot.calls(self.readers[chain], requests)
        values = {}
        for contract, result in zip(contracts, results):
            if len(result) != 66:
                raise QueryError(
                    f"{getter_name}({contract}) on {chain} returned {result!r}: "
                    f"check chains.{chain}.ledger"
                )
            values[contract] = int(result, 16)
        return values


def debt_intervals(events) -> list:
    """Debt intervals of one contract from its ledger events in block order.

    Returns (start log, end log) pairs. The start is None for an interval
    that was open at `from`: a `WhitelistContract` with no earlier
    `BlacklistContract` in the range. The end is None for an interval that
    is open at `to`.
    """
    intervals = []
    opened = None
    for log in events:
        if log["topics"][0] == BLACKLIST:
            opened = opened or log
        elif opened:
            intervals.append((opened, log))
            opened = None
        elif not intervals:
            intervals.append((None, log))
    if opened:
        intervals.append((opened, None))
    return intervals


def _events_by_contract(logs) -> dict:
    by_contract = defaultdict(list)
    for log in logs:
        by_contract[address_of(log["topics"][1])].append(log)
    return by_contract


def _number(value):
    """An integer when the value is whole, so that 2.0 and 2 give the same text."""
    return int(value) if float(value).is_integer() else value


def _median(values):
    return _number(statistics.median(values)) if values else None


def _percentile(values, percent):
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


# ------------------------------------------------------------------ queries


def lasna_debt_events(scan):
    count = len(scan.debt_events(scan.reactive))
    return {"count": count}, [("debt events", count, [scan.reactive])]


def destination_debt_events(scan):
    by_chain = {chain: len(scan.debt_events(chain)) for chain in scan.destinations}
    total = sum(by_chain.values())
    rows = [("debt events", count, [chain]) for chain, count in by_chain.items()]
    rows.append(("debt events, all destination chains", total, scan.destinations))
    return {"byChain": by_chain, "total": total}, rows


def stopped_contracts(scan):
    by_chain, rows = {}, []
    for chain in scan.chains:
        cohort = sorted(_events_by_contract(scan.debt_events(chain)))
        debts = scan.debts(chain, cohort)
        stopped = [contract for contract in cohort if debts[contract] > 0]
        by_chain[chain] = {"cohort": len(cohort), "stopped": len(stopped), "contracts": stopped}
        rows.append(("stopped contracts", len(stopped), [chain]))
        rows.append(("cohort: contracts with a debt event", len(cohort), [chain]))
    return {"byChain": by_chain}, rows


def cron_stopped_share(scan):
    subscribers, cron = set(), set()
    for log in scan.subscribes():
        subscriber = address_of(log["topics"][1])
        subscribers.add(subscriber)
        if log["data"][:66].lower() in CRON_TOPICS:  # the first data word is topic0 of the subscription
            cron.add(subscriber)
    debts = scan.debts(scan.reactive, sorted(subscribers))

    def group(contracts):
        stopped = sum(1 for contract in contracts if debts[contract] > 0)
        share = round(stopped / len(contracts), 4) if contracts else None
        return {"contracts": len(contracts), "stopped": stopped, "share": share}

    result = {"cron": group(cron), "others": group(subscribers - cron)}
    rows = []
    for label, values in (("cron-subscribed", result["cron"]), ("other subscribed", result["others"])):
        rows.append((f"{label} contracts", values["contracts"], [scan.reactive]))
        rows.append((f"{label} contracts, stopped", values["stopped"], [scan.reactive]))
        rows.append((f"{label} contracts, stopped share", values["share"], [scan.reactive]))
    return result, rows


def repeat_debtors(scan):
    by_chain, rows = {}, []
    for chain in scan.chains:
        counts = Counter(address_of(log["topics"][1]) for log in scan.debt_events(chain))
        repeat = sorted(contract for contract, count in counts.items() if count >= REPEAT_DEBT_EVENTS)
        largest = max(counts.values(), default=0)
        by_chain[chain] = {
            "contracts": len(repeat),
            "largest": largest,
            "largestContract": min((c for c, n in counts.items() if n == largest), default=None),
        }
        rows.append((f"contracts with {REPEAT_DEBT_EVENTS} or more debt events", len(repeat), [chain]))
        rows.append(("largest number of debt events of one contract", largest, [chain]))
    return {"minimumDebtEvents": REPEAT_DEBT_EVENTS, "byChain": by_chain}, rows


def dropped_callbacks(scan):
    """A join of logs. It sends log queries and block-header reads only."""
    margin = scan.snapshot.params["latencyMarginSeconds"]
    events = {chain: _events_by_contract(scan.ledger_events(chain)) for chain in scan.destinations}
    requests = scan.callback_requests()

    counts = {chain: Counter() for chain in scan.destinations}
    not_scanned = Counter()
    matched = []  # requests whose recipient has a ledger event on the destination chain
    for log in requests:
        chain_id = int(log["topics"][1], 16)
        chain = scan.destination_of.get(chain_id)
        if chain is None:
            not_scanned[chain_id] += 1
            continue
        counts[chain]["requests"] += 1
        recipient = address_of(log["topics"][3])
        if recipient in events[chain]:
            matched.append((chain, recipient, log))
        else:
            counts[chain]["notInDebt"] += 1

    # Block headers are read only for the blocks of matched logs.
    request_time = scan.timestamps(scan.reactive, [block_of(log) for _, _, log in matched])
    edge_time = {
        chain: scan.timestamps(chain, [
            block_of(event)
            for recipient in {r for c, r, _ in matched if c == chain}
            for event in events[chain][recipient]
        ])
        for chain in scan.destinations
    }

    delays = []
    start_before_from = 0
    for chain, recipient, log in matched:
        time = request_time[block_of(log)]
        intervals = [
            (start and edge_time[chain][block_of(start)], end and edge_time[chain][block_of(end)])
            for start, end in debt_intervals(events[chain][recipient])
        ]
        edges = [edge for interval in intervals for edge in interval if edge is not None]
        inside = [start for start, end in intervals
                  if (start is None or start < time) and (end is None or time < end)]
        if any(abs(time - edge) <= margin for edge in edges):
            counts[chain]["uncertain"] += 1
        elif inside:
            counts[chain]["dropped"] += 1
            if inside[0] is None:
                start_before_from += 1
            else:
                delays.append(time - inside[0])
        else:
            counts[chain]["notInDebt"] += 1

    def total(key):
        return sum(counts[chain][key] for chain in scan.destinations)

    dropped = total("dropped")
    result = {
        "derivation": (
            "Derived from debt intervals on the destination proxy and the proxy rule (InDebt). "
            "It is not a count of observed reverted transactions."
        ),
        "limit": (
            "An interval that opened before `from` and did not close in the range is not visible. "
            "An interval that is open at `to` of the destination chain counts as open for each later request "
            "in the Lasna range; the heights of all chains are pinned at the same time."
        ),
        "latencyMarginSeconds": margin,
        "requests": len(requests),
        "dropped": dropped,
        "droppedInIntervalOpenAtFrom": start_before_from,
        "uncertain": total("uncertain"),
        "notInDebt": total("notInDebt"),
        "notScanned": {
            "requests": sum(not_scanned.values()),
            "byChainId": {str(chain_id): not_scanned[chain_id] for chain_id in sorted(not_scanned)},
        },
        "byChain": {
            chain: {key: counts[chain][key] for key in ("requests", "dropped", "uncertain", "notInDebt")}
            for chain in scan.destinations
        },
        "delaySeconds": {
            "count": len(delays),
            "smallest": min(delays, default=None),
            "within60": sum(1 for delay in delays if delay <= 60),
            "median": _median(delays),
            "below10800": sum(1 for delay in delays if delay < 10800),
        },
        "specificationFigure": SPEC_DROPPED_CALLBACKS,
        "differenceFromSpecification": dropped - SPEC_DROPPED_CALLBACKS,
    }
    everywhere = scan.chains
    delay = result["delaySeconds"]
    rows = [("dropped callbacks (derived from debt intervals and InDebt)", dropped, everywhere)]
    rows += [("dropped callbacks", counts[chain]["dropped"], [scan.reactive, chain]) for chain in scan.destinations]
    rows += [
        ("smallest delay from debt event to request, seconds", delay["smallest"], everywhere),
        ("requests within 60 s of the debt event", delay["within60"], everywhere),
        ("median delay, seconds", delay["median"], everywhere),
        ("requests sooner than 10800 s after the debt event", delay["below10800"], everywhere),
        (f"uncertain: within {margin} s of an interval edge", result["uncertain"], everywhere),
        ("not scanned: destination chain is not configured", result["notScanned"]["requests"], [scan.reactive]),
        (f"difference from the specification figure {SPEC_DROPPED_CALLBACKS}",
         result["differenceFromSpecification"], everywhere),
    ]
    return result, rows


def fastfill_fill_times(scan):
    subject = scan.config["subjects"]["fastfill"]
    join = subject["joinField"]

    def side(name):
        entry = subject[name]
        try:
            event = parse_event(entry["event"])
            position = event.topic_index(join)
            topics = event.filter(entry.get("where", {}))
        except ValueError as error:
            raise QueryError(f"subjects.fastfill.{name}: {error}") from None
        logs = scan.logs(entry["chain"], entry["address"], topics)
        return entry["chain"], logs, position

    deposit_chain, deposit_logs, deposit_position = side("deposit")
    fill_chain, fill_logs, fill_position = side("fill")

    deposits = {}
    for log in deposit_logs:
        deposits.setdefault(log["topics"][deposit_position], log)
    pairs = [(deposits[log["topics"][fill_position]], log)
             for log in fill_logs if log["topics"][fill_position] in deposits]
    deposit_time = scan.timestamps(deposit_chain, [block_of(deposit) for deposit, _ in pairs])
    fill_time = scan.timestamps(fill_chain, [block_of(fill) for _, fill in pairs])
    seconds = [fill_time[block_of(fill)] - deposit_time[block_of(deposit)] for deposit, fill in pairs]

    result = {
        "joinField": join,
        "depositLogs": len(deposit_logs),
        "fillLogs": len(fill_logs),
        "fills": len(seconds),
        "medianSeconds": _median(seconds),
        "p90Seconds": _percentile(seconds, 90),
        "maximumSeconds": max(seconds, default=None),
    }
    chains = [deposit_chain, fill_chain]
    rows = [
        ("fills", result["fills"], chains),
        ("deposit to fill, median seconds", result["medianSeconds"], chains),
        ("deposit to fill, 90th percentile seconds", result["p90Seconds"], chains),
        ("deposit to fill, maximum seconds", result["maximumSeconds"], chains),
    ]
    return result, rows


def cron_poller_ward_dead_time(scan):
    subject = scan.config["subjects"]["cronPoller"]
    chain, ward = subject["chain"], subject["ward"].lower()
    start_block, end_block = scan.span(chain)
    intervals = [
        (block_of(start) if start else start_block, block_of(end) if end else end_block, start is None, end is None)
        for start, end in debt_intervals(_events_by_contract(scan.ledger_events(chain)).get(ward, []))
    ]
    times = scan.timestamps(chain, [block for interval in intervals for block in interval[:2]])
    longest = max(intervals, key=lambda i: (times[i[1]] - times[i[0]], -i[0]), default=None)
    result = {
        "poller": subject.get("address"),
        "ward": ward,
        "intervals": len(intervals),
        "longestSeconds": times[longest[1]] - times[longest[0]] if longest else 0,
        "longestBlocks": longest[1] - longest[0] if longest else 0,
        "longest": longest and {
            "fromBlock": longest[0],
            "toBlock": longest[1],
            "openAtFrom": longest[2],
            "openAtTo": longest[3],
        },
    }
    rows = [
        ("longest debt interval of the ward, seconds", result["longestSeconds"], [chain]),
        ("longest debt interval of the ward, blocks", result["longestBlocks"], [chain]),
    ]
    return result, rows


def fastfill_reactive_reserves(scan):
    subject = scan.config["subjects"]["fastfillReactive"]
    chain, contract = subject["chain"], subject["address"].lower()
    reserves = scan.reserves(chain, [contract])[contract]
    result = {"contract": contract, "reservesWei": str(reserves), "block": scan.span(chain)[1]}
    return result, [("reserves of FastfillReactive, wei", str(reserves), [chain])]


def guest_candidates(scan):
    params = scan.snapshot.params
    recipients = defaultdict(set)
    for log in scan.callback_requests():
        chain = scan.destination_of.get(int(log["topics"][1], 16))
        if chain:
            recipients[chain].add(address_of(log["topics"][3]))
    known = dict(recipients)
    known[scan.reactive] = {address_of(log["topics"][1]) for log in scan.subscribes()}

    candidates, thresholds, rows = [], {}, []
    for chain in scan.chains:
        role = scan.config["chains"][chain]["role"]
        threshold = params["guestFewRuns"] * int(params["sampleFeeWei"][role])
        thresholds[chain] = str(threshold)
        debt_events = _events_by_contract(scan.debt_events(chain))
        contracts = sorted(set(debt_events) | known.get(chain, set()))
        debts = scan.debts(chain, contracts)
        reserves = scan.reserves(chain, contracts)
        entries = []
        for contract in contracts:
            events = debt_events.get(contract, [])
            reasons = (
                ["debt_event"] * bool(events)
                + ["in_debt_at_to"] * (debts[contract] > 0)
                + ["low_reserve"] * (reserves[contract] < threshold)
            )
            if not reasons:
                continue
            entry = {"chain": chain, "address": contract, "reasons": reasons, "debtEvents": len(events)}
            if events:
                entry["lastDebtTx"] = events[-1]["transactionHash"]
                entry["lastDebtBlock"] = block_of(events[-1])
            entry.update(debtWei=str(debts[contract]), reservesWei=str(reserves[contract]),
                         readBlock=scan.span(chain)[1])
            entries.append(entry)
        entries.sort(key=lambda entry: (-entry["debtEvents"], entry["address"]))
        candidates += entries
        in_debt = sum(1 for entry in entries if entry["reasons"] != ["low_reserve"])
        rows.append(("guest candidates", len(entries), [chain]))
        rows.append(("guest candidates with a debt event or debt at to", in_debt, [chain]))
        rows.append(("guest candidates with low reserve only", len(entries) - in_debt, [chain]))
    result = {
        "sampleFeeNote": "The sample fees are samples, not protocol constants (specification F8).",
        "fewRuns": params["guestFewRuns"],
        "lowReserveThresholdWei": thresholds,
        "count": len(candidates),
        "candidates": candidates,
    }
    return result, rows


@dataclass(frozen=True)
class Query:
    name: str
    run: callable
    definition: str
    requires: tuple = ()  # configuration keys, for example subjects.fastfill.joinField


QUERIES = (
    Query("lasna_debt_events", lasna_debt_events,
          "A debt event is a BlacklistContract log of the Lasna system contract in from..to; "
          "the ledger emits it only when a debt goes from zero to positive."),
    Query("destination_debt_events", destination_debt_events,
          "A debt event is a BlacklistContract log of the callback proxy of a configured "
          "destination chain in from..to."),
    Query("stopped_contracts", stopped_contracts,
          "A contract is stopped if its ledger emitted BlacklistContract for it in from..to and "
          "debt(contract) is above zero at to; the cohort is each contract with a debt event in from..to."),
    Query("cron_stopped_share", cron_stopped_share,
          "Among the contracts with a Subscribe log on Lasna in from..to, a contract is cron-subscribed "
          "if one of these logs has a cron topic (Cron1 to Cron10000) as topic0, and a contract is stopped "
          "if debt(contract) is above zero at to."),
    Query("repeat_debtors", repeat_debtors,
          f"A repeat debtor is a contract with {REPEAT_DEBT_EVENTS} or more debt events on one chain in from..to."),
    Query("dropped_callbacks", dropped_callbacks,
          "A CallbackRequest log on Lasna is counted as dropped if, at the block timestamp of the request, "
          "its recipient has an open debt interval on the destination proxy (from BlacklistContract(recipient) "
          "to the next WhitelistContract(recipient), or to `to`); the number is derived from debt intervals "
          "and the proxy rule (InDebt), not from observed reverted transactions, and a request within the "
          "latency margin of an interval edge is counted as uncertain."),
    Query("fastfill_fill_times", fastfill_fill_times,
          "A fill is a fill log whose join field is equal to the join field of a deposit log in from..to; "
          "its time is the fill block timestamp minus the deposit block timestamp, and the 90th percentile "
          "is the nearest-rank value.",
          ("subjects.fastfill.deposit.chain", "subjects.fastfill.deposit.address",
           "subjects.fastfill.deposit.event", "subjects.fastfill.fill.chain",
           "subjects.fastfill.fill.address", "subjects.fastfill.fill.event",
           "subjects.fastfill.joinField")),
    Query("cron_poller_ward_dead_time", cron_poller_ward_dead_time,
          "The dead time is the longest debt interval of the ward in from..to: from BlacklistContract(ward), "
          "or from `from` if the debt was open then, to the next WhitelistContract(ward), or to `to`.",
          ("subjects.cronPoller.chain", "subjects.cronPoller.ward")),
    Query("fastfill_reactive_reserves", fastfill_reactive_reserves,
          "The value is reserves(FastfillReactive) on the ledger of its chain at to.",
          ("subjects.fastfillReactive.chain", "subjects.fastfillReactive.address")),
    Query("guest_candidates", guest_candidates,
          "A guest candidate is a contract seen in the scan (a debtor, a subscriber on Lasna, or a callback "
          "recipient on a destination chain) that has a debt event in from..to, has debt above zero at to, "
          "or holds reserves below a few runs of the sample fee at to."),
)


def missing_key(config, keys):
    """The first required configuration key that has no value, or None."""
    for key in keys:
        value = config
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if not value or (isinstance(value, str) and "…" in value):
            return key
    return None
