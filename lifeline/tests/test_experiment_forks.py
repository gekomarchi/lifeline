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

ROUNDS_BEFORE_THE_TICK, ROUNDS_AFTER_THE_TICK = 9, 3


def test_load_uses_up_the_start_reserves_and_measure_builds_the_table(net):
    entries = net.fixtures()

    def load_round():
        result = net.experiment("load", "--pokes", "1", "--interval", "0")
        assert result.returncode == 0, result.stderr + result.stdout
        relayed(net.relay())

    start = net.blocks()[LASNA]
    for _ in range(ROUNDS_BEFORE_THE_TICK):
        load_round()
    # One cron tick: the poller of arm B and the gauge of the hub run.
    relayed(net.relay("--cron-tick"))
    for _ in range(ROUNDS_AFTER_THE_TICK):
        load_round()
    rounds = ROUNDS_BEFORE_THE_TICK + ROUNDS_AFTER_THE_TICK

    # The scenario file has the blocks and the hashes of the load, and no time.
    load = net.scenario("load")
    assert load["chain"] == LASNA and start <= load["startBlock"] < load["endBlock"] <= net.blocks()[LASNA]
    assert load["addresses"] == {f"arm{arm}.reactive": entries[f"arm{arm}.reactive"]["address"]
                                 for arm in experiment.ARMS}
    pokes = [entry for entry in load["steps"] if entry["step"].startswith("poke")]
    assert len(pokes) == 3 * rounds and step_names(load).count("start") == rounds
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
    result = net.experiment("measure", keys=False)
    assert result.returncode == 2 and "a dry run needs its own --cache and --out" in result.stderr
    assert not (ops.ROOT / "evidence" / "experiment" / "data.json").exists()


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
    assert 0 < record["junkRequests"] <= 1000

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
    assert 0 < junk["callbackFailures"] <= record["junkRequests"]
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
