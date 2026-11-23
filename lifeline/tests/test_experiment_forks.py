"""Fork dry run of the experiment: fixtures, load, scenarios and measurement.

The fixtures, the drivers, the operator commands, the contracts and the reader are real: the tests run
`python -m lifeline.experiment` and `python -m lifeline.forkrelay` against local anvil forks, with throwaway
keys and a scratch manifest. Only the node is emulated (the fork relay); cron ticks come from `--cron-tick`.

The tests need what the fork dry run of the operator commands needs (see `test_ops_forks.py`). They run in
the order of the file: each one continues on the state that the tests before it left.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("eth_account")

from eth_account import Account

from lifeline import experiment, ops
from lifeline.tests import forknet
from lifeline.tests.test_ops_forks import BASE, BUILT, LASNA, UPSTREAMS, DryRun, relayed

pytestmark = pytest.mark.skipif(
    UPSTREAMS is None or not Path(forknet.ANVIL).exists() or not BUILT
    or not (ops.ARTIFACTS / "CronPoller.sol" / "CronPoller.json").exists(),
    reason="the fork dry run needs anvil, `forge build` and the three RPC variables "
           + ", ".join(forknet.FORK_VARIABLES.values()),
)

HASH = re.compile(r"0x[0-9a-f]{64}")
ENTRY_NAMES = [name for name, _, _ in experiment.FIXTURES]
MAX_RESCUES = 2
KEYS = (ops.OWNER_KEY_VARIABLE, experiment.FIXTURE_KEY_VARIABLE)


class Experiment(DryRun):
    """The dry-run forks with a deployed hub and two vaults, a fixture key, and the experiment commands."""

    def _start(self, directory: Path):
        super()._start(directory)
        self.origin = self.blocks()  # no log query of a test goes below these blocks
        key = "0x" + secrets.token_hex(32)
        self.fixture = Account.from_key(key)
        self.env[experiment.FIXTURE_KEY_VARIABLE] = key
        os.environ[experiment.FIXTURE_KEY_VARIABLE] = key  # for the driver functions that a test calls
        for name in self.anvils:
            self.anvils[name].request("anvil_setBalance", [self.fixture.address, hex(100 * 10**18)])
        self.scenarios = directory / "scenarios.json"
        self.cache = directory / "cache"
        self.canary_abi = self.reactive.abi
        self.poller_abi = ops.load_artifact("CronPoller").abi

        result = self.ops("deploy", "--config", str(self.write_config()))
        assert result.returncode == 0, result.stderr + result.stdout
        # The fees of the node emulation: the gas price of the fork point, not the price that goes down.
        coeff = self.read(LASNA, self.ledger[LASNA], self.ledger_abi, "_gasPriceCoeffPer1000")
        self.reaction_fee = ops.trigger_fee(self.node.forks[LASNA].gas_price, coeff, experiment.REACTION_GAS)
        extra = self.read(BASE, self.ledger[BASE], self.ledger_abi, "_extraGas")
        coeff = self.read(BASE, self.ledger[BASE], self.ledger_abi, "_gasPriceCoeffPer1000")
        self.callback_fee = ops.sweep_fee(extra, experiment.CALLBACK_GAS_USED, self.node.forks[BASE].gas_price, coeff)

    def experiment(self, *arguments, manifest=None, env=None, keys=True, rpc=True) -> subprocess.CompletedProcess:
        """Runs `python -m lifeline.experiment`. Each request of the command goes through the recording proxy."""
        command = [sys.executable, "-m", "lifeline.experiment", *arguments, "--manifest",
                   str(manifest or self.manifest), *(self._rpc_arguments() if rpc else [])]
        if arguments[0] != "fixtures":
            command += ["--scenarios", str(self.scenarios)]
        environment = {**self.env, **(env or {})}
        if not keys:
            environment = {name: value for name, value in environment.items() if name not in KEYS}
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=600, cwd=ops.ROOT)

    def measure(self, out: Path, *arguments) -> subprocess.CompletedProcess:
        """`measure` with no key in the environment."""
        return self.experiment("measure", "--out", str(out), "--cache", str(self.cache), *arguments, keys=False)

    @contextlib.contextmanager
    def watch(self):
        """The node emulation in the background, for a command that waits for the node."""
        log = open(self.dir / "relay-watch.log", "a")
        process = subprocess.Popen(self.relay_command("--watch"), env=self.env, stdout=log,
                                   stderr=subprocess.STDOUT, cwd=ops.ROOT)
        try:
            yield process
        finally:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=120)
            log.close()
        assert process.returncode == 0, (self.dir / "relay-watch.log").read_text()[-4000:]

    def fixtures(self) -> dict:
        return self.data()["fixtures"]

    def address(self, name: str) -> str:
        return self.fixtures()[name]["address"]

    def ledger_state(self, name, account) -> tuple:
        ledger = self.ledger[name]
        return (self.read(name, ledger, self.ledger_abi, "reserves", account),
                self.read(name, ledger, self.ledger_abi, "debt", account))

    def numbers(self, name, address, abi, event) -> set:
        """The `n` of each log of one canary event: an independent pass, with no reader."""
        return {int(log["topics"][1], 16)
                for log in self.rpcs[name].logs(address, [abi.topic(event)], self.origin[name])}

    def events(self, name, address, abi, event, *indexed) -> list:
        logs = self.rpcs[name].logs(address, [abi.topic(event), *indexed], self.origin[name])
        return [abi.decode_event(event, log) for log in logs]

    def scenario(self, name: str) -> dict:
        return json.loads(self.scenarios.read_text())[name]


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    run = Experiment(tmp_path_factory.mktemp("experiment"))
    yield run
    run.close()


def amount_flags(net) -> list:
    """The amounts of the fixtures at the fees of the node emulation."""
    reaction, callback = net.reaction_fee, net.callback_fee
    return [
        "--reactive-reserve", ops.from_wei(5 * reaction), "--callback-reserve", ops.from_wei(callback * 3 // 2),
        "--lasna-low", ops.from_wei(reaction), "--lasna-high", ops.from_wei(4 * reaction),
        "--dest-low", ops.from_wei(callback), "--dest-high", ops.from_wei(4 * callback),
        "--poller-float", ops.from_wei(20 * reaction), "--poller-principal", ops.from_wei(12 * reaction),
        "--looping-high", ops.from_wei(reaction // 16), "--max-rescues", str(MAX_RESCUES),
    ]


def step_names(record: dict) -> list:
    return [entry["step"] for entry in record["steps"]]


# ----------------------------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------------------------


def test_fixtures_writes_the_ten_entries_and_enrolls_arm_c_only(net):
    before = net.data()
    result = net.experiment("fixtures", *amount_flags(net))
    assert result.returncode == 0, result.stderr + result.stdout

    data, entries = net.data(), net.fixtures()
    assert list(entries) == ENTRY_NAMES and len(entries) == 10
    for name, contract, on_lasna in experiment.FIXTURES:
        entry, chain = entries[name], LASNA if on_lasna else BASE
        assert (entry["contract"], entry["chain"], entry["chainId"]) == (contract, chain, net.chain_id[chain])
        receipt = net.rpcs[chain].receipt(entry["deployTx"])
        assert ops.same_address(receipt["contractAddress"], entry["address"])
        assert int(receipt["blockNumber"], 16) == entry["deployBlock"]
        assert ops.same_address(receipt["from"], net.fixture.address), "the fixture key deploys the fixtures"
        assert net.rpcs[chain].code(entry["address"]) and "pendingDeploy" not in entry
    # The command writes only the fixtures section. The enroll command adds its own records.
    for key in before:
        if key not in ("fixtures", "enrollments"):
            assert data[key] == before[key], key
    assert [record["label"] for record in data["enrollments"][len(before["enrollments"]):]] == \
        ["experiment armC.reactive", "experiment armC.callback", "experiment looping"]

    # Arm C has its two contracts at the hub and at the vault. Arm A and arm B have no guardian there.
    vault = net.vault(BASE)
    for arm, enrolled in (("A", False), ("B", False), ("C", True)):
        reactive, callback = entries[f"arm{arm}.reactive"]["address"], entries[f"arm{arm}.callback"]["address"]
        assert net.target_state(LASNA, reactive) == ("Active" if enrolled else "None")
        assert net.target_state(BASE, callback) == ("Active" if enrolled else "None")
        assert net.read(BASE, vault, net.vault_abi, "hasPolicy", callback) is enrolled
    assert net.target_state(LASNA, entries["looping"]["address"]) == "Active"
    assert net.target_state(BASE, entries["waiting.callback"]["address"]) == "None"

    # The three automations are the same: the code, the start reserves on both ledgers, the callback target.
    reaction, callback_fee = net.reaction_fee, net.callback_fee
    for arm in experiment.ARMS:
        reactive, callback = entries[f"arm{arm}.reactive"], entries[f"arm{arm}.callback"]
        assert net.ledger_state(LASNA, reactive["address"]) == (5 * reaction, 0)
        assert net.ledger_state(BASE, callback["address"]) == (callback_fee * 3 // 2, 0)
        creation = bytes.fromhex(net.rpcs[LASNA].transaction(reactive["deployTx"])["input"][2:])
        assert creation.startswith(net.reactive.bytecode), "the code of the build output"
        arguments = net.reactive.abi.decode_constructor(creation[len(net.reactive.bytecode):])
        assert arguments == {"systemContract": net.ledger[LASNA], "callbackChainId": 84532,
                             "callbackRecipient": callback["address"], "callbackGasLimit": ops.CANARY_CALLBACK_GAS}

    # Arm B: the poller has the ward, the cron topic of the manifest and the marks of arm C on Lasna.
    poller = entries["armB.poller"]["address"]
    policy = next(r["policy"] for r in data["enrollments"] if r["label"] == "experiment armC.reactive")
    assert net.read(LASNA, poller, net.poller_abi, "ward") == entries["armB.reactive"]["address"]
    assert net.read(LASNA, poller, net.poller_abi, "cronTopic") == experiment.cron_topic0(data["cronTopic"])
    assert (str(net.read(LASNA, poller, net.poller_abi, "low")), str(net.read(LASNA, poller, net.poller_abi, "high"))) \
        == (policy["low"], policy["high"]) == (str(reaction), str(4 * reaction))
    assert net.ledger_state(LASNA, poller) == (20 * reaction, 0)
    assert net.rpcs[LASNA].balance(poller) == 12 * reaction
    looping = next(r["policy"] for r in data["enrollments"] if r["label"] == "experiment looping")
    assert int(looping["high"]) < reaction and looping["low"] == "0" and looping["maxRescues"] == MAX_RESCUES

    # A second run sends nothing and changes nothing.
    blocks, mark = net.blocks(), net.proxy.mark()
    result = net.experiment("fixtures", *amount_flags(net))
    assert result.returncode == 0, result.stderr + result.stdout
    assert net.blocks() == blocks and net.proxy.sent(mark) == [] and net.data() == data


def test_the_drivers_take_their_targets_from_the_fixtures_section_only(net):
    blocks, mark = net.blocks(), net.proxy.mark()
    # No command line argument names a target.
    stranger = net.address("armA.reactive")
    for command in (["load", "--pokes", "1", "--interval", "0"], ["quarantine"]):
        for flag in ("--target", "--address", "--canary"):
            result = net.experiment(*command, flag, stranger)
            assert result.returncode == 2 and "unrecognized arguments" in result.stderr

    # A manifest with no fixtures entry for one canary: `load` stops before any transaction.
    for absent in ("armB.reactive", "armC.callback"):
        manifest = net.dir / "absent.json"
        data = net.data()
        del data["fixtures"][absent]
        manifest.write_text(json.dumps(data))
        result = net.experiment("load", "--pokes", "1", "--interval", "0", manifest=manifest)
        assert result.returncode == 1, result.stdout
        assert f"no fixtures entry {absent}" in result.stderr and "No transaction was sent" in result.stderr
    manifest.write_text(json.dumps({**net.data(), "fixtures": {}}))
    assert net.experiment("quarantine", manifest=manifest).returncode == 1
    assert net.blocks() == blocks and net.proxy.sent(mark) == []
    assert not net.scenarios.exists()

    # A dry run does not write the scenario file of the live run.
    command = [sys.executable, "-m", "lifeline.experiment", "load", "--pokes", "1", "--interval", "0", "--manifest",
               str(net.manifest), *net._rpc_arguments()]
    result = subprocess.run(command, env=net.env, capture_output=True, text=True, timeout=120, cwd=ops.ROOT)
    assert result.returncode == 1 and "local development node" in result.stderr
    assert net.blocks() == blocks


# ----------------------------------------------------------------------------------------------
# Load and table
# ----------------------------------------------------------------------------------------------

# The test of the run that stops leaves 2 steps: the one that stopped and the one of the run after it.
ROUNDS_OF_THE_STOPPED_RUN, ROUNDS_BEFORE_THE_TICK, ROUNDS_AFTER_THE_TICK = 2, 7, 3


def test_load_completes_the_step_of_a_run_that_stopped_and_gives_each_arm_the_same_pokes(net):
    """A run that stops after the poke of arm A must not leave arm A with one poke more than the other arms:
    the three arms get the same scripted load. The chain is the journal: the next run writes the poke that
    the record does not have, sends the pokes of the step that stopped, and then runs its own steps."""
    reactives = {arm: net.address(f"arm{arm}.reactive") for arm in experiment.ARMS}
    load = ["load", "--pokes", "1", "--interval", "0"]

    def pokes() -> dict:
        return {arm: net.numbers(LASNA, reactives[arm], net.canary_abi, "Poke") for arm in experiment.ARMS}

    # The node takes the first transaction of the command and gives no answer after it: the poke of arm A is
    # in a block, the command did not see its receipt, and it sent no poke for arm B and arm C.
    def stop_after_the_first_transaction(chain, method, params, result):
        if method == "eth_sendRawTransaction":
            net.proxy.refuse = ("eth_",)
        return result

    mark = net.proxy.mark()
    net.proxy.tamper = stop_after_the_first_transaction
    try:
        result = net.experiment(*load)
    finally:
        net.proxy.tamper, net.proxy.refuse = None, ()
    assert result.returncode == 1, result.stdout
    (first,) = net.proxy.sent(mark)
    assert pokes() == {"A": {1}, "B": set(), "C": set()}
    record = net.scenario("load")
    assert step_names(record) == ["start", "stopped"], "the run that stopped does not leave `end`"
    assert record["startBlock"] <= record["endBlock"]

    # A node that is behind gives no `Poke` log. The counter of the canary at the block of the read says 1, so
    # the command does not take arm A for an arm with no poke: it sends nothing.
    saved = net.scenarios.read_text()
    topic = net.canary_abi.topic("Poke")

    def no_poke_logs(chain, method, params, result):
        return [] if method == "eth_getLogs" and params[0]["topics"][0] == topic else result

    def refused(text: str) -> None:
        blocks, mark = net.blocks(), net.proxy.mark()
        net.proxy.tamper = no_poke_logs
        try:
            result = net.experiment(*load)
        finally:
            net.proxy.tamper = None
        assert result.returncode == 1 and text in result.stderr, result.stderr + result.stdout
        assert "No transaction was sent" in result.stderr and net.proxy.sent(mark) == [] and net.blocks() == blocks

    refused("0 Poke log(s) of armA.reactive up to block")
    # A record with a poke that the chain does not have is the record of another chain.
    other = json.loads(saved)
    experiment.add_step(other["load"], "poke 1 armA", LASNA, other["load"]["endBlock"], "0x" + "ee" * 32)
    net.scenarios.write_text(json.dumps(other))
    mark = net.proxy.mark()
    result = net.experiment(*load)
    assert result.returncode == 1 and "does not have the log of poke 1 armA" in result.stderr, result.stdout
    assert net.proxy.sent(mark) == []
    net.scenarios.write_text(saved)

    # The next run, at a node that goes back: after the read that shows the account as idle, the node gives
    # the block before the poke of arm A as its newest block three times. The command reads the logs at the
    # block of the idle read, not at an older one. The poke of arm A gets its step, arm B and arm C get the
    # poke of the step that stopped, and then the one step of this run goes to each arm.
    poke_block = int(net.rpcs[LASNA].receipt(first[1])["blockNumber"], 16)
    node = {"idle": False, "stale": 0}

    def goes_back(chain, method, params, result):
        if chain != LASNA:
            return result
        if method == "eth_getTransactionCount" and params[1] == "pending":
            node["idle"] = True
        elif method == "eth_blockNumber" and node["idle"] and node["stale"] < 3:
            node["stale"] += 1
            return hex(poke_block - 1)
        return result

    mark = net.proxy.mark()
    net.proxy.tamper = goes_back
    try:
        result = net.experiment(*load)
    finally:
        net.proxy.tamper = None
    assert result.returncode == 0, result.stderr + result.stdout
    assert node["stale"] == 3, "the node went back after the idle read"
    assert "arm A: Poke(1)" in result.stdout and "was not in the record" in result.stdout
    assert result.stdout.count("step of an earlier run that stopped,") == 2
    assert pokes() == {arm: {1, 2} for arm in experiment.ARMS}, "the same number of pokes for each arm"
    assert len(net.proxy.sent(mark)) == 5, "2 pokes of the step that stopped and 3 of the new step"
    record = net.scenario("load")
    names = step_names(record)
    assert sorted(name for name in names if name.startswith("poke")) == sorted(
        f"poke {n} arm{arm}" for n in (1, 2) for arm in experiment.ARMS)
    assert names.count("start") == 2 and names.count("stopped") == 1 and names[-1] == "end"
    assert next(entry for entry in record["steps"] if entry["step"] == "poke 1 armA")["tx"] == first[1]
    relayed(net.relay())


def test_load_uses_up_the_start_reserves_and_measure_builds_the_table(net):
    entries = net.fixtures()

    def load_round():
        result = net.experiment("load", "--pokes", "1", "--interval", "0")
        assert result.returncode == 0, result.stderr + result.stdout
        relayed(net.relay())

    for _ in range(ROUNDS_BEFORE_THE_TICK):
        load_round()
    # One cron tick: the poller of arm B and the gauge of the hub run.
    relayed(net.relay("--cron-tick"))
    for _ in range(ROUNDS_AFTER_THE_TICK):
        load_round()
    rounds = ROUNDS_OF_THE_STOPPED_RUN + ROUNDS_BEFORE_THE_TICK + ROUNDS_AFTER_THE_TICK

    # The scenario file has the blocks and the hashes of the load, and no time.
    load = net.scenario("load")
    assert load["chain"] == LASNA and net.origin[LASNA] <= load["startBlock"] < load["endBlock"] <= net.blocks()[LASNA]
    assert load["addresses"] == {f"arm{arm}.reactive": entries[f"arm{arm}.reactive"]["address"]
                                 for arm in experiment.ARMS}
    pokes = [entry for entry in load["steps"] if entry["step"].startswith("poke")]
    assert len(pokes) == 3 * rounds and step_names(load).count("start") == rounds
    # Each run but the one that stopped sent each of its steps.
    assert step_names(load).count("end") == rounds - 1 and step_names(load).count("stopped") == 1
    for entry in pokes:
        receipt = net.rpcs[LASNA].receipt(entry["tx"])
        assert int(receipt["blockNumber"], 16) == entry["block"] and set(entry) == {"step", "chain", "block", "tx"}
        assert ops.same_address(receipt["from"], net.fixture.address)
    assert not re.search(r"time|date", net.scenarios.read_text(), re.IGNORECASE)

    out = net.dir / "out-load"
    result = net.measure(out)
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads((out / "data.json").read_text())
    table = (out / "table.md").read_text()
    heights = json.loads((out / "heights.json").read_text())

    # Heights: `from` is the lowest fixture block of each chain, `to` is pinned.
    assert set(data["heights"]) == {LASNA, BASE} and heights["chains"] == data["heights"]
    for chain in (LASNA, BASE):
        lowest = min(entry["deployBlock"] for entry in entries.values() if entry["chain"] == chain)
        assert data["heights"][chain]["from"] == lowest and data["heights"][chain]["to"] <= net.blocks()[chain]
    assert set(data) == {"version", "heights", "definitions", "arms", "junk"} and data["version"] == 1
    assert set(data["arms"]) == {"A", "B", "C"} and data["junk"] == {"status": "not recorded"}
    metrics = {"downtimeBlocks", "eventsMissed", "callbacksDropped", "guardianCost", "capitalLocked"}
    assert metrics <= set(data["definitions"])
    for arm in data["arms"].values():
        assert metrics <= set(arm) and set(arm["downtimeBlocks"]) == {LASNA, BASE}
        assert {"principal", "fees", "feeScope"} <= set(arm["guardianCost"])

    # The counts of each arm equal an independent pass over the Poke, Tick and Pong logs.
    counts = {}
    for name, arm in data["arms"].items():
        reactive, callback = entries[f"arm{name}.reactive"]["address"], entries[f"arm{name}.callback"]["address"]
        pokes = net.numbers(LASNA, reactive, net.canary_abi, "Poke")
        ticks = net.numbers(LASNA, reactive, net.canary_abi, "Tick")
        pongs = net.numbers(BASE, callback, net.callback.abi, "Pong")
        assert pokes == set(range(1, rounds + 1)), "the same scripted load for each arm"
        assert (arm["pokes"], arm["ticks"], arm["pongs"]) == (len(pokes), len(ticks), len(pongs))
        assert arm["eventsMissed"] == len(pokes - ticks) and arm["callbacksDropped"] == len(ticks - pongs)
        counts[name] = (arm["eventsMissed"], arm["callbacksDropped"])

    a, b, c = (data["arms"][name] for name in experiment.ARMS)
    # Arm A used its start reserves up and stays in debt on both chains.
    assert counts["A"][0] > 0 and counts["A"][1] > 0
    assert net.ledger_state(LASNA, entries["armA.reactive"]["address"])[1] > 0
    assert a["downtimeBlocks"][LASNA] > 0 and a["downtimeBlocks"][BASE] > 0
    assert a["guardianCost"]["principal"] == {LASNA: "0", BASE: "0"} and a["guardianCost"]["fees"] == {LASNA: "0", BASE: "0"}
    assert a["capitalLocked"] == {"reactive.reserves": "0", "callback.reserves": "0"}
    # Arm B: the poller paid at the tick, so fewer events are missed than in arm A. Its callback canary has no
    # guardian.
    paid = sum(event["amount"] for event in net.events(LASNA, entries["armB.poller"]["address"], net.poller_abi, "Paid"))
    assert paid > 0 and b["guardianCost"]["principal"] == {LASNA: str(paid), BASE: "0"}
    assert 0 < counts["B"][0] < counts["A"][0] and counts["B"][1] > 0
    assert 0 < b["downtimeBlocks"][LASNA] < a["downtimeBlocks"][LASNA]
    reserves, debt = net.ledger_state(LASNA, entries["armB.poller"]["address"])
    assert int(b["guardianCost"]["fees"][LASNA]) == 20 * net.reaction_fee - reserves + debt > 0
    assert b["capitalLocked"]["poller.reserves"] == str(reserves)
    assert b["capitalLocked"]["poller.balance"] == str(net.rpcs[LASNA].balance(entries["armB.poller"]["address"]))
    # Arm C: no event missed and no callback dropped. Its principal is the sum of the payments of the hub and
    # of the vault to its two contracts.
    assert counts["C"] == (0, 0)
    reactive, callback = (ops.topic_of_address(entries[f"armC.{part}"]["address"]) for part in ("reactive", "callback"))
    rescued = net.events(LASNA, net.hub, net.hub_abi, "Rescued", reactive)
    topped = net.events(LASNA, net.hub, net.hub_abi, "ToppedUp", reactive)
    results = net.events(BASE, net.vault(BASE), net.vault_abi, "Result", None, callback)
    assert rescued and [r for r in results if ops.OUTCOMES[r["outcome"]] == "Rescued"]
    assert c["guardianCost"]["principal"] == {
        LASNA: str(sum(event["paid"] for event in rescued + topped)),
        BASE: str(sum(event["paid"] for event in results)),
    }
    assert int(c["guardianCost"]["fees"][LASNA]) > 0 and int(c["guardianCost"]["fees"][BASE]) > 0
    assert "each enrolled target" in c["guardianCost"]["feeScope"] and "no share is estimated" in c["guardianCost"]["feeScope"]
    for holder, (chain, address) in {"hub": (LASNA, net.hub), "vault": (BASE, net.vault(BASE))}.items():
        assert c["capitalLocked"][f"{holder}.balance"] == str(net.rpcs[chain].balance(address))
        assert c["capitalLocked"][f"{holder}.reserves"] == str(net.ledger_state(chain, address)[0])
    assert c["capitalShared"] == ["hub.balance", "hub.reserves", "vault.balance", "vault.reserves"]
    assert c["guardianDowntimeBlocks"] == {LASNA: 0, BASE: 0}
    # The fees of the hub: the ledger identity with the self-care deposits of the range.
    detail = c["guardianCost"]["feeDetail"][LASNA]
    self_care = sum(net.hub_abi.decode_event("SelfCare", log)["added"]
                    for log in net.rpcs[LASNA].logs(net.hub, [net.hub_abi.topic("SelfCare")],
                                                    data["heights"][LASNA]["from"], data["heights"][LASNA]["to"]))
    assert detail["selfCare"] == str(self_care) and detail["deposits"] == "0"
    assert int(detail["fees"]) == int(detail["opening"]) - int(detail["closing"]) + self_care

    # The table has the three arms and the five metrics, and each number of it is in the data file.
    assert "| Metric | A: no guardian | B: cron poller | C: Lifeline |" in table
    for label in ("Blocks of downtime", "Events missed", "Callbacks dropped", "Guardian cost", "Capital locked"):
        assert f"| {label}" in table and f"**{label}**" in table
    text = (out / "data.json").read_text()
    for number in set(re.findall(r"\d{4,}", table.split("## Definitions")[0])):
        assert number in text, number
    assert f"| Events missed | {counts['A'][0]} | {counts['B'][0]} | 0 |" in table
    assert f"| Callbacks dropped | {counts['A'][1]} | {counts['B'][1]} | 0 |" in table
    for name in ("data.json", "table.md", "heights.json"):
        content = (out / name).read_text()
        assert "127.0.0.1" not in content and not re.search(r"\d{4}-\d{2}-\d{2}|wall", content), name


def test_measure_refuses_the_cache_and_the_evidence_of_the_live_run_for_a_local_node(net):
    evidence = ops.ROOT / "evidence" / "experiment"

    def files() -> dict:
        return {path.name: path.read_bytes() for path in evidence.glob("*")} if evidence.exists() else {}

    before = files()
    result = net.experiment("measure", keys=False)
    assert result.returncode == 2 and "a dry run needs its own --cache and --out" in result.stderr
    assert files() == before, "the dry run changed the evidence of the live run"


# ----------------------------------------------------------------------------------------------
# The guard of the junk driver
# ----------------------------------------------------------------------------------------------


def test_junk_refuses_a_recipient_that_is_not_a_manifest_vault_of_the_key(net):
    blocks, mark = net.blocks(), net.proxy.mark()

    def refused(result, text):
        assert result.returncode == 1, result.stdout
        assert "junk refuses" in result.stderr and text in result.stderr and "No transaction was sent" in result.stderr

    # A chain with no vault in the manifest.
    refused(net.experiment("junk", "--chain", LASNA), "has no vault")
    manifest = net.dir / "no-vault.json"
    data = net.data()
    del data["contracts"]["vaults"][BASE]
    manifest.write_text(json.dumps(data))
    refused(net.experiment("junk", "--chain", BASE, manifest=manifest), "has no vault")

    # A manifest entry that is not a vault: a canary of the experiment.
    data = net.data()
    data["contracts"]["vaults"][BASE]["address"] = net.address("armA.callback")
    manifest.write_text(json.dumps(data))
    refused(net.experiment("junk", "--chain", BASE, manifest=manifest), "it is not a vault")

    # A manifest vault with another owner: the configured key is not its `owner()`.
    other = "0x" + secrets.token_hex(32)
    result = net.experiment("junk", "--chain", BASE, env={ops.OWNER_KEY_VARIABLE: other})
    refused(result, f"the owner of the vault {net.vault(BASE)} of {BASE} is {net.account.address}")

    # The request function refuses each address that is not that vault and not a fixture.
    arguments = type("Arguments", (), {"manifest": str(net.manifest), "rpc": [
        f"{name}={net.proxy.url(name)}" for name in net.anvils]})()
    session = ops.Session(arguments, owner=False)
    payload = net.vault_abi.encode("reconcile", experiment.ZERO_ADDRESS, experiment.ZERO_ADDRESS, 0)
    # A contract of the fleet, an account, an address with no code, a fixture of another chain. (On the forks
    # a fresh key gives the hub and the vaults one address, so the hub is not in this list.)
    stranger = Account.from_key("0x" + secrets.token_hex(32)).address
    for recipient in (net.live["fleet"][1]["address"], net.fixture.address, stranger, net.address("looping")):
        with pytest.raises(ops.OpsError, match="junk refuses"):
            experiment.request_callbacks(session, net.fixture, net.account.address, net.address("junkRequester"),
                                         BASE, recipient, [payload], experiment.JUNK_CALLBACK_GAS)
    with pytest.raises(ops.OpsError, match="junk refuses"):
        experiment.request_callbacks(session, net.fixture, net.fixture.address, net.address("junkRequester"), BASE,
                                     net.vault(BASE), [payload], experiment.JUNK_CALLBACK_GAS)

    assert net.blocks() == blocks and net.proxy.sent(mark) == [], "a fork has a new transaction"
    assert "junk" not in json.loads(net.scenarios.read_text())

    # The guardian check reads the self-care of the day from the logs: the fee float of the constructor
    # is not in it, and the hub of the dry run can serve.
    deploy_block = net.data()["contracts"]["hub"]["deployBlock"]
    logs = net.rpcs[LASNA].logs(net.hub, [net.hub_abi.topic("SelfCare")], deploy_block)
    assert int(logs[0]["blockNumber"], 16) == deploy_block and len(logs) > 1
    assert experiment.self_care_today(session) == sum(
        net.hub_abi.decode_event("SelfCare", log)["added"] for log in logs[1:]) > 0
    assert experiment.guardian_problems(session, BASE) == []


# ----------------------------------------------------------------------------------------------
# Quarantine scenario
# ----------------------------------------------------------------------------------------------


def test_quarantine_cuts_the_looping_canary_off_after_max_rescues(net):
    looping = net.address("looping")
    policy = next(r["policy"] for r in net.data()["enrollments"] if r["label"] == "experiment looping")
    with net.watch():
        result = net.experiment("quarantine", "--timeout", "90", "--settle", "4")
    assert result.returncode == 0, result.stderr + result.stdout

    record = net.scenario("quarantine")
    names = step_names(record)
    assert [name for name in names if name.startswith("Rescued")] == [f"Rescued {n}" for n in range(1, MAX_RESCUES + 1)]
    assert names.count("Quarantined") == 1 and names[-1] == "poke after Quarantined"
    assert [name for name in names if name.startswith("poke ")] == [f"poke {n}" for n in range(1, MAX_RESCUES + 2)] \
        + ["poke after Quarantined"]
    assert record["chain"] == LASNA and record["addresses"] == {"looping": looping, "hub": net.hub}
    target = ops.topic_of_address(looping)
    for entry in record["steps"]:
        assert HASH.fullmatch(entry["tx"])
        receipt = net.rpcs[LASNA].receipt(entry["tx"])
        assert int(receipt["blockNumber"], 16) == entry["block"]
        if entry["step"].startswith(("Rescued", "Quarantined")):
            event = "Rescued" if entry["step"].startswith("Rescued") else "Quarantined"
            assert [log for log in ops.receipt_logs(receipt, net.hub, net.hub_abi.topic(event))
                    if log["topics"][1] == target], entry
    assert record["startBlock"] < record["endBlock"] <= net.blocks()[LASNA]

    # The budget is intact.
    rescues = net.events(LASNA, net.hub, net.hub_abi, "Rescued", target)
    paid = sum(event["paid"] for event in rescues)
    assert record["budget"] == {"maxRescues": MAX_RESCUES, "rescues": MAX_RESCUES, "paymentsAfterQuarantined": 0,
                                "principalPaid": str(paid), "dailyCap": policy["dailyCap"], "hubState": "Quarantined",
                                "intact": True}
    assert len(rescues) == MAX_RESCUES and 0 < paid <= int(policy["dailyCap"])
    assert net.target_state(LASNA, looping) == "Quarantined"
    assert not [key for key in net.node.read_subscriptions() if key[0] == net.hub and key[3][1] == int(looping, 16)]

    # A further poke gives no payment to the looping canary.
    reserves, debt = net.ledger_state(LASNA, looping)
    assert debt > 0
    experiment.poke(net.rpcs[LASNA], net.fixture, looping)
    relayed(net.relay())
    assert net.ledger_state(LASNA, looping) == (reserves, debt)
    assert len(net.events(LASNA, net.hub, net.hub_abi, "Rescued", target)) == MAX_RESCUES
    assert net.events(LASNA, net.hub, net.hub_abi, "ToppedUp", target) == []

    # A second run sends nothing: the record is complete.
    blocks = net.blocks()
    result = net.experiment("quarantine", "--timeout", "30", "--settle", "0")
    assert result.returncode == 0 and "complete quarantine scenario" in result.stdout
    assert net.blocks() == blocks and net.scenario("quarantine") == record


# ----------------------------------------------------------------------------------------------
# Junk scenario
# ----------------------------------------------------------------------------------------------


def junk_request_logs(net) -> list:
    topic = ops.load_artifact("ISystemContractCallbacks").abi.topic("CallbackRequest")
    return net.rpcs[LASNA].logs(net.ledger[LASNA], [topic, ops.topic_of_int(84532),
                                                    ops.topic_of_address(net.address("junkRequester")),
                                                    ops.topic_of_address(net.vault(BASE))], net.origin[LASNA])


def junk_requests(net) -> int:
    """The callback requests of the requester to the vault of Base Sepolia: an independent count."""
    return len(junk_request_logs(net))


def junk_numbers(net, tag: int) -> list:
    """The number of each junk request on the chain, in the order of the requests."""
    return [experiment.junk_number(net.vault_abi, tag, experiment.requested_payload(log))
            for log in junk_request_logs(net)]


def test_junk_counts_each_request_on_the_chain_and_stops_at_its_limit(net):
    """The limit of the junk requests holds over runs that stop: the chain is the journal of the requests."""
    short = ["--burst", "8", "--pause", "1", "--timeout", "60"]
    with net.watch():
        result = net.experiment("junk", "--chain", BASE, "--max-junk", "3", *short)
        assert result.returncode == 1 and "not in debt after 3 junk requests" in result.stderr, result.stdout
        record = net.scenario("junk")
        assert record["junkRequests"] == 3 == junk_requests(net)
        assert step_names(record) == ["flood start", "first junk request"] and record["endBlock"] is None
        # The first request goes out alone: the fee of one delivery is not known before it.
        assert "; 1 more sent" in result.stdout

        # A run that continues checks the hub again: a paused hub cannot report the vault as down.
        ops.transact(net.rpcs[LASNA], net.account, net.hub, net.hub_abi.encode("pause"), abi=net.hub_abi)
        try:
            blocks = net.blocks()
            result = net.experiment("junk", "--chain", BASE, "--max-junk", "50", *short)
            assert result.returncode == 1 and "the hub is paused" in result.stderr, result.stdout
            assert net.blocks() == blocks and junk_requests(net) == 3
        finally:
            ops.transact(net.rpcs[LASNA], net.account, net.hub, net.hub_abi.encode("unpause"), abi=net.hub_abi)

        # Requests of a run that stopped before it wrote its record: 2 more are on the chain. The burst of
        # that run had a transaction that failed, so the numbers have a gap: 4 and 6.
        arguments = type("Arguments", (), {"manifest": str(net.manifest), "rpc": [
            f"{name}={net.proxy.url(name)}" for name in net.anvils]})()
        payloads = [experiment.junk_payload(net.vault_abi, record["lasnaStartBlock"], number) for number in (4, 6)]
        experiment.request_callbacks(ops.Session(arguments, owner=False), net.fixture, net.account.address,
                                     net.address("junkRequester"), BASE, net.vault(BASE), payloads,
                                     experiment.JUNK_CALLBACK_GAS)
        assert junk_requests(net) == 5 and net.scenario("junk")["junkRequests"] == 3
        result = net.experiment("junk", "--chain", BASE, "--max-junk", "5", *short)
        assert result.returncode == 1 and "not in debt after 5 junk requests" in result.stderr, result.stdout
        assert junk_requests(net) == 5 == net.scenario("junk")["junkRequests"], "a request above the limit"
        assert junk_numbers(net, record["lasnaStartBlock"]) == [1, 2, 3, 4, 6]
    assert net.debt(BASE, net.vault(BASE)) == 0


def test_junk_takes_the_vault_offline_and_recover_does_the_waiting_rescue(net):
    vault, waiting = net.vault(BASE), net.address("waiting.callback")
    balance = net.rpcs[BASE].balance(vault)
    with net.watch():
        result = net.experiment("junk", "--chain", BASE, "--burst", "8", "--pause", "1", "--timeout", "180")
    assert result.returncode == 0, result.stderr + result.stdout

    record = net.scenario("junk")
    names = step_names(record)
    order = ["flood start", "first junk request", "BlacklistContract(vault)", "VaultDown", "flood end",
             "pong request 1", "BlacklistContract(waiting.callback)", "Pending", "recovery deposit", "VaultUp",
             "Result(waiting.callback)"]
    assert names == order, names
    steps = {entry["step"]: entry for entry in record["steps"]}
    chains = {"first junk request": LASNA, "VaultDown": LASNA, "pong request 1": LASNA, "Pending": LASNA, "VaultUp": LASNA}
    for name, entry in steps.items():
        chain = chains.get(name, BASE)
        assert entry["chain"] == chain
        if name in ("flood start", "flood end"):
            assert entry["tx"] is None
            continue
        assert HASH.fullmatch(entry["tx"]), name
        assert int(net.rpcs[chain].receipt(entry["tx"])["blockNumber"], 16) == entry["block"], name
    assert record["chain"] == BASE and record["addresses"] == {
        "vault": vault, "hub": net.hub, "junkRequester": net.address("junkRequester"), "waiting.callback": waiting}
    assert record["startBlock"] < steps["BlacklistContract(vault)"]["block"] <= record["floodEndBlock"] < record["endBlock"]
    # Each request is in the count, and the request that made the debt was the last one that went out.
    assert 5 < record["junkRequests"] == junk_requests(net) <= 1000
    # After the gap of the run that stopped, each new request got a number above each number on the chain.
    numbers = junk_numbers(net, record["lasnaStartBlock"])
    assert numbers == [1, 2, 3, 4, *range(6, len(numbers) + 2)], "a number was used two times"
    assert record["undeliveredAtDebt"] == 0 and record["junkAfterRecovery"] == 0
    assert len(net.rpcs[BASE].logs(net.ledger[BASE], [net.ledger_abi.topic("CallbackFailure"),
                                                       ops.topic_of_address(vault)], net.origin[BASE])) \
        == record["junkRequests"]

    # Each recorded hash is the transaction of its event.
    def has(name, chain, emitter, abi, event, *indexed):
        receipt = net.rpcs[chain].receipt(steps[name]["tx"])
        logs = [log for log in ops.receipt_logs(receipt, emitter, abi.topic(event))
                if log["topics"][1:1 + len(indexed)] == list(indexed)]
        assert logs, name
        return abi.decode_event(event, logs[0])

    chain_topic = ops.topic_of_int(84532)
    has("BlacklistContract(vault)", BASE, net.ledger[BASE], net.ledger_abi, "BlacklistContract", ops.topic_of_address(vault))
    has("VaultDown", LASNA, net.hub, net.hub_abi, "VaultDown", chain_topic)
    has("Pending", LASNA, net.hub, net.hub_abi, "Pending", chain_topic, ops.topic_of_address(waiting))
    up = has("VaultUp", LASNA, net.hub, net.hub_abi, "VaultUp", chain_topic)
    assert up["remaining"] == 0 and up["sent"] == 1 and str(up["requestId"]) == record["requestId"]
    result_event = has("Result(waiting.callback)", BASE, vault, net.vault_abi, "Result",
                       ops.topic_of_int(up["requestId"]), ops.topic_of_address(waiting))
    assert ops.OUTCOMES[result_event["outcome"]] == "Rescued" and result_event["paid"] > 0
    assert record["result"] == {"outcome": "Rescued", "paid": str(result_event["paid"])}
    deposit = net.rpcs[BASE].transaction(steps["recovery deposit"]["tx"])
    assert ops.same_address(deposit["to"], net.ledger[BASE]) and ops.same_address(deposit["from"], net.account.address)
    recovery = net.data()["contracts"]["vaults"][BASE]["recoveries"][-1]
    assert recovery["deposit"] == steps["recovery deposit"]["tx"]

    # The state after the scenario: the vault is up, the canary that waited has no debt.
    assert net.debt(BASE, waiting) == 0 and net.debt(BASE, vault) == 0
    _, _, _, up_state, pending = net.read(LASNA, net.hub, net.hub_abi, "vaultInfo", 84532)
    assert (up_state, pending) == (True, 0)
    # The balance of the vault moved only by the rescue of the canary that waited.
    assert net.rpcs[BASE].balance(vault) == balance - result_event["paid"]

    # The data file: the flood changed the balance of the vault by 0.
    out = net.dir / "out-junk"
    result = net.measure(out)
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads((out / "data.json").read_text())
    junk = data["junk"]
    assert junk["status"] == "ok" and junk["chain"] == BASE and junk["vault"] == vault
    assert (junk["startBlock"], junk["endBlock"]) == (record["startBlock"], record["floodEndBlock"])
    assert junk["unexplainedBalanceChange"] == "0"
    assert junk["balanceStart"] == junk["balanceEnd"] == str(balance)
    assert junk["resultPaid"] == "0" and junk["selfCareAdded"] == "0"
    assert int(junk["reservesStart"]) > 0 and junk["reservesEnd"] == "0" and int(junk["debtEnd"]) > 0
    assert junk["callbackFailures"] == record["junkRequests"]
    assert "unexplainedBalanceChange" in (out / "data.json").read_text()
    assert f"| Balance change that the payments do not explain | 0 |" in (out / "table.md").read_text()
    heights = json.loads((out / "heights.json").read_text())
    assert heights["params"] == {"junk": {"chain": BASE, "startBlock": record["startBlock"],
                                          "endBlock": record["floodEndBlock"]}}

    # Arm C: the fees of the vault hold the float that the flood took, by the recovery deposit; the vault was
    # down for some blocks.
    c = data["arms"]["C"]
    detail = c["guardianCost"]["feeDetail"][BASE]
    assert detail["deposits"] == str(int(deposit["value"], 16))
    assert int(c["guardianCost"]["fees"][BASE]) >= int(junk["reservesStart"]) + int(junk["debtEnd"])
    assert c["guardianDowntimeBlocks"][BASE] > 0 and c["guardianDowntimeBlocks"][LASNA] == 0

    # A second run sends nothing.
    blocks = net.blocks()
    result = net.experiment("junk", "--chain", BASE)
    assert result.returncode == 0 and "complete junk scenario" in result.stdout and net.blocks() == blocks

    # As if the run had stopped after it saw `VaultDown`, with a record that has no end of the flood: the
    # next run builds the same record from the chain, sends nothing, and the flood evidence stays.
    saved = net.scenarios.read_text()
    stopped = json.loads(saved)
    cut = step_names(record).index("VaultDown") + 1
    stopped["junk"]["steps"] = stopped["junk"]["steps"][:cut]
    for key in ("floodEndBlock", "endBlock", "lasnaEndBlock", "requestId", "result", "junkAfterRecovery"):
        stopped["junk"].pop(key)
    stopped["junk"]["endBlock"] = None
    net.scenarios.write_text(json.dumps(stopped))
    result = net.experiment("junk", "--chain", BASE, "--timeout", "30")
    assert result.returncode == 0, result.stderr + result.stdout
    assert net.blocks() == blocks, "the run that continues sent a transaction"
    rebuilt = net.scenario("junk")
    assert rebuilt["floodEndBlock"] == steps["BlacklistContract(vault)"]["block"]
    assert step_names(rebuilt) == order and rebuilt["result"] == record["result"]
    assert [(entry["step"], entry["tx"]) for entry in rebuilt["steps"]] == \
        [(entry["step"], entry["tx"]) for entry in record["steps"]]
    result = net.measure(net.dir / "out-rebuilt")
    assert result.returncode == 0, result.stderr + result.stdout
    again = json.loads((net.dir / "out-rebuilt" / "data.json").read_text())["junk"]
    assert again["status"] == "ok" and again["unexplainedBalanceChange"] == "0"
    assert (again["startBlock"], again["endBlock"]) == (record["startBlock"], rebuilt["floodEndBlock"])
    assert again["reservesEnd"] == "0" and int(again["debtEnd"]) > 0
    net.scenarios.write_text(saved)


# ----------------------------------------------------------------------------------------------
# Reproduction
# ----------------------------------------------------------------------------------------------


def test_measure_with_heights_gives_identical_files_and_needs_no_key(net):
    first, second = net.dir / "out-junk", net.dir / "out-again"
    # More blocks and more logs after the first run: the saved heights fix the result.
    experiment.poke(net.rpcs[LASNA], net.fixture, net.address("armA.reactive"))
    net.scenarios.unlink()  # the flood range comes from the snapshot
    blocks, mark = net.blocks(), net.proxy.mark()
    result = net.measure(second, "--heights", str(first / "heights.json"))
    assert result.returncode == 0, result.stderr + result.stdout
    for name in ("table.md", "data.json", "heights.json"):
        assert (second / name).read_bytes() == (first / name).read_bytes(), name

    # No key and no transaction: read methods only.
    assert net.blocks() == blocks and net.proxy.sent(mark) == []
    methods = {record["method"] for record in net.proxy.records[mark:]}
    assert methods <= {"eth_chainId", "eth_blockNumber", "eth_getLogs", "eth_getBlockByNumber", "eth_call",
                       "eth_getBalance"}, methods

    # A snapshot of other chains is refused.
    wrong = net.dir / "wrong-heights.json"
    document = json.loads((first / "heights.json").read_text())
    document["chains"]["unichain-sepolia"] = document["chains"].pop(BASE)
    wrong.write_text(json.dumps(document))
    result = net.measure(net.dir / "out-wrong", "--heights", str(wrong))
    assert result.returncode == 2 and not (net.dir / "out-wrong").exists()


# ----------------------------------------------------------------------------------------------
# A junk scenario that stops after each step
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def lone(tmp_path_factory):
    """A second dry run with its own fixtures: a junk scenario uses its vault and its waiting canary up."""
    run = Experiment(tmp_path_factory.mktemp("experiment-stops"))
    try:
        result = run.experiment("fixtures", *amount_flags(run))
        assert result.returncode == 0, result.stderr + result.stdout
        yield run
    finally:
        run.close()


def wait_until(condition, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timeout: {what}"
        time.sleep(0.2)


def test_junk_continues_after_each_stop_and_counts_only_its_own_deliveries(lone):
    """The node of this test delivers nothing by itself: the test runs the relay between two runs of the
    command. So each run stops at the step that waits for the node, and the next run continues there."""
    net = lone
    vault, waiting = net.vault(BASE), net.address("waiting.callback")
    fast = ["--pause", "0.5", "--timeout", "6", "--max-pongs", "1"]

    def junk(*flags) -> subprocess.CompletedProcess:
        return net.experiment("junk", "--chain", BASE, *flags)

    def record() -> dict:
        return net.scenario("junk") if net.scenarios.exists() else {}

    # 1. No delivery: the driver sends one request and waits for it.
    command = [sys.executable, "-m", "lifeline.experiment", "junk", "--chain", BASE, "--manifest", str(net.manifest),
               *net._rpc_arguments(), "--scenarios", str(net.scenarios), "--pause", "0.5", "--timeout", "300",
               "--drain", "600"]
    log = open(net.dir / "junk-first-run.log", "w")
    process = subprocess.Popen(command, env=net.env, stdout=log, stderr=subprocess.STDOUT, cwd=ops.ROOT)
    try:
        wait_until(lambda: record().get("junkRequests") == 1, 120, "the first junk request")
        # A failed callback of another caller is not a delivery of the driver: no second request goes out.
        # The other caller copies the payload of the request that is on its way; the node writes the address
        # of that caller into it.
        copied = experiment.junk_payload(net.vault_abi, record()["lasnaStartBlock"], 1)
        assert experiment.requested_payload(junk_request_logs(net)[0]) == copied
        net.node.deliver(BASE, vault, copied, 200_000, net.account.address)
        net.node.deliver(BASE, vault, net.vault_abi.encode("reconcile", experiment.ZERO_ADDRESS, vault, 1), 200_000,
                         net.address("junkRequester"))
        time.sleep(4)
        assert process.poll() is None and junk_requests(net) == 1, "a request went out before the first delivery"

        # 2. Callbacks of that other caller empty the float. The request of the driver is still on its way at
        #    the debt: the record has it, and the driver waits before it continues.
        net.junk_until_vault_debt(BASE)
        wait_until(lambda: "BlacklistContract(vault)" in step_names(record() or {"steps": []}), 60, "the debt step")
        assert (record()["undeliveredAtDebt"], record()["drained"]) == (1, False)
        wait_until(lambda: "wait 600.0 s" in (net.dir / "junk-first-run.log").read_text(), 30, "the drain wait")
    finally:
        process.kill()
        process.wait()
        log.close()

    # 3. The run stopped in the drain wait: the next run starts the wait again before each other step.
    started = time.monotonic()
    result = junk(*fast, "--drain", "3")
    assert result.returncode == 1 and "no VaultDown of the hub in 6.0 s" in result.stderr, result.stdout
    assert "wait 3.0 s" in result.stdout and time.monotonic() - started >= 3
    assert record()["drained"] is True and junk_requests(net) == 1

    # The node: the hub hears the debt of the vault, and the proxy rejects the request of the driver.
    assert relayed(net.relay()) == (1, 1)
    result = junk(*fast)
    assert result.returncode == 1 and "not in debt after 1 pong requests" in result.stderr, result.stdout
    assert "were not delivered before the debt: wait" not in result.stdout, "the drain wait ran a second time"
    assert step_names(record())[-2:] == ["VaultDown", "flood end"]
    assert record()["floodEndBlock"] >= find(record(), "BlacklistContract(vault)")["block"]

    # The node: the callback of the canary that waits, its debt, and `Pending` of the hub.
    assert relayed(net.relay()) == (1, 1)
    debt = net.debt(BASE, waiting)
    assert debt > 0
    # Another account clears that debt before the recovery: the sweep then finds nothing to rescue.
    ops.transact(net.rpcs[BASE], net.account, net.ledger[BASE], net.ledger_abi.encode("depositTo", waiting),
                 value=debt, abi=net.ledger_abi)
    result = junk(*fast)
    assert result.returncode == 1 and "no Result of the vault for waiting.callback" in result.stderr, result.stdout
    assert step_names(record())[-5:] == ["pong request 1", "BlacklistContract(waiting.callback)", "Pending",
                                         "recovery deposit", "VaultUp"]
    assert net.debt(BASE, vault) == 0

    # The node: the catch-up sweep. Its result for the canary is `NoNeed`. Each run refuses it: a result
    # that is not a rescue never gives a complete record.
    assert relayed(net.relay()) == (0, 1)
    for _ in range(2):
        blocks = net.blocks()
        result = junk(*fast)
        assert result.returncode == 1 and "has outcome NoNeed, not Rescued" in result.stderr, result.stdout
        assert net.blocks() == blocks
        assert record()["endBlock"] is None and "Result(waiting.callback)" not in step_names(record())
    assert junk_requests(net) == 1 and record()["junkRequests"] == 1


def find(record: dict, name: str) -> dict:
    return next(entry for entry in record["steps"] if entry["step"] == name)
