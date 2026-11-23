"""The evidence generator against a mock node.

The command, the reader, the snapshot and the ABIs of the build output are real. The node is a local JSON-RPC
server with a known set of logs and of ledger values, so each figure of `JUDGE.md` has a value that this file
states.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("eth_abi")

import eth_abi

from lifeline import judge, ops
from lifeline.events import BLACKLIST, DEBT, RESERVES, WHITELIST, ledger_call, topic, word
from lifeline.reader import READ_METHODS

from .mocknode import MockChain, MockNode

pytestmark = pytest.mark.skipif(
    not (ops.ARTIFACTS / "LifelineVault.sol" / "LifelineVault.json").exists(),
    reason="the evidence generator reads the ABIs of `forge build`",
)

LASNA, BASE, UNI = "lasna", "base-sepolia", "unichain-sepolia"
GENESIS = 1_700_000_000


def address(tag: str) -> str:
    """An address in the EIP-55 form, as the data file has it."""
    return ops.checksum("0x" + tag.rjust(40, "0"))


LEDGERS = {LASNA: "0x" + "88" * 20, BASE: address("b0"), UNI: address("c0")}
HUB, VAULT_BASE, VAULT_UNI = address("f1"), address("f2"), address("f3")
REACTIVE, EXECUTOR, LATE = address("a1"), address("a2"), address("a3")
LOOPING, WAITING, ARM_C, GUEST = address("d1"), address("d2"), address("d3"), address("d4")
FILLS, RELAYER, OTHER_RELAYER = address("e2"), address("e3"), address("e4")
FILL_EVENT = "Filled(uint256 indexed origin, uint256 indexed id, address indexed relayer)"
DEPLOY = {LASNA: 900, BASE: 400, UNI: 300}
# A request ID of the hub is 2**128 + n.
R1, R2, R3, R4, R5 = (2**128 + n for n in range(1, 6))
OUTCOME = {name: position for position, name in enumerate(ops.OUTCOMES)}

# A table with a heading of its own and with spaces at the end of a line: the command must not change a byte.
TABLE = ("# Three-arm experiment\n\n| Metric | A | B | C |\n|---|---|---|---|\n| Events missed | 3 | 2 | 0 |  \n\n"
         "## Definitions\n\nLast line.\n")
JUNK_FIGURES = {
    "status": "ok", "chain": BASE, "vault": VAULT_BASE, "startBlock": 990, "endBlock": 1005, "balanceStart": "800",
    "balanceEnd": "800", "resultPaid": "0", "selfCareAdded": "0", "unexplainedBalanceChange": "0",
}
EXPERIMENT = {"version": 1, "heights": {LASNA: {"chainId": 5318007, "from": 1000, "to": 2900}},
              "definitions": {"eventsMissed": "A definition."},
              "arms": {"A": {"eventsMissed": 3}, "B": {"eventsMissed": 2}, "C": {"eventsMissed": 0}},
              "junk": JUNK_FIGURES}


def ref(log: dict) -> dict:
    return {"block": int(log["blockNumber"], 16), "tx": log["transactionHash"]}


class Network:
    """Three mock chains with the events of one hub and two vaults, and the files that the command reads."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.lasna = MockChain(5318007, 3000, GENESIS, 1)
        self.base = MockChain(84532, 2000, GENESIS, 2)
        self.uni = MockChain(1301, 2500, GENESIS, 2)
        self.chains = {LASNA: self.lasna, BASE: self.base, UNI: self.uni}
        self.node = MockNode(self.chains)
        self.abis = {name: ops.load_artifact(name).abi for name in ("LifelineHub", "LifelineVault")}
        self.logs = {}
        self._fill()

    # Chain data -------------------------------------------------------------------------------

    def event(self, chain, emitter, name, block, indexed=(), values=(), tx_of=None):
        """One log of the hub or of a vault. `tx_of`: a log of the same transaction."""
        abi = self.abis["LifelineHub" if emitter == HUB else "LifelineVault"]
        plain = [ops._type_of(p) for p in abi._item("event", name)["inputs"] if not p.get("indexed")]
        data = "0x" + eth_abi.encode(plain, list(values)).hex()
        log = self.chains[chain].add_log(emitter, [abi.topic(name), *(word(value) for value in indexed)], block, data)
        if tx_of is not None:
            log["transactionHash"] = tx_of["transactionHash"]
        return log

    def ledger_event(self, chain, topic0, contract, block, tx_of=None):
        log = self.chains[chain].add_log(LEDGERS[chain], [topic0, word(contract)], block)
        if tx_of is not None:
            log["transactionHash"] = tx_of["transactionHash"]
        return log

    def set_ledger(self, chain, contract, reserves=0, debt=0):
        calls = self.chains[chain].calls
        calls[(LEDGERS[chain].lower(), ledger_call(RESERVES, contract))] = word(reserves)
        calls[(LEDGERS[chain].lower(), ledger_call(DEBT, contract))] = word(debt)

    def fill(self, block, relayer=RELAYER):
        self.uni.add_log(FILLS, [topic("Filled(uint256,uint256,address)"), word(84532), word(block), word(relayer)],
                         block)

    def _fill(self):
        logs = self.logs
        hub = lambda *arguments, **options: self.event(LASNA, HUB, *arguments, **options)  # noqa: E731
        base = lambda *arguments, **options: self.event(BASE, VAULT_BASE, *arguments, **options)  # noqa: E731
        uni = lambda *arguments, **options: self.event(UNI, VAULT_UNI, *arguments, **options)  # noqa: E731

        # The constructors emit `SelfCare(feeFloat)`.
        hub("SelfCare", 900, values=[200])
        base("SelfCare", 400, values=[100])
        uni("SelfCare", 300, values=[100])

        # The fleet. The Lasna target: one top-up, then a debt that the hub clears 2 blocks later.
        logs["enroll reactive"] = hub("Enrolled", 1000, [5318007, REACTIVE])
        hub("ToppedUp", 1100, [REACTIVE], [40])
        self.ledger_event(LASNA, BLACKLIST, REACTIVE, 1498)
        logs["rescued reactive"] = hub("Rescued", 1500, [REACTIVE], [10, 110])
        hub("Gap", 1500, [REACTIVE], [1498, 1500], tx_of=logs["rescued reactive"])
        hub("SelfCare", 1500, values=[30], tx_of=logs["rescued reactive"])
        self.ledger_event(LASNA, WHITELIST, REACTIVE, 1500, tx_of=logs["rescued reactive"])

        # The destination target: `setPolicy` first, then `hub.enroll`. One rescue, one top-up of a sweep, and
        # a second debt with a relay request that has no result.
        # A debt of the target that ended before its enrollment: not a gap of the fleet report.
        self.ledger_event(BASE, BLACKLIST, EXECUTOR, 450)
        self.ledger_event(BASE, WHITELIST, EXECUTOR, 470)
        logs["policy executor"] = base("PolicySet", 600, [EXECUTOR])
        base("Result", 600, [0, EXECUTOR], [OUTCOME["NoNeed"], 0], tx_of=logs["policy executor"])
        logs["enroll executor"] = hub("Enrolled", 1010, [84532, EXECUTOR])
        # A first debt that a deposit of another account cleared: no relay request and no result.
        self.ledger_event(BASE, BLACKLIST, EXECUTOR, 650)
        logs["deposit executor"] = self.ledger_event(BASE, WHITELIST, EXECUTOR, 660)
        logs["debt executor"] = self.ledger_event(BASE, BLACKLIST, EXECUTOR, 800)
        logs["relayed executor"] = hub("Relayed", 1700, [R1, 84532, EXECUTOR])
        logs["result executor"] = base("Result", 805, [R1, EXECUTOR], [OUTCOME["Rescued"], 55])
        logs["whitelist executor"] = self.ledger_event(BASE, WHITELIST, EXECUTOR, 805, tx_of=logs["result executor"])
        base("SelfCare", 805, values=[8], tx_of=logs["result executor"])
        base("Result", 850, [1800, EXECUTOR], [OUTCOME["ToppedUp"], 7])
        logs["debt executor 2"] = self.ledger_event(BASE, BLACKLIST, EXECUTOR, 900)
        logs["relayed executor 2"] = hub("Relayed", 1900, [R2, 84532, EXECUTOR])

        # A target with `hub.enroll` and no policy: the vault answers `NoPolicy`.
        logs["enroll late"] = hub("Enrolled", 1020, [1301, LATE])
        logs["debt late"] = self.ledger_event(UNI, BLACKLIST, LATE, 700)
        logs["relayed late"] = hub("Relayed", 1400, [R3, 1301, LATE])
        logs["result late"] = uni("Result", 710, [R3, LATE], [OUTCOME["NoPolicy"], 0])
        # A deposit of another account in the transaction of that result: not the whitelist of a rescue.
        self.ledger_event(UNI, WHITELIST, LATE, 710, tx_of=logs["result late"])

        # The quarantine scenario: two rescues of the looping canary, then `Quarantined`.
        logs["enroll looping"] = hub("Enrolled", 1200, [5318007, LOOPING])
        logs["looping 1"] = hub("Rescued", 1210, [LOOPING], [11, 12])
        hub("Gap", 1210, [LOOPING], [1209, 1210], tx_of=logs["looping 1"])
        logs["looping 2"] = hub("Rescued", 1220, [LOOPING], [10, 11])
        hub("Gap", 1220, [LOOPING], [0, 1220], tx_of=logs["looping 2"])
        logs["quarantined"] = hub("Quarantined", 1230, [LOOPING])

        # The junk scenario on base-sepolia: the vault goes into debt, a target waits, the owner recovers it.
        base("PolicySet", 980, [WAITING])
        logs["vault debt"] = self.ledger_event(BASE, BLACKLIST, VAULT_BASE, 1000)
        logs["vault down"] = hub("VaultDown", 2100, [84532])
        self.ledger_event(BASE, BLACKLIST, WAITING, 1010)
        logs["pending"] = hub("Pending", 2110, [84532, WAITING])
        logs["vault deposit"] = self.ledger_event(BASE, WHITELIST, VAULT_BASE, 1020)
        logs["vault up"] = hub("VaultUp", 2120, [84532, R4], [1, 0])
        logs["result waiting"] = base("Result", 1030, [R4, WAITING], [OUTCOME["Rescued"], 9])
        self.ledger_event(BASE, WHITELIST, WAITING, 1030, tx_of=logs["result waiting"])

        # Fills of the fleet on unichain-sepolia, and one of another relayer.
        for block in (450, 500, 700):
            self.fill(block)
        self.fill(800, OTHER_RELAYER)

        # State at `to`.
        self.set_ledger(LASNA, HUB, reserves=150)
        self.set_ledger(BASE, VAULT_BASE, reserves=60)
        self.set_ledger(UNI, VAULT_UNI, debt=4)
        self.set_ledger(LASNA, REACTIVE, reserves=77)
        self.set_ledger(BASE, EXECUTOR, debt=3)
        self.lasna.balances[HUB.lower()] = 9000
        self.base.balances[VAULT_BASE.lower()] = 790
        self.uni.balances[VAULT_UNI.lower()] = 500

    def guest(self):
        """A guest on base-sepolia with one rescue. Returns its enrollment record."""
        policy = self.event(BASE, VAULT_BASE, "PolicySet", 620, [GUEST])
        enroll = self.event(LASNA, HUB, "Enrolled", 1030, [84532, GUEST])
        self.ledger_event(BASE, BLACKLIST, GUEST, 950)
        self.logs["relayed guest"] = self.event(LASNA, HUB, "Relayed", 2000, [R5, 84532, GUEST])
        self.logs["result guest"] = self.event(BASE, VAULT_BASE, "Result", 955, [R5, GUEST], [OUTCOME["Rescued"], 5])
        self.ledger_event(BASE, WHITELIST, GUEST, 955, tx_of=self.logs["result guest"])
        return self.enrollment("a guest canary", BASE, GUEST, policy, enroll, guest=True,
                               consent="written consent, message of the owner")

    # Files ------------------------------------------------------------------------------------

    def enrollment(self, label, chain, target, policy_log, enroll_log, guest=False, consent=None, removed=None):
        return {
            "label": label, "chain": chain, "chainId": self.chains[chain].chain_id, "target": target,
            "policy": {"low": "50", "high": "250", "maxDebt": "250", "dailyCap": "500", "maxRescues": 2,
                       "window": 3600},
            "guest": guest, "consent": consent,
            "txs": {"setPolicy": policy_log and policy_log["transactionHash"], "readBackBlock": None,
                    "enroll": enroll_log and enroll_log["transactionHash"]},
            "removed": removed,
        }

    def manifest(self, change=None):
        def fixture(contract, chain, contract_address, block):
            return {"contract": contract, "chain": chain, "chainId": self.chains[chain].chain_id,
                    "address": contract_address, "deployBlock": block, "deployTx": "0x" + "ab" * 32}

        logs = self.logs
        data = {
            "version": 1, "cronTopic": "Cron10000",
            "chains": {name: {"chainId": chain.chain_id, "ledger": LEDGERS[name], "rpc": []}
                       for name, chain in self.chains.items()},
            "contracts": {
                "hub": {"chain": LASNA, "chainId": 5318007, "address": HUB, "deployBlock": DEPLOY[LASNA]},
                "vaults": {BASE: {"chainId": 84532, "address": VAULT_BASE, "deployBlock": DEPLOY[BASE]},
                           UNI: {"chainId": 1301, "address": VAULT_UNI, "deployBlock": DEPLOY[UNI]}},
            },
            "enrollments": [
                self.enrollment("FleetReactive", LASNA, REACTIVE, None, logs["enroll reactive"]),
                self.enrollment("FleetExecutor", BASE, EXECUTOR, logs["policy executor"], logs["enroll executor"],
                                removed={"hubDisarm": "0x" + "aa" * 32, "vaultDisarm": None}),
                self.enrollment("late canary", UNI, LATE, None, logs["enroll late"]),
                self.enrollment("experiment looping", LASNA, LOOPING, None, logs["enroll looping"]),
                self.enrollment("experiment armC.callback", BASE, ARM_C, None, None),
            ],
            "loopCheck": None,
            "fixtures": {
                "armC.callback": fixture("CanaryCallback", BASE, ARM_C, 520),
                "looping": fixture("LoopingCanary", LASNA, LOOPING, 1150),
                "waiting.callback": fixture("CanaryCallback", BASE, WAITING, 530),
            },
        }
        if change:
            change(data)
        path = self.tmp_path / "manifest.json"
        path.write_text(json.dumps(data))
        return path

    def config(self, subjects=True):
        chains = {name: {"chainId": chain.chain_id, "role": "reactive" if name == LASNA else "destination",
                         "ledger": LEDGERS[name], "rpc": []} for name, chain in self.chains.items()}
        fill = {"chain": UNI, "address": FILLS, "event": FILL_EVENT, "where": {"origin": 84532, "relayer": RELAYER}}
        path = self.tmp_path / "network.json"
        path.write_text(json.dumps({"chains": chains, "subjects": {"fastfill": {"fill": fill}} if subjects else {}}))
        return path

    def scenarios(self) -> dict:
        def steps(chain_of, names):
            return [{"step": name, "chain": chain_of(name), **ref(self.logs[key])} for name, key in names]

        return {
            "quarantine": {
                "chain": LASNA, "addresses": {"looping": LOOPING, "hub": HUB}, "startBlock": 1205, "endBlock": 1300,
                "budget": {"maxRescues": 2, "dailyCap": "500"},
                "steps": [{"step": "poke 1", "chain": LASNA, "block": 1208, "tx": "0x" + "01" * 32},
                          *steps(lambda name: LASNA, (("Rescued 1", "looping 1"), ("Rescued 2", "looping 2"),
                                                      ("Quarantined", "quarantined")))],
            },
            "junk": {
                "chain": BASE, "addresses": {"vault": VAULT_BASE, "hub": HUB, "waiting.callback": WAITING},
                "startBlock": 990, "floodEndBlock": 1005, "endBlock": 1040, "requestId": str(R4),
                "steps": steps(lambda name: LASNA if name in ("VaultDown", "Pending", "VaultUp") else BASE, (
                    ("BlacklistContract(vault)", "vault debt"), ("VaultDown", "vault down"), ("Pending", "pending"),
                    ("recovery deposit", "vault deposit"), ("VaultUp", "vault up"),
                    ("Result(waiting.callback)", "result waiting"))),
            },
        }

    def experiment(self, scenarios=None, table=TABLE, data=EXPERIMENT):
        """The directory of the experiment. `table` or `data` None: the file is absent."""
        directory = self.tmp_path / "experiment"
        directory.mkdir(exist_ok=True)
        for name, text in (("table.md", table), ("data.json", data and json.dumps(data, indent=1)),
                           ("scenarios.json", json.dumps(self.scenarios() if scenarios is None else scenarios))):
            if text is None:
                (directory / name).unlink(missing_ok=True)
            else:
                (directory / name).write_text(text)
        return directory

    # The command ------------------------------------------------------------------------------

    def run(self, out, *arguments, manifest=None, config=None, experiment=None) -> int:
        argv = ["--manifest", str(manifest or self.manifest()), "--config", str(config or self.config()),
                "--experiment", str(experiment or self.experiment()), "--out", str(self.tmp_path / out),
                "--judge", str(self.tmp_path / out / "JUDGE.md"), "--cache", str(self.tmp_path / "cache"),
                "--retries", "2", "--backoff", "0.01", *arguments]
        for name in self.chains:
            argv += ["--rpc", f"{name}={self.node.url(name)}"]
        return judge.main(argv)

    def data(self, out) -> dict:
        return json.loads((self.tmp_path / out / "data.json").read_text())

    def document(self, out) -> str:
        return (self.tmp_path / out / "JUDGE.md").read_text()

    def requests(self) -> int:
        return sum(len(chain.requests) for chain in self.chains.values())


@pytest.fixture
def network(tmp_path):
    network = Network(tmp_path)
    yield network
    network.node.close()


def section(document: str, title: str) -> str:
    """The text of one `##` section of `JUDGE.md`. A heading inside the included table is not a section."""
    text = document[:document.index(judge.TABLE_BEGIN)] + document[document.index(judge.TABLE_END):]
    start = text.index(f"\n## {title}\n")
    following = text.find("\n## ", start + 1)
    return text[start:following if following != -1 else len(text)]


# ----------------------------------------------------------------------------------------------
# The join and each section
# ----------------------------------------------------------------------------------------------


def test_the_data_file_has_the_join_the_fleet_the_cost_and_the_scenarios_from_the_chain_data(network, capsys):
    assert network.run("out") == 0
    data, logs = network.data("out"), network.logs
    assert list(data) == sorted(["version", "heights", "rescues", "lasnaRescues", "fleet", "guardian", "quarantine",
                                 "junkRecovery", "guests", "experiment"])
    assert data["version"] == 1
    # `from` is the lowest deployment block that the run uses on the chain; `to` is the head.
    assert data["heights"] == {
        LASNA: {"chainId": 5318007, "from": 900, "to": 3000, "toTimestamp": GENESIS + 3000},
        BASE: {"chainId": 84532, "from": 400, "to": 2000, "toTimestamp": GENESIS + 4000},
        UNI: {"chainId": 1301, "from": 300, "to": 2500, "toTimestamp": GENESIS + 5000},
    }

    # The join, in the order of the `Relayed` logs: skip outcome, rescue, open.
    late, rescue, opened = data["rescues"]
    assert late == {
        "requestId": str(R3), "chain": UNI, "chainId": 1301, "target": LATE, "status": "joined",
        "debtEvent": ref(logs["debt late"]), "relayed": ref(logs["relayed late"]),
        "result": {**ref(logs["result late"]), "outcome": "NoPolicy", "paid": "0"}, "whitelist": None,
    }
    assert rescue == {
        "requestId": str(R1), "chain": BASE, "chainId": 84532, "target": EXECUTOR, "status": "joined",
        "debtEvent": {"block": 800, "tx": logs["debt executor"]["transactionHash"]},
        "relayed": {"block": 1700, "tx": logs["relayed executor"]["transactionHash"]},
        "result": {"block": 805, "tx": logs["result executor"]["transactionHash"], "outcome": "Rescued",
                   "paid": "55"},
        "whitelist": {"block": 805, "tx": logs["result executor"]["transactionHash"]},
    }
    # The payment of the vault and the `WhitelistContract` of the proxy are one transaction: the whitelist
    # of another deposit, and the debt event that it closed, are not the ones of this rescue.
    assert rescue["whitelist"] == ref(logs["whitelist executor"]) != ref(logs["deposit executor"])
    # A `Relayed` with no `Result`: the debt event is the last one before `to`.
    assert opened == {
        "requestId": str(R2), "chain": BASE, "chainId": 84532, "target": EXECUTOR, "status": "open",
        "debtEvent": ref(logs["debt executor 2"]), "relayed": ref(logs["relayed executor 2"]),
        "result": None, "whitelist": None,
    }

    # Each `Rescued` of the hub with the `Gap` of the same transaction.
    assert data["lasnaRescues"] == [
        {"target": LOOPING, "rescued": {**ref(logs["looping 1"]), "debt": "11", "paid": "12"},
         "gap": {"deadFromBlock": 1209, "aliveAtBlock": 1210}},
        {"target": LOOPING, "rescued": {**ref(logs["looping 2"]), "debt": "10", "paid": "11"},
         "gap": {"deadFromBlock": 0, "aliveAtBlock": 1220}},
        {"target": REACTIVE, "rescued": {**ref(logs["rescued reactive"]), "debt": "10", "paid": "110"},
         "gap": {"deadFromBlock": 1498, "aliveAtBlock": 1500}},
    ]

    # The fleet: no guest, no fixture. The target with no `setPolicy` in the chain data is left out.
    reactive, executor = data["fleet"]
    assert reactive == {
        "label": "FleetReactive", "chain": LASNA, "chainId": 5318007, "target": REACTIVE, "enrollBlock": 1000,
        "enrollTx": logs["enroll reactive"]["transactionHash"], "removed": None, "marks": {"low": "50", "high": "250"},
        # Lasna block 1000 has the timestamp of block 500 of unichain-sepolia: the fills of blocks 500 and 700.
        "fills": 2, "fillsFrom": {"chain": UNI, "address": FILLS, "block": 500},
        "rescues": 1, "topUps": 1,
        "gaps": [{"deadFromBlock": 1498, "aliveAtBlock": 1500, "blocks": 2,
                  "tx": logs["rescued reactive"]["transactionHash"]}],
        "principalPaid": "150", "reserves": {"block": 3000, "value": "77"},
    }
    assert executor == {
        "label": "FleetExecutor", "chain": BASE, "chainId": 84532, "target": EXECUTOR, "enrollBlock": 600,
        "enrollTx": logs["policy executor"]["transactionHash"],
        "removed": {"hubDisarm": "0x" + "aa" * 32, "vaultDisarm": None}, "marks": {"low": "50", "high": "250"},
        # Block 600 of base-sepolia has the timestamp of block 600 of unichain-sepolia: the fill of block 700.
        "fills": 1, "fillsFrom": {"chain": UNI, "address": FILLS, "block": 600},
        "rescues": 1, "topUps": 1,
        # Debt intervals at the proxy: one that a deposit of another account closed, one that the vault
        # closed, one that is open at `to`.
        "gaps": [{"deadFromBlock": 650, "aliveAtBlock": 660, "blocks": 10,
                  "tx": logs["deposit executor"]["transactionHash"]},
                 {"deadFromBlock": 800, "aliveAtBlock": 805, "blocks": 5,
                  "tx": logs["result executor"]["transactionHash"]},
                 {"deadFromBlock": 900, "aliveAtBlock": None, "blocks": 1100, "tx": None}],
        "principalPaid": "62", "reserves": {"block": 2000, "value": "0"},
    }
    output = capsys.readouterr().out
    assert "the enrollment of late canary" in output and "is not in the fleet report" in output

    # The cost: the payment events, and `SelfCare.added` minus the reserves plus the debt at `to`.
    assert data["guardian"] == {
        "hub": {"chain": LASNA, "address": HUB, "deployBlock": 900, "principalPaid": str(40 + 110 + 12 + 11),
                "selfCareAdded": "230", "floatConsumed": "80", "balance": {"block": 3000, "value": "9000"},
                "reserves": {"block": 3000, "value": "150"}, "debt": {"block": 3000, "value": "0"}},
        "vaults": {
            BASE: {"chain": BASE, "address": VAULT_BASE, "deployBlock": 400, "principalPaid": str(55 + 7 + 9),
                   "selfCareAdded": "108", "floatConsumed": "48", "balance": {"block": 2000, "value": "790"},
                   "reserves": {"block": 2000, "value": "60"}, "debt": {"block": 2000, "value": "0"}},
            UNI: {"chain": UNI, "address": VAULT_UNI, "deployBlock": 300, "principalPaid": "0",
                  "selfCareAdded": "100", "floatConsumed": "104", "balance": {"block": 2500, "value": "500"},
                  "reserves": {"block": 2500, "value": "0"}, "debt": {"block": 2500, "value": "4"}},
        },
    }

    assert data["quarantine"] == {
        "recorded": True, "chain": LASNA, "target": LOOPING,
        "rescues": [ref(logs["looping 1"]), ref(logs["looping 2"])], "quarantined": ref(logs["quarantined"]),
        "maxRescues": 2, "dailyCap": "500", "policyTx": logs["enroll looping"]["transactionHash"],
        "principalPaid": "23", "paidAfterQuarantine": "0",
    }
    assert data["junkRecovery"] == {
        "recorded": True, "chain": BASE, "vault": VAULT_BASE,
        "steps": [
            {"step": "BlacklistContract(vault)", "chain": BASE, **ref(logs["vault debt"])},
            {"step": "VaultDown", "chain": LASNA, **ref(logs["vault down"])},
            {"step": "Pending", "chain": LASNA, **ref(logs["pending"])},
            {"step": "WhitelistContract(vault)", "chain": BASE, **ref(logs["vault deposit"])},
            {"step": "VaultUp", "chain": LASNA, **ref(logs["vault up"])},
            {"step": "Result(waiting.callback)", "chain": BASE, **ref(logs["result waiting"])},
        ],
        "waitingRescue": {"target": WAITING, "requestId": str(R4), "outcome": "Rescued", "paid": "9"},
        # The balance figures are the ones of the data file of the experiment.
        "balanceStart": {"block": 990, "value": "800"}, "balanceEnd": {"block": 1005, "value": "800"},
        "unexplainedChange": "0",
    }
    assert data["guests"] == []
    assert data["experiment"] == EXPERIMENT


def test_judge_md_has_the_sections_in_order_and_the_open_request_in_the_open_list(network):
    assert network.run("out") == 0
    document = network.document("out")
    titles = [line[3:] for line in document.replace(TABLE, "").splitlines() if line.startswith("## ")]
    assert titles == ["Claim limit", "Heights", "Destination rescues", "Lasna rescues and gaps", "Fleet report",
                      "Guardian cost", "Three-arm table", "Quarantine", "Junk recovery", "Guests"]
    assert "restores future delivery, not zero loss" in section(document, "Claim limit")
    assert "Do not edit it by hand" in document.split("## Claim limit")[0]
    assert "| lasna | 5318007 | 900 | 3000 |" in section(document, "Heights")

    rescues = section(document, "Destination rescues")
    joined, opened = rescues.split("### Open")
    logs = network.logs
    # Joined: the four blocks and the four hashes of the rescue; a skip outcome has no whitelist.
    row = next(line for line in joined.splitlines() if line.startswith(f"| {R1} "))
    for name in ("debt executor", "relayed executor", "result executor", "whitelist executor"):
        assert f"{ref(logs[name])['block']}<br>`{logs[name]['transactionHash']}`" in row, name
    assert "| Rescued | 55 |" in row
    row = next(line for line in joined.splitlines() if line.startswith(f"| {R3} "))
    assert "| NoPolicy | 0 | none |" in row
    # Open: the request with no result, and only that one.
    assert str(R2) not in joined
    rows = [line for line in opened.splitlines() if line.startswith("| 3")]
    assert len(rows) == 1 and rows[0].startswith(f"| {R2} | {BASE} | 84532 | `{EXECUTOR}` |")
    assert f"1900<br>`{logs['relayed executor 2']['transactionHash']}`" in rows[0]

    assert f"| `{REACTIVE}` | 1500<br>`{logs['rescued reactive']['transactionHash']}` | 10 | 110 | 1498 | 1500 |" \
        in section(document, "Lasna rescues and gaps")
    fleet = section(document, "Fleet report")
    assert "### FleetReactive" in fleet and "### FleetExecutor" in fleet and "late canary" not in fleet
    assert "experiment" not in fleet.replace("fixture of the experiment", "")
    assert f"| Fills | 2 | {UNI}, from block 500, logs of `{FILLS}` |" in fleet
    assert "| Reserves kept | 77 | at block 3000 |" in fleet
    assert "| 900 | in debt at `to` | 1100 | none |" in fleet
    assert "A guardian top-up is a separate figure." in fleet
    cost = section(document, "Guardian cost")
    assert f"| hub | {LASNA} | `{HUB}` | 900 | 173 | 230 | 80 | 9000 | 150 | 0 | 3000 |" in cost
    assert "A deposit to the float by another account is not visible in these events" in cost
    quarantine = section(document, "Quarantine")
    assert "| Rescues from the enrollment to `Quarantined` | 2 |" in quarantine
    assert "| Principal paid after `Quarantined` | 0 | after the `Quarantined` log of block 1230, to block 3000 |" \
        in quarantine
    assert f"| `maxRescues` of the policy | 2 | `hub.enroll` `{logs['enroll looping']['transactionHash']}` |" \
        in quarantine
    recovery = section(document, "Junk recovery")
    rows = recovery.split("| Step | Chain | Block | Transaction |\n")[1].split("\n\n")[0].splitlines()[1:]
    steps = [line.split("|")[1].strip() for line in rows]
    assert steps == ["`BlacklistContract(vault)`", "`VaultDown`", "`Pending`", "`WhitelistContract(vault)`",
                     "`VaultUp`", "`Result(waiting.callback)`"]
    assert f"| `{WAITING}` | {R4} | Rescued | 9 | 1030<br>`{logs['result waiting']['transactionHash']}` |" in recovery
    assert "| Balance change that the payments do not explain | 0 | from block 990 to block 1005 |" in recovery


def test_each_figure_of_judge_md_is_in_the_data_file(network):
    record = network.guest()
    assert network.run("out", manifest=network.manifest(lambda data: data["enrollments"].append(record))) == 0
    data = network.data("out")
    data.pop("experiment")
    figures, texts = set(), set()

    def collect(value):
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            figures.add(str(len(value)))  # a count of the rows of a list
            for item in value:
                collect(item)
        elif isinstance(value, str) and not re.fullmatch(r"0x[0-9a-fA-F]+|\d+", value):
            texts.add(value)  # a label, a chain name, a step name
        elif value is not None and not isinstance(value, bool):
            figures.add(str(value))

    collect(data)
    document = network.document("out")
    main = document[:document.index(judge.TABLE_BEGIN)] + document[document.index(judge.TABLE_END):]
    for text in sorted(texts, key=len, reverse=True):
        main = main.replace(text, "")
    tokens = set(re.findall(r"0x[0-9a-fA-F]+|\d+", main))
    assert len(tokens) > 60
    assert tokens <= figures, sorted(tokens - figures)

    # Each block of a row has its hash next to it, and each amount has a block: no cell is a bare figure.
    for entry in data["rescues"]:
        for name in ("debtEvent", "relayed", "result", "whitelist"):
            evidence = entry[name]
            assert evidence is None or (evidence["block"] > 0 and re.fullmatch(r"0x[0-9a-f]{64}", evidence["tx"]))
    for entry in data["fleet"]:
        assert entry["enrollBlock"] > 0 and entry["reserves"]["block"] > 0
        assert entry["fillsFrom"]["block"] > 0


# ----------------------------------------------------------------------------------------------
# Start blocks and read methods
# ----------------------------------------------------------------------------------------------


def test_no_log_query_starts_before_the_deploy_block_of_its_contract_and_each_method_is_a_read(network):
    assert network.run("out") == 0
    # The contract that a query belongs to: the hub, a vault, and for a callback proxy or the fill contract
    # the Lifeline contract of that chain. The gaps of a Lasna target come from the hub: no ledger query.
    owners = {LASNA: {HUB}, BASE: {VAULT_BASE, LEDGERS[BASE]}, UNI: {VAULT_UNI, LEDGERS[UNI], FILLS}}
    for name, chain in network.chains.items():
        queries = [query[0] for query in chain.methods("eth_getLogs")]
        assert queries, name
        assert {ops.checksum(query["address"]) for query in queries} == owners[name]
        for query in queries:
            assert int(query["fromBlock"], 16) >= DEPLOY[name], query
            assert int(query["toBlock"], 16) <= chain.head, query
        assert min(int(query["fromBlock"], 16) for query in queries) == DEPLOY[name]
        assert chain.method_names() <= READ_METHODS
        assert not any(method.startswith(("eth_send", "eth_sign", "personal_")) for method in chain.method_names())


# ----------------------------------------------------------------------------------------------
# Reproduction
# ----------------------------------------------------------------------------------------------


def test_a_run_with_heights_gives_identical_files_with_no_run_date_and_no_url(network, capsys):
    assert network.run("first") == 0
    capsys.readouterr()
    heights = network.tmp_path / "first" / "heights.json"
    saved = json.loads(heights.read_text())
    assert set(saved) == {"version", "chains", "params", "state"} and saved["version"] == 1
    assert saved["chains"][BASE] == {"chainId": 84532, "from": 400, "to": 2000, "toTimestamp": GENESIS + 4000}
    assert {(entry["chain"], entry["block"]) for entry in saved["state"]} == {(LASNA, 3000), (BASE, 2000), (UNI, 2500)}

    # The chains go on: new blocks, new events, other ledger values, and no state at the old blocks.
    network.lasna.head, network.base.head, network.uni.head = 9000, 8000, 7000
    network.event(LASNA, HUB, "Rescued", 5000, [REACTIVE], [1, 2])
    network.event(BASE, VAULT_BASE, "Result", 3000, [R2, EXECUTOR], [OUTCOME["Rescued"], 99])
    network.fill(2600)
    network.set_ledger(LASNA, REACTIVE, reserves=1)
    for chain in network.chains.values():
        chain.refuse_state = True
    for out in ("second", "third"):
        assert network.run(out, "--heights", str(heights)) == 0
    for name in ("JUDGE.md", "data.json", "heights.json"):
        first = (network.tmp_path / "first" / name).read_bytes()
        assert (network.tmp_path / "second" / name).read_bytes() == first, name
        assert (network.tmp_path / "third" / name).read_bytes() == first, name
    # A value that the node cannot serve at the old block comes from the snapshot: named on standard output,
    # not in the files.
    output = capsys.readouterr().out
    assert "from snapshot (the node has no state at the old block): reserves(FleetReactive) on lasna" in output
    assert "from snapshot (the node has no state at the old block): balance of hub on lasna" in output
    for name in ("JUDGE.md", "data.json"):
        text = (network.tmp_path / "second" / name).read_text()
        assert "from snapshot" not in text and "http" not in text and "127.0.0.1" not in text, name
        assert not re.search(r"\b20\d\d-\d\d-\d\d\b|\d\d:\d\d", text), name
    assert "http" not in heights.read_text()

    # Heights of another deployment are refused, with no file.
    assert network.run("fourth", "--heights", str(heights),
                       manifest=network.manifest(lambda data: data["contracts"]["hub"].update(deployBlock=950))) == 2
    assert "the heights start at block 900" in capsys.readouterr().err
    assert not (network.tmp_path / "fourth").exists()


def test_a_failed_run_exits_non_zero_and_writes_no_file(network, capsys):
    network.base.always_fail = True
    assert network.run("out") == 1
    assert not (network.tmp_path / "out").exists()
    network.base.always_fail = False

    # A document path that cannot be written: the data file is not left behind.
    (network.tmp_path / "blocked").mkdir()
    assert network.run("out", "--judge", str(network.tmp_path / "blocked")) == 1
    assert not (network.tmp_path / "out" / "data.json").exists()
    assert "cannot write the output files" in capsys.readouterr().err

    # A local node and the paths of the live evidence: refused before any request.
    before = network.requests()
    argv = ["--manifest", str(network.manifest()), "--config", str(network.config()),
            "--experiment", str(network.experiment()), "--cache", str(network.tmp_path / "cache")]
    for name in network.chains:
        argv += ["--rpc", f"{name}={network.node.url(name)}"]
    assert judge.main(argv) == 2
    assert "a dry run needs its own --out and --judge" in capsys.readouterr().err
    assert network.requests() == before


# ----------------------------------------------------------------------------------------------
# Guests
# ----------------------------------------------------------------------------------------------


def test_the_guest_section_says_that_no_guest_is_enrolled_or_lists_the_hashes(network):
    record = network.guest()  # the events of the guest are on the chain in both runs
    assert network.run("none") == 0
    assert network.data("none")["guests"] == []
    assert section(network.document("none"), "Guests").strip().splitlines()[-1] == "No guest enrolled."

    assert network.run("one", manifest=network.manifest(lambda data: data["enrollments"].append(record))) == 0
    data, logs = network.data("one"), network.logs
    # The enrollment hashes come from the manifest, the rescue hashes from the chain.
    assert data["guests"] == [{
        "label": "a guest canary", "chain": BASE, "chainId": 84532, "target": GUEST,
        "consent": "written consent, message of the owner",
        "enrollment": {"setPolicy": record["txs"]["setPolicy"], "enroll": record["txs"]["enroll"]},
        "rescues": [ref(logs["result guest"])],
    }]
    guests = section(network.document("one"), "Guests")
    assert "No guest enrolled." not in guests
    for text in ("a guest canary", "written consent, message of the owner", record["txs"]["setPolicy"],
                 record["txs"]["enroll"], f"955<br>`{logs['result guest']['transactionHash']}`"):
        assert text in guests, text
    # A guest is not in the fleet report, and its rescue is in the join.
    assert [entry["label"] for entry in data["fleet"]] == ["FleetReactive", "FleetExecutor"]
    assert [entry["status"] for entry in data["rescues"] if entry["target"] == GUEST] == ["joined"]


# ----------------------------------------------------------------------------------------------
# The experiment and the sections with no record
# ----------------------------------------------------------------------------------------------


def test_the_table_of_the_experiment_is_included_unchanged_and_a_run_without_it_writes_nothing(network, capsys):
    assert network.run("out") == 0
    document = network.document("out")
    start = document.index(judge.TABLE_BEGIN) + len(judge.TABLE_BEGIN) + 1
    included = document[start:document.index(judge.TABLE_END)]
    assert included.encode() == (network.tmp_path / "experiment" / "table.md").read_bytes() == TABLE.encode()
    assert network.data("out")["experiment"] == json.loads((network.tmp_path / "experiment" / "data.json").read_text())

    for absent, name in ((dict(table=None), "table.md"), (dict(data=None), "data.json")):
        before = network.requests()
        assert network.run("absent", experiment=network.experiment(**absent)) != 0
        error = capsys.readouterr().err
        assert str(network.tmp_path / "experiment" / name) in error
        assert not (network.tmp_path / "absent").exists()
        assert network.requests() == before, "no request before the files of the experiment are read"


def test_a_section_with_no_record_and_a_fill_subject_that_is_not_configured(network, capsys):
    experiment = network.experiment(scenarios={}, data={**EXPERIMENT, "junk": {"status": "not recorded"}})
    assert network.run("out", experiment=experiment, config=network.config(subjects=False)) == 0
    data, document = network.data("out"), network.document("out")
    assert data["quarantine"] == {
        "recorded": False, "chain": None, "target": None, "rescues": [], "quarantined": None, "maxRescues": None,
        "dailyCap": None, "policyTx": None, "principalPaid": None, "paidAfterQuarantine": None}
    assert data["junkRecovery"] == {
        "recorded": False, "chain": None, "vault": None, "steps": [], "waitingRescue": None, "balanceStart": None,
        "balanceEnd": None, "unexplainedChange": None}
    assert section(document, "Quarantine").strip().endswith("Status: not recorded.")
    assert section(document, "Junk recovery").strip().endswith("Status: not recorded.")
    assert [(entry["fills"], entry["fillsFrom"]) for entry in data["fleet"]] == [(None, None), (None, None)]
    assert section(document, "Fleet report").count("| Fills | not configured |  |") == 2
    assert not network.uni.methods("eth_getBlockByNumber")[1:], "no header read for a fill count"
    output = capsys.readouterr().out
    assert "not configured: fills; subjects.fastfill.fill.chain is missing" in output
    assert "quarantine: not recorded" in output and "junkRecovery: not recorded" in output

    # A junk record with no balance figures in the data file of the experiment: the steps, and no figure.
    experiment = network.experiment(data={**EXPERIMENT, "junk": {"status": "not recorded"}})
    assert network.run("steps", experiment=experiment) == 0
    recovery = network.data("steps")["junkRecovery"]
    assert recovery["recorded"] and len(recovery["steps"]) == 6
    assert (recovery["balanceStart"], recovery["balanceEnd"], recovery["unexplainedChange"]) == (None, None, None)
    assert "Balance figures of the vault: not recorded in the data file of the experiment." \
        in section(network.document("steps"), "Junk recovery")


def test_a_payment_after_the_quarantined_log_is_in_its_own_figure(network):
    network.event(LASNA, HUB, "ToppedUp", 1300, [LOOPING], [5])
    assert network.run("out") == 0
    quarantine = network.data("out")["quarantine"]
    assert (quarantine["principalPaid"], quarantine["paidAfterQuarantine"]) == ("23", "5")
    assert len(quarantine["rescues"]) == 2
    assert "| Principal paid after `Quarantined` | 5 | after the `Quarantined` log of block 1230, to block 3000 |" \
        in section(network.document("out"), "Quarantine")


def test_the_join_takes_the_first_result_and_keeps_a_request_of_a_chain_with_no_vault_open(network, capsys):
    logs = network.logs
    # A duplicate callback: a second `Result` with the id and the target of the first one.
    network.event(BASE, VAULT_BASE, "Result", 820, [R1, EXECUTOR], [OUTCOME["NoNeed"], 0])
    # A result with the id of the open request and another target is not its result.
    network.event(BASE, VAULT_BASE, "Result", 910, [R2, WAITING], [OUTCOME["Rescued"], 3])
    # A request for a target with no debt log, and one for a chain that the manifest has no vault for.
    quiet = network.event(LASNA, HUB, "Relayed", 2300, [2**128 + 7, 1301, GUEST])
    network.event(UNI, VAULT_UNI, "Result", 900, [2**128 + 7, GUEST], [OUTCOME["NoNeed"], 0])
    far = network.event(LASNA, HUB, "Relayed", 2310, [2**128 + 8, 11155111, GUEST])
    assert network.run("out") == 0
    rescues = {entry["requestId"]: entry for entry in network.data("out")["rescues"]}
    assert rescues[str(R1)]["result"] == {**ref(logs["result executor"]), "outcome": "Rescued", "paid": "55"}
    assert rescues[str(R1)]["whitelist"] == ref(logs["whitelist executor"])
    assert (rescues[str(R2)]["status"], rescues[str(R2)]["result"]) == ("open", None)
    assert rescues[str(2**128 + 7)] == {
        "requestId": str(2**128 + 7), "chain": UNI, "chainId": 1301, "target": GUEST, "status": "joined",
        "debtEvent": None, "relayed": ref(quiet), "whitelist": None,
        "result": {"block": 900, "tx": rescues[str(2**128 + 7)]["result"]["tx"], "outcome": "NoNeed", "paid": "0"},
    }
    assert rescues[str(2**128 + 8)] == {
        "requestId": str(2**128 + 8), "chain": None, "chainId": 11155111, "target": GUEST, "status": "open",
        "debtEvent": None, "relayed": ref(far), "result": None, "whitelist": None,
    }
    row = next(line for line in network.document("out").splitlines() if line.startswith(f"| {2**128 + 8} "))
    assert "| not in the manifest | 11155111 |" in row and "| none |" in row
    assert "the manifest has no vault for chain ID 11155111" in capsys.readouterr().out


def test_two_rescues_of_one_transaction_have_each_the_gap_of_their_target(network):
    first = network.event(LASNA, HUB, "Rescued", 2400, [REACTIVE], [1, 2])
    second = network.event(LASNA, HUB, "Rescued", 2400, [LOOPING], [3, 4], tx_of=first)
    # The gaps are in the other order in the transaction.
    network.event(LASNA, HUB, "Gap", 2400, [LOOPING], [2390, 2400], tx_of=first)
    network.event(LASNA, HUB, "Gap", 2400, [REACTIVE], [2380, 2400], tx_of=first)
    assert network.run("out") == 0
    pairs = {(entry["target"], entry["rescued"]["debt"]): entry["gap"]
             for entry in network.data("out")["lasnaRescues"] if entry["rescued"]["tx"] == first["transactionHash"]}
    assert second["transactionHash"] == first["transactionHash"]
    assert pairs == {(REACTIVE, "1"): {"deadFromBlock": 2380, "aliveAtBlock": 2400},
                     (LOOPING, "3"): {"deadFromBlock": 2390, "aliveAtBlock": 2400}}


def test_a_gap_that_started_before_the_enrollment_counts_from_the_enrollment_block(network):
    # The target is in debt from block 580, its guardian takes it at block 600, and block 610 clears the debt.
    network.ledger_event(BASE, BLACKLIST, EXECUTOR, 580)
    cleared = network.ledger_event(BASE, WHITELIST, EXECUTOR, 610)
    # A Lasna target: the hub gives a dead-from block before the enrollment of block 1000.
    rescued = network.event(LASNA, HUB, "Rescued", 1040, [REACTIVE], [1, 1])
    network.event(LASNA, HUB, "Gap", 1040, [REACTIVE], [990, 1040], tx_of=rescued)
    assert network.run("out") == 0
    reactive, executor = network.data("out")["fleet"]
    assert executor["enrollBlock"] == 600
    assert executor["gaps"][0] == {"deadFromBlock": 580, "aliveAtBlock": 610, "blocks": 10,
                                   "tx": cleared["transactionHash"]}
    assert reactive["gaps"][0] == {"deadFromBlock": 990, "aliveAtBlock": 1040, "blocks": 40,
                                   "tx": rescued["transactionHash"]}
    fleet = section(network.document("out"), "Fleet report")
    assert f"| 580 | 610 | 10 | `{cleared['transactionHash']}` |" in fleet
    assert "The block count of a gap starts at the enrollment block if the debt started before it." in fleet


def test_the_fill_count_of_a_target_enrolled_before_the_fill_chain_heights_reads_no_header_below_them(network):
    # The vault of the fill chain is deployed at block 1200: after each enrollment of the fleet.
    change = lambda data: data["contracts"]["vaults"][UNI].update(deployBlock=1200)  # noqa: E731
    network.fill(1300)
    assert network.run("out", manifest=network.manifest(change)) == 0
    reactive, executor = network.data("out")["fleet"]
    assert reactive["fillsFrom"] == executor["fillsFrom"] == {"chain": UNI, "address": FILLS, "block": 1200}
    assert (reactive["fills"], executor["fills"]) == (1, 1)
    headers = [int(query[0], 16) for query in network.uni.methods("eth_getBlockByNumber") if query[0] != "latest"]
    assert headers and min(headers) >= 1200, headers
    assert min(int(query[0]["fromBlock"], 16) for query in network.uni.methods("eth_getLogs")) == 1200


def test_the_quarantine_has_the_policy_and_the_counts_of_the_enrollment_before_its_log(network, capsys):
    # Before the enrollment of the record: a rescue of an earlier enrollment. After the quarantine: the
    # target is enrolled again with another policy.
    network.event(LASNA, HUB, "Rescued", 1190, [LOOPING], [6, 7])
    again = network.event(LASNA, HUB, "Enrolled", 1600, [5318007, LOOPING])

    def change(data):
        record = network.enrollment("experiment looping, again", LASNA, LOOPING, None, again)
        record["policy"].update(maxRescues=4, dailyCap="1000")
        data["enrollments"].append(record)

    assert network.run("out", manifest=network.manifest(change)) == 0
    logs, quarantine = network.logs, network.data("out")["quarantine"]
    assert (quarantine["maxRescues"], quarantine["dailyCap"]) == (2, "500")
    assert quarantine["policyTx"] == logs["enroll looping"]["transactionHash"]
    assert quarantine["rescues"] == [ref(logs["looping 1"]), ref(logs["looping 2"])]
    assert quarantine["principalPaid"] == "23"

    # A record with another budget than the policy of that enrollment stops the run.
    scenarios = network.scenarios()
    scenarios["quarantine"]["budget"]["maxRescues"] = 3
    assert network.run("other", manifest=network.manifest(change),
                       experiment=network.experiment(scenarios=scenarios)) == 2
    assert "the quarantine record has maxRescues 3" in capsys.readouterr().err
    assert not (network.tmp_path / "other").exists()


def test_the_junk_recovery_needs_the_rescue_of_a_sweep_of_the_recovery_and_the_figures_of_its_flood(network, capsys):
    # Two more results of the target that waited: the `Result` of a `setPolicy` with the id 0, and a
    # `NoPolicy` with the id of the sweep. Neither is a rescue of a sweep of the recovery.
    others = [network.event(BASE, VAULT_BASE, "Result", 1035 + number, [request, WAITING], [OUTCOME[outcome], 0])
              for number, (request, outcome) in enumerate(((0, "Rescued"), (R4, "NoPolicy")))]

    # The figures of another flood of the same vault are not the figures of this record.
    for name, block in (("startBlock", 401), ("endBlock", 1004)):
        stale = {**EXPERIMENT, "junk": {**JUNK_FIGURES, name: block}}
        assert network.run(name, experiment=network.experiment(data=stale)) == 0
        recovery = network.data(name)["junkRecovery"]
        assert recovery["recorded"] and len(recovery["steps"]) == 6
        assert (recovery["balanceStart"], recovery["balanceEnd"], recovery["unexplainedChange"]) == (None,) * 3
        assert "has no balance figures of this junk flood" in capsys.readouterr().out
    network.experiment()

    # The record names another request ID than the result of its step.
    scenarios = network.scenarios()
    scenarios["junk"]["requestId"] = str(R5)
    assert network.run("id", experiment=network.experiment(scenarios=scenarios)) == 2
    assert f"the junk record has the request ID {R5}" in capsys.readouterr().err

    # The step of the record is one of the two other results.
    for other in others:
        scenarios = network.scenarios()
        scenarios["junk"]["steps"][-1].update(ref(other))
        scenarios["junk"].pop("requestId")
        assert network.run("other", experiment=network.experiment(scenarios=scenarios)) == 2
        assert "is not a rescue of a sweep of the recovery" in capsys.readouterr().err
    assert not any((network.tmp_path / name).exists() for name in ("id", "other"))


def test_heights_of_another_contract_and_output_paths_that_collide_are_refused(network, capsys):
    assert network.run("first") == 0
    heights = network.tmp_path / "first" / "heights.json"
    assert json.loads(heights.read_text())["params"] == {
        "contracts": {LASNA: HUB, BASE: VAULT_BASE, UNI: VAULT_UNI}}
    capsys.readouterr()

    # Heights above the head of a chain: the block does not exist.
    above = json.loads(heights.read_text())
    above["chains"][UNI]["to"] = 999_999
    (network.tmp_path / "above.json").write_text(json.dumps(above))
    assert network.run("above", "--heights", str(network.tmp_path / "above.json")) == 2
    assert f"{UNI} has no block 999999" in capsys.readouterr().err
    assert not (network.tmp_path / "above").exists()

    # A heights file that is not an object.
    (network.tmp_path / "list.json").write_text("[]")
    assert network.run("list", "--heights", str(network.tmp_path / "list.json")) == 2
    assert "Traceback" not in capsys.readouterr().err and not (network.tmp_path / "list").exists()

    # Another hub with the same deployment block is another deployment.
    other = network.manifest(lambda data: data["contracts"]["hub"].update(address=address("f9")))
    assert network.run("other", "--heights", str(heights), manifest=other) == 2
    assert "the heights are of the contracts" in capsys.readouterr().err
    assert not (network.tmp_path / "other").exists()

    # The document must not be a file of the output directory.
    before = network.requests()
    for name in ("data.json", "heights.json"):
        assert network.run("same", "--judge", str(network.tmp_path / "same" / name)) == 2
        assert "is an output file of --out" in capsys.readouterr().err
    assert network.requests() == before and not (network.tmp_path / "same").exists()

    # A file that is the staging file of another one: refused before the first write.
    kept = network.tmp_path / "first" / "data.json.tmp"
    kept.write_text("kept")
    data = (network.tmp_path / "first" / "data.json").read_bytes()
    assert network.run("first", "--judge", str(kept)) == 1
    assert "cannot write the output files" in capsys.readouterr().err
    assert kept.read_text() == "kept" and (network.tmp_path / "first" / "data.json").read_bytes() == data


@pytest.mark.parametrize("change, scenarios", [
    (lambda data: data["contracts"]["hub"].update(deployBlock="soon"), None),
    (lambda data: data["contracts"]["hub"].update(deployBlock=-5), None),
    (lambda data: data["contracts"]["vaults"][BASE].update(deployBlock=1.5), None),
    (lambda data: data["contracts"]["vaults"][UNI].update(deployBlock=True), None),
    (lambda data: data["enrollments"][0].update(policy=None), None),
    (lambda data: data["fixtures"].update(looping=None, extra=7), {"quarantine": {"chain": LASNA, "steps": None}}),
    (None, {"junk": {"chain": BASE, "steps": [{"step": name} for _, name in judge.JUNK_STEPS]}}),
])
def test_an_input_file_with_another_shape_gives_status_2_and_no_file(network, capsys, change, scenarios):
    code = network.run("out", manifest=network.manifest(change),
                       experiment=network.experiment(scenarios=scenarios))
    error = capsys.readouterr().err
    assert code == 2 and error.startswith("error: ") and "Traceback" not in error, error
    assert not (network.tmp_path / "out").exists()


def test_a_step_of_a_scenario_record_that_the_chain_does_not_have_stops_the_run(network, capsys):
    scenarios = network.scenarios()
    scenarios["quarantine"]["steps"][-1]["tx"] = "0x" + "77" * 32
    assert network.run("out", experiment=network.experiment(scenarios=scenarios)) == 2
    assert "does not have the step 'Quarantined' of the quarantine record" in capsys.readouterr().err
    assert not (network.tmp_path / "out").exists()

    scenarios = network.scenarios()
    scenarios["junk"]["steps"][1]["tx"] = "0x" + "78" * 32
    assert network.run("out", experiment=network.experiment(scenarios=scenarios)) == 2
    assert "does not have the step 'VaultDown' of the junk record" in capsys.readouterr().err
    assert not (network.tmp_path / "out").exists()
