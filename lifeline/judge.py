"""Evidence generator: `JUDGE.md` and its data file from Lifeline's own events.

    python -m lifeline.judge --rpc <chain>=<url> ... [--heights <file>] [--manifest <file>] [--out <dir>]
                             [--judge <file>]

The command joins each relay request of the hub (`Relayed` on Lasna) with the result of the vault (`Result` on
the destination chain), reports the builder's fleet since enrollment, the cost of the guardian, the quarantine
and the junk-recovery evidence, and the guests. It includes the table and the data file of the experiment
unchanged. It needs no key and sends read requests only.

A first run pins the heights and writes `JUDGE.md`, `data.json` and `heights.json`. A run with
`--heights <file>` gives the same `JUDGE.md` and `data.json`, byte for byte. Each log query starts at the
deployment block of the Lifeline contract of its chain. `JUDGE.md` is generated: do not edit it by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import experiment, ops
from .config import ConfigError, load_config, open_readers, parse_rpc
from .events import BLACKLIST, DEBT, RESERVES, WHITELIST, ledger_call, parse_event, word
from .ops import LASNA, OUTCOMES, Manifest, OpsError, block_of, checksum, same_address
from .queries import debt_intervals, missing_key
from .reader import (
    CallReverted,
    ChainMismatch,
    ReaderError,
    Snapshot,
    SnapshotError,
    StateUnavailable,
    _log_position,
    first_block_at,
)
from .stats import publish_paths

DEFAULT_OUT = ops.ROOT / "evidence" / "judge"
DEFAULT_JUDGE = ops.ROOT / "JUDGE.md"
DEFAULT_CACHE = ".cache/chain"

CLAIM_LIMIT = "restores future delivery, not zero loss"
NO_GUEST = "No guest enrolled."
NOT_RECORDED = "not recorded"
NOT_CONFIGURED = "not configured"
# The table of the experiment stands between these two lines of `JUDGE.md`, unchanged.
TABLE_BEGIN = "<!-- begin: evidence/experiment/table.md, unchanged -->"
TABLE_END = "<!-- end: evidence/experiment/table.md -->"

HUB_EVENTS = ("Enrolled", "Rescued", "ToppedUp", "Gap", "Quarantined", "SelfCare", "Relayed", "VaultDown",
              "Pending", "VaultUp")
VAULT_EVENTS = ("Result", "PolicySet", "SelfCare")
FILL_KEYS = tuple(f"subjects.fastfill.fill.{key}" for key in ("chain", "address", "event"))
# The steps of the junk record that the chain must confirm: (name in `data.json`, name in the record).
JUNK_STEPS = (
    ("BlacklistContract(vault)", "BlacklistContract(vault)"),
    ("VaultDown", "VaultDown"),
    ("Pending", "Pending"),
    ("WhitelistContract(vault)", "recovery deposit"),
    ("VaultUp", "VaultUp"),
    ("Result(waiting.callback)", "Result(waiting.callback)"),
)


def _ref(log: dict) -> dict:
    """The block and the transaction hash of a log."""
    return {"block": block_of(log), "tx": log["transactionHash"]}


def deploy_blocks(manifest: dict) -> dict:
    """The deployment block of the Lifeline contract of each chain: the hub on Lasna, a vault on a
    destination chain. No log query of that chain starts before it."""
    hub = manifest["contracts"].get("hub")
    if not (hub and hub.get("address") and hub.get("deployBlock")):
        raise OpsError("the manifest has no hub with an address and a deployBlock")
    blocks = {LASNA: int(hub["deployBlock"])}
    for chain, vault in manifest["contracts"]["vaults"].items():
        if vault and vault.get("address") and vault.get("deployBlock"):
            blocks[chain] = int(vault["deployBlock"])
    return blocks


class Evidence:
    """The evidence at the heights of one snapshot. Event scans and state reads go through the reader and
    the snapshot of the network statistics."""

    def __init__(self, manifest: dict, config: dict, scenarios: dict, experiment_data: dict, readers: dict,
                 snapshot: Snapshot):
        self.manifest, self.config, self.scenarios = manifest, config, scenarios
        self.experiment, self.readers, self.snapshot = experiment_data, readers, snapshot
        self.hub = manifest["contracts"]["hub"]["address"]
        self.vaults = {chain: manifest["contracts"]["vaults"][chain]["address"]
                       for chain in readers if chain != LASNA}
        self.chain_of = {int(manifest["contracts"]["vaults"][chain]["chainId"]): chain for chain in self.vaults}
        self.hub_abi = ops.load_artifact("LifelineHub").abi
        self.vault_abi = ops.load_artifact("LifelineVault").abi
        self.notes = []  # lines for standard output: what the run left out, and why
        self._logs, self._events = {}, {}

    # Reads ------------------------------------------------------------------------------------

    def to(self, chain: str) -> int:
        return self.snapshot.chains[chain]["to"]

    def ledger(self, chain: str) -> str:
        return self.manifest["chains"][chain]["ledger"]

    def logs(self, chain: str, address: str, topics: list) -> list:
        """The logs of one emitter in `from..to`. `from` is the deployment block of the Lifeline contract of
        the chain, so no query starts before it."""
        key = (chain, address.lower(), json.dumps(topics).lower())
        if key not in self._logs:
            heights = self.snapshot.chains[chain]
            self._logs[key] = self.readers[chain].logs(address, topics, heights["from"], heights["to"])
        return self._logs[key]

    def _decoded(self, chain: str, address: str, abi: ops.Abi, names: tuple) -> dict:
        """{event name: [(log, parameters)]} of one Lifeline contract: one scan for each event that the run
        uses."""
        key = (chain, address.lower())
        if key not in self._events:
            name_of = {abi.topic(name): name for name in names}
            events = {name: [] for name in names}
            for log in self.logs(chain, address, [sorted(name_of)]):
                name = name_of[log["topics"][0]]
                events[name].append((log, abi.decode_event(name, log)))
            self._events[key] = events
        return self._events[key]

    def hub_events(self, name: str) -> list:
        return self._decoded(LASNA, self.hub, self.hub_abi, HUB_EVENTS)[name]

    def vault_events(self, chain: str, name: str) -> list:
        return self._decoded(chain, self.vaults[chain], self.vault_abi, VAULT_EVENTS)[name]

    def ledger_events(self, chain: str, account: str) -> list:
        """The `BlacklistContract` and `WhitelistContract` logs of the ledger of a chain for one account."""
        return self.logs(chain, self.ledger(chain), [[BLACKLIST, WHITELIST], word(account)])

    def hub_payments(self, target: str) -> list:
        """(log, paid) of each `Rescued` and each `ToppedUp` of the hub for one target, in log order."""
        logs = [(log, event["paid"]) for name in ("Rescued", "ToppedUp") for log, event in self.hub_events(name)
                if same_address(event["target"], target)]
        return sorted(logs, key=lambda entry: _log_position(entry[0]))

    def results(self, chain: str, target: str) -> list:
        """(log, parameters) of each `Result` of the vault of a chain for one target."""
        return [(log, event) for log, event in self.vault_events(chain, "Result")
                if same_address(event["target"], target)]

    def state(self, chain: str, account: str, label: str) -> dict:
        """The reserves and the debt of an account at the ledger of its chain, at `to`."""
        ledger = self.ledger(chain)
        reserves, debt = self.snapshot.calls(self.readers[chain], [
            (ledger, ledger_call(RESERVES, account), f"reserves({label}) on {chain}"),
            (ledger, ledger_call(DEBT, account), f"debt({label}) on {chain}"),
        ])
        return {"reserves": self._at(chain, int(reserves, 16)), "debt": self._at(chain, int(debt, 16))}

    def _at(self, chain: str, value: int) -> dict:
        return {"block": self.to(chain), "value": str(value)}

    # Join -------------------------------------------------------------------------------------

    def rescues(self) -> list:
        """One entry for each `Relayed` of the hub, joined with the `Result` of the vault that has the same
        id and target."""
        entries = []
        for log, event in self.hub_events("Relayed"):
            chain, target = self.chain_of.get(event["chainId"]), event["target"]
            entry = {
                "requestId": str(event["requestId"]), "chain": chain, "chainId": event["chainId"],
                "target": checksum(target), "status": "open", "debtEvent": None, "relayed": _ref(log),
                "result": None, "whitelist": None,
            }
            entries.append(entry)
            if chain is None:
                self.notes.append(f"the manifest has no vault for chain ID {event['chainId']} of the relay "
                                  f"request {event['requestId']}: it is open")
                continue
            result = next(((result_log, values) for result_log, values in self.results(chain, target)
                           if values["id"] == event["requestId"]), None)
            ledger = self.ledger_events(chain, target)
            # The debt event: the last one before the result, or before `to` for an open entry.
            before = [debt for debt in ledger if debt["topics"][0] == BLACKLIST
                      and (result is None or _log_position(debt) < _log_position(result[0]))]
            if before:
                entry["debtEvent"] = _ref(before[-1])
            if result is None:
                continue
            result_log, values = result
            outcome = OUTCOMES[values["outcome"]]
            entry["status"] = "joined"
            entry["result"] = {**_ref(result_log), "outcome": outcome, "paid": str(values["paid"])}
            if outcome == "Rescued":
                # The payment of the vault and the `WhitelistContract` of the proxy are one transaction.
                cleared = [entry_log for entry_log in ledger if entry_log["topics"][0] == WHITELIST
                           and entry_log["transactionHash"] == result_log["transactionHash"]]
                if cleared:
                    entry["whitelist"] = _ref(cleared[0])
                else:
                    self.notes.append(f"the transaction of the Result of the relay request "
                                      f"{event['requestId']} has no WhitelistContract of its target")
        return entries

    def lasna_rescues(self) -> list:
        """Each `Rescued` of the hub with the `Gap` of the same transaction."""
        gaps = {}
        for log, event in self.hub_events("Gap"):
            gaps.setdefault((log["transactionHash"], event["target"]), []).append(event)
        entries = []
        for log, event in self.hub_events("Rescued"):
            waiting = gaps.get((log["transactionHash"], event["target"]))
            gap = waiting.pop(0) if waiting else None
            entries.append({
                "target": checksum(event["target"]),
                "rescued": {**_ref(log), "debt": str(event["debt"]), "paid": str(event["paid"])},
                "gap": gap and {"deadFromBlock": gap["deadFromBlock"], "aliveAtBlock": gap["aliveAtBlock"]},
            })
        return entries

    # Fleet ------------------------------------------------------------------------------------

    def enrollment_log(self, record: dict):
        """The log of the target's own chain with which its guardian took the target: `Enrolled` of the hub
        for a Lasna target (`hub.enroll`), `PolicySet` of the vault for a destination target
        (`vault.setPolicy`). Its transaction is the one of the manifest, and it gave the policy to the
        guardian of that chain. None if the heights do not have that transaction."""
        txs, chain, target = record.get("txs") or {}, record["chain"], record["target"]
        if chain == LASNA:
            logs = [log for log, event in self.hub_events("Enrolled")
                    if log["transactionHash"] == txs.get("enroll") and same_address(event["target"], target)]
        elif chain in self.vaults:
            logs = [log for log, event in self.vault_events(chain, "PolicySet")
                    if log["transactionHash"] == txs.get("setPolicy") and same_address(event["target"], target)]
        else:
            logs = []
        return logs[0] if logs else None

    def fill_logs(self):
        """(chain, address, logs) of the fills of the fleet, as `config/network.json` defines them. None if
        the subject is not configured."""
        missing = missing_key(self.config, FILL_KEYS)
        if missing:
            self.notes.append(f"{NOT_CONFIGURED}: fills; {missing} is missing")
            return None
        fill = self.config["subjects"]["fastfill"]["fill"]
        if fill["chain"] not in self.readers:
            self.notes.append(f"{NOT_CONFIGURED}: fills; the manifest has no Lifeline contract on "
                              f"{fill['chain']}, the chain of subjects.fastfill.fill")
            return None
        try:
            topics = parse_event(fill["event"]).filter(fill.get("where", {}))
        except ValueError as error:
            raise ConfigError(f"subjects.fastfill.fill: {error}") from None
        return fill["chain"], fill["address"], self.logs(fill["chain"], fill["address"], topics)

    def fills(self, subject, chain: str, enroll_block: int) -> tuple:
        """(count, the block of the fill chain that the count starts at) for a target that is enrolled since
        `enroll_block` of its own chain."""
        if subject is None:
            return None, None
        fill_chain, address, logs = subject
        start = enroll_block
        if chain != fill_chain:
            # The first block of the fill chain at or after the enrollment, by the block timestamps.
            time = self.readers[chain].timestamp(enroll_block)
            reader, head = self.readers[fill_chain], self.to(fill_chain)
            start = first_block_at(reader, time, head) if reader.timestamp(head) >= time else None
        if start is not None:
            start = max(start, self.snapshot.chains[fill_chain]["from"])
        count = sum(1 for log in logs if start is not None and block_of(log) >= start)
        return count, {"chain": fill_chain, "address": checksum(address), "block": start}

    def fleet(self) -> list:
        """Each enrollment of the manifest that is not a guest and not a fixture of the experiment."""
        fixtures = {(entry["chain"], entry["address"].lower()) for entry in self.manifest["fixtures"].values()
                    if isinstance(entry, dict) and entry.get("address")}
        subject, entries = self.fill_logs(), []
        for record in self.manifest["enrollments"]:
            chain, target, label = record["chain"], record["target"], record["label"]
            if record.get("guest") or (chain, target.lower()) in fixtures:
                continue
            enrolled = self.enrollment_log(record)
            if enrolled is None:
                self.notes.append(f"the enrollment of {label} ({target} on {chain}) is not in the chain data of "
                                  f"the heights: it is not in the fleet report")
                continue
            start = block_of(enrolled)
            if chain == LASNA:
                rescued = [(log, event) for log, event in self.hub_events("Rescued")
                           if same_address(event["target"], target) and block_of(log) >= start]
                topped_up = [log for log, event in self.hub_events("ToppedUp")
                             if same_address(event["target"], target) and block_of(log) >= start]
                paid = sum(amount for log, amount in self.hub_payments(target) if block_of(log) >= start)
                rescues, top_ups = len(rescued), len(topped_up)
                gaps = [self._gap(event["deadFromBlock"] or None, event["aliveAtBlock"], log["transactionHash"],
                                  chain)
                        for log, event in self.hub_events("Gap")
                        if same_address(event["target"], target) and block_of(log) >= start]
            else:
                results = [(log, event) for log, event in self.results(chain, target) if block_of(log) >= start]
                rescues = sum(1 for _, event in results if OUTCOMES[event["outcome"]] == "Rescued")
                top_ups = sum(1 for _, event in results if OUTCOMES[event["outcome"]] == "ToppedUp")
                paid = sum(event["paid"] for _, event in results)
                gaps = [self._gap(opened and block_of(opened), closed and block_of(closed),
                                  closed and closed["transactionHash"], chain)
                        for opened, closed in debt_intervals(self.ledger_events(chain, target))
                        if closed is None or block_of(closed) >= start]
            count, fills_from = self.fills(subject, chain, start)
            entries.append({
                "label": label, "chain": chain, "chainId": record["chainId"], "target": checksum(target),
                "enrollBlock": start, "enrollTx": enrolled["transactionHash"], "removed": self._removal(record),
                "marks": {"low": str(record["policy"]["low"]), "high": str(record["policy"]["high"])},
                "fills": count, "fillsFrom": fills_from,
                "rescues": rescues, "topUps": top_ups, "gaps": gaps, "principalPaid": str(paid),
                "reserves": self.state(chain, target, label)["reserves"],
            })
        return entries

    @staticmethod
    def _removal(record: dict):
        """The hashes of the removal of an enrollment, or None for an enrollment that is not removed."""
        removed = record.get("removed")
        return removed and {name: removed.get(name) for name in ("hubDisarm", "vaultDisarm")}

    def _gap(self, dead, alive, tx, chain: str) -> dict:
        """One gap of a target. `dead` is None if no debt log of the heights started it, `alive` is None if
        the target is in debt at `to`. `blocks` counts to `to` for a gap that is open."""
        blocks = None if dead is None else (self.to(chain) if alive is None else alive) - dead
        return {"deadFromBlock": dead, "aliveAtBlock": alive, "blocks": blocks, "tx": tx}

    # Guardian ---------------------------------------------------------------------------------

    def _guardian(self, chain: str, address: str, label: str, paid: int, self_care: list) -> dict:
        added = sum(event["added"] for _, event in self_care)
        state = self.state(chain, address, label)
        balance = self.snapshot.balance(self.readers[chain], address, f"balance of {label} on {chain}")
        consumed = added - int(state["reserves"]["value"]) + int(state["debt"]["value"])
        return {
            "chain": chain, "address": checksum(address), "deployBlock": self.snapshot.chains[chain]["from"],
            "principalPaid": str(paid), "selfCareAdded": str(added), "floatConsumed": str(consumed),
            "balance": self._at(chain, balance), "reserves": state["reserves"], "debt": state["debt"],
        }

    def guardian(self) -> dict:
        """The cost of the hub and of each vault since deployment."""
        hub_paid = sum(event["paid"] for name in ("Rescued", "ToppedUp") for _, event in self.hub_events(name))
        vaults = {}
        for chain, vault in self.vaults.items():
            paid = sum(event["paid"] for _, event in self.vault_events(chain, "Result"))
            vaults[chain] = self._guardian(chain, vault, f"vault of {chain}", paid,
                                           self.vault_events(chain, "SelfCare"))
        return {"hub": self._guardian(LASNA, self.hub, "hub", hub_paid, self.hub_events("SelfCare")),
                "vaults": vaults}

    # Scenarios --------------------------------------------------------------------------------

    def _confirmed(self, scenario: str, record: dict, step: str, logs: list) -> dict:
        """The log of the chain that has the transaction of one step of a scenario record."""
        entry = experiment.find_step(record, step)
        log = next((log for log in logs if log["transactionHash"] == entry["tx"]), None)
        if log is None:
            raise OpsError(f"the chain data of the heights does not have the step {step!r} of the {scenario} "
                           f"record (tx {entry['tx']}, block {entry['block']} of {entry['chain']})")
        return log

    def quarantine(self) -> dict:
        """The quarantine scenario: the record of the driver, confirmed on the chain."""
        empty = {"recorded": False, "chain": None, "target": None, "rescues": [], "quarantined": None,
                 "maxRescues": None, "dailyCap": None, "policyTx": None, "principalPaid": None,
                 "paidAfterQuarantine": None}
        record = self.scenarios.get("quarantine")
        if not isinstance(record, dict) or not experiment.find_step(record, "Quarantined"):
            if isinstance(record, dict):
                self.notes.append(f"{NOT_RECORDED}: quarantine; the scenario record has no Quarantined step")
            return empty
        if record["chain"] != LASNA:
            raise OpsError(f"the quarantine record is of {record['chain']}: the hub quarantines a target of "
                           f"{LASNA} only")
        target = record["addresses"]["looping"]
        quarantined = self._confirmed("quarantine", record, "Quarantined", [
            log for log, event in self.hub_events("Quarantined") if same_address(event["target"], target)])
        rescued = [log for log, event in self.hub_events("Rescued") if same_address(event["target"], target)]
        for entry in record["steps"]:
            if entry["step"].startswith("Rescued"):
                self._confirmed("quarantine", record, entry["step"], rescued)
        cut = _log_position(quarantined)
        payments = self.hub_payments(target)
        enrollment = next((entry for entry in reversed(self.manifest["enrollments"])
                           if entry["chain"] == LASNA and same_address(entry["target"], target)), None)
        policy = enrollment["policy"] if enrollment else {}
        enrolled = enrollment and self.enrollment_log(enrollment)
        return {
            "recorded": True, "chain": LASNA, "target": checksum(target),
            "rescues": [_ref(log) for log in rescued if _log_position(log) < cut],
            "quarantined": _ref(quarantined),
            "maxRescues": policy.get("maxRescues"),
            "dailyCap": None if policy.get("dailyCap") is None else str(policy["dailyCap"]),
            "policyTx": enrolled and enrolled["transactionHash"],
            "principalPaid": str(sum(paid for log, paid in payments if _log_position(log) < cut)),
            "paidAfterQuarantine": str(sum(paid for log, paid in payments if _log_position(log) > cut)),
        }

    def junk_recovery(self) -> dict:
        """The junk scenario: the steps of the record, confirmed on the chain, with the balance figures of
        the data file of the experiment."""
        empty = {"recorded": False, "chain": None, "vault": None, "steps": [], "waitingRescue": None,
                 "balanceStart": None, "balanceEnd": None, "unexplainedChange": None}
        record = self.scenarios.get("junk")
        if not isinstance(record, dict):
            return empty
        absent = [name for _, name in JUNK_STEPS if not experiment.find_step(record, name)]
        if absent:
            self.notes.append(f"{NOT_RECORDED}: junk recovery; the scenario record has no step "
                              f"{', '.join(absent)}")
            return empty
        chain, addresses = record["chain"], record["addresses"]
        vault, waiting = addresses["vault"], addresses["waiting.callback"]
        if not same_address(self.vaults.get(chain, ""), vault):
            raise OpsError(f"the junk record names the vault {vault} of {chain}: the manifest does not have it")
        chain_id = next(number for number, name in self.chain_of.items() if name == chain)
        ledger = self.ledger_events(chain, vault)
        sources = {
            "BlacklistContract(vault)": (chain, [log for log in ledger if log["topics"][0] == BLACKLIST]),
            "VaultDown": (LASNA, [log for log, event in self.hub_events("VaultDown")
                                  if event["chainId"] == chain_id]),
            "Pending": (LASNA, [log for log, event in self.hub_events("Pending")
                                if event["chainId"] == chain_id and same_address(event["target"], waiting)]),
            "WhitelistContract(vault)": (chain, [log for log in ledger if log["topics"][0] == WHITELIST]),
            "VaultUp": (LASNA, [log for log, event in self.hub_events("VaultUp") if event["chainId"] == chain_id]),
            "Result(waiting.callback)": (chain, [log for log, _ in self.results(chain, waiting)]),
        }
        steps, found = [], {}
        for name, recorded in JUNK_STEPS:
            step_chain, logs = sources[name]
            found[name] = self._confirmed("junk", record, recorded, logs)
            steps.append({"step": name, "chain": step_chain, **_ref(found[name])})
        result = next(event for log, event in self.results(chain, waiting)
                      if log is found["Result(waiting.callback)"])
        figures = self.experiment.get("junk") or {}
        usable = (figures.get("status") == "ok" and figures.get("chain") == chain
                  and same_address(figures.get("vault", ""), vault))
        if not usable:
            self.notes.append("the data file of the experiment has no balance figures of this junk flood: run "
                              "`python -m lifeline.experiment measure` again")
        return {
            "recorded": True, "chain": chain, "vault": checksum(vault), "steps": steps,
            "waitingRescue": {"target": checksum(waiting), "requestId": str(result["id"]),
                              "outcome": OUTCOMES[result["outcome"]], "paid": str(result["paid"])},
            "balanceStart": {"block": figures["startBlock"], "value": figures["balanceStart"]} if usable else None,
            "balanceEnd": {"block": figures["endBlock"], "value": figures["balanceEnd"]} if usable else None,
            "unexplainedChange": figures["unexplainedBalanceChange"] if usable else None,
        }

    def guests(self) -> list:
        """Each enrollment with `guest: true`: the hashes of the manifest and the rescues of the chain."""
        entries = []
        for record in self.manifest["enrollments"]:
            if not record.get("guest"):
                continue
            chain, target, txs = record["chain"], record["target"], record.get("txs") or {}
            if chain == LASNA:
                rescues = [log for log, event in self.hub_events("Rescued") if same_address(event["target"], target)]
            elif chain in self.vaults:
                rescues = [log for log, event in self.results(chain, target)
                           if OUTCOMES[event["outcome"]] == "Rescued"]
            else:
                rescues = []
                self.notes.append(f"the manifest has no vault on {chain}, the chain of the guest "
                                  f"{record['label']}: no rescue is read")
            entries.append({
                "label": record["label"], "chain": chain, "chainId": record["chainId"],
                "target": checksum(target), "consent": record.get("consent"),
                "enrollment": {"setPolicy": txs.get("setPolicy"), "enroll": txs.get("enroll")},
                "rescues": [_ref(log) for log in rescues],
            })
        return entries

    def run(self) -> dict:
        return {
            "version": 1,
            "heights": self.snapshot.chains,
            "rescues": self.rescues(),
            "lasnaRescues": self.lasna_rescues(),
            "fleet": self.fleet(),
            "guardian": self.guardian(),
            "quarantine": self.quarantine(),
            "junkRecovery": self.junk_recovery(),
            "guests": self.guests(),
            "experiment": self.experiment,
        }


# ----------------------------------------------------------------------------------------------
# JUDGE.md
# ----------------------------------------------------------------------------------------------


def _cell(ref) -> str:
    """A block and a transaction hash in one table cell."""
    return "none" if ref is None else f"{ref['block']}<br>`{ref['tx']}`"


def _hash(tx) -> str:
    return "none" if tx is None else f"`{tx}`"


def _table(header: tuple, rows: list) -> list:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + ["| " + " | ".join(str(value) for value in row) + " |" for row in rows]


def render_judge(data: dict, table: str) -> str:
    """`JUDGE.md`: each figure comes from the data file, and the table of the experiment is unchanged."""
    chains = [LASNA, *sorted(name for name in data["heights"] if name != LASNA)]
    lines = [
        "# Lifeline: evidence for the judges", "",
        "This file is generated: `python -m lifeline.judge` writes it from chain data. Do not edit it by hand. "
        "The data file of the same run, `data.json`, has each figure of this file. Each amount is in wei of the "
        "chain that its row names. A block is a block of the chain that its row or its column names.", "",
        "## Claim limit", "",
        f"Lifeline {CLAIM_LIMIT}. Work that arrives while a target is in debt is lost, and nothing replays it. "
        "Each gap of this file gives the blocks in which a target was dead, so that its owner knows what to "
        "retry.", "",
        "## Heights", "",
    ]
    lines += _table(("Chain", "Chain ID", "From", "To"), [
        (name, data["heights"][name]["chainId"], data["heights"][name]["from"], data["heights"][name]["to"])
        for name in chains])
    lines += ["", "Each log query of this run is inside `from..to` of its chain. `from` is the deployment block "
              "of the Lifeline contract of that chain: the hub on lasna, the vault on a destination chain. Each "
              "state value is read at `to`."]

    lines += ["", "## Destination rescues", "",
              "Each row is one relay request of the hub: a `Relayed` log on lasna. The join finds the `Result` "
              "log of the vault of the destination chain with the same request ID and the same target. The debt "
              "event is the last `BlacklistContract` log of the callback proxy for the target before the "
              "result. For a rescue, the `WhitelistContract` log of the proxy is in the transaction of the "
              "result.", "", "### Joined", ""]
    joined = [entry for entry in data["rescues"] if entry["status"] == "joined"]
    if joined:
        lines += _table(
            ("Request ID", "Chain", "Target", "Debt event", "`Relayed`, lasna", "`Result`", "Outcome", "Paid",
             "`WhitelistContract`"),
            [(entry["requestId"], entry["chain"], f"`{entry['target']}`", _cell(entry["debtEvent"]),
              _cell(entry["relayed"]), _cell(entry["result"]), entry["result"]["outcome"], entry["result"]["paid"],
              _cell(entry["whitelist"])) for entry in joined])
    else:
        lines.append("None.")
    lines += ["", "### Open", "", "A relay request with no `Result` log in the heights of its destination chain.",
              ""]
    opened = [entry for entry in data["rescues"] if entry["status"] == "open"]
    if opened:
        lines += _table(
            ("Request ID", "Chain", "Chain ID", "Target", "Debt event", "`Relayed`, lasna"),
            [(entry["requestId"], entry["chain"] or "not in the manifest", entry["chainId"], f"`{entry['target']}`",
              _cell(entry["debtEvent"]), _cell(entry["relayed"])) for entry in opened])
    else:
        lines.append("None.")

    lines += ["", "## Lasna rescues and gaps", "",
              "Each row is one `Rescued` log of the hub with the `Gap` log of the same transaction. A dead-from "
              "block of zero means that no debt log started the rescue.", ""]
    if data["lasnaRescues"]:
        lines += _table(
            ("Target", "`Rescued`", "Debt", "Paid", "Dead from block", "Alive at block"),
            [(f"`{entry['target']}`", _cell(entry["rescued"]), entry["rescued"]["debt"], entry["rescued"]["paid"],
              *((entry["gap"]["deadFromBlock"], entry["gap"]["aliveAtBlock"]) if entry["gap"] else ("none", "none")))
             for entry in data["lasnaRescues"]])
    else:
        lines.append("None.")

    lines += ["", "## Fleet report", "",
              "The fleet is each enrollment of the manifest that is not a guest and not a fixture of the "
              "experiment. Each figure counts from the enrollment block of the target on its own chain to `to` of "
              "that chain. The enrollment block is the block of `hub.enroll` for a lasna target, and the block of "
              "`vault.setPolicy` for a target of a destination chain.", ""]
    if data["fleet"]:
        for entry in data["fleet"]:
            removed = entry["removed"]
            fills, fills_from = entry["fills"], entry["fillsFrom"]
            if fills is None:
                fill_cells = (NOT_CONFIGURED, "")
            else:
                since = "no block at or after the enrollment" if fills_from["block"] is None \
                    else f"from block {fills_from['block']}"
                fill_cells = (fills, f"{fills_from['chain']}, {since}, logs of `{fills_from['address']}`")
            rows = [
                ("Chain", entry["chain"], f"chain ID {entry['chainId']}"),
                ("Target", f"`{entry['target']}`", ""),
                ("Enrollment block", entry["enrollBlock"], _hash(entry["enrollTx"])),
                ("Removed", "no" if not removed else "yes",
                 "" if not removed else "<br>".join(f"{name} {_hash(tx)}" for name, tx in sorted(removed.items()))),
                ("Fills", *fill_cells),
                ("Rescues", entry["rescues"], f"from block {entry['enrollBlock']}"),
                ("Guardian top-ups", entry["topUps"], f"from block {entry['enrollBlock']}"),
                ("Principal paid", entry["principalPaid"], f"from block {entry['enrollBlock']}"),
                ("Low mark of the policy", entry["marks"]["low"], "the enrollment transaction"),
                ("High mark of the policy", entry["marks"]["high"], "the enrollment transaction"),
                ("Reserves kept", entry["reserves"]["value"], f"at block {entry['reserves']['block']}"),
            ]
            lines += [f"### {entry['label']}", ""] + _table(("Figure", "Value", "Chain, block or hash"), rows) + [""]
            if entry["gaps"]:
                lines += _table(
                    ("Gap: dead from block", "Alive at block", "Blocks", "Transaction that ended the gap"),
                    [("no debt log in the heights" if gap["deadFromBlock"] is None else gap["deadFromBlock"],
                      "in debt at `to`" if gap["aliveAtBlock"] is None else gap["aliveAtBlock"],
                      "not known" if gap["blocks"] is None else gap["blocks"], _hash(gap["tx"]))
                     for gap in entry["gaps"]]) + [""]
            else:
                lines += ["Gaps: none.", ""]
        lines += ["The specification does not define a fill. This report reads it as a fill event of the fleet "
                  "itself, as `subjects.fastfill.fill` of `config/network.json` defines it: the count is the number "
                  "of these logs from the first block of the fill chain at or after the enrollment, by the block "
                  "timestamps, to `to`. A guardian top-up is a separate figure. For a lasna target a gap is a `Gap` "
                  "log of the hub. For a target of a destination chain a gap is a debt interval at the callback "
                  "proxy: from a `BlacklistContract` log to the next `WhitelistContract` log, or to `to`."]
    else:
        lines.append("None.")

    lines += ["", "## Guardian cost", "", "Each figure counts from the deployment block of the contract to `to`.", ""]
    guardians = [("hub", data["guardian"]["hub"])] + [
        (f"vault of {name}", data["guardian"]["vaults"][name]) for name in chains if name in data["guardian"]["vaults"]]
    lines += _table(
        ("Guardian", "Chain", "Address", "Deployment block", "Principal paid", "`SelfCare` added",
         "Fee float consumed", "Balance", "Reserves", "Debt", "State block"),
        [(label, entry["chain"], f"`{entry['address']}`", entry["deployBlock"], entry["principalPaid"],
          entry["selfCareAdded"], entry["floatConsumed"], entry["balance"]["value"], entry["reserves"]["value"],
          entry["debt"]["value"], entry["balance"]["block"]) for label, entry in guardians])
    lines += ["", "The principal paid is the sum of the payment events: `Rescued` and `ToppedUp` of the hub, "
              "`Result` of a vault. The fee float consumed is the sum of `SelfCare.added`, minus the reserves at "
              "`to`, plus the debt at `to`. A deposit to the float by another account is not visible in these "
              "events: it makes the figure too low by its amount."]

    lines += ["", "## Three-arm table", "",
              "The text between the two markers is `evidence/experiment/table.md`, unchanged. Its heights are the "
              "heights of the experiment.", "", TABLE_BEGIN, table + TABLE_END]

    quarantine = data["quarantine"]
    lines += ["", "## Quarantine", ""]
    if not quarantine["recorded"]:
        lines.append(f"Status: {NOT_RECORDED}.")
    else:
        cut = quarantine["quarantined"]["block"]
        policy = "the manifest" if quarantine["policyTx"] is None else f"`hub.enroll` {_hash(quarantine['policyTx'])}"
        rows = [("Looping canary", f"`{quarantine['target']}`", quarantine["chain"])]
        rows += [("`Rescued`", "", _cell(ref)) for ref in quarantine["rescues"]]
        rows += [
            ("`Quarantined`", "", _cell(quarantine["quarantined"])),
            ("Rescues before `Quarantined`", len(quarantine["rescues"]), "the `Rescued` rows"),
            ("`maxRescues` of the policy", "not known" if quarantine["maxRescues"] is None
             else quarantine["maxRescues"], policy),
            ("Principal paid before `Quarantined`", quarantine["principalPaid"],
             f"before the `Quarantined` log of block {cut}"),
            ("`dailyCap` of the policy", quarantine["dailyCap"] or "not known", policy),
            ("Principal paid after `Quarantined`", quarantine["paidAfterQuarantine"],
             f"after the `Quarantined` log of block {cut}, to block {data['heights'][quarantine['chain']]['to']}"),
        ]
        lines += _table(("Figure", "Value", "Chain, block and hash"), rows)

    junk = data["junkRecovery"]
    lines += ["", "## Junk recovery", ""]
    if not junk["recorded"]:
        lines.append(f"Status: {NOT_RECORDED}.")
    else:
        waiting = junk["waitingRescue"]
        lines += [f"Vault `{junk['vault']}` on {junk['chain']}.", ""]
        lines += _table(("Step", "Chain", "Block", "Transaction"),
                        [(f"`{step['step']}`", step["chain"], step["block"], _hash(step["tx"]))
                         for step in junk["steps"]])
        lines += [""] + _table(
            ("Target that waited", "Request ID of the sweep", "Outcome", "Paid", "`Result`"),
            [(f"`{waiting['target']}`", waiting["requestId"], waiting["outcome"], waiting["paid"],
              _cell(junk["steps"][-1]))]) + [""]
        if junk["balanceStart"] is None:
            lines.append(f"Balance figures of the vault: {NOT_RECORDED} in the data file of the experiment.")
        else:
            lines += _table(("Figure of the data file of the experiment", "Value", "Block"), [
                ("Balance of the vault at the start block of the flood", junk["balanceStart"]["value"],
                 junk["balanceStart"]["block"]),
                ("Balance of the vault at the end block of the flood", junk["balanceEnd"]["value"],
                 junk["balanceEnd"]["block"]),
                ("Balance change that the payments do not explain", junk["unexplainedChange"],
                 f"from block {junk['balanceStart']['block']} to block {junk['balanceEnd']['block']}"),
            ])

    lines += ["", "## Guests", ""]
    if not data["guests"]:
        lines.append(NO_GUEST)
    else:
        lines += _table(
            ("Guest", "Chain", "Target", "Consent", "`setPolicy`", "`enroll`", "Rescues"),
            [(entry["label"], f"{entry['chain']}, chain ID {entry['chainId']}", f"`{entry['target']}`",
              entry["consent"], _hash(entry["enrollment"]["setPolicy"]), _hash(entry["enrollment"]["enroll"]),
              "<br>".join(_cell(ref) for ref in entry["rescues"]) or "none") for entry in data["guests"]])
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------------------------


def parse_arguments(argv):
    parser = argparse.ArgumentParser(prog="python -m lifeline.judge", description=__doc__.split("\n\n")[0])
    parser.add_argument("--rpc", action="append", default=[], metavar="<chain>=<url>",
                        help="RPC URL of one chain; repeatable; replaces the RPC list of that chain in the manifest")
    parser.add_argument("--heights", metavar="<file>", help="reproduce the run at the heights of a saved snapshot")
    parser.add_argument("--manifest", default=str(ops.DEFAULT_MANIFEST), metavar="<file>",
                        help="manifest file (default deployments/manifest.json)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), metavar="<dir>",
                        help="directory of data.json and heights.json (default evidence/judge)")
    parser.add_argument("--judge", default=str(DEFAULT_JUDGE), metavar="<file>",
                        help="the judge document (default JUDGE.md at the repository root)")
    parser.add_argument("--experiment", default=str(experiment.DEFAULT_OUT), metavar="<dir>",
                        help="directory of table.md and data.json of the experiment (default evidence/experiment)")
    parser.add_argument("--scenarios", metavar="<file>",
                        help="scenario file of the experiment (default scenarios.json in the --experiment directory)")
    parser.add_argument("--config", metavar="<file>",
                        help="configuration with the fill subject (default config/network.json)")
    parser.add_argument("--cache", default=DEFAULT_CACHE, metavar="<dir>", help="cache directory")
    parser.add_argument("--retries", type=int, default=8, help="retries of one request before the run fails")
    parser.add_argument("--backoff", type=float, default=1.0, help="first retry delay in seconds; it doubles")
    return parser.parse_args(argv)


def _experiment_files(directory: Path, scenarios=None) -> tuple:
    """(table text, data, scenario records) of the experiment. The table and the data file must exist. A
    scenario file that is absent has no record."""
    try:
        table = (directory / "table.md").read_text()
    except OSError as error:
        raise OpsError(f"the table of the experiment {directory / 'table.md'}: {error.strerror or error}") from None
    data = ops._read_json(directory / "data.json", "the data file of the experiment")
    return table, data, experiment.Scenarios(scenarios or directory / "scenarios.json").data


def main(argv=None) -> int:
    args = parse_arguments(argv)
    out, judge = Path(args.out), Path(args.judge)
    try:
        # The files of the experiment come first: a run with no table sends no request.
        table, experiment_data, scenarios = _experiment_files(Path(args.experiment), args.scenarios)
        manifest = Manifest.load(args.manifest).data
        config = load_config(args.config)
        starts = deploy_blocks(manifest)
        names = list(starts)
        rpc = parse_rpc(args.rpc)
        unknown = sorted(set(rpc) - set(manifest["chains"]))
        if unknown:
            raise OpsError(f"--rpc names a chain that the manifest does not have: {', '.join(unknown)}")
        # A chain of the manifest with no Lifeline contract is not read.
        rpc = {name: urls for name, urls in rpc.items() if name in names}
        chains = {"chains": {name: manifest["chains"][name] for name in names}}
        experiment.guard_local_outputs(
            [url for name in names for url in rpc.get(name) or manifest["chains"][name].get("rpc") or []],
            (("--cache", args.cache, DEFAULT_CACHE), ("--out", out, DEFAULT_OUT), ("--judge", judge, DEFAULT_JUDGE)))
        readers = open_readers(chains, rpc, args.cache, retries=args.retries, backoff=args.backoff)
        if args.heights:
            snapshot = Snapshot.load(args.heights, readers)
        else:
            snapshot = Snapshot.pin(readers, lambda reader, to: starts[reader.name], {})
        for name in names:
            heights = snapshot.chains[name]
            if heights["from"] != starts[name]:
                raise SnapshotError(f"{name}: the heights start at block {heights['from']}, and the Lifeline "
                                    f"contract of the manifest has the deployment block {starts[name]}")
            if heights["from"] > heights["to"]:
                raise SnapshotError(f"{name}: the block {heights['to']} is before the deployment block "
                                    f"{heights['from']}")
        evidence = Evidence(manifest, config, scenarios, experiment_data, readers, snapshot)
        data = evidence.run()
    except (ConfigError, ChainMismatch, SnapshotError, OpsError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (ReaderError, StateUnavailable, CallReverted) as error:
        # No file is written. The cache keeps the finished chunks.
        print(f"error: {error}", file=sys.stderr)
        return 1

    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    try:
        publish_paths({
            out / "data.json": text,
            out / "heights.json": snapshot.text(),
            # The document is made from the text of the data file: each figure of it is in that file.
            judge: render_judge(json.loads(text), table),
        })
    except OSError as error:
        print(f"error: cannot write the output files: {error}", file=sys.stderr)
        return 1

    for name in names:
        heights = snapshot.chains[name]
        print(f"heights {name} chainId {heights['chainId']} from {heights['from']} to {heights['to']}")
    joined = sum(1 for entry in data["rescues"] if entry["status"] == "joined")
    print(f"destination rescues: {joined} joined, {len(data['rescues']) - joined} open")
    print(f"lasna rescues: {len(data['lasnaRescues'])}; fleet: {len(data['fleet'])}; guests: {len(data['guests'])}")
    for section in ("quarantine", "junkRecovery"):
        print(f"{section}: {'recorded' if data[section]['recorded'] else NOT_RECORDED}")
    for note in evidence.notes:
        print(note)
    for name in snapshot.from_saved:
        print(f"from snapshot (the node has no state at the old block): {name}")
    for value in snapshot.differs:
        print(f"differs from snapshot (the saved value is used): {value}")
    print(f"wrote {judge}, {out / 'data.json'}, {out / 'heights.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
