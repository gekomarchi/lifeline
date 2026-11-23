"""Fork dry run of the operator commands.

The commands, the contracts and the ledgers are real: the tests run `python -m lifeline.ops` and
`python -m lifeline.forkrelay` against local anvil forks of Lasna, Unichain Sepolia and Base Sepolia, with
a throwaway key and a scratch manifest. Only the node is emulated (the fork relay), and the read-back case
changes one RPC answer.

The tests need `anvil`, the build output (`forge build`) and the variables `LASNA_RPC_URL`,
`UNICHAIN_SEPOLIA_RPC_URL` and `BASE_SEPOLIA_RPC_URL`. Without them the module is skipped. The tests run
in the order of the file: each one continues on the state that the tests before it left.
"""

from __future__ import annotations

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

from lifeline import forkrelay, ops
from lifeline.tests import forknet

UPSTREAMS = forknet.fork_urls()
BUILT = (ops.ARTIFACTS / "LifelineHub.sol" / "LifelineHub.json").exists()

pytestmark = pytest.mark.skipif(
    UPSTREAMS is None or not Path(forknet.ANVIL).exists() or not BUILT,
    reason="the fork dry run needs anvil, `forge build` and the three RPC variables "
           + ", ".join(forknet.FORK_VARIABLES.values()),
)

LASNA, UNICHAIN, BASE = "lasna", "unichain-sepolia", "base-sepolia"
ZERO = "0x" + "00" * 20
# The policy of a canary: `low` 0, so `setPolicy` pays nothing; `high` 1000 wei, so one more callback
# after a rescue makes a new debt.
CANARY_POLICY = ["--low", "0", "--high", "0.000000000000001", "--max-debt", "0.0001", "--daily-cap", "0.001",
                 "--max-rescues", "4", "--window", "3600"]
CANARY_POLICY_VALUE = ops.Policy(0, 1000, 10**14, 10**15, 4, 3600)
HASH = r"0x[0-9a-f]{64}"
# The reserves of the Lasna canary of the loop check. The default comes from the current gas price, which
# goes down on an anvil fork; the node emulation pays the price of the fork point.
LOOP_RESERVE = ["--canary-reserve", "0.05"]


class DryRun:
    def __init__(self, directory: Path):
        self.dir = directory
        self.anvils = {name: forknet.Anvil(name, url, directory) for name, url in UPSTREAMS.items()}
        self.proxy = forknet.RecordingProxy({name: anvil.url for name, anvil in self.anvils.items()})
        key = "0x" + secrets.token_hex(32)
        self.account = Account.from_key(key)
        self.env = {**os.environ, ops.OWNER_KEY_VARIABLE: key, "LIFELINE_POLL_SECONDS": "0.2",
                    "PYTHONPATH": str(ops.ROOT)}
        os.environ[ops.OWNER_KEY_VARIABLE] = key  # for the helpers of this module that send transactions
        self.manifest = directory / "manifest.json"
        self.config = directory / "config.json"
        self.state = directory / "relay-state.json"
        self.rpcs = {name: ops.Rpc(name, [anvil.url]) for name, anvil in self.anvils.items()}
        for name in self.anvils:
            self.fund(name, 100 * 10**18)

        self.live = json.loads(ops.DEFAULT_CONFIG.read_text())
        self.ledger = {name: self.live["chains"][name]["ledger"] for name in self.anvils}
        self.chain_id = {name: self.live["chains"][name]["chainId"] for name in self.anvils}
        self.hub_abi = ops.load_artifact("LifelineHub").abi
        self.vault_abi = ops.load_artifact("LifelineVault").abi
        self.ledger_abi = ops.load_artifact("ICallbackProxy").abi
        self.callback = ops.load_artifact("CanaryCallback")
        self.reactive = ops.load_artifact("CanaryReactive")
        self._node = None
        self.canaries = {}

    def close(self):
        self.proxy.close()
        for anvil in self.anvils.values():
            anvil.close()

    # Commands ---------------------------------------------------------------------------------

    def _rpc_arguments(self, through_proxy=True) -> list:
        arguments = []
        for name, anvil in self.anvils.items():
            arguments += ["--rpc", f"{name}={self.proxy.url(name) if through_proxy else anvil.url}"]
        return arguments

    def ops(self, *arguments, manifest=None) -> subprocess.CompletedProcess:
        """Runs `python -m lifeline.ops`. Each request of the command goes through the recording proxy."""
        command = [sys.executable, "-m", "lifeline.ops", *arguments, "--manifest", str(manifest or self.manifest),
                   *self._rpc_arguments()]
        return subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=300, cwd=ops.ROOT)

    def relay_command(self, *arguments) -> list:
        return [sys.executable, "-m", "lifeline.forkrelay", *self._rpc_arguments(through_proxy=False),
                "--manifest", str(self.manifest), "--state", str(self.state), *arguments]

    def relay(self, *arguments) -> subprocess.CompletedProcess:
        """Runs `python -m lifeline.forkrelay` until no new log remains."""
        return subprocess.run(self.relay_command(*arguments), env=self.env, capture_output=True, text=True,
                              timeout=300, cwd=ops.ROOT)

    # Chain helpers ----------------------------------------------------------------------------

    def fund(self, name, wei):
        self.anvils[name].request("anvil_setBalance", [self.account.address, hex(wei)])

    def blocks(self) -> dict:
        """The block number of each fork. With automine, one transaction is one block."""
        return {name: anvil.block_number() for name, anvil in self.anvils.items()}

    def read(self, name, to, abi, function, *arguments):
        return abi.decode(function, self.rpcs[name].call(to, abi.encode(function, *arguments)))

    def debt(self, name, account) -> int:
        return self.read(name, self.ledger[name], self.ledger_abi, "debt", account)

    def data(self) -> dict:
        return json.loads(self.manifest.read_text())

    @property
    def hub(self) -> str:
        return self.data()["contracts"]["hub"]["address"]

    def vault(self, name) -> str:
        return self.data()["contracts"]["vaults"][name]["address"]

    def target_state(self, name, target) -> str:
        _, state, _ = self.read(LASNA, self.hub, self.hub_abi, "targetInfo", self.chain_id[name], target)
        return ops.TARGET_STATES[state]

    def new_callback_canary(self, name=BASE) -> str:
        receipt = ops.deploy_contract(self.rpcs[name], self.account, self.callback)
        return ops.checksum(receipt["contractAddress"])

    @property
    def node(self) -> forkrelay.Relay:
        """The node emulation inside the test process, for single deliveries."""
        if self._node is None:
            forks = {
                name: forkrelay.Fork(name, self.chain_id[name], self.ledger[name], self.rpcs[name])
                for name in self.anvils
            }
            self._node = forkrelay.Relay(forks, quiet=True)
        return self._node

    def pong(self, name, canary, n=1) -> None:
        """One callback to a canary. The canary has no funds, so the proxy records a debt or takes reserves."""
        payload = self.callback.abi.encode("pong", ZERO, n)
        receipt = self.node.deliver(name, canary, payload, ops.CANARY_CALLBACK_GAS, self.account.address)
        assert int(receipt["status"], 16) == 1

    def junk_until_vault_debt(self, name) -> int:
        """Callbacks to the vault with a wrong first argument: each one reverts and costs the float a fee."""
        vault = self.vault(name)
        payload = self.vault_abi.encode("reconcile", ZERO, vault, 1)
        for count in range(1, 400):
            self.node.deliver(name, vault, payload, 200_000, self.account.address)
            if self.debt(name, vault) > 0:
                return count
        raise AssertionError("the junk deliveries did not put the vault into debt")

    # Configuration ----------------------------------------------------------------------------

    def sweep_fee(self, name, at_fork_point=False) -> int:
        """The fee of one full sweep at the current gas price, as `deploy` computes it. On an anvil fork the
        base fee goes down with each empty block; the node emulation pays the price of the fork point."""
        ledger = self.ledger[name]
        extra = self.read(name, ledger, self.ledger_abi, "_extraGas")
        coeff = self.read(name, ledger, self.ledger_abi, "_gasPriceCoeffPer1000")
        price = self.node.forks[name].gas_price if at_fork_point else self.rpcs[name].gas_price()
        return ops.sweep_fee(extra, self.live["chains"][name]["measuredSweepGas"], price, coeff)

    def trigger_fee(self) -> int:
        coeff = self.read(LASNA, self.ledger[LASNA], self.ledger_abi, "_gasPriceCoeffPer1000")
        return ops.trigger_fee(self.rpcs[LASNA].gas_price(), coeff)

    def write_config(self, path=None, hub=None, vaults=None, fleet=()) -> Path:
        config = json.loads(json.dumps(self.live))
        config["hub"] = {"principal": str(3 * 10**18), "feeFloat": str(5 * 10**17),
                         "selfReserveTarget": str(5 * 10**17), "selfCareDailyCap": str(4 * 10**18),
                         "principalFloor": str(10**17), **(hub or {})}
        for name in (UNICHAIN, BASE):
            # The float is small, so that a few junk deliveries empty it: 5 full sweeps.
            target = 5 * self.sweep_fee(name, at_fork_point=True)
            config["vaults"][name] = {"principal": str(10**16), "feeFloat": str(target),
                                      "selfReserveTarget": str(target), "selfCareDailyCap": str(10 * target),
                                      "principalFloor": str(10**15), **((vaults or {}).get(name) or {})}
        config["fleet"] = list(fleet)
        path = path or self.config
        path.write_text(json.dumps(config, indent=2))
        return path


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    run = DryRun(tmp_path_factory.mktemp("dryrun"))
    yield run
    run.close()


def relayed(result) -> tuple:
    """(triggers, deliveries) of one relay run."""
    assert result.returncode == 0, result.stdout + result.stderr
    counts = re.search(r"triggers: (\d+)\ndeliveries: (\d+)", result.stdout)
    assert counts, result.stdout
    print(result.stdout)  # shown by pytest when the test fails
    return int(counts.group(1)), int(counts.group(2))


def step_lines(output: str) -> list:
    return [line.strip() for line in output.splitlines() if re.match(r"\s+\d ", line)]


# ----------------------------------------------------------------------------------------------
# Float checks and deploy
# ----------------------------------------------------------------------------------------------


def test_float_checks_stop_deploy_before_any_transaction(net):
    before = net.blocks()

    low_vault = {UNICHAIN: {"selfReserveTarget": str(2 * net.sweep_fee(UNICHAIN))}}
    result = net.ops("deploy", "--config", str(net.write_config(net.dir / "low-vault.json", vaults=low_vault)))
    assert result.returncode != 0
    assert "deploy sent no transaction" in result.stderr
    assert "vault of unichain-sepolia: selfReserveTarget" in result.stderr
    assert "covers 4 full sweeps" in result.stderr

    for field in ("feeFloat", "selfReserveTarget"):
        low_hub = {field: str(net.trigger_fee() - 1)}
        result = net.ops("deploy", "--config", str(net.write_config(net.dir / "low-hub.json", hub=low_hub)))
        assert result.returncode != 0
        assert f"hub: {field}" in result.stderr and "covers one full trigger" in result.stderr

    assert net.blocks() == before, "a fork has a new transaction"
    assert not net.manifest.exists()


def test_deploy_writes_the_manifest_and_a_second_run_deploys_nothing(net):
    fleet = [
        {"label": "FastfillReactive", "chain": LASNA, "address": net.live["fleet"][0]["address"],
         "policy": {"low": str(5 * 10**16), "high": str(25 * 10**16), "maxDebt": str(10**18),
                    "dailyCap": str(2 * 10**18), "maxRescues": 2, "window": 3600}},
        {"label": "FastfillExecutor", "chain": UNICHAIN, "address": net.live["fleet"][1]["address"],
         "policy": {"low": str(10**13), "high": str(5 * 10**13), "maxDebt": str(10**14), "dailyCap": str(10**15),
                    "maxRescues": 2, "window": 3600}},
    ]
    result = net.ops("deploy", "--config", str(net.write_config(fleet=fleet)))
    assert result.returncode == 0, result.stderr + result.stdout

    data = net.data()
    hub = data["contracts"]["hub"]
    assert hub["chain"] == LASNA and hub["chainId"] == 5318007
    assert data["cronTopic"] == "Cron10000" and data["fixtures"] == {} and data["loopCheck"] is None
    entries = {LASNA: hub, **data["contracts"]["vaults"]}
    assert set(entries) == {LASNA, UNICHAIN, BASE}
    for name, entry in entries.items():
        assert entry["chainId"] == net.chain_id[name]
        assert net.rpcs[name].code(entry["address"]), "no code at the manifest address"
        receipt = net.rpcs[name].receipt(entry["deployTx"])
        assert int(receipt["blockNumber"], 16) == entry["deployBlock"]
        assert ops.same_address(receipt["contractAddress"], entry["address"])
        assert data["chains"][name]["ledger"] == net.ledger[name]
        assert data["chains"][name]["rpc"] == net.live["chains"][name]["rpc"], "the manifest holds a fork URL"

    for name in (UNICHAIN, BASE):
        proxy, vault, sweep_base, up, pending = net.read(LASNA, net.hub, net.hub_abi, "vaultInfo", net.chain_id[name])
        assert (proxy, vault, sweep_base, up, pending) == (net.ledger[name], net.vault(name), 220_000, True, 0)
        assert net.read(name, net.vault(name), net.vault_abi, "callbackSender") == net.hub
    assert net.read(LASNA, net.hub, net.hub_abi, "cronSubscribed") is True

    before, mark = net.blocks(), net.proxy.mark()
    result = net.ops("deploy", "--config", str(net.config))
    assert result.returncode == 0, result.stderr
    assert net.blocks() == before and net.proxy.sent(mark) == [], "the second run sent a transaction"
    assert result.stdout.count("no deployment") == 3
    assert net.data() == data


def test_verify_refuses_a_local_development_node(net):
    result = net.ops("verify", "--sourcify", "http://127.0.0.1:9")
    assert result.returncode != 0
    assert "local development node" in result.stderr


# ----------------------------------------------------------------------------------------------
# Onboarding
# ----------------------------------------------------------------------------------------------


def test_enroll_of_a_destination_target_sets_the_vault_policy_first(net):
    canary = net.canaries["order"] = net.new_callback_canary()
    mark = net.proxy.mark()
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "order canary", *CANARY_POLICY)
    assert result.returncode == 0, result.stderr

    # The chain: one transaction on the destination, then the read-back, then one transaction on Lasna.
    records = net.proxy.records[mark:]
    sends = [i for i, r in enumerate(records) if r["method"] == "eth_sendRawTransaction"]
    assert [records[i]["chain"] for i in sends] == [BASE, LASNA]
    set_policy = net.rpcs[BASE].transaction(records[sends[0]]["result"])
    enroll = net.rpcs[LASNA].transaction(records[sends[1]]["result"])
    selector = "0x" + net.vault_abi.selector("policyOf").hex()
    read_back = [i for i, r in enumerate(records)
                 if r["chain"] == BASE and r["method"] == "eth_call" and r["params"][0]["data"].startswith(selector)]
    assert read_back and sends[0] < read_back[0] < sends[1]

    # The two policies are equal: the same value went to the vault and to the hub.
    assert ops.same_address(set_policy["to"], net.vault(BASE)) and ops.same_address(enroll["to"], net.hub)
    codec = ops._abi_codec()
    policy_type = "(uint256,uint256,uint256,uint256,uint32,uint32)"
    assert set_policy["input"].startswith("0x" + net.vault_abi.selector("setPolicy").hex())
    assert enroll["input"].startswith("0x" + net.hub_abi.selector("enroll").hex())
    _, vault_policy = codec.decode(["address", policy_type], bytes.fromhex(set_policy["input"][10:]))
    chain_id, _, hub_policy = codec.decode(["uint256", "address", policy_type], bytes.fromhex(enroll["input"][10:]))
    assert vault_policy == hub_policy == CANARY_POLICY_VALUE.as_tuple() and chain_id == 84532
    assert net.read(BASE, net.vault(BASE), net.vault_abi, "policyOf", canary) == CANARY_POLICY_VALUE.as_tuple()
    assert net.target_state(BASE, canary) == "Active"

    # The output: the three steps in this order, with the two hashes and the block of the read-back.
    steps = step_lines(result.stdout)
    assert [line.split()[1] for line in steps] == ["setPolicy", "read-back", "enroll"]
    assert set_policy["hash"] in steps[0] and enroll["hash"] in steps[2]
    record = net.data()["enrollments"][-1]
    assert f"block {record['txs']['readBackBlock']}" in steps[1]
    assert "0 (0 wei)" in result.stdout and "0.000000000000001 (1000 wei)" in result.stdout
    assert record["txs"]["setPolicy"] == set_policy["hash"] and record["txs"]["enroll"] == enroll["hash"]
    assert record["label"] == "order canary" and record["chain"] == BASE and record["chainId"] == 84532
    assert record["target"] == canary and record["guest"] is False and record["consent"] is None
    assert record["policy"] == CANARY_POLICY_VALUE.to_json() and record["removed"] is None


def test_enroll_of_a_lasna_target_sends_one_transaction_on_lasna(net):
    receipt = ops.deploy_contract(net.rpcs[LASNA], net.account, net.reactive, (net.ledger[LASNA], 0, ZERO, 0))
    canary = ops.checksum(receipt["contractAddress"])
    before, mark = net.blocks(), net.proxy.mark()
    result = net.ops("enroll", "--chain", LASNA, "--target", canary, "--label", "lasna canary", "--low", "0.01",
                     "--high", "0.02", "--max-debt", "0.05", "--daily-cap", "1", "--max-rescues", "2",
                     "--window", "3600")
    assert result.returncode == 0, result.stderr

    sent = net.proxy.sent(mark)
    assert [chain for chain, _ in sent] == [LASNA]
    after = net.blocks()
    assert after[LASNA] == before[LASNA] + 1 and after[BASE] == before[BASE] and after[UNICHAIN] == before[UNICHAIN]
    transaction = net.rpcs[LASNA].transaction(sent[0][1])
    chain_id, target, _ = ops._abi_codec().decode(
        ["uint256", "address", "(uint256,uint256,uint256,uint256,uint32,uint32)"],
        bytes.fromhex(transaction["input"][10:]))
    assert chain_id == 5318007 and ops.same_address(target, canary)
    record = net.data()["enrollments"][-1]
    assert record["txs"] == {"setPolicy": None, "readBackBlock": None, "enroll": sent[0][1],
                             "enrollBlock": after[LASNA]}
    assert "0.01 (10000000000000000 wei)" in result.stdout
    # The hub reconciled the new Lasna target at once: reserves at the high mark.
    assert net.read(LASNA, net.ledger[LASNA], net.ledger_abi, "reserves", canary) == 2 * 10**16


def test_read_back_mismatch_stops_before_lasna(net):
    canary = net.canaries["guest"] = net.new_callback_canary()
    selector = "0x" + net.vault_abi.selector("policyOf").hex()

    def other_high(chain, method, params, result):
        if chain == BASE and method == "eth_call" and params[0]["data"].startswith(selector):
            words = [result[2 + i:66 + i] for i in range(0, len(result) - 2, 64)]
            words[1] = format(int(words[1], 16) + 1, "064x")  # `high`
            return "0x" + "".join(words)
        return result

    before, enrollments = net.blocks(), len(net.data()["enrollments"])
    net.proxy.tamper = other_high
    try:
        result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "mismatch", *CANARY_POLICY)
    finally:
        net.proxy.tamper = None
    assert result.returncode != 0
    assert "high: stored 1001, input 1000" in result.stderr and "Nothing was sent to Lasna" in result.stderr
    after = net.blocks()
    assert after[LASNA] == before[LASNA], "Lasna has an enroll transaction"
    assert after[BASE] == before[BASE] + 1
    assert net.target_state(BASE, canary) == "None"
    assert len(net.data()["enrollments"]) == enrollments


def test_guest_needs_a_consent_reference(net):
    canary = net.canaries["guest"]
    before, mark = net.blocks(), net.proxy.mark()
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "guest", "--guest", *CANARY_POLICY)
    assert result.returncode != 0 and "consent" in result.stderr
    assert net.proxy.mark() == mark, "a refused guest sent a request"
    assert net.blocks() == before

    # With a consent reference the guest goes through the same path.
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "guest", "--guest", "--consent",
                     "message 2026-11-23", *CANARY_POLICY)
    assert result.returncode == 0, result.stderr
    assert [line.split()[1] for line in step_lines(result.stdout)] == ["setPolicy", "read-back", "enroll"]
    record = net.data()["enrollments"][-1]
    assert record["guest"] is True and record["consent"] == "message 2026-11-23" and record["target"] == canary


def test_fleet_enrolls_each_entry_through_the_same_path(net):
    mark = net.proxy.mark()
    result = net.ops("fleet", "--config", str(net.config))
    assert result.returncode == 0, result.stderr

    # FastfillReactive: one `hub.enroll` on the Lasna chain ID. FastfillExecutor: setPolicy, read-back, enroll.
    assert [chain for chain, _ in net.proxy.sent(mark)] == [LASNA, UNICHAIN, LASNA]
    reactive, executor = result.stdout.split("enroll FastfillExecutor")
    assert [line.split()[1] for line in step_lines(reactive)] == ["enroll"]
    assert "hub.enroll(5318007," in reactive
    assert [line.split()[1] for line in step_lines(executor)][:3] == ["setPolicy", "read-back", "enroll"]
    records = net.data()["enrollments"][-2:]
    assert [r["label"] for r in records] == ["FastfillReactive", "FastfillExecutor"]
    for record in records:
        assert f"{record['label']}: block {record['txs']['enrollBlock']}" in result.stdout
        assert net.target_state(record["chain"], record["target"]) == "Active"
    assert "48-hour window" in result.stdout

    before = net.blocks()
    result = net.ops("fleet", "--config", str(net.config))
    assert result.returncode == 0 and net.blocks() == before, "a second fleet run sent a transaction"


# ----------------------------------------------------------------------------------------------
# Loop check
# ----------------------------------------------------------------------------------------------


def test_loop_check_names_the_missing_step_without_a_relay_and_passes_with_it(net):
    result = net.ops("loop-check", "--chain", BASE, "--timeout", "3", *LOOP_RESERVE)
    assert result.returncode != 0
    assert "timed out" in result.stderr and "BlacklistContract(canary)" in result.stderr
    check = net.data()["loopCheck"]
    assert check["status"] == "timeout" and check["missing"] == "debt"

    watcher = subprocess.Popen(net.relay_command("--watch"), env=net.env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, cwd=ops.ROOT)
    try:
        # The same check continues: no new canary and no new poke.
        before = net.proxy.mark()
        result = net.ops("loop-check", "--chain", BASE, "--timeout", "60")
        assert result.returncode == 0, result.stderr + result.stdout
        assert net.proxy.sent(before) == []
        steps = step_lines(result.stdout)
        assert len(steps) == 4 and "loop-check passed" in result.stdout
        assert "BlacklistContract(canary)" in steps[0] and "Relayed" in steps[1]
        assert "outcome Rescued" in steps[2] and "debt(canary)" in steps[3] and ": 0 at block" in steps[3]
        for line in steps[:3]:
            assert re.search(HASH, line), line
        check = net.data()["loopCheck"]
        assert check["status"] == "passed" and check["chain"] == BASE and check["missing"] is None
        assert check["steps"]["relayed"]["requestId"] in steps[1] and check["steps"]["relayed"]["requestId"] in steps[2]
        assert net.debt(BASE, check["canaryCallback"]["address"]) == 0
        # Each hash is a transaction of its fork.
        assert net.rpcs[BASE].receipt(check["steps"]["debt"]["tx"])
        assert net.rpcs[LASNA].receipt(check["steps"]["relayed"]["tx"])
        assert net.rpcs[BASE].receipt(check["steps"]["result"]["tx"])

        # A check on the second chain, with the relay running from the start.
        result = net.ops("loop-check", "--chain", UNICHAIN, "--timeout", "60", *LOOP_RESERVE)
        assert result.returncode == 0, result.stderr + result.stdout
        check = net.data()["loopCheck"]
        assert check["status"] == "passed" and check["chain"] == UNICHAIN
        assert net.debt(UNICHAIN, check["canaryCallback"]["address"]) == 0
    finally:
        watcher.send_signal(signal.SIGTERM)
        output, _ = watcher.communicate(timeout=30)
    # Two loops: the canary reaction and the hub reaction on Lasna, the canary callback and the reconcile.
    assert "triggers: 4" in output and "deliveries: 4" in output, output


# ----------------------------------------------------------------------------------------------
# Removal
# ----------------------------------------------------------------------------------------------


def test_remove_leaves_no_subscription(net):
    canary = net.canaries["order"]

    # While the canary is enrolled, its debt wakes the hub, and the vault pays.
    net.pong(BASE, canary)
    assert net.debt(BASE, canary) > 0
    assert relayed(net.relay()) == (1, 1)
    assert net.debt(BASE, canary) == 0

    mark = net.proxy.mark()
    result = net.ops("remove", "--chain", BASE, "--target", canary)
    assert result.returncode == 0, result.stderr
    sent = net.proxy.sent(mark)
    assert [chain for chain, _ in sent] == [LASNA, BASE], "the order is hub.disarm, then vault.disarm"
    assert "the receipt holds Unsubscribe" in result.stdout and "state Disarmed" in result.stdout
    assert net.target_state(BASE, canary) == "Disarmed"
    assert net.read(BASE, net.vault(BASE), net.vault_abi, "hasPolicy", canary) is False
    record = next(r for r in net.data()["enrollments"] if r["target"] == canary)
    assert record["removed"] == {"hubDisarm": sent[0][1], "vaultDisarm": sent[1][1]}

    # A new debt of the removed canary wakes no hub reaction.
    net.pong(BASE, canary, 2)
    debt = net.debt(BASE, canary)
    assert debt > 0
    assert relayed(net.relay()) == (0, 0)
    assert net.debt(BASE, canary) == debt
    assert net.target_state(BASE, canary) == "Disarmed"
    assert not [key for key in net.node.read_subscriptions() if key[3][1] == int(canary, 16)]

    # A second removal sends nothing.
    before = net.blocks()
    result = net.ops("remove", "--chain", BASE, "--target", canary)
    assert result.returncode == 0 and net.blocks() == before


# ----------------------------------------------------------------------------------------------
# Recovery
# ----------------------------------------------------------------------------------------------

PENDING = 9  # more than `SWEEP_MAX`, so the catch-up needs two `vaultUp`


def test_recover_refuses_vault_up_while_the_vault_is_in_debt(net):
    targets = net.canaries["pending"] = [net.new_callback_canary() for _ in range(PENDING)]
    for index, target in enumerate(targets):
        result = net.ops("enroll", "--chain", BASE, "--target", target, "--label", f"pending {index}", *CANARY_POLICY)
        assert result.returncode == 0, result.stderr

    # Junk deliveries empty the float. The hub then reports the vault as down.
    net.junk_until_vault_debt(BASE)
    assert relayed(net.relay()) == (1, 0)
    _, _, _, up, _ = net.read(LASNA, net.hub, net.hub_abi, "vaultInfo", 84532)
    assert up is False

    net.fund(BASE, 0)
    try:
        before = net.blocks()
        result = net.ops("recover", "--chain", BASE)
        assert result.returncode != 0
        assert "hub.vaultUp was not sent" in result.stderr
        assert net.blocks() == before, "Lasna has a vaultUp transaction, or the destination has a deposit"
    finally:
        net.fund(BASE, 100 * 10**18)
    _, _, _, up, _ = net.read(LASNA, net.hub, net.hub_abi, "vaultInfo", 84532)
    assert up is False and net.debt(BASE, net.vault(BASE)) > 0


def test_recover_deposits_then_sends_vault_up_until_nothing_is_pending(net):
    targets = net.canaries["pending"]
    for index, target in enumerate(targets):
        net.pong(BASE, target, index + 1)
        assert net.debt(BASE, target) > 0
    assert relayed(net.relay()) == (PENDING, 0)
    _, _, _, up, pending = net.read(LASNA, net.hub, net.hub_abi, "vaultInfo", 84532)
    assert (up, pending) == (False, PENDING)

    vault = net.vault(BASE)
    debt = net.debt(BASE, vault)
    reserve = net.read(BASE, vault, net.vault_abi, "selfReserveTarget")
    mark = net.proxy.mark()
    result = net.ops("recover", "--chain", BASE)
    assert result.returncode == 0, result.stderr + result.stdout

    sent = net.proxy.sent(mark)
    assert [chain for chain, _ in sent] == [BASE, LASNA, LASNA], "one deposit, then two vaultUp"
    deposit = net.rpcs[BASE].transaction(sent[0][1])
    assert ops.same_address(deposit["to"], net.ledger[BASE]) and int(deposit["value"], 16) == debt + reserve
    assert f"= debt {debt} + float {reserve}" in result.stdout
    assert "sent 8, remaining 1" in result.stdout and "sent 1, remaining 0" in result.stdout
    assert result.stdout.index("proxy.depositTo on") < result.stdout.index("hub.vaultUp on")
    assert sent[1][1] in result.stdout and sent[2][1] in result.stdout
    assert net.debt(BASE, vault) == 0
    _, _, _, up, pending = net.read(LASNA, net.hub, net.hub_abi, "vaultInfo", 84532)
    assert (up, pending) == (True, 0)

    assert relayed(net.relay()) == (0, 2)
    for target in targets:
        assert net.debt(BASE, target) == 0


# ----------------------------------------------------------------------------------------------
# Fork relay
# ----------------------------------------------------------------------------------------------


def test_cron_tick_runs_the_gauge(net):
    assert net.read(LASNA, net.hub, net.hub_abi, "lastTick") == 0
    result = net.relay("--cron-tick")
    # One trigger for the hub. One sweep for each vault that is up and has an enrolled target.
    assert relayed(result) == (1, 2)
    assert "cron tick of block" in result.stdout
    assert net.read(LASNA, net.hub, net.hub_abi, "lastTick") > 0


def test_forkrelay_refuses_an_rpc_that_is_not_a_local_development_node(net, tmp_path):
    command = [sys.executable, "-m", "lifeline.forkrelay", "--manifest", str(net.manifest), "--state",
               str(tmp_path / "state.json")]
    # A remote host: refused by its address, with no request.
    result = subprocess.run([*command, "--rpc", "lasna=http://203.0.113.7:8545"], env=net.env, capture_output=True,
                            text=True, timeout=20, cwd=ops.ROOT)
    assert result.returncode != 0 and "not a local development node" in result.stderr

    # A local server that is not a development node: it has no `anvil_` method.
    not_a_node = forknet.RecordingProxy({LASNA: net.anvils[LASNA].url})
    not_a_node.refuse = ("anvil_", "hardhat_", "evm_")
    try:
        before = net.blocks()
        result = subprocess.run([*command, "--rpc", f"lasna={not_a_node.url(LASNA)}"], env=net.env,
                                capture_output=True, text=True, timeout=60, cwd=ops.ROOT)
        methods = {record["method"] for record in not_a_node.records}
    finally:
        not_a_node.close()
    assert result.returncode != 0 and "not a local development node" in result.stderr
    assert not methods & {"eth_sendTransaction", "eth_sendRawTransaction"} and net.blocks() == before
