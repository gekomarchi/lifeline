"""Fork dry run of the evidence generator.

The command, the reader, the contracts, the operator commands and the drivers of the experiment are real: the
tests run `python -m lifeline.judge` against local anvil forks, on the events that `loop-check`, `enroll`,
`quarantine`, `junk` and `measure` leave there. Only the node is emulated (the fork relay). Each expected value
is a log that the test reads from the forks itself, with no reader.

The tests need what the fork dry run of the experiment needs (see `test_experiment_forks.py`). They run in the
order of the file: each one continues on the state that the tests before it left.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytest.importorskip("eth_account")

from lifeline import experiment, judge, ops
from lifeline.events import BLACKLIST, WHITELIST
from lifeline.reader import READ_METHODS
from lifeline.tests import test_experiment_forks
from lifeline.tests.test_experiment_forks import KEYS, Experiment, amount_flags
from lifeline.tests.test_ops_forks import BASE, CANARY_POLICY, LASNA, LOOP_RESERVE, relayed

pytestmark = test_experiment_forks.pytestmark

CONSENT = "consent reference of the dry run"


def ref(log: dict) -> dict:
    return {"block": int(log["blockNumber"], 16), "tx": log["transactionHash"]}


class Judged(Experiment):
    """The dry-run forks with the experiment, and the evidence generator."""

    def _start(self, directory):
        super()._start(directory)
        self.evidence = directory / "experiment"  # `table.md` and `data.json` of `measure`

    def measured(self) -> dict:
        result = self.measure(self.evidence)
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads((self.evidence / "data.json").read_text())

    def judge(self, out, *arguments) -> subprocess.CompletedProcess:
        """`python -m lifeline.judge` with no key in the environment. Each request goes through the
        recording proxy."""
        command = [sys.executable, "-m", "lifeline.judge", "--manifest", str(self.manifest),
                   "--experiment", str(self.evidence), "--scenarios", str(self.scenarios), "--out", str(out),
                   "--judge", str(out / "JUDGE.md"), "--cache", str(self.cache), *self._rpc_arguments(), *arguments]
        environment = {name: value for name, value in self.env.items() if name not in KEYS}
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=600, cwd=ops.ROOT)

    def judged(self, name: str, *arguments) -> tuple:
        """Runs the command. Returns (data, text of `JUDGE.md`, the requests that the run sent)."""
        out, mark, blocks = self.dir / name, self.proxy.mark(), self.blocks()
        result = self.judge(out, *arguments)
        assert result.returncode == 0, result.stderr + result.stdout
        records = self.proxy.records[mark:]
        # No key and no transaction: read methods only, and no new block.
        assert {record["method"] for record in records} <= READ_METHODS
        assert self.proxy.sent(mark) == [] and self.blocks() == blocks
        return json.loads((out / "data.json").read_text()), (out / "JUDGE.md").read_text(), records

    def logs(self, name, address, topics) -> list:
        """The logs of one emitter, read from the fork with no reader."""
        return self.rpcs[name].logs(address, topics, self.origin[name])


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    run = Judged(tmp_path_factory.mktemp("judge"))
    yield run
    run.close()


def section(document: str, title: str) -> str:
    text = document[:document.index(judge.TABLE_BEGIN)] + document[document.index(judge.TABLE_END):]
    start = text.index(f"\n## {title}\n")
    following = text.find("\n## ", start + 1)
    return text[start:following if following != -1 else len(text)]


def test_the_rescue_of_the_loop_check_canary_is_joined_with_the_logs_of_the_nodes(net):
    result = net.experiment("fixtures", *amount_flags(net))
    assert result.returncode == 0, result.stderr + result.stdout
    with net.watch():
        result = net.ops("loop-check", "--chain", BASE, "--timeout", "90", *LOOP_RESERVE)
    assert result.returncode == 0, result.stderr + result.stdout
    net.measured()
    data, document, records = net.judged("first")

    manifest = net.data()
    check = manifest["loopCheck"]
    canary, target = check["canaryCallback"]["address"], ops.topic_of_address(check["canaryCallback"]["address"])
    entry, = [entry for entry in data["rescues"] if entry["target"] == canary]
    assert entry["status"] == "joined" and entry["chain"] == BASE and entry["chainId"] == 84532
    assert entry["requestId"] == check["steps"]["relayed"]["requestId"]

    # The four blocks and the four hashes equal the logs that the nodes give.
    debt, = net.logs(BASE, net.ledger[BASE], [BLACKLIST, target])
    relay, = net.logs(LASNA, net.hub, [net.hub_abi.topic("Relayed"), None, None, target])
    result_log, = net.logs(BASE, net.vault(BASE), [net.vault_abi.topic("Result"),
                                                    ops.topic_of_int(int(entry["requestId"])), target])
    whitelist, = net.logs(BASE, net.ledger[BASE], [WHITELIST, target])
    event = net.vault_abi.decode_event("Result", result_log)
    assert entry["debtEvent"] == ref(debt)
    assert entry["relayed"] == ref(relay)
    assert entry["result"] == {**ref(result_log), "outcome": "Rescued", "paid": str(event["paid"])}
    assert entry["whitelist"] == ref(whitelist)
    assert whitelist["transactionHash"] == result_log["transactionHash"] and event["paid"] > 0
    assert ref(debt)["block"] < ref(result_log)["block"]
    row = next(line for line in section(document, "Destination rescues").splitlines()
               if line.startswith(f"| {entry['requestId']} "))
    for log in (debt, relay, result_log, whitelist):
        assert f"{ref(log)['block']}<br>`{log['transactionHash']}`" in row

    # The canary is an enrollment of the manifest that is no guest and no fixture: one rescue, one gap.
    fleet = {item["label"]: item for item in data["fleet"]}
    assert [label for label in fleet if "experiment" in label] == []
    report = fleet[next(record["label"] for record in manifest["enrollments"] if record["target"] == canary)]
    assert (report["rescues"], report["topUps"], report["principalPaid"]) == (1, 0, str(event["paid"]))
    assert report["gaps"] == [{"deadFromBlock": ref(debt)["block"], "aliveAtBlock": ref(result_log)["block"],
                               "blocks": ref(result_log)["block"] - ref(debt)["block"],
                               "tx": result_log["transactionHash"]}]
    policy, = net.logs(BASE, net.vault(BASE), [net.vault_abi.topic("PolicySet"), target])
    assert (report["enrollBlock"], report["enrollTx"]) == (ref(policy)["block"], policy["transactionHash"])
    assert report["enrollTx"] == next(r["txs"]["setPolicy"] for r in manifest["enrollments"] if r["target"] == canary)
    assert report["reserves"] == {"block": data["heights"][BASE]["to"],
                                  "value": str(net.ledger_state(BASE, canary)[0])}

    # The cost of the vault: its payment events, and its state at `to`.
    paid = sum(net.vault_abi.decode_event("Result", log)["paid"]
               for log in net.logs(BASE, net.vault(BASE), [net.vault_abi.topic("Result")]))
    added = sum(net.vault_abi.decode_event("SelfCare", log)["added"]
                for log in net.logs(BASE, net.vault(BASE), [net.vault_abi.topic("SelfCare")]))
    reserves, vault_debt = net.ledger_state(BASE, net.vault(BASE))
    cost = data["guardian"]["vaults"][BASE]
    assert cost["principalPaid"] == str(paid) and cost["selfCareAdded"] == str(added)
    assert cost["floatConsumed"] == str(added - reserves + vault_debt)
    assert cost["balance"]["value"] == str(net.rpcs[BASE].balance(net.vault(BASE)))

    # The heights start at the deployment blocks of the manifest, and no log query starts before them.
    starts = {LASNA: manifest["contracts"]["hub"]["deployBlock"],
              **{name: vault["deployBlock"] for name, vault in manifest["contracts"]["vaults"].items()}}
    assert {name: heights["from"] for name, heights in data["heights"].items()} == starts
    queries = [record for record in records if record["method"] == "eth_getLogs"]
    assert {record["chain"] for record in queries} == set(starts)
    for record in queries:
        assert int(record["params"][0]["fromBlock"], 16) >= starts[record["chain"]], record["params"]

    # The table of the experiment is in the document unchanged, and no scenario is recorded yet.
    start = document.index(judge.TABLE_BEGIN) + len(judge.TABLE_BEGIN) + 1
    assert document[start:document.index(judge.TABLE_END)].encode() == (net.evidence / "table.md").read_bytes()
    assert data["experiment"] == json.loads((net.evidence / "data.json").read_text())
    assert not data["quarantine"]["recorded"] and not data["junkRecovery"]["recorded"]
    assert "Status: not recorded." in section(document, "Quarantine")
    assert "Status: not recorded." in section(document, "Junk recovery")
    assert data["guests"] == [] and section(document, "Guests").strip().endswith(judge.NO_GUEST)
    for text in (document, (net.dir / "first" / "data.json").read_text()):
        assert "http" not in text and "127.0.0.1" not in text


def test_a_guest_has_the_enrollment_hashes_of_the_manifest_and_the_rescue_hash_of_the_chain(net):
    canary = net.new_callback_canary(BASE)
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "guest canary", "--guest",
                     "--consent", CONSENT, *CANARY_POLICY)
    assert result.returncode == 0, result.stderr + result.stdout
    relayed(net.relay())
    net.pong(BASE, canary)  # the canary has no reserves: its first callback is a debt
    relayed(net.relay())
    assert net.debt(BASE, canary) == 0

    data, document, _ = net.judged("guest")
    record = net.data()["enrollments"][-1]
    rescue, = [log for log in net.logs(BASE, net.vault(BASE), [net.vault_abi.topic("Result"), None,
                                                               ops.topic_of_address(canary)])
               if ops.OUTCOMES[net.vault_abi.decode_event("Result", log)["outcome"]] == "Rescued"]
    assert data["guests"] == [{
        "label": "guest canary", "chain": BASE, "chainId": 84532, "target": canary, "consent": CONSENT,
        "enrollment": {"setPolicy": record["txs"]["setPolicy"], "enroll": record["txs"]["enroll"]},
        "rescues": [ref(rescue)],
    }]
    assert net.rpcs[BASE].receipt(record["txs"]["setPolicy"]) and net.rpcs[LASNA].receipt(record["txs"]["enroll"])
    guests = section(document, "Guests")
    for text in ("guest canary", CONSENT, record["txs"]["setPolicy"], record["txs"]["enroll"],
                 rescue["transactionHash"]):
        assert text in guests, text
    assert judge.NO_GUEST not in guests
    assert "guest canary" not in [entry["label"] for entry in data["fleet"]]
    assert [entry["status"] for entry in data["rescues"] if entry["target"] == canary] == ["joined"]


def test_the_quarantine_and_the_junk_recovery_are_the_scenario_records_confirmed_on_the_chain(net):
    with net.watch():
        result = net.experiment("quarantine", "--timeout", "90", "--settle", "4")
        assert result.returncode == 0, result.stderr + result.stdout
        # The vault of base-sepolia serves the canary of the loop check and the guest of this dry run.
        result = net.experiment("junk", "--chain", BASE, "--burst", "8", "--pause", "1", "--timeout", "180",
                                "--allow-other-targets")
        assert result.returncode == 0, result.stderr + result.stdout
    measured = net.measured()
    data, document, _ = net.judged("scenarios")

    # Quarantine: the `Rescued` logs and the `Quarantined` log of the looping canary, from the nodes.
    record, looping = net.scenario("quarantine"), net.address("looping")
    target = ops.topic_of_address(looping)
    rescued = net.logs(LASNA, net.hub, [net.hub_abi.topic("Rescued"), target])
    quarantined, = net.logs(LASNA, net.hub, [net.hub_abi.topic("Quarantined"), target])
    paid = sum(net.hub_abi.decode_event("Rescued", log)["paid"] for log in rescued)
    policy = next(r["policy"] for r in net.data()["enrollments"] if r["label"] == "experiment looping")
    assert data["quarantine"] == {
        "recorded": True, "chain": LASNA, "target": looping, "rescues": [ref(log) for log in rescued],
        "quarantined": ref(quarantined), "maxRescues": policy["maxRescues"], "dailyCap": policy["dailyCap"],
        "policyTx": next(r["txs"]["enroll"] for r in net.data()["enrollments"] if r["label"] == "experiment looping"),
        "principalPaid": str(paid), "paidAfterQuarantine": "0",
    }
    assert len(rescued) == policy["maxRescues"] == record["budget"]["rescues"]
    assert str(paid) == record["budget"]["principalPaid"]
    steps = {entry["step"]: entry for entry in record["steps"]}
    assert [steps[f"Rescued {n}"]["tx"] for n in range(1, len(rescued) + 1)] == \
        [log["transactionHash"] for log in rescued]
    assert steps["Quarantined"]["tx"] == quarantined["transactionHash"]
    # Each rescue of the hub on Lasna has its `Gap`.
    assert [(entry["target"], entry["rescued"]["tx"]) for entry in data["lasnaRescues"]] == \
        [(looping, log["transactionHash"]) for log in rescued]
    gaps = net.logs(LASNA, net.hub, [net.hub_abi.topic("Gap"), target])
    assert [entry["gap"] for entry in data["lasnaRescues"]] == [
        {name: net.hub_abi.decode_event("Gap", log)[name] for name in ("deadFromBlock", "aliveAtBlock")}
        for log in gaps]
    text = section(document, "Quarantine")
    assert f"| Rescues from the enrollment to `Quarantined` | {len(rescued)} |" in text
    assert "| Principal paid after `Quarantined` | 0 |" in text and quarantined["transactionHash"] in text

    # Junk recovery: each step of the record is the transaction of its event on its chain.
    record, vault, waiting = net.scenario("junk"), net.vault(BASE), net.address("waiting.callback")
    steps = {entry["step"]: entry for entry in record["steps"]}
    chain_topic, vault_topic = ops.topic_of_int(84532), ops.topic_of_address(vault)
    vault_debt, = net.logs(BASE, net.ledger[BASE], [BLACKLIST, vault_topic])
    vault_back, = net.logs(BASE, net.ledger[BASE], [WHITELIST, vault_topic])
    down, = net.logs(LASNA, net.hub, [net.hub_abi.topic("VaultDown"), chain_topic])
    pending, = net.logs(LASNA, net.hub, [net.hub_abi.topic("Pending"), chain_topic, ops.topic_of_address(waiting)])
    up, = net.logs(LASNA, net.hub, [net.hub_abi.topic("VaultUp"), chain_topic])
    # `setPolicy` gave the target a first `Result` with the id 0. The rescue has the id of the sweep.
    rescue, = net.logs(BASE, vault, [net.vault_abi.topic("Result"), ops.topic_of_int(int(record["requestId"])),
                                     ops.topic_of_address(waiting)])
    event = net.vault_abi.decode_event("Result", rescue)
    recovery = data["junkRecovery"]
    assert recovery["recorded"] and (recovery["chain"], recovery["vault"]) == (BASE, vault)
    assert recovery["steps"] == [
        {"step": "BlacklistContract(vault)", "chain": BASE, **ref(vault_debt)},
        {"step": "VaultDown", "chain": LASNA, **ref(down)},
        {"step": "Pending", "chain": LASNA, **ref(pending)},
        {"step": "WhitelistContract(vault)", "chain": BASE, **ref(vault_back)},
        {"step": "VaultUp", "chain": LASNA, **ref(up)},
        {"step": "Result(waiting.callback)", "chain": BASE, **ref(rescue)},
    ]
    assert vault_back["transactionHash"] == steps["recovery deposit"]["tx"]
    assert recovery["waitingRescue"] == {"target": waiting, "requestId": record["requestId"], "outcome": "Rescued",
                                         "paid": str(event["paid"])}
    # The balance figures are the ones of the data file of the experiment.
    figures = measured["junk"]
    assert figures["status"] == "ok"
    assert recovery["balanceStart"] == {"block": record["startBlock"], "value": figures["balanceStart"]}
    assert recovery["balanceEnd"] == {"block": record["floodEndBlock"], "value": figures["balanceEnd"]}
    assert recovery["unexplainedChange"] == figures["unexplainedBalanceChange"] == "0"
    text = section(document, "Junk recovery")
    for log in (vault_debt, down, pending, vault_back, up, rescue):
        assert log["transactionHash"] in text
    assert "| Balance change that the payments do not explain | 0 |" in text
    # The rescue of the target that waited came from a sweep of `vaultUp`: no `Relayed`, so no row of the join.
    assert record["requestId"] not in [entry["requestId"] for entry in data["rescues"]]
    assert {entry["status"] for entry in data["rescues"]} == {"joined"}

    # Reproduction: more blocks and more events after the first run, and the saved heights fix the files.
    first = net.dir / "scenarios"
    experiment.poke(net.rpcs[LASNA], net.fixture, net.address("armA.reactive"))
    relayed(net.relay())
    net.judged("again", "--heights", str(first / "heights.json"))
    for name in ("JUDGE.md", "data.json", "heights.json"):
        assert (net.dir / "again" / name).read_bytes() == (first / name).read_bytes(), name
