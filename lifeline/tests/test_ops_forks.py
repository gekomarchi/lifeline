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
        self.anvils, self.proxy = {}, None
        try:
            for name, url in UPSTREAMS.items():
                self.anvils[name] = forknet.Anvil(name, url, directory)
            self._start(directory)
        except BaseException:
            self.close()
            raise

    def _start(self, directory: Path):
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
        if self.proxy is not None:
            self.proxy.close()
        for anvil in self.anvils.values():
            anvil.close()

    # Commands ---------------------------------------------------------------------------------

    def _rpc_arguments(self, through_proxy=True) -> list:
        arguments = []
        for name, anvil in self.anvils.items():
            arguments += ["--rpc", f"{name}={self.proxy.url(name) if through_proxy else anvil.url}"]
        return arguments

    def ops(self, *arguments, manifest=None, env=None) -> subprocess.CompletedProcess:
        """Runs `python -m lifeline.ops`. Each request of the command goes through the recording proxy."""
        command = [sys.executable, "-m", "lifeline.ops", *arguments, "--manifest", str(manifest or self.manifest),
                   *self._rpc_arguments()]
        return subprocess.run(command, env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=300,
                              cwd=ops.ROOT)

    def nonce(self, name, tag="latest") -> int:
        return int(self.rpcs[name].request("eth_getTransactionCount", [self.account.address, tag]), 16)

    def edit_manifest(self, change) -> None:
        data = self.data()
        change(data)
        self.manifest.write_text(json.dumps(data, indent=2))

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

    result = net.ops("status")
    assert result.returncode == 0 and "status: ok" in result.stdout, result.stdout + result.stderr
    assert net.hub in result.stdout and net.vault(BASE) in result.stdout and "cron subscribed true" in result.stdout


def test_deploy_closes_the_journal_of_a_step_that_is_done(net):
    data = net.data()
    resume = data["contracts"]["hub"]["cron"]["resumeCronTx"]
    set_vault = data["contracts"]["vaults"][BASE]["setVaultTx"]
    nonce = int(net.rpcs[LASNA].transaction(resume)["nonce"], 16)

    def stopped_before_the_receipt(manifest):
        # `resumeCron` is in a block, and the run stopped before it wrote the hash to its place.
        cron = manifest["contracts"]["hub"]["cron"]
        cron.update(resumeCronTx=None, pending={"resumeCronTx": {"nonce": nonce, "txs": [resume]}})
        # A journal of `setVault` with a transaction that the node does not know; the vault is registered.
        vault = manifest["contracts"]["vaults"][BASE]
        vault["pending"] = {"setVaultTx": {"nonce": nonce - 1, "txs": ["0x" + "33" * 32]}}

    net.edit_manifest(stopped_before_the_receipt)
    before, mark = net.blocks(), net.proxy.mark()
    result = net.ops("deploy", "--config", str(net.config))
    assert result.returncode == 0, result.stderr
    assert net.blocks() == before and net.proxy.sent(mark) == []
    assert "its journal is closed" in result.stdout
    # No journal stays open: a later run cannot take the old receipt for a new step.
    assert net.data() == data
    assert net.data()["contracts"]["hub"]["cron"]["resumeCronTx"] == resume
    assert net.data()["contracts"]["vaults"][BASE]["setVaultTx"] == set_vault


def test_the_float_checks_apply_to_a_deployment_that_never_reached_the_chain(net):
    # A manifest with the journal of a hub deployment that the node does not know: no hub exists.
    manifest = net.dir / "journal-only.json"
    journal = {"nonce": net.nonce(LASNA), "txs": ["0x" + "44" * 32]}
    manifest.write_text(json.dumps({
        "version": 1, "chains": net.data()["chains"],
        "contracts": {"hub": {"chain": LASNA, "chainId": 5318007, "address": None, "deployBlock": None,
                              "deployTx": None, "pendingDeploy": journal}, "vaults": {}},
    }))
    before = net.blocks()
    result = net.ops("deploy", "--config", str(net.dir / "low-hub.json"), manifest=manifest)
    assert result.returncode != 0 and "covers one full trigger" in result.stderr
    assert net.blocks() == before
    assert json.loads(manifest.read_text())["contracts"]["hub"]["pendingDeploy"] == journal


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
    # The record of the enrollment that did not finish: the vault has the policy, the hub has no target.
    records = net.data()["enrollments"]
    assert len(records) == enrollments + 1 and records[-1]["target"] == canary
    assert re.fullmatch(HASH, records[-1]["txs"]["setPolicy"])
    assert records[-1]["txs"]["enroll"] is None and records[-1]["txs"]["enrollBlock"] is None


def test_guest_needs_a_consent_reference(net):
    canary = net.canaries["guest"]
    before, mark = net.blocks(), net.proxy.mark()
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "guest", "--guest", *CANARY_POLICY)
    assert result.returncode != 0 and "consent" in result.stderr
    assert net.proxy.mark() == mark, "a refused guest sent a request"
    assert net.blocks() == before

    # With a consent reference the guest goes through the same path. The test before this one stopped the
    # enrollment of this canary after `setPolicy`: the command continues it and sends `hub.enroll` only.
    set_policy = net.data()["enrollments"][-1]["txs"]["setPolicy"]
    enrollments, mark = len(net.data()["enrollments"]), net.proxy.mark()
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "guest", "--guest", "--consent",
                     "message 2026-11-23", *CANARY_POLICY)
    assert result.returncode == 0, result.stderr
    steps = step_lines(result.stdout)
    assert [line.split()[1] for line in steps] == ["setPolicy", "read-back", "enroll"]
    assert [chain for chain, _ in net.proxy.sent(mark)] == [LASNA] and set_policy in steps[0]
    assert len(net.data()["enrollments"]) == enrollments
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
        assert (check["canaryCallback"]["chain"], check["canaryCallback"]["chainId"]) == (BASE, 84532)
        assert (check["canaryReactive"]["chain"], check["canaryReactive"]["chainId"]) == (LASNA, 5318007)
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
        print(output)  # shown by pytest when the test fails
    assert watcher.returncode == 0, output
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

    # As if the run had stopped after `hub.disarm` was in a block and before it wrote the hash: the next run
    # takes the hash from the journal, sends nothing, and loses no hash.
    def stopped(manifest):
        entry = next(r for r in manifest["enrollments"] if r["target"] == canary)
        removed = entry.pop("removed")
        nonce = int(net.rpcs[LASNA].transaction(removed["hubDisarm"])["nonce"], 16)
        entry["removed"] = None
        entry["removing"] = {"hubDisarm": None, "vaultDisarm": removed["vaultDisarm"],
                             "pending": {"hubDisarm": {"nonce": nonce, "txs": [removed["hubDisarm"]]}}}

    net.edit_manifest(stopped)
    result = net.ops("remove", "--chain", BASE, "--target", canary)
    assert result.returncode == 0 and net.blocks() == before, result.stderr
    assert "the receipt holds Unsubscribe" in result.stdout
    assert next(r for r in net.data()["enrollments"] if r["target"] == canary) == record


# ----------------------------------------------------------------------------------------------
# The order of a destination log and a subscription change
# ----------------------------------------------------------------------------------------------


def test_relay_does_not_replay_a_debt_from_before_the_enrollment(net):
    canary = net.new_callback_canary()
    net.pong(BASE, canary)
    assert net.debt(BASE, canary) > 0
    # The relay reads the debt log now: no subscription matches it.
    assert relayed(net.relay()) == (0, 0)

    # `setPolicy` pays the old debt at once. The subscription of the hub does not replay the old event.
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "old debt", *CANARY_POLICY)
    assert result.returncode == 0, result.stderr
    assert "the vault reconciled at once: Rescued" in result.stdout and net.debt(BASE, canary) == 0
    result = net.relay()
    assert relayed(result) == (0, 0) and "order not known" not in result.stdout


def test_relay_names_a_log_with_an_order_that_it_does_not_know(net):
    # A debt and then an enrollment, with no read of the relay between them: the relay cannot know the
    # order. It uses the subscriptions of the time of its read, sends the log, and says so.
    canary = net.new_callback_canary()
    net.pong(BASE, canary)
    result = net.ops("enroll", "--chain", BASE, "--target", canary, "--label", "order one", *CANARY_POLICY)
    assert result.returncode == 0, result.stderr
    result = net.relay()
    assert relayed(result) == (1, 1)
    assert "order not known: 1" in result.stdout and f"the Subscribe of {net.hub}" in result.stdout

    # A debt and then a removal, with no read between them: the relay does not send the log, and says so.
    assert net.debt(BASE, canary) == 0
    net.pong(BASE, canary, 2)
    assert net.debt(BASE, canary) > 0
    result = net.ops("remove", "--chain", BASE, "--target", canary)
    assert result.returncode == 0, result.stderr
    result = net.relay()
    assert relayed(result) == (0, 0)
    assert "order not known: 1" in result.stdout and f"the Unsubscribe of {net.hub}" in result.stdout


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
    result = net.ops("status")
    assert result.returncode == 1 and "the vault of base-sepolia needs a recovery" in result.stdout

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
    assert "recoveries" not in net.data()["contracts"]["vaults"][BASE]


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

    # The rule: before each `vaultUp` the command reads `debt(vault)` at the proxy, and the answer is 0. The
    # read after the deposit is at the block of the deposit or a later one.
    records = net.proxy.records[mark:]
    sends = [i for i, r in enumerate(records) if r["method"] == "eth_sendRawTransaction"]
    debt_call = "0x" + net.ledger_abi.encode("debt", vault).hex()
    reads = [i for i, r in enumerate(records)
             if r["chain"] == BASE and r["method"] == "eth_call" and r["params"][0]["data"] == debt_call
             and ops.same_address(r["params"][0]["to"], net.ledger[BASE])]
    deposit_block = int(net.rpcs[BASE].receipt(sent[0][1])["blockNumber"], 16)
    for before_send, after_send in ((sends[0], sends[1]), (sends[1], sends[2])):
        between = [i for i in reads if before_send < i < after_send]
        assert between, "no debt read before vaultUp"
        assert int(records[between[-1]]["result"], 16) == 0
        assert int(records[between[-1]]["params"][1], 16) >= deposit_block

    recovery = net.data()["contracts"]["vaults"][BASE]["recoveries"]
    assert len(recovery) == 1 and recovery[0]["deposit"] == sent[0][1] and "pending" not in recovery[0]
    assert [(call["tx"], call["sent"], call["remaining"]) for call in recovery[0]["vaultUp"]] == \
        [(sent[1][1], 8, 1), (sent[2][1], 1, 0)]

    assert relayed(net.relay()) == (0, 2)
    for target in targets:
        assert net.debt(BASE, target) == 0

    # The vault is up and nothing is pending: a second run sends nothing.
    before = net.blocks()
    result = net.ops("recover", "--chain", BASE)
    assert result.returncode == 0 and net.blocks() == before, result.stderr
    assert "no deposit" in result.stdout and "no pending target; no transaction" in result.stdout
    assert net.data()["contracts"]["vaults"][BASE]["recoveries"] == recovery


# ----------------------------------------------------------------------------------------------
# The measured gas of a full sweep
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [UNICHAIN, BASE])
def test_measured_sweep_gas_of_the_configuration_holds_on_each_proxy(net, name):
    """`measuredSweepGas` of `deployments/config.json` is the input of the float check of `deploy`. The
    number comes from the contract test `test_sweepFitsItsGasLimit` on a Base Sepolia fork. This test
    measures a full sweep (`SWEEP_MAX` targets, each one rescued) through the real proxy of each chain."""
    vault, chain_id = net.vault(name), net.chain_id[name]
    targets = [net.new_callback_canary(name) for _ in range(ops.SWEEP_MAX)]
    for index, target in enumerate(targets):
        result = net.ops("enroll", "--chain", name, "--target", target, "--label", f"sweep {index}", *CANARY_POLICY)
        assert result.returncode == 0, result.stderr
    relayed(net.relay())

    net.junk_until_vault_debt(name)
    assert relayed(net.relay()) == (1, 0)
    for index, target in enumerate(targets):
        net.pong(name, target, index + 1)
    assert relayed(net.relay()) == (ops.SWEEP_MAX, 0)
    result = net.ops("recover", "--chain", name)
    assert result.returncode == 0, result.stderr
    assert f"sent {ops.SWEEP_MAX}, remaining 0" in result.stdout

    result = net.relay()
    assert relayed(result) == (0, 1)
    delivery = net.rpcs[name].receipt(re.search(rf"delivery  {name} {vault} gas (\d+) .*tx ({HASH})", result.stdout)[2])
    results = [net.vault_abi.decode_event("Result", log)
               for log in ops.receipt_logs(delivery, vault, net.vault_abi.topic("Result"))]
    assert len(results) == ops.SWEEP_MAX and all(ops.OUTCOMES[r["outcome"]] == "Rescued" for r in results)
    assert not ops.receipt_logs(delivery, vault, net.vault_abi.topic("SweepPartial"))
    _, _, sweep_base, _, _ = net.read(LASNA, net.hub, net.hub_abi, "vaultInfo", chain_id)
    limit = int(net.rpcs[name].transaction(delivery["transactionHash"])["gas"], 16)
    assert limit == sweep_base + 80_000 * ops.SWEEP_MAX

    # The fee that the real proxy charged for this sweep: the change of the reserves of the vault, with the
    # self-care deposit of the same callback added back.
    block = int(delivery["blockNumber"], 16)
    reserves = [net.ledger_abi.decode("reserves", net.rpcs[name].call(
        net.ledger[name], net.ledger_abi.encode("reserves", vault), number)) for number in (block - 1, block)]
    added = sum(net.vault_abi.decode_event("SelfCare", log)["added"]
                for log in ops.receipt_logs(delivery, vault, net.vault_abi.topic("SelfCare")))
    fee = reserves[0] + added - reserves[1]
    assert fee > 0 and net.debt(name, vault) == 0

    # The fee of the float check of `deploy`, from `measuredSweepGas` of the configuration, at the gas price
    # of this delivery. It must cover the fee that the proxy charged, and not be far above it.
    extra = net.read(name, net.ledger[name], net.ledger_abi, "_extraGas")
    coeff = net.read(name, net.ledger[name], net.ledger_abi, "_gasPriceCoeffPer1000")
    price = int(delivery["effectiveGasPrice"], 16)
    configured = net.live["chains"][name]["measuredSweepGas"]
    checked = ops.sweep_fee(extra, configured, price, coeff)
    charged = fee * 1000 // (price * coeff) - extra
    print(f"SWEEP_GAS chain={name} n={ops.SWEEP_MAX} charged={charged} receipt={int(delivery['gasUsed'], 16)} "
          f"configured={configured} limit={limit} fee={fee} checked={checked}")
    assert int(delivery["gasUsed"], 16) < limit
    assert fee <= checked <= fee * 1.25, "measuredSweepGas of the configuration does not fit a real full sweep"


def test_recover_does_not_send_a_second_deposit_while_the_first_one_waits(net):
    """A deposit that waits for its receipt when the command stops must not be sent a second time: the
    second one would stay in the reserves of the vault, and reserves cannot be withdrawn."""
    name, vault = UNICHAIN, net.vault(UNICHAIN)
    net.junk_until_vault_debt(name)
    assert relayed(net.relay()) == (1, 0)
    reserve = net.read(name, vault, net.vault_abi, "selfReserveTarget")
    nonce, short = net.nonce(name), {"LIFELINE_RECEIPT_TIMEOUT": "3"}

    net.anvils[name].request("evm_setAutomine", [False])
    try:
        # The deposit goes to the node and gets no receipt: the run stops.
        result = net.ops("recover", "--chain", name, env=short)
        assert result.returncode != 0 and "no receipt" in result.stderr and "hub.vaultUp was not sent" in result.stderr
        journal = net.data()["contracts"]["vaults"][name]["recoveries"][-1]["pending"]["deposit"]
        assert journal["nonce"] == nonce and len(journal["txs"]) == 1
        assert (net.nonce(name), net.nonce(name, "pending")) == (nonce, nonce + 1)

        # The next run finds the deposit of the journal and waits for it. It sends no second one.
        before_lasna = net.blocks()[LASNA]
        result = net.ops("recover", "--chain", name, env=short)
        assert result.returncode != 0 and "an earlier run stopped at this transaction" in result.stdout
        assert net.nonce(name, "pending") == nonce + 1 and net.blocks()[LASNA] == before_lasna
        assert net.data()["contracts"]["vaults"][name]["recoveries"][-1]["pending"]["deposit"] == journal

        # A new transaction of the same account does not go out while one waits for its receipt.
        ops_timeout, ops.RECEIPT_TIMEOUT = ops.RECEIPT_TIMEOUT, 2
        try:
            with pytest.raises(ops.OpsError, match="has a transaction with no receipt"):
                ops.send_transaction(net.rpcs[name], net.account, net.account.address, b"", 1)
        finally:
            ops.RECEIPT_TIMEOUT = ops_timeout
        assert net.nonce(name, "pending") == nonce + 1
    finally:
        net.anvils[name].request("evm_setAutomine", [True])
        net.anvils[name].request("anvil_mine", ["0x1"])

    result = net.ops("recover", "--chain", name)
    assert result.returncode == 0, result.stderr + result.stdout
    assert journal["txs"][0] in result.stdout and "sent 0, remaining 0" in result.stdout
    assert net.nonce(name) == nonce + 1, "a second deposit"
    assert net.debt(name, vault) == 0
    assert net.read(name, net.ledger[name], net.ledger_abi, "reserves", vault) == reserve
    recovery = net.data()["contracts"]["vaults"][name]["recoveries"][-1]
    assert recovery["deposit"] == journal["txs"][0] and "pending" not in recovery and len(recovery["vaultUp"]) == 1


# ----------------------------------------------------------------------------------------------
# Fork relay
# ----------------------------------------------------------------------------------------------


def test_cron_tick_runs_the_gauge(net):
    assert net.read(LASNA, net.hub, net.hub_abi, "lastTick") == 0
    result = net.relay("--cron-tick")
    # One trigger for the hub. One sweep for each vault that is up and has an enrolled target.
    assert relayed(result) == (1, 2)
    assert "cron tick Cron10000 of block" in result.stdout
    assert net.read(LASNA, net.hub, net.hub_abi, "lastTick") > 0


def test_forkrelay_refuses_a_state_file_that_is_not_usable_and_a_payload_with_no_first_argument(net, tmp_path):
    before = net.blocks()
    state = tmp_path / "state.json"
    state.write_text('{"lasna": {"block"')
    command = [part if part != str(net.state) else str(state) for part in net.relay_command()]
    result = subprocess.run(command, env=net.env, capture_output=True, text=True, timeout=60, cwd=ops.ROOT)
    assert result.returncode != 0 and "relay state" in result.stderr

    with pytest.raises(ops.OpsError):
        net.node.deliver(BASE, net.vault(BASE), bytes.fromhex("12345678"), 200_000, net.account.address)
    assert net.blocks() == before


# ----------------------------------------------------------------------------------------------
# A run that stops between two steps
# ----------------------------------------------------------------------------------------------


class LostAnswer(ops.Rpc):
    """The node gets the transaction, and its answer does not arrive."""

    def request(self, method, params=(), attempts=None):
        result = super().request(method, params, attempts)
        if method == "eth_sendRawTransaction":
            raise ops.TransportError("the answer is lost")
        return result


class NeverSent(ops.Rpc):
    """The transaction does not reach the node."""

    def request(self, method, params=(), attempts=None):
        if method == "eth_sendRawTransaction":
            raise ops.TransportError("the node is down")
        return super().request(method, params, attempts)


@pytest.mark.parametrize("broken", [LostAnswer, NeverSent])
def test_a_deployment_that_stopped_is_continued_and_not_sent_a_second_time(net, broken):
    rpc, sender = net.rpcs[LASNA], net.account.address
    nonce = int(rpc.request("eth_getTransactionCount", [sender, "latest"]), 16)
    entry, saved = {}, []

    def save():
        saved.append(json.loads(json.dumps(entry)))

    with pytest.raises(ops.TransportError):
        ops.deploy_recorded(broken(LASNA, [net.anvils[LASNA].url]), net.account, net.callback, (), 0, entry, save,
                            "canary")
    # The nonce and the hash were in the file before the transaction went to the node.
    assert saved and saved[0]["pendingDeploy"]["nonce"] == nonce and saved[0]["pendingDeploy"]["txs"]
    assert entry["address"] is None

    first = saved[0]["pendingDeploy"]
    receipt = ops.deploy_recorded(rpc, net.account, net.callback, (), 0, entry, save, "canary")
    assert int(rpc.request("eth_getTransactionCount", [sender, "latest"]), 16) == nonce + 1, "two deployments"
    if broken is NeverSent:
        # The second transaction has the same nonce and fees that a node accepts for a replacement.
        mined = rpc.transaction(receipt["transactionHash"])
        assert receipt["transactionHash"] not in first["txs"] and int(mined["nonce"], 16) == nonce
        for field, value in first["fees"].items():
            assert int(mined[field], 16) >= int(value) * 9 // 8 + 1
    else:
        assert receipt["transactionHash"] == first["txs"][0]
    assert entry["address"] == ops.create_address(sender, nonce) and rpc.code(entry["address"])
    assert entry["deployTx"] == receipt["transactionHash"] and "pendingDeploy" not in entry
    assert entry["deployBlock"] == int(receipt["blockNumber"], 16)


def test_a_journal_with_a_used_nonce_and_an_unknown_hash_stops_the_command(net):
    rpc, sender = net.rpcs[LASNA], net.account.address
    nonce = int(rpc.request("eth_getTransactionCount", [sender, "latest"]), 16)
    # The journal of an earlier run: a nonce that another transaction used, and a hash that the node does
    # not know. The result of that run is not known, so nothing is sent.
    entry = {"pendingDeploy": {"nonce": nonce - 1, "txs": ["0x" + "11" * 32]}}
    before = net.blocks()
    with pytest.raises(ops.OpsError, match="is not known"):
        ops.deploy_recorded(rpc, net.account, net.callback, (), 0, entry, lambda: None, "canary")
    assert net.blocks() == before and entry["pendingDeploy"]["txs"] == ["0x" + "11" * 32]


def test_an_enrollment_that_stopped_is_continued_from_its_journal(net):
    canary = net.new_callback_canary()
    arguments = ["enroll", "--chain", BASE, "--target", canary, "--label", "journal"]
    result = net.ops(*arguments, *CANARY_POLICY)
    assert result.returncode == 0, result.stderr
    complete = net.data()["enrollments"][-1]
    nonce = int(net.rpcs[LASNA].transaction(complete["txs"]["enroll"])["nonce"], 16)
    before = net.blocks()

    def edit(change):
        data = net.data()
        change(data["enrollments"])
        net.manifest.write_text(json.dumps(data, indent=2))

    def journal_of(tx_hash):
        def change(records):
            txs = records[-1]["txs"]
            txs["pending"] = {"enroll": {"nonce": nonce, "txs": [tx_hash]}}
            txs.update(enroll=None, enrollBlock=None)
        return change

    # As if the run had stopped after it sent `hub.enroll` and before it saw the receipt. The journal has
    # the hash: the next run takes the receipt and sends nothing.
    edit(journal_of(complete["txs"]["enroll"]))
    result = net.ops(*arguments, *CANARY_POLICY)
    assert result.returncode == 0, result.stderr
    assert "an earlier run stopped" in result.stdout
    restored = net.data()["enrollments"][-1]
    assert {**restored, "txs": {**restored["txs"], "readBackBlock": None}} == \
        {**complete, "txs": {**complete["txs"], "readBackBlock": None}}

    # A journal with a hash that the node does not know, and a nonce that was used: the result is not known.
    # `enroll` stops. `remove` stops too: that transaction could still get into a block after the removal.
    edit(journal_of("0x" + "22" * 32))
    result = net.ops(*arguments, *CANARY_POLICY)
    assert result.returncode != 0 and "is not known" in result.stderr
    result = net.ops("remove", "--chain", BASE, "--target", canary)
    assert result.returncode != 0 and "Run enroll again" in result.stderr

    # The hub has the target and the manifest has no record of the enrollment: refused.
    edit(lambda records: records.pop())
    result = net.ops(*arguments, *CANARY_POLICY)
    assert result.returncode != 0 and "no record of that enrollment" in result.stderr

    assert net.blocks() == before, "a command sent a transaction"
    assert net.target_state(BASE, canary) == "Active"
    edit(lambda records: records.append(complete))


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
