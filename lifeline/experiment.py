"""Live proof scenarios and the three-arm measurement.

    python -m lifeline.experiment fixtures   [--dest-chain <name>]
    python -m lifeline.experiment load       --pokes <n> --interval <seconds>
    python -m lifeline.experiment quarantine
    python -m lifeline.experiment junk       --chain <name>
    python -m lifeline.experiment measure    --rpc <chain>=<url> ... [--heights <file>] [--out <dir>]

Three identical canary automations get one scripted load: arm A has no guardian, arm B has a cron poller,
arm C is enrolled in Lifeline. `quarantine` drives a looping canary into quarantine. `junk` floods the
builder's own vault with junk callbacks and restores it with the `recover` command. `measure` builds the
three-arm table from chain data: it needs no key and sends no transaction.

The fixture key comes from `LIFELINE_FIXTURE_KEY`: it deploys, owns and pokes the fixtures. The owner key
(`LIFELINE_OWNER_KEY`) is used only through the operator commands `enroll` and `recover`, and by the guard
of `junk`. This module writes only the `fixtures` section of the manifest.

Reusable parts, for the later units: `poke`, `wait_for_event`, `FixtureStore`, `Scenarios`.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

from . import ops
from .config import ConfigError, open_readers
from .config import parse_rpc as parse_reader_rpc
from .events import BLACKLIST, DEBT, RESERVES, WHITELIST, ledger_call, word
from .ops import (
    LASNA,
    OUTCOMES,
    TARGET_STATES,
    Manifest,
    OpsError,
    Policy,
    RpcError,
    TransportError,
    amount,
    block_of,
    hex_int,
    same_address,
    say,
    topic_of_address,
    topic_of_int,
)
from .queries import debt_intervals
from .reader import (
    CallReverted,
    ChainMismatch,
    ReaderError,
    Snapshot,
    SnapshotError,
    StateUnavailable,
)
from .stats import publish

FIXTURE_KEY_VARIABLE = "LIFELINE_FIXTURE_KEY"
DEFAULT_OUT = ops.ROOT / "evidence" / "experiment"
DEFAULT_SCENARIOS = DEFAULT_OUT / "scenarios.json"
ZERO_ADDRESS = "0x" + "00" * 20

ARMS = ("A", "B", "C")
# The fixtures that `fixtures` enrolls with the enroll command: arm C and the looping canary.
ENROLLED = ("armC.reactive", "armC.callback", "looping")
# The entries of the `fixtures` section of the manifest: name, contract, and `True` for a Lasna contract.
FIXTURES = (
    ("armA.reactive", "CanaryReactive", True),
    ("armA.callback", "CanaryCallback", False),
    ("armB.reactive", "CanaryReactive", True),
    ("armB.callback", "CanaryCallback", False),
    ("armB.poller", "CronPoller", True),
    ("armC.reactive", "CanaryReactive", True),
    ("armC.callback", "CanaryCallback", False),
    ("looping", "LoopingCanary", True),
    ("waiting.callback", "CanaryCallback", False),
    ("junkRequester", "JunkRequester", True),
)

# Start values of the default amounts of `fixtures`. Each one is a sample, not a protocol constant: the
# command prints the amounts that it used, and each amount has a flag.
REACTION_GAS = 160_000  # the gas that the system contract charges for one canary reaction (0.016 lREACT at 100 gwei)
CALLBACK_GAS_USED = 30_000  # the gas of one `pong` callback above `_extraGas` of the proxy
# The gas limit of a junk callback. The proxy keeps `_extraGas + _maxChargeGas` (130,000) before the call.
JUNK_CALLBACK_GAS = 250_000


# ----------------------------------------------------------------------------------------------
# Keys, the fixtures section and the scenario file
# ----------------------------------------------------------------------------------------------


def load_fixture_account():
    """The account of `LIFELINE_FIXTURE_KEY`. The key is never printed and never stored."""
    key = os.environ.get(FIXTURE_KEY_VARIABLE, "").strip()
    if not key:
        raise OpsError(f"the environment variable {FIXTURE_KEY_VARIABLE} is not set")
    try:
        from eth_account import Account
    except ImportError:
        raise OpsError('the experiment commands need eth-account: pip install -e ".[ops]"') from None
    try:
        return Account.from_key(key)
    except Exception:  # the message of the library can hold the key
        raise OpsError(f"{FIXTURE_KEY_VARIABLE} is not a private key") from None


class FixtureStore:
    """The `fixtures` section of the manifest. `save()` reads the file again and replaces only that section,
    so each entry that an operator command wrote in the meantime stays."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries = Manifest.load(self.path).data["fixtures"]
        if not isinstance(self.entries, dict):
            raise OpsError(f"the manifest {self.path}: fixtures is not an object")

    def save(self) -> None:
        manifest = Manifest.load(self.path)
        manifest.data["fixtures"] = self.entries
        manifest.save()

    def entry(self, name: str) -> dict:
        """A recorded fixture. A driver acts only on a fixture that `fixtures` recorded."""
        entry = self.entries.get(name)
        if not isinstance(entry, dict) or not entry.get("address"):
            raise OpsError(f"the manifest {self.path} has no fixtures entry {name}: run "
                           f"`python -m lifeline.experiment fixtures` first. No transaction was sent")
        return entry

    def address(self, name: str) -> str:
        return ops.parse_address(self.entry(name)["address"], f"fixtures.{name}.address")


class Scenarios:
    """`scenarios.json`: the blocks and the hashes that the drivers leave. No wall-clock time."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = ops._read_json(self.path, "scenario file") if self.path.exists() else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(temporary, self.path)

    def section(self, name: str, chain: str, addresses: dict) -> dict:
        record = self.data.get(name)
        if not isinstance(record, dict):
            record = {"chain": chain, "addresses": addresses, "startBlock": None, "endBlock": None, "steps": []}
            self.data[name] = record
        elif record.get("chain") != chain or record.get("addresses") != addresses:
            raise OpsError(f"{self.path}: {name} is the record of other fixtures or of another chain. Use "
                           f"another --scenarios file")
        return record


def guard_scenarios(path, chains) -> None:
    """A dry run on local development nodes must not write the scenario file of the live run."""
    if Path(path).resolve() == DEFAULT_SCENARIOS.resolve():
        local = [chain.name for chain in chains if chain.rpc.is_dev_node()]
        if local:
            raise OpsError(f"the RPC of {', '.join(local)} is a local development node: a dry run writes to "
                           f"another scenario file, given with --scenarios")


def find_step(record: dict, step: str):
    return next((entry for entry in record["steps"] if entry["step"] == step), None)


def add_step(record: dict, step: str, chain: str, block, tx=None) -> dict:
    """Adds one step to a scenario record: the name, the chain, the block and the transaction hash."""
    entry = {"step": step, "chain": chain, "block": block, "tx": tx}
    record["steps"].append(entry)
    return entry


# ----------------------------------------------------------------------------------------------
# Poke and wait: unit 11 uses these two
# ----------------------------------------------------------------------------------------------


def poke(rpc: ops.Rpc, account, canary: str) -> dict:
    """Calls `poke()` of a canary with the fixture key and waits for the receipt. Returns
    `{"tx", "block", "n"}`: `n` is the number of the `Poke(n)` log."""
    abi = ops.load_artifact("CanaryReactive").abi  # `poke()` and `Poke` are the same in `LoopingCanary`
    receipt = ops.transact(rpc, account, canary, abi.encode("poke"), abi=abi, what="poke")
    logs = ops.receipt_logs(receipt, canary, abi.topic("Poke"))
    if not logs:
        raise OpsError(f"poke() of {canary} on {rpc.name} gave no Poke log: {receipt['transactionHash']}")
    return {"tx": receipt["transactionHash"], "block": block_of(receipt), "n": hex_int(logs[0]["topics"][1])}


def wait_for_event(rpc: ops.Rpc, address: str, topics: list, from_block: int, timeout: float):
    """Waits at most `timeout` seconds for the first log of one emitter that matches the topics, at or after
    `from_block`. Returns the log, or None at the timeout."""
    return ops.wait_for_log(rpc, address, topics, from_block, time.monotonic() + timeout)


def _log_place(log: dict) -> tuple:
    return block_of(log), hex_int(log["logIndex"])


# ----------------------------------------------------------------------------------------------
# The guardian check: limits of the deployed hub and vault that a load must respect
# ----------------------------------------------------------------------------------------------


def self_care_today(session: ops.Session) -> int:
    """The principal that the self-care of the hub moved to its float in the current calendar day (UTC), from
    its `SelfCare` logs and the timestamps of their blocks."""
    lasna, hub = session.lasna, session.hub
    head = lasna.rpc.head()
    day_start = hex_int(lasna.rpc.block(head)["timestamp"]) // 86_400 * 86_400
    logs = lasna.rpc.logs(hub, [session.hub_abi.topic("SelfCare")],
                          max(session.manifest.hub["deployBlock"], head - 100_000), head)
    spent, stamps = 0, {}
    for log in reversed(logs):
        number = block_of(log)
        if number not in stamps:
            stamps[number] = hex_int(lasna.rpc.block(number)["timestamp"])
        if stamps[number] < day_start:
            break
        spent += session.hub_abi.decode_event("SelfCare", log)["added"]
    return spent


def guardian_problems(session: ops.Session, dest: str = None) -> list:
    """The reasons why a driver must not add load now. Empty if the hub (and the vault of `dest`) can serve.

    The deployed hub has three limits that a load must respect:
    - Self-care refills the fee float with at most `selfCareDailyCap` for each calendar day. Under a heavy
      load the float then goes down with each reaction.
    - The hub emits `HubLow` only below `principalFloor`. At the floor it keeps its subscriptions and goes
      into debt with no event when the float is empty. A hub in debt gets no reaction, so the fleet has no
      guardian.
    - So a driver stops while the float still covers one full trigger and the principal above the floor
      covers one more refill."""
    lasna, hub = session.lasna, session.hub
    problems = []
    _, debt = session.read_ledger(lasna, "debt", hub)
    _, reserves = session.read_ledger(lasna, "reserves", hub)
    balance, floor = lasna.rpc.balance(hub), session.read_hub("principalFloor")
    coeff, _ = ops.ledger_parameter(lasna, session.ledger_abi, "_gasPriceCoeffPer1000",
                                    session.manifest.data["chains"][LASNA], "gasPriceCoeffPer1000")
    fee = ops.trigger_fee(lasna.rpc.gas_price(), coeff or 1000)
    if debt:
        problems.append(f"the hub is in debt ({amount(debt)}) and gets no reaction")
    elif reserves < fee:
        cap = session.read_hub("selfCareDailyCap")
        problems.append(
            f"the fee float of the hub is {amount(reserves)}, below one full trigger ({amount(fee)}). Self-care "
            f"moved {amount(self_care_today(session))} of its daily cap {amount(cap)} today; the hub emits no "
            f"HubLow in this state. Continue in the next calendar day (UTC), or deposit to the float of the hub"
        )
    if balance < floor + fee:
        problems.append(f"the principal of the hub above its floor ({amount(max(balance - floor, 0))}) does not "
                        f"cover one full trigger: self-care cannot refill the float, and the hub emits no HubLow "
                        f"at the floor")
    if session.read_hub("paused"):
        problems.append("the hub is paused")
    if dest is not None:
        chain, vault = session.chain(dest), session.vault(dest)
        _, _, _, up, pending = session.read_hub("vaultInfo", chain.chain_id)
        _, vault_debt = session.read_ledger(chain, "debt", vault)
        if vault_debt or not up or pending:
            problems.append(f"the vault of {dest} needs a recovery (debt {vault_debt}, up {str(up).lower()}, "
                            f"pending {pending})")
        if session.read_vault(dest, "paused"):
            problems.append(f"the vault of {dest} is paused")
    return problems


def require_guardian(session: ops.Session, dest: str = None, what="the load") -> None:
    problems = guardian_problems(session, dest)
    if problems:
        raise OpsError(f"{what} stops, so that the enrolled fleet keeps its guardian: " + "; ".join(problems))


# ----------------------------------------------------------------------------------------------
# Operator commands of unit 6
# ----------------------------------------------------------------------------------------------


def run_ops(args, command: str, *arguments) -> None:
    """Runs one operator command of `lifeline.ops` with the manifest and the RPC URLs of this command."""
    argv = [command, *arguments, "--manifest", str(args.manifest)]
    for value in args.rpc:
        argv += ["--rpc", value]
    say(f"  python -m lifeline.ops {command} {' '.join(str(a) for a in arguments)}")
    if ops.main(argv) != 0:
        raise OpsError(f"the operator command `{command}` did not end with status 0: see its error above")


def ensure_enrolled(args, chain_name: str, target: str, label: str, policy: Policy) -> None:
    """Enrolls a fixture with the `enroll` command, or does nothing if the manifest has that enrollment."""
    manifest = Manifest.load(args.manifest)
    record = manifest.active_enrollment(chain_name, target)
    if manifest.enrollment_complete(record):
        if record["policy"] != policy.to_json():
            raise OpsError(f"the manifest has an enrollment of {label} ({target}) with another policy: remove it "
                           f"first")
        say(f"  {label}: enrolled already (hub.enroll in block {record['txs']['enrollBlock']})")
        return
    run_ops(args, "enroll", "--chain", chain_name, "--target", target, "--label", label,
            "--low", ops.from_wei(policy.low), "--high", ops.from_wei(policy.high),
            "--max-debt", ops.from_wei(policy.maxDebt), "--daily-cap", ops.from_wei(policy.dailyCap),
            "--max-rescues", str(policy.maxRescues), "--window", str(policy.window))


def target_state(session: ops.Session, chain: ops.Chain, target: str) -> tuple:
    """(kind, state name) of a target at the hub. Kind 0: the hub never had the target."""
    kind, state, _ = session.read_hub("targetInfo", chain.chain_id, target)
    return kind, TARGET_STATES[state]


# ----------------------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------------------


def cron_topic0(name: str) -> int:
    return int.from_bytes(ops.keccak256(f"{name}(uint256)".encode()), "big")


def _flag(args, name: str, default: int) -> int:
    value = getattr(args, name)
    return default if value is None else ops.to_wei(value)


def _fixture_amounts(args, session: ops.Session, dest: ops.Chain) -> dict:
    """The amounts of the fixtures, in wei. Each default comes from the current gas price of its chain."""
    lasna, chains = session.lasna, session.manifest.data["chains"]
    coeff, _ = ops.ledger_parameter(lasna, session.ledger_abi, "_gasPriceCoeffPer1000", chains[LASNA],
                                    "gasPriceCoeffPer1000")
    reaction_fee = ops.trigger_fee(lasna.rpc.gas_price(), coeff or 1000, REACTION_GAS)
    extra, _ = ops.ledger_parameter(dest, session.ledger_abi, "_extraGas", chains[dest.name], "extraGas")
    coeff, _ = ops.ledger_parameter(dest, session.ledger_abi, "_gasPriceCoeffPer1000", chains[dest.name],
                                    "gasPriceCoeffPer1000")
    callback_fee = ops.sweep_fee(extra or 0, CALLBACK_GAS_USED, dest.rpc.gas_price(), coeff or 1000)
    values = {"reactionFee": reaction_fee, "callbackFee": callback_fee}
    # The callback canary uses its start reserves up before the reactive canary does: an arm with no guardian
    # then shows dropped callbacks first and missed events later.
    values["reactiveReserve"] = _flag(args, "reactive_reserve", 5 * reaction_fee)
    values["callbackReserve"] = _flag(args, "callback_reserve", 2 * callback_fee)
    values["lasnaLow"] = _flag(args, "lasna_low", reaction_fee)
    values["lasnaHigh"] = _flag(args, "lasna_high", 4 * reaction_fee)
    values["destLow"] = _flag(args, "dest_low", callback_fee)
    values["destHigh"] = _flag(args, "dest_high", 4 * callback_fee)
    values["pollerFloat"] = _flag(args, "poller_float", 20 * reaction_fee)
    values["pollerPrincipal"] = _flag(args, "poller_principal", 3 * values["lasnaHigh"])
    # Below one reaction fee: each poke after a rescue gives a new debt.
    values["loopingHigh"] = _flag(args, "looping_high", max(reaction_fee // 16, 1))
    return values


def _fixture_policies(args, values: dict) -> dict:
    reaction_fee, callback_fee = values["reactionFee"], values["callbackFee"]
    lasna_debt = max(4 * reaction_fee, values["lasnaHigh"])
    dest_debt = max(4 * callback_fee, values["destHigh"])
    return {
        "armC.reactive": Policy(values["lasnaLow"], values["lasnaHigh"], lasna_debt,
                                10 * (values["lasnaHigh"] + lasna_debt), 4, args.window),
        "armC.callback": Policy(values["destLow"], values["destHigh"], dest_debt,
                                10 * (values["destHigh"] + dest_debt), 4, args.window),
        # `low` 0: `enroll` and the gauge pay nothing to the looping canary while it has no debt.
        "looping": Policy(0, values["loopingHigh"], lasna_debt,
                          (args.max_rescues + 2) * (lasna_debt + values["loopingHigh"]), args.max_rescues,
                          args.window),
    }


def cmd_fixtures(args) -> int:
    session = ops.Session(args)  # the owner key: `enroll` needs it, and it must be the owner of the vault
    fixture = load_fixture_account()
    lasna, dest = session.lasna, session.destination(args.dest_chain)
    session.require_owner(args.dest_chain)
    store = FixtureStore(args.manifest)
    ledger_abi = session.ledger_abi
    artifacts = {contract: ops.load_artifact(contract) for contract in {contract for _, contract, _ in FIXTURES}}

    recorded = {entry.get("chain") for name, entry in store.entries.items()
                if isinstance(entry, dict) and name.endswith(".callback")}
    if recorded - {args.dest_chain}:
        raise OpsError(f"the manifest has fixtures on {', '.join(sorted(recorded))}: the three arms use one "
                       f"destination chain. Give --dest-chain {sorted(recorded)[0]}")

    def chain_of(on_lasna: bool) -> ops.Chain:
        return lasna if on_lasna else dest

    # The amounts are fixed by the first run: a later run continues with the same values.
    if "armA.reactive" not in store.entries:
        values = _fixture_amounts(args, session, dest)
        policies = _fixture_policies(args, values)
        for policy in policies.values():
            policy.validate("a fixture policy")
        for name, contract, on_lasna in FIXTURES:
            chain = chain_of(on_lasna)
            store.entries[name] = {"contract": contract, "chain": chain.name, "chainId": chain.chain_id,
                                   "address": None, "deployBlock": None, "deployTx": None}
        for arm in ARMS:
            store.entries[f"arm{arm}.reactive"].update(reserve=str(values["reactiveReserve"]), fundTx=None,
                                                       callbackGas=args.callback_gas)
            store.entries[f"arm{arm}.callback"].update(reserve=str(values["callbackReserve"]), fundTx=None)
        store.entries["armB.poller"].update(
            cronTopic=session.manifest.data["cronTopic"], low=str(values["lasnaLow"]), high=str(values["lasnaHigh"]),
            feeFloat=str(values["pollerFloat"]), principal=str(values["pollerPrincipal"]))
        for name, policy in policies.items():
            store.entries[name]["policy"] = policy.to_json()
        store.save()
    entries = store.entries
    for name, _, _ in FIXTURES:
        if not isinstance(entries.get(name), dict):
            raise OpsError(f"the manifest {store.path}: fixtures.{name} is absent, and other fixtures exist. "
                           f"Correct the manifest, or use a new deployment")

    say(f"fixtures: fixture account {fixture.address}; destination chain {args.dest_chain} "
        f"(chain ID {dest.chain_id})")
    say(f"  start reserves of each reactive canary on {LASNA}: {amount(int(entries['armA.reactive']['reserve']))}")
    say(f"  start reserves of each callback canary on {args.dest_chain}: "
        f"{amount(int(entries['armA.callback']['reserve']))}")
    poller = entries["armB.poller"]
    say(f"  marks of arm B (poller) and of arm C on {LASNA}: low {amount(int(poller['low']))}, high "
        f"{amount(int(poller['high']))}; cron topic {poller['cronTopic']}")

    def deploy(name: str, arguments=(), value=0) -> str:
        entry = entries[name]
        chain = session.chain(entry["chain"])
        if entry.get("address"):
            if not chain.rpc.code(entry["address"]):
                raise OpsError(f"the manifest has {name} at {entry['address']}, and {chain.name} has no code "
                               f"there: the manifest is from another network or from a dry run")
            say(f"  {name}: in the manifest at {entry['address']} (block {entry['deployBlock']}); no deployment")
        else:
            ops.deploy_recorded(chain.rpc, fixture, artifacts[entry["contract"]], arguments, value, entry,
                                store.save, name)
            say(f"  {name}: {entry['contract']} deployed at {entry['address']} on {chain.name}, block "
                f"{entry['deployBlock']}, tx {entry['deployTx']}"
                + (f", value {amount(value)}" if value else ""))
        return entry["address"]

    def fund(name: str) -> None:
        entry = entries[name]
        chain = session.chain(entry["chain"])
        reserve = int(entry["reserve"])
        if reserve and not entry.get("fundTx"):
            ops.transact_recorded(chain.rpc, fixture, chain.ledger, ledger_abi.encode("depositTo", entry["address"]),
                                  entry, "fundTx", store.save, value=reserve, abi=ledger_abi,
                                  what=f"depositTo for {name}")
        say(f"  {name}: start reserves {amount(reserve)}" + (f", tx {entry['fundTx']}" if reserve else ""))

    # One automation: a `CanaryCallback` on the destination chain and the `CanaryReactive` on Lasna that
    # calls it. The three are the same but for the recipient.
    for arm in ARMS:
        callback = deploy(f"arm{arm}.callback")
        reactive = entries[f"arm{arm}.reactive"]
        deploy(f"arm{arm}.reactive", (lasna.ledger, dest.chain_id, callback, reactive["callbackGas"]))
        fund(f"arm{arm}.callback")
        fund(f"arm{arm}.reactive")

    value = int(poller["feeFloat"]) + int(poller["principal"])
    deploy("armB.poller", (lasna.ledger, cron_topic0(poller["cronTopic"]), entries["armB.reactive"]["address"],
                           int(poller["low"]), int(poller["high"]), int(poller["feeFloat"])), value)
    poller.setdefault("ward", entries["armB.reactive"]["address"])
    deploy("looping", (lasna.ledger,))
    deploy("waiting.callback")
    deploy("junkRequester", (lasna.ledger,))
    store.save()

    say("arm C and the looping canary, with the enroll command:")
    for name in ENROLLED:
        ensure_enrolled(args, entries[name]["chain"], entries[name]["address"], f"experiment {name}",
                        Policy.from_json(entries[name]["policy"], f"fixtures.{name}.policy"))

    # The state that the experiment needs: arm C has a guardian; arm A and arm B have none at the hub.
    say("state after fixtures:")
    for name, _, on_lasna in FIXTURES:
        if name in ("armB.poller", "junkRequester"):
            continue
        chain, address = chain_of(on_lasna), entries[name]["address"]
        kind, state = target_state(session, chain, address)
        has_policy = (not on_lasna) and session.read_vault(args.dest_chain, "hasPolicy", address)
        enrolled = kind != 0 and state != "Disarmed"
        say(f"  {name} {address} on {chain.name}: hub state {state if kind else 'not a target'}"
            + ("" if on_lasna else f", vault policy {str(has_policy).lower()}"))
        if name == "waiting.callback":
            continue  # the junk scenario enrolls it
        wanted = name in ENROLLED
        if enrolled != wanted or (not on_lasna and has_policy != wanted):
            raise OpsError(f"{name} is {'not ' if wanted else ''}enrolled: the three arms are not the experiment")
    say(f"fixtures: {len(FIXTURES)} entries in fixtures of {store.path}")
    return 0


# ----------------------------------------------------------------------------------------------
# load
# ----------------------------------------------------------------------------------------------


def _owned_fixture(store: FixtureStore, rpc: ops.Rpc, name: str, account) -> str:
    """The address of a recorded fixture on Lasna, after the check that the fixture key is its owner."""
    address = store.address(name)
    abi = ops.load_artifact("CanaryReactive").abi
    owner = abi.decode("owner", rpc.call(address, abi.encode("owner")))
    if not same_address(owner, account.address):
        raise OpsError(f"{FIXTURE_KEY_VARIABLE} is the key of {account.address}; the owner of {name} is {owner}")
    return address


def cmd_load(args) -> int:
    if args.pokes < 1 or args.interval < 0:
        raise OpsError("--pokes is 1 or more and --interval is 0 or more")
    store = FixtureStore(args.manifest)
    # Each address comes from the fixtures section. An absent entry stops the command before any request.
    for arm in ARMS:
        store.entry(f"arm{arm}.reactive")
        store.entry(f"arm{arm}.callback")
    account = load_fixture_account()
    session = ops.Session(args, owner=False)
    lasna = session.lasna
    reactives = {arm: _owned_fixture(store, lasna.rpc, f"arm{arm}.reactive", account) for arm in ARMS}
    dest = store.entry("armC.callback")["chain"]

    guard_scenarios(args.scenarios, [lasna])
    scenarios = Scenarios(args.scenarios)
    record = scenarios.section("load", LASNA, {f"arm{arm}.reactive": reactives[arm] for arm in ARMS})
    require_guardian(session, dest)

    start = lasna.rpc.head()
    if record["startBlock"] is None:
        record["startBlock"] = start
    add_step(record, "start", LASNA, start)
    scenarios.save()
    say(f"load: {args.pokes} steps, {args.interval} s apart, from block {start} of {LASNA}")
    try:
        for step in range(1, args.pokes + 1):
            # Arm C is the experiment only while its guardian works. The check also keeps the fleet safe.
            require_guardian(session, dest)
            for arm in ARMS:
                result = poke(lasna.rpc, account, reactives[arm])
                add_step(record, f"poke {result['n']} arm{arm}", LASNA, result["block"], result["tx"])
                scenarios.save()
                say(f"  step {step} arm {arm}: Poke({result['n']}) tx {result['tx']} block {result['block']}")
            if step < args.pokes:
                time.sleep(args.interval)
    finally:
        end = lasna.rpc.head()
        record["endBlock"] = end
        add_step(record, "end", LASNA, end)
        scenarios.save()
    say(f"load: end at block {end} of {LASNA}; blocks and hashes in {scenarios.path}")
    return 0


# ----------------------------------------------------------------------------------------------
# quarantine
# ----------------------------------------------------------------------------------------------


def cmd_quarantine(args) -> int:
    store = FixtureStore(args.manifest)
    store.entry("looping")
    account = load_fixture_account()
    session = ops.Session(args, owner=False)
    lasna, hub, hub_abi = session.lasna, session.hub, session.hub_abi
    looping = _owned_fixture(store, lasna.rpc, "looping", account)
    enrollment = session.manifest.active_enrollment(LASNA, looping)
    if not session.manifest.enrollment_complete(enrollment):
        raise OpsError("the manifest has no enrollment of the looping canary: run fixtures first")
    policy = Policy.from_json(enrollment["policy"])
    enroll_block = enrollment["txs"]["enrollBlock"]
    target = topic_of_address(looping)

    guard_scenarios(args.scenarios, [lasna])
    scenarios = Scenarios(args.scenarios)
    record = scenarios.section("quarantine", LASNA, {"looping": looping, "hub": hub})
    if record.get("endBlock") is not None and record.get("budget"):
        if not record["budget"]["intact"]:
            raise OpsError(f"{scenarios.path} has a quarantine scenario with a budget that is not intact: "
                           f"{record['budget']}")
        say(f"quarantine: {scenarios.path} has a complete quarantine scenario (blocks {record['startBlock']}.."
            f"{record['endBlock']}), budget intact; no transaction")
        return 0
    if record["startBlock"] is None:
        record["startBlock"] = lasna.rpc.head()
    scenarios.save()
    say(f"quarantine: looping canary {looping} on {LASNA}; maxRescues {policy.maxRescues} in {policy.window} s, "
        f"high {amount(policy.high)}, dailyCap {amount(policy.dailyCap)}")

    def hub_logs(*names) -> list:
        return lasna.rpc.logs(hub, [[hub_abi.topic(name) for name in names], target], enroll_block)

    def record_new() -> dict:
        """Writes each `Rescued` and the `Quarantined` log of the canary to the record. Returns the
        `Quarantined` log, or None."""
        quarantined = None
        for log in hub_logs("Rescued", "Quarantined"):
            is_rescue = log["topics"][0] == hub_abi.topic("Rescued")
            if is_rescue and not any(e["tx"] == log["transactionHash"] and e["step"].startswith("Rescued")
                                     for e in record["steps"]):
                count = sum(1 for e in record["steps"] if e["step"].startswith("Rescued")) + 1
                event = hub_abi.decode_event("Rescued", log)
                add_step(record, f"Rescued {count}", LASNA, block_of(log), log["transactionHash"])
                say(f"  Rescued {count}: tx {log['transactionHash']} block {hex_int(log['blockNumber'])}, debt "
                    f"{amount(event['debt'])}, paid {amount(event['paid'])}")
            elif not is_rescue:
                quarantined = log
                if not find_step(record, "Quarantined"):
                    add_step(record, "Quarantined", LASNA, block_of(log), log["transactionHash"])
                    say(f"  Quarantined: tx {log['transactionHash']} block {hex_int(log['blockNumber'])}")
        scenarios.save()
        return quarantined

    quarantined = record_new()
    pokes = 0
    while quarantined is None:
        if pokes == args.max_pokes:
            raise OpsError(f"the hub did not emit Quarantined after {pokes} pokes of this run")
        require_guardian(session, what="the quarantine scenario")
        seen = len(hub_logs("Rescued", "Quarantined"))
        result = poke(lasna.rpc, account, looping)
        pokes += 1
        add_step(record, f"poke {result['n']}", LASNA, result["block"], result["tx"])
        scenarios.save()
        say(f"  poke {result['n']}: tx {result['tx']} block {result['block']}")
        deadline = time.monotonic() + args.timeout
        while len(hub_logs("Rescued", "Quarantined")) == seen:
            if time.monotonic() >= deadline:
                raise OpsError(f"no Rescued and no Quarantined of the hub in {args.timeout} s after poke "
                               f"{result['n']}: run the command again to continue")
            time.sleep(ops.POLL_SECONDS)
        quarantined = record_new()

    # One more poke after the quarantine: the canary must get no payment.
    if not find_step(record, "poke after Quarantined"):
        result = poke(lasna.rpc, account, looping)
        add_step(record, "poke after Quarantined", LASNA, result["block"], result["tx"])
        scenarios.save()
        say(f"  poke after Quarantined: tx {result['tx']} block {result['block']}; wait {args.settle} s")
        time.sleep(args.settle)

    # Budget intact: `maxRescues` rescues, no payment after `Quarantined`, principal inside `dailyCap`.
    end = lasna.rpc.head()
    rescues = hub_logs("Rescued")
    paid = sum(hub_abi.decode_event("Rescued", log)["paid"] for log in rescues)
    top_ups = hub_logs("ToppedUp")
    paid += sum(hub_abi.decode_event("ToppedUp", log)["paid"] for log in top_ups)
    after = [log for log in rescues + top_ups if _log_place(log) > _log_place(quarantined)]
    _, state = target_state(session, lasna, looping)
    say(f"  end block {end}: {len(rescues)} Rescued (maxRescues {policy.maxRescues}), {len(after)} payment(s) after "
        f"Quarantined, principal paid {amount(paid)} of dailyCap {amount(policy.dailyCap)}, hub state {state}")
    failures = []
    if len(rescues) != policy.maxRescues:
        failures.append(f"{len(rescues)} Rescued, not maxRescues {policy.maxRescues}")
    if after:
        failures.append(f"{len(after)} payment(s) after Quarantined")
    if paid > policy.dailyCap:
        failures.append("the principal paid is above dailyCap")
    if state != "Quarantined":
        failures.append(f"the hub has the canary in state {state}")
    record["endBlock"] = end
    record["budget"] = {
        "maxRescues": policy.maxRescues, "rescues": len(rescues), "paymentsAfterQuarantined": len(after),
        "principalPaid": str(paid), "dailyCap": str(policy.dailyCap), "hubState": state, "intact": not failures,
    }
    scenarios.save()
    if failures:
        raise OpsError("the budget is not intact: " + "; ".join(failures))
    say(f"quarantine: Quarantined after {policy.maxRescues} rescues, budget intact; hashes in {scenarios.path}")
    return 0


# ----------------------------------------------------------------------------------------------
# junk
# ----------------------------------------------------------------------------------------------


def junk_recipient(session: ops.Session, chain_name: str, owner_address: str) -> str:
    """The guard of the junk driver. The one recipient that it accepts for a chain: the vault of that chain
    in the manifest, if the configured owner key is its `owner()`. It sends nothing."""
    entry = session.manifest.vaults.get(chain_name) if chain_name != LASNA else None
    if not session.manifest.deployed(entry):
        raise OpsError(f"junk refuses: the manifest {session.manifest.path} has no vault on {chain_name!r}. The "
                       f"junk driver sends callbacks only to a vault of the manifest. No transaction was sent")
    vault = ops.parse_address(entry["address"], f"contracts.vaults.{chain_name}.address")
    chain = session.chain(chain_name)
    try:
        data = chain.rpc.call(vault, session.vault_abi.encode("owner"))
        owner = session.vault_abi.decode("owner", data) if len(data) == 32 else None
    except RpcError:
        owner = None
    if owner is None:
        raise OpsError(f"junk refuses: {vault} on {chain_name} has no owner(): it is not a vault. No transaction "
                       f"was sent")
    if not same_address(owner, owner_address):
        raise OpsError(f"junk refuses: {ops.OWNER_KEY_VARIABLE} is the key of {owner_address}; the owner of the "
                       f"vault {vault} of {chain_name} is {owner}. The junk driver floods only the vault of the "
                       f"configured key. No transaction was sent")
    return vault


def request_callbacks(session: ops.Session, account, owner_address: str, requester: str, chain_name: str,
                      recipient: str, payloads: list, gas_limit: int) -> list:
    """Requests one callback for each payload through `JunkRequester`, in one burst of transactions. Returns
    the receipts.

    The recipient is the vault of the chain (after `junk_recipient`) or a recorded fixture of this unit; any
    other address is refused before a transaction."""
    fixtures = session.manifest.data["fixtures"]
    is_fixture = any(isinstance(entry, dict) and entry.get("chain") == chain_name
                     and same_address(entry.get("address") or "", recipient) for entry in fixtures.values())
    if not is_fixture and not same_address(recipient, junk_recipient(session, chain_name, owner_address)):
        raise OpsError(f"junk refuses the recipient {recipient}: it is not the vault of {chain_name} in the "
                       f"manifest and not a fixture of this unit. No transaction was sent")
    lasna, chain = session.lasna, session.chain(chain_name)
    abi = ops.load_artifact("JunkRequester").abi
    calls = [abi.encode("request", chain.chain_id, recipient, gas_limit, payload) for payload in payloads]
    nonce = lasna.rpc.fresh_nonce(account.address)
    try:
        estimate = hex_int(lasna.rpc.request("eth_estimateGas", [{
            "from": account.address, "to": requester, "data": "0x" + calls[0].hex()}]))
    except RpcError as error:
        raise OpsError(f"the callback request on {LASNA} would revert: {error.message}") from None
    gas = estimate * 6 // 5 + 10_000
    hashes = [ops.send_transaction(lasna.rpc, account, requester, data, gas=gas, abi=abi, what="callback request",
                                   nonce=nonce + index)
              for index, data in enumerate(calls)]
    return [ops.wait_receipt(lasna.rpc, tx_hash) for tx_hash in hashes]


def next_burst(largest: int, limit_left: int, delivered: int, float_at_start: int, float_now: int) -> int:
    """The number of junk requests of the next burst. Each request of a burst that arrives after the debt of
    the vault is rejected with `InDebt`: it must not arrive after the recovery. So a burst is at most half of
    the requests that the float still covers, from the fee of the deliveries so far, and one request when
    that fee is not known or the float is near its end. The last requests then go out one at a time, and the
    request that makes the debt is the last one that the driver sent."""
    spent = float_at_start - float_now
    if delivered <= 0 or spent <= 0:
        return max(min(1, limit_left), 0)
    covered = float_now // max(spent // delivered, 1)
    return max(min(largest, limit_left, covered // 2), min(1, limit_left), 0)


def rescue_of(results: list, request_ids: set):
    """The `Result` of the canary that waits, from the sweeps of this recovery. `results` is the list of its
    decoded `Result` events with their logs, oldest first; `request_ids` are the request IDs of the `VaultUp`
    events. A catch-up of more than one sweep can rescue the canary in a sweep that is not the last one.
    Returns (log, event) of the first result of one of these sweeps, or None."""
    return next(((log, event) for log, event in results if event["id"] in request_ids), None)


def cmd_junk(args) -> int:
    # The guard comes first: no request with a key and no transaction before it.
    owner = ops.load_owner()
    session = ops.Session(args, owner=False)
    vault = junk_recipient(session, args.chain, owner.address)
    account = load_fixture_account()
    store = FixtureStore(args.manifest)
    waiting, waiting_chain = store.address("waiting.callback"), store.entry("waiting.callback")["chain"]
    if waiting_chain != args.chain:
        raise OpsError(f"the fixture waiting.callback is on {waiting_chain}, not on {args.chain}: run junk on the "
                       f"destination chain of the fixtures")
    lasna, chain = session.lasna, session.chain(args.chain)
    hub, hub_abi, vault_abi, ledger_abi = session.hub, session.hub_abi, session.vault_abi, session.ledger_abi
    callback_abi = ops.load_artifact("CanaryCallback").abi
    request_topic = ops.load_artifact("ISystemContractCallbacks").abi.topic("CallbackRequest")
    requester = _owned_fixture(store, lasna.rpc, "junkRequester", account)

    # The flood takes the vault offline: each other target of that chain has no guardian in that time.
    recorded = {entry["address"].lower() for entry in store.entries.values()
                if isinstance(entry, dict) and entry.get("address")}
    others = [f"{r['label']} ({r['target']})" for r in session.manifest.data["enrollments"]
              if r.get("chain") == args.chain and not r.get("removed") and r["target"].lower() not in recorded]
    if others and not args.allow_other_targets:
        raise OpsError(f"junk refuses: the vault of {args.chain} serves targets that are not fixtures of the "
                       f"experiment: {', '.join(others)}. The flood stops their protection until the recovery. "
                       f"Remove them, or give --allow-other-targets. No transaction was sent")

    guard_scenarios(args.scenarios, [lasna, chain])
    scenarios = Scenarios(args.scenarios)
    addresses = {"vault": vault, "hub": hub, "junkRequester": requester, "waiting.callback": waiting}
    record = scenarios.section("junk", args.chain, addresses)
    if record.get("endBlock") is not None:
        say(f"junk: {scenarios.path} has a complete junk scenario of {args.chain} (blocks "
            f"{record['startBlock']}..{record['endBlock']}); no transaction. A second scenario needs a new "
            f"waiting canary and another --scenarios file")
        return 0
    say(f"junk on {args.chain} (chain ID {chain.chain_id}): vault {vault}, owner {owner.address}")

    def step(name: str, chain_name: str, log_or_block, tx=None) -> None:
        if isinstance(log_or_block, dict):
            log_or_block, tx = block_of(log_or_block), log_or_block["transactionHash"]
        add_step(record, name, chain_name, log_or_block, tx)
        scenarios.save()
        say(f"  {name}: {chain_name} block {log_or_block}" + (f", tx {tx}" if tx else ""))

    def expect(what: str, rpc: ops.Rpc, emitter: str, topics: list, from_block: int) -> dict:
        log = wait_for_event(rpc, emitter, topics, from_block, args.timeout)
        if log is None:
            raise OpsError(f"no {what} in {args.timeout} s: run the command again to continue")
        return log

    def ledger(contract: str) -> tuple:
        """(debt, reserves) of a contract at the proxy of the chain."""
        return (session.read_ledger(chain, "debt", contract)[1], session.read_ledger(chain, "reserves", contract)[1])

    vault_topic, waiting_topic = topic_of_address(vault), topic_of_address(waiting)
    chain_topic = topic_of_int(chain.chain_id)
    vault_debt_log = [ledger_abi.topic("BlacklistContract"), vault_topic]

    # 1. The canary that waits: `low` 0, so `setPolicy` does not fund it and its first callback is a debt.
    if record["startBlock"] is None:
        require_guardian(session, args.chain, what="the junk scenario")
        target = session.read_vault(args.chain, "selfReserveTarget")
        high, max_debt = max(target // 4, 1), max(target, 1)
        ensure_enrolled(args, args.chain, waiting, "experiment waiting.callback",
                        Policy(0, high, max_debt, max_debt + high, 2, 3600))
        if ledger(waiting) != (0, 0):
            raise OpsError("waiting.callback has a debt or reserves: its first callback must be its first debt")
        # 2. The start block of the flood, on both chains, and the float that the flood must use up.
        record["lasnaStartBlock"] = lasna.rpc.head()
        record["floatAtStart"] = str(ledger(vault)[1])
        record["junkRequests"] = 0
        record["startBlock"] = chain.rpc.head()
        step("flood start", args.chain, record["startBlock"])
    flood_start, lasna_start = record["startBlock"], record["lasnaStartBlock"]

    def requests_to(recipient: str) -> list:
        """The callback requests of the requester for one recipient since the start of the flood. The chain is
        the journal: a request of a run that stopped is in this list."""
        if not lasna.rpc.wait_idle(account.address):
            raise lasna.rpc.not_idle(account.address)
        return lasna.rpc.logs(lasna.ledger, [request_topic, chain_topic, topic_of_address(requester),
                                             topic_of_address(recipient)], lasna_start)

    def delivered() -> int:
        """The junk callbacks that the proxy delivered: each one is a `CallbackFailure(vault)`."""
        return len(chain.rpc.logs(chain.ledger, [ledger_abi.topic("CallbackFailure"), vault_topic], flood_start))

    def count_requests() -> int:
        sent = requests_to(vault)
        if sent and not find_step(record, "first junk request"):
            step("first junk request", LASNA, sent[0])
        if sent:
            record["lastJunkRequest"] = {"block": block_of(sent[-1]), "tx": sent[-1]["transactionHash"]}
        record["junkRequests"] = len(sent)
        scenarios.save()
        return len(sent)

    # 2. Junk requests until the proxy reports the debt of the vault. Each one has the selector of
    #    `reconcile` and a wrong first argument: the vault refuses it, and the proxy charges the float. The
    #    next burst goes out only when the proxy delivered each request before it.
    if not find_step(record, "BlacklistContract(vault)"):
        payload = vault_abi.encode("reconcile", ZERO_ADDRESS, ZERO_ADDRESS, 0)
        deadline = time.monotonic() + args.timeout
        log = wait_for_event(chain.rpc, chain.ledger, vault_debt_log, flood_start, 0)
        while log is None:
            if time.monotonic() >= deadline:
                raise OpsError(f"no BlacklistContract(vault) in {args.timeout} s, after {record['junkRequests']} "
                               f"junk requests: run the command again to continue")
            sent, arrived = count_requests(), delivered()
            debt, reserves = ledger(vault)
            if not debt and arrived >= sent:
                if sent >= args.max_junk:
                    raise OpsError(f"the vault is not in debt after {sent} junk requests (--max-junk); its float "
                                   f"is {amount(reserves)}")
                # The flood needs a hub that can hear the debt event of the vault.
                require_guardian(session, what="the junk scenario")
                burst = next_burst(args.burst, args.max_junk - sent, arrived, int(record["floatAtStart"]), reserves)
                request_callbacks(session, account, owner.address, requester, args.chain, vault,
                                  [payload] * burst, JUNK_CALLBACK_GAS)
                say(f"  junk requests: {sent} sent and delivered, float of the vault {amount(reserves)}; "
                    f"{burst} more sent")
            log = wait_for_event(chain.rpc, chain.ledger, vault_debt_log, flood_start, args.pause)
        sent = count_requests()
        # A request that the proxy did not deliver before the debt is rejected with `InDebt` when it arrives.
        record["undeliveredAtDebt"] = max(sent - delivered(), 0)
        step("BlacklistContract(vault)", args.chain, log)
        if record["undeliveredAtDebt"]:
            say(f"  {record['undeliveredAtDebt']} junk request(s) were not delivered before the debt: wait "
                f"{args.drain} s for the node to reject them")
            time.sleep(args.drain)

    # 3. The hub reports the vault as down. Then the flood has ended.
    if not find_step(record, "VaultDown"):
        log = expect("VaultDown of the hub", lasna.rpc, hub, [hub_abi.topic("VaultDown"), chain_topic], lasna_start)
        record["floodEndBlock"] = chain.rpc.head()  # saved with the step
        step("VaultDown", LASNA, log)
    if record.get("floodEndBlock") is None:
        # A record of an earlier version of this step: the debt event of the vault is the end of its float.
        record["floodEndBlock"] = find_step(record, "BlacklistContract(vault)")["block"]
    if not find_step(record, "flood end"):
        step("flood end", args.chain, record["floodEndBlock"])

    # 4. A callback to the canary that waits: its debt event comes while the vault is down.
    if not find_step(record, "Pending"):
        waiting_debt_log = [ledger_abi.topic("BlacklistContract"), waiting_topic]
        log = wait_for_event(chain.rpc, chain.ledger, waiting_debt_log, flood_start, 0)
        waited = False
        while log is None:
            pongs = len(requests_to(waiting))
            if pongs and not waited:
                # A request of this run or of a run that stopped: wait for its delivery before the next one.
                log = wait_for_event(chain.rpc, chain.ledger, waiting_debt_log, flood_start,
                                     args.timeout / args.max_pongs)
                waited = True
                continue
            if pongs >= args.max_pongs:
                raise OpsError(f"waiting.callback is not in debt after {pongs} pong requests")
            require_guardian(session, what="the junk scenario")
            request_callbacks(session, account, owner.address, requester, args.chain, waiting,
                              [callback_abi.encode("pong", ZERO_ADDRESS, pongs + 1)], ops.CANARY_CALLBACK_GAS)
            waited = False
        # Each request is a step, also the ones of a run that stopped: the chain has them.
        for index, request in enumerate(requests_to(waiting), 1):
            if not find_step(record, f"pong request {index}"):
                step(f"pong request {index}", LASNA, request)
        # The node delivered a request that the driver sent after each junk request.
        step("BlacklistContract(waiting.callback)", args.chain, log)
        step("Pending", LASNA, expect("Pending of the hub for waiting.callback", lasna.rpc, hub,
                                      [hub_abi.topic("Pending"), chain_topic, waiting_topic], lasna_start))

    # 5. The recovery command of unit 6, then the catch-up: `VaultUp` with 0 remaining, and the rescue of the
    #    canary that waits in one of the sweeps of this recovery.
    if not find_step(record, "VaultUp"):
        run_ops(args, "recover", "--chain", args.chain)
        recoveries = Manifest.load(args.manifest).vaults[args.chain].get("recoveries") or []
        deposit = next((r for r in reversed(recoveries)
                        if r.get("deposit") and (r.get("depositBlock") or 0) >= flood_start), None)
        if deposit is None:
            raise OpsError("the manifest has no recovery deposit after the start of the flood")
        step("recovery deposit", args.chain, deposit["depositBlock"], deposit["deposit"])
    # The logs from the start of the flood: a run that continues finds the `VaultUp` of the run before it.
    ups = [(log, hub_abi.decode_event("VaultUp", log))
           for log in lasna.rpc.logs(hub, [hub_abi.topic("VaultUp"), chain_topic], lasna_start)]
    last = next((log for log, event in ups if event["remaining"] == 0), None)
    if last is None:
        raise OpsError("the hub emitted no VaultUp with 0 remaining after the recovery")
    if not find_step(record, "VaultUp"):
        step("VaultUp", LASNA, last)

    # The result is checked in each run: a result that is not a rescue never becomes a complete record.
    request_ids = {event["requestId"] for _, event in ups}
    results_filter = [vault_abi.topic("Result"), None, waiting_topic]
    deadline = time.monotonic() + args.timeout
    while True:
        results = [(log, vault_abi.decode_event("Result", log))
                   for log in chain.rpc.logs(vault, results_filter, flood_start)]
        found = rescue_of(results, request_ids)
        if found is not None or time.monotonic() >= deadline:
            break
        time.sleep(ops.POLL_SECONDS)
    if found is None:
        raise OpsError(f"no Result of the vault for waiting.callback from a sweep of this recovery in "
                       f"{args.timeout} s: run the command again to continue")
    log, event = found
    outcome = OUTCOMES[event["outcome"]]
    if outcome != "Rescued":
        raise OpsError(f"the Result of waiting.callback (tx {log['transactionHash']}) has outcome {outcome}, not "
                       f"Rescued: the scenario does not show the waiting rescue")
    record["requestId"] = str(event["id"])
    record["result"] = {"outcome": outcome, "paid": str(event["paid"])}
    if not find_step(record, "Result(waiting.callback)"):
        step("Result(waiting.callback)", args.chain, log)

    chain.rpc.floor = max(chain.rpc.floor, block_of(log))
    debt, _ = ledger(waiting)
    vault_debt, _ = ledger(vault)
    if debt or vault_debt:
        raise OpsError(f"after the recovery: debt of waiting.callback {debt}, debt of the vault {vault_debt}")
    # A junk callback that arrives after the recovery takes its fee from the new float. The record has the
    # number, so that the owner knows it.
    late = chain.rpc.logs(chain.ledger, [ledger_abi.topic("CallbackFailure"), vault_topic],
                          find_step(record, "recovery deposit")["block"])
    record["junkAfterRecovery"] = len(late)
    if late:
        say(f"  WARN {len(late)} junk callback(s) reached the vault after the recovery deposit (first tx "
            f"{late[0]['transactionHash']}): the new float paid them. Check the vault with "
            f"`python -m lifeline.ops status`")
    record["lasnaEndBlock"] = lasna.rpc.head()
    record["endBlock"] = chain.rpc.head()
    scenarios.save()
    say(f"junk: VaultDown after {record['junkRequests']} junk requests; recovery with the waiting rescue done "
        f"(paid {amount(int(record['result']['paid']))}); debt(waiting.callback) 0 at block {record['endBlock']}; "
        f"hashes in {scenarios.path}")
    return 0


# ----------------------------------------------------------------------------------------------
# measure
# ----------------------------------------------------------------------------------------------

GUARDIANS = {"A": "none", "B": "cron poller", "C": "Lifeline"}

DEFINITIONS = {
    "downtimeBlocks": (
        "Blocks inside the debt intervals of the reactive canary on Lasna and of the callback canary on the "
        "destination chain, for each chain: from a `BlacklistContract` log of the ledger to the next "
        "`WhitelistContract` log, or to `to`; the `Gap` event of the hub is not used, because its start block "
        "is 0 for a rescue that no debt log started."
    ),
    "eventsMissed": "`Poke(n)` logs of the reactive canary in `from..to` with no `Tick(n)` log of that canary.",
    "callbacksDropped": (
        "`Tick(n)` logs of the reactive canary in `from..to` with no `Pong(n)` log of its callback canary."
    ),
    "guardianCost": (
        "Principal is the sum that the guardian paid to the two contracts of the arm (arm B: `Paid` of the "
        "poller; arm C: `Rescued` and `ToppedUp` of the hub and `Result.paid` of the vault); fees are what the "
        "ledger took from the guardian: its reserves minus its debt at block `from - 1`, minus the same value at "
        "`to`, plus its `SelfCare` deposits, plus each deposit that cleared a debt of it (the ledger state at "
        "the block of its `WhitelistContract` log against the block before), and for the poller plus its fee "
        "float of the deployment; a fee that the ledger took in the block of such a deposit is not in the "
        "figure, and `feeDetail.exact` is false if a log shows work of the guardian in such a block."
    ),
    "capitalLocked": (
        "At `to`: the reserves of the two contracts of the arm, and the funds of the guardian (arm B: the "
        "balance and the reserves of the poller; arm C: the balance and the reserves of the hub and of the "
        "vault, which are shared with each other enrolled target)."
    ),
    "guardianDowntimeBlocks": (
        "Blocks inside the debt intervals of the guardian itself (arm B: the poller; arm C: the hub on Lasna and "
        "the vault on the destination chain), from the ledger logs: a hub at its principal floor goes into debt "
        "with no `HubLow` event, so that event is not the source."
    ),
    "junk": (
        "The balance of the vault at the start block and at the end block of the junk flood, the sums of "
        "`Result.paid` and `SelfCare.added` of the vault after the start block up to the end block, and the "
        "balance change that these payments do not explain; the expected value is 0."
    ),
}

FEE_SCOPES = {
    "A": "no guardian",
    "B": "the poller serves the reactive canary of this arm only; the figure is from its deployment to `to`",
    "C": ("the hub and the vault serve each enrolled target of the deployment and each scenario; the figures "
          "are their whole fee consumption in `from..to`, and no share is estimated"),
}


class Measurement:
    """The three-arm metrics and the junk evidence at the heights of one snapshot. Event scans over
    `from..to` and state reads go through the reader and the snapshot of unit 1."""

    def __init__(self, manifest: dict, readers: dict, snapshot: Snapshot):
        self.manifest, self.readers, self.snapshot = manifest, readers, snapshot
        self.fixtures = manifest["fixtures"]
        self.dest = self.fixtures["armA.callback"]["chain"]
        self.hub = manifest["contracts"]["hub"]["address"]
        self.hub_abi = ops.load_artifact("LifelineHub").abi
        self.vault_abi = ops.load_artifact("LifelineVault").abi
        self.reactive_abi = ops.load_artifact("CanaryReactive").abi
        self.callback_abi = ops.load_artifact("CanaryCallback").abi
        self.poller_abi = ops.load_artifact("CronPoller").abi
        self.proxy_abi = ops.load_artifact("ICallbackProxy").abi
        self._logs = {}

    # Reads ------------------------------------------------------------------------------------

    def span(self, chain: str) -> tuple:
        heights = self.snapshot.chains[chain]
        return heights["from"], heights["to"]

    def ledger(self, chain: str) -> str:
        return self.manifest["chains"][chain]["ledger"]

    def vault(self, chain: str) -> str:
        return self.manifest["contracts"]["vaults"][chain]["address"]

    def logs(self, chain: str, address: str, topics: list) -> list:
        key = (chain, address.lower(), json.dumps(topics).lower())
        if key not in self._logs:
            start, end = self.span(chain)
            self._logs[key] = self.readers[chain].logs(address, topics, start, end)
        return self._logs[key]

    def decoded(self, chain: str, address: str, abi: ops.Abi, event: str, *indexed) -> list:
        """(block, parameters) of each log of one event, with the given values of its indexed parameters."""
        logs = self.logs(chain, address, [abi.topic(event), *indexed])
        return [(int(log["blockNumber"], 16), abi.decode_event(event, log)) for log in logs]

    def ledger_state(self, chain: str, account: str, label: str, block=None) -> tuple:
        """(reserves, debt) of an account at the ledger of a chain, at `to` or at one block."""
        at = "" if block is None else f" at block {block}"
        ledger = self.ledger(chain)
        reserves, debt = self.snapshot.calls(self.readers[chain], [
            (ledger, ledger_call(RESERVES, account), f"reserves({label}) on {chain}{at}"),
            (ledger, ledger_call(DEBT, account), f"debt({label}) on {chain}{at}"),
        ], block)
        return int(reserves, 16), int(debt, 16)

    def balance(self, chain: str, address: str, label: str, block=None) -> int:
        at = "" if block is None else f" at block {block}"
        return self.snapshot.balance(self.readers[chain], address, f"balance of {label} on {chain}{at}", block)

    # Metrics ----------------------------------------------------------------------------------

    def debt_blocks(self, chain: str, account: str) -> tuple:
        """(blocks inside debt intervals, number of intervals) of one account in `from..to`."""
        start, end = self.span(chain)
        events = self.logs(chain, self.ledger(chain), [[BLACKLIST, WHITELIST], word(account)])
        intervals = debt_intervals(events)
        blocks = sum((int(closed["blockNumber"], 16) if closed else end)
                     - (int(opened["blockNumber"], 16) if opened else start) for opened, closed in intervals)
        return blocks, len(intervals)

    def numbers(self, chain: str, address: str, abi: ops.Abi, event: str) -> set:
        return {int(log["topics"][1], 16) for log in self.logs(chain, address, [abi.topic(event)])}

    def total(self, chain: str, address: str, abi: ops.Abi, event: str, field: str, *indexed) -> int:
        """The sum of one parameter over each log of one event."""
        return sum(values[field] for _, values in self.decoded(chain, address, abi, event, *indexed))

    def consumption(self, chain: str, account: str, label: str, abi: ops.Abi = None, first_deposit=0) -> dict:
        """What the ledger of a chain took from a guardian in `from..to`. No log has the amount of a fee, so
        the figure comes from the ledger state and from the deposits that the chain data shows.

        A deposit of another account emits no log with its amount. The one that clears a debt emits
        `WhitelistContract`: the ledger state at that block against the block before gives the amount, if
        the ledger took no fee from the guardian in that block. A fee of that block makes the deposit and
        the figure too low by that fee. `exact` is false if a log shows work of the guardian in such a
        block: a log of the guardian itself, or a `CallbackFailure` for it."""
        start, _ = self.span(chain)

        def net(block=None) -> int:
            reserves, debt = self.ledger_state(chain, account, label, block)
            return reserves - debt

        opening, closing = net(start - 1), net()
        self_care = self.decoded(chain, account, abi, "SelfCare") if abi is not None else []
        cleared = {int(log["blockNumber"], 16)
                   for log in self.logs(chain, self.ledger(chain), [WHITELIST, word(account)])}
        deposits = first_deposit
        for block in sorted(cleared):
            same_block = sum(event["added"] for number, event in self_care if number == block)
            deposits += max(net(block) - net(block - 1) - same_block, 0)
        work = self.logs(chain, account, []) + self.logs(
            chain, self.ledger(chain), [self.proxy_abi.topic("CallbackFailure"), word(account)])
        added = sum(event["added"] for _, event in self_care)
        return {
            "opening": str(opening), "closing": str(closing), "selfCare": str(added), "deposits": str(deposits),
            "fees": str(opening - closing + added + deposits),
            "exact": not any(int(log["blockNumber"], 16) in cleared for log in work),
        }

    def arm(self, arm: str) -> dict:
        dest = self.dest
        reactive = self.fixtures[f"arm{arm}.reactive"]["address"]
        callback = self.fixtures[f"arm{arm}.callback"]["address"]
        pokes = self.numbers(LASNA, reactive, self.reactive_abi, "Poke")
        ticks = self.numbers(LASNA, reactive, self.reactive_abi, "Tick")
        pongs = self.numbers(dest, callback, self.callback_abi, "Pong")
        lasna_blocks, lasna_intervals = self.debt_blocks(LASNA, reactive)
        dest_blocks, dest_intervals = self.debt_blocks(dest, callback)
        capital = {
            "reactive.reserves": str(self.ledger_state(LASNA, reactive, f"arm{arm}.reactive")[0]),
            "callback.reserves": str(self.ledger_state(dest, callback, f"arm{arm}.callback")[0]),
        }
        principal, detail, guardian_blocks, shared = {LASNA: 0, dest: 0}, {}, {}, []

        def holder(key: str, chain: str, address: str, label: str) -> None:
            capital[f"{key}.balance"] = str(self.balance(chain, address, label))
            capital[f"{key}.reserves"] = str(self.ledger_state(chain, address, label)[0])

        if arm == "B":
            poller = self.fixtures["armB.poller"]["address"]
            principal[LASNA] = self.total(LASNA, poller, self.poller_abi, "Paid", "amount", word(reactive))
            first = int(self.snapshot.call(self.readers[LASNA], poller, "0x" + self.poller_abi.encode("feeFloat").hex(),
                                           "feeFloat of the poller"), 16)
            detail[LASNA] = self.consumption(LASNA, poller, "armB.poller", first_deposit=first)
            guardian_blocks[LASNA] = self.debt_blocks(LASNA, poller)[0]
            holder("poller", LASNA, poller, "armB.poller")
        elif arm == "C":
            hub, vault = self.hub, self.vault(dest)
            principal[LASNA] = (self.total(LASNA, hub, self.hub_abi, "Rescued", "paid", word(reactive))
                                + self.total(LASNA, hub, self.hub_abi, "ToppedUp", "paid", word(reactive)))
            principal[dest] = self.total(dest, vault, self.vault_abi, "Result", "paid", None, word(callback))
            detail[LASNA] = self.consumption(LASNA, hub, "hub", self.hub_abi)
            detail[dest] = self.consumption(dest, vault, "vault", self.vault_abi)
            guardian_blocks = {LASNA: self.debt_blocks(LASNA, hub)[0], dest: self.debt_blocks(dest, vault)[0]}
            holder("hub", LASNA, hub, "hub")
            holder("vault", dest, vault, "vault")
            shared = ["hub.balance", "hub.reserves", "vault.balance", "vault.reserves"]
        fees = {LASNA: "0", dest: "0", **{chain: parts["fees"] for chain, parts in detail.items()}}

        return {
            "guardian": GUARDIANS[arm],
            "contracts": {"reactive": reactive, "callback": callback},
            "pokes": len(pokes), "ticks": len(ticks), "pongs": len(pongs),
            "downtimeBlocks": {LASNA: lasna_blocks, dest: dest_blocks},
            "debtIntervals": {LASNA: lasna_intervals, dest: dest_intervals},
            "eventsMissed": len(pokes - ticks),
            "callbacksDropped": len(ticks - pongs),
            "guardianCost": {
                "principal": {chain: str(value) for chain, value in principal.items()},
                "fees": fees,
                "feeScope": FEE_SCOPES[arm],
                "feeDetail": detail,
            },
            "guardianDowntimeBlocks": guardian_blocks,
            "capitalLocked": capital,
            "capitalShared": shared,
        }

    def junk(self) -> dict:
        flood = self.snapshot.params.get("junk")
        if not flood:
            return {"status": "not recorded"}
        chain, start, end = flood["chain"], flood["startBlock"], flood["endBlock"]
        result = {"status": "ok", "chain": chain, "startBlock": start, "endBlock": end}
        heights = self.snapshot.chains.get(chain)
        if heights is None or not heights["from"] - 1 <= start <= end <= heights["to"]:
            return {**result, "status": "outside the heights"}
        vault = self.vault(chain)

        def inside(events) -> list:
            return [event for block, event in events if start < block <= end]

        paid = sum(event["paid"] for event in inside(self.decoded(chain, vault, self.vault_abi, "Result")))
        added = sum(event["added"] for event in inside(self.decoded(chain, vault, self.vault_abi, "SelfCare")))
        failures = self.logs(chain, self.ledger(chain), [self.proxy_abi.topic("CallbackFailure"), word(vault)])
        before = self.balance(chain, vault, "the vault", start)
        after = self.balance(chain, vault, "the vault", end)
        reserves_start, _ = self.ledger_state(chain, vault, "vault", start)
        reserves_end, debt_end = self.ledger_state(chain, vault, "vault", end)
        return {
            **result,
            "vault": vault,
            "balanceStart": str(before), "balanceEnd": str(after),
            "resultPaid": str(paid), "selfCareAdded": str(added),
            "unexplainedBalanceChange": str(after - before + paid + added),
            "reservesStart": str(reserves_start), "reservesEnd": str(reserves_end), "debtEnd": str(debt_end),
            "callbackFailures": len([log for log in failures if start < int(log["blockNumber"], 16) <= end]),
        }

    def run(self) -> dict:
        return {
            "version": 1,
            "heights": self.snapshot.chains,
            "definitions": DEFINITIONS,
            "arms": {arm: self.arm(arm) for arm in ARMS},
            "junk": self.junk(),
        }


def render_table(data: dict) -> str:
    """`table.md`: each value comes from the data file."""
    arms = data["arms"]
    # One order in each run: Lasna, then the destination chain.
    chains = [LASNA, *sorted(name for name in data["heights"] if name != LASNA)]
    lines = ["# Three-arm experiment", ""]
    lines += ["| Chain | Chain ID | From | To |", "|---|---|---|---|"]
    for name in chains:
        heights = data["heights"][name]
        lines.append(f"| {name} | {heights['chainId']} | {heights['from']} | {heights['to']} |")
    lines += ["", "Amounts are in wei of the chain that the row names.", ""]
    lines += ["| Metric | A: no guardian | B: cron poller | C: Lifeline |", "|---|---|---|---|"]

    def row(label: str, value) -> None:
        lines.append(f"| {label} | " + " | ".join(str(value(arms[arm])) for arm in ARMS) + " |")

    def listing(values: dict) -> str:
        return "<br>".join(f"{key} {value}" for key, value in values.items()) or "none"

    row("Pokes, ticks, pongs", lambda arm: f"{arm['pokes']}, {arm['ticks']}, {arm['pongs']}")
    for chain in chains:
        row(f"Blocks of downtime, {chain}", lambda arm, chain=chain: arm["downtimeBlocks"][chain])
    row("Events missed", lambda arm: arm["eventsMissed"])
    row("Callbacks dropped", lambda arm: arm["callbacksDropped"])
    for chain in chains:
        row(f"Guardian cost: principal, {chain}", lambda arm, chain=chain: arm["guardianCost"]["principal"][chain])
    for chain in chains:
        row(f"Guardian cost: fees, {chain}", lambda arm, chain=chain: arm["guardianCost"]["fees"][chain])
    row("Guardian cost: fee scope", lambda arm: arm["guardianCost"]["feeScope"])
    row("Blocks of guardian downtime", lambda arm: listing(arm["guardianDowntimeBlocks"]))
    row("Capital locked", lambda arm: listing(arm["capitalLocked"]))
    row("Capital shared with the fleet", lambda arm: ", ".join(arm["capitalShared"]) or "none")

    lines += ["", "## Definitions", ""]
    titles = {"downtimeBlocks": "Blocks of downtime", "eventsMissed": "Events missed",
              "callbacksDropped": "Callbacks dropped", "guardianCost": "Guardian cost",
              "capitalLocked": "Capital locked", "guardianDowntimeBlocks": "Blocks of guardian downtime",
              "junk": "Junk flood"}
    lines += [f"- **{titles[name]}**: {text}" for name, text in data["definitions"].items()]

    junk = data["junk"]
    lines += ["", "## Junk flood", ""]
    if junk["status"] != "ok":
        lines.append(f"Status: {junk['status']}.")
    else:
        lines += ["| Value | Number |", "|---|---|"]
        lines += [f"| {label} | {junk[key]} |" for label, key in (
            ("Chain", "chain"), ("Vault", "vault"), ("Start block", "startBlock"), ("End block", "endBlock"),
            ("Balance of the vault at the start block", "balanceStart"),
            ("Balance of the vault at the end block", "balanceEnd"),
            ("Sum of `Result.paid`", "resultPaid"), ("Sum of `SelfCare.added`", "selfCareAdded"),
            ("Balance change that the payments do not explain", "unexplainedBalanceChange"),
            ("Reserves of the vault at the start block", "reservesStart"),
            ("Reserves of the vault at the end block", "reservesEnd"),
            ("Debt of the vault at the end block", "debtEnd"),
            ("`CallbackFailure` logs of the vault", "callbackFailures"),
        )]
    return "\n".join(lines) + "\n"


def _flood_of(path: Path):
    """The block range of the junk flood from the scenario file, or None if no flood is recorded."""
    junk = Scenarios(path).data.get("junk") or {}
    if junk.get("startBlock") is None or junk.get("floodEndBlock") is None:
        return None
    return {"chain": junk["chain"], "startBlock": junk["startBlock"], "endBlock": junk["floodEndBlock"]}


def _guard_local_outputs(args, urls: list) -> None:
    """A fork has the chain IDs of the live chains and other logs. A run against a local node must not write
    the cache or the evidence of the live run. `urls`: each RPC URL that the run uses."""

    def is_local(url: str) -> bool:
        host = urllib.parse.urlsplit(url).hostname or ""
        try:
            return host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    if not any(is_local(url) for url in urls):
        return
    defaults = [flag for flag, value, default in (("--cache", args.cache, ".cache/chain"),
                                                  ("--out", args.out, str(DEFAULT_OUT)))
                if Path(value).resolve() == Path(default).resolve()]
    if defaults:
        raise OpsError(f"an RPC is a local node: a dry run needs its own {' and '.join(defaults)}")


def cmd_measure(args) -> int:
    try:
        manifest = Manifest.load(args.manifest).data
        fixtures = manifest["fixtures"]
        absent = [name for name, _, _ in FIXTURES if name.startswith("arm")
                  and not (isinstance(fixtures.get(name), dict) and fixtures[name].get("address"))]
        if absent:
            raise OpsError(f"the manifest {args.manifest} has no fixtures entry {', '.join(absent)}")
        if not manifest["contracts"].get("hub"):
            raise OpsError(f"the manifest {args.manifest} has no hub")
        arm_chains = {fixtures[f"arm{arm}.callback"]["chain"] for arm in ARMS}
        if len(arm_chains) != 1:
            raise OpsError("the callback canaries of the three arms are not on one chain")
        names = [LASNA, *arm_chains]
        config = {"chains": {name: manifest["chains"][name] for name in names}}
        rpc = parse_reader_rpc(args.rpc)
        unknown = sorted(set(rpc) - set(manifest["chains"]))
        if unknown:
            raise OpsError(f"--rpc names a chain that the manifest does not have: {', '.join(unknown)}")
        # A chain of the manifest with no fixture is not read.
        rpc = {name: urls for name, urls in rpc.items() if name in names}
        # The URLs of the run: of `--rpc`, or of the manifest for a chain with no `--rpc`.
        _guard_local_outputs(args, [url for name in names
                                    for url in rpc.get(name) or manifest["chains"][name].get("rpc") or []])
        readers = open_readers(config, rpc, args.cache, retries=args.retries, backoff=args.backoff)
        lowest = {name: min(entry["deployBlock"] for entry in fixtures.values()
                            if isinstance(entry, dict) and entry.get("chain") == name and entry.get("deployBlock"))
                  for name in names}
        if args.heights:
            snapshot = Snapshot.load(args.heights, readers)
            if "junk" not in snapshot.params:
                raise SnapshotError(f"{args.heights}: params has no junk")
        else:
            snapshot = Snapshot.pin(readers, lambda reader, to: lowest[reader.name],
                                    {"junk": _flood_of(Path(args.scenarios))})
        for name in names:
            if snapshot.chains[name]["from"] > snapshot.chains[name]["to"]:
                raise SnapshotError(f"{name}: the head block {snapshot.chains[name]['to']} is before the first "
                                    f"fixture block {snapshot.chains[name]['from']}")
        data = Measurement(manifest, readers, snapshot).run()
    except (ConfigError, ChainMismatch, SnapshotError, OpsError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (ReaderError, StateUnavailable, CallReverted) as error:
        # No number is printed and no file is written. The cache keeps the finished chunks.
        print(f"error: {error}", file=sys.stderr)
        return 1

    out = Path(args.out)
    try:
        publish(out, {
            "data.json": json.dumps(data, indent=1, sort_keys=True) + "\n",
            "table.md": render_table(data),
            "heights.json": snapshot.text(),
        })
    except OSError as error:
        print(f"error: cannot write the output files: {error}", file=sys.stderr)
        return 1

    for name in names:
        heights = snapshot.chains[name]
        say(f"heights {name} chainId {heights['chainId']} from {heights['from']} to {heights['to']}")
    for arm in ARMS:
        values = data["arms"][arm]
        say(f"arm {arm} ({values['guardian']}): downtime {values['downtimeBlocks']}, events missed "
            f"{values['eventsMissed']} of {values['pokes']}, callbacks dropped {values['callbacksDropped']} of "
            f"{values['ticks']}")
    junk = data["junk"]
    say(f"junk flood: {junk['status']}"
        + (f"; balance change of the vault that the payments do not explain: {junk['unexplainedBalanceChange']}"
           if junk["status"] == "ok" else ""))
    for name in snapshot.from_saved:
        say(f"from snapshot (the node has no state at the old block): {name}")
    for value in snapshot.differs:
        say(f"differs from snapshot (the saved value is used): {value}")
    say(f"wrote {out / 'data.json'}, {out / 'table.md'}, {out / 'heights.json'}")
    return 0


# ----------------------------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", default=str(ops.DEFAULT_MANIFEST), metavar="<file>",
                        help="manifest file (default deployments/manifest.json)")
    common.add_argument("--rpc", action="append", default=[], metavar="<chain>=<url>",
                        help="RPC URL of one chain; repeatable; replaces the RPC list of that chain")
    scenario = argparse.ArgumentParser(add_help=False)
    scenario.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS), metavar="<file>",
                          help="scenario file (default evidence/experiment/scenarios.json)")

    parser = argparse.ArgumentParser(prog="python -m lifeline.experiment",
                                     description="Lifeline experiment: fixtures, load, scenarios and measurement.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    fixtures = commands.add_parser("fixtures", parents=[common], help="deploy and record the experiment fixtures")
    fixtures.add_argument("--dest-chain", default="base-sepolia", metavar="<name>",
                          help="destination chain of the three callback canaries (default base-sepolia)")
    for flag, text in (
        ("--reactive-reserve", "start reserves of each reactive canary, in lREACT"),
        ("--callback-reserve", "start reserves of each callback canary, in the unit of the destination chain"),
        ("--lasna-low", "low mark of arm B and of arm C on Lasna"),
        ("--lasna-high", "high mark of arm B and of arm C on Lasna"),
        ("--dest-low", "low mark of the callback canary of arm C"),
        ("--dest-high", "high mark of the callback canary of arm C"),
        ("--poller-float", "fee float of the cron poller, in lREACT"),
        ("--poller-principal", "balance of the cron poller for its payments, in lREACT"),
        ("--looping-high", "high mark of the looping canary: below one reaction fee"),
    ):
        fixtures.add_argument(flag, metavar="<amount>", help=f"{text} (default: from the current gas price)")
    fixtures.add_argument("--max-rescues", type=int, default=2, metavar="<n>",
                          help="maxRescues of the looping canary (default 2)")
    fixtures.add_argument("--window", type=int, default=3600, metavar="<seconds>",
                          help="window of each fixture policy (default 3600)")
    fixtures.add_argument("--callback-gas", type=int, default=ops.CANARY_CALLBACK_GAS, metavar="<gas>",
                          help="gas limit of the callback of each reactive canary")
    fixtures.set_defaults(run=cmd_fixtures)

    load = commands.add_parser("load", parents=[common, scenario], help="poke the three reactive canaries")
    load.add_argument("--pokes", required=True, type=int, metavar="<n>", help="number of steps")
    load.add_argument("--interval", required=True, type=float, metavar="<seconds>", help="time between two steps")
    load.set_defaults(run=cmd_load)

    quarantine = commands.add_parser("quarantine", parents=[common, scenario],
                                     help="poke the looping canary until the hub quarantines it")
    quarantine.add_argument("--max-pokes", type=int, default=8, metavar="<n>", help="step limit of one run")
    quarantine.add_argument("--timeout", type=float, default=600.0, metavar="<seconds>",
                            help="longest wait for the hub after one poke")
    quarantine.add_argument("--settle", type=float, default=60.0, metavar="<seconds>",
                            help="wait after the poke that follows Quarantined")
    quarantine.set_defaults(run=cmd_quarantine)

    junk = commands.add_parser("junk", parents=[common, scenario],
                               help="flood the vault of one chain with junk callbacks, then recover it")
    junk.add_argument("--chain", required=True, metavar="<name>")
    junk.add_argument("--max-junk", type=int, default=1000, metavar="<n>", help="count limit of the junk requests")
    junk.add_argument("--burst", type=int, default=10, metavar="<n>", help="junk requests between two checks")
    junk.add_argument("--pause", type=float, default=5.0, metavar="<seconds>",
                      help="time between two reads of the destination chain while a burst is on its way")
    junk.add_argument("--drain", type=float, default=60.0, metavar="<seconds>",
                      help="wait after the debt of the vault if a junk request was not delivered before it")
    junk.add_argument("--max-pongs", type=int, default=3, metavar="<n>",
                      help="count limit of the callbacks to waiting.callback")
    junk.add_argument("--timeout", type=float, default=900.0, metavar="<seconds>", help="longest wait of one step")
    junk.add_argument("--allow-other-targets", action="store_true",
                      help="run although the vault serves targets that are not fixtures")
    junk.set_defaults(run=cmd_junk)

    measure = commands.add_parser("measure", parents=[common, scenario],
                                  help="build the three-arm table from chain data; no key, no transaction")
    measure.add_argument("--heights", metavar="<file>", help="reproduce the run at the heights of a saved snapshot")
    measure.add_argument("--out", default=str(DEFAULT_OUT), metavar="<dir>",
                         help="output directory (default evidence/experiment)")
    measure.add_argument("--cache", default=".cache/chain", metavar="<dir>", help="cache directory")
    measure.add_argument("--retries", type=int, default=8, help="retries of one request before the run fails")
    measure.add_argument("--backoff", type=float, default=1.0, help="first retry delay in seconds; it doubles")
    measure.set_defaults(run=cmd_measure)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (OpsError, RpcError, TransportError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
