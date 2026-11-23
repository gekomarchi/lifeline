"""The measuring command against a mock node, and the two files of the drivers.

The command, the reader, the snapshot and the ABIs of the build output are real. The node is a local
JSON-RPC server with a known set of logs and of ledger values for each block, so each number of the table
has a value that this file states.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("eth_abi")

import eth_abi

from lifeline import experiment, ops
from lifeline.events import BLACKLIST, DEBT, RESERVES, WHITELIST, ledger_call, word

from .mocknode import MockChain, MockNode

BUILT = (ops.ARTIFACTS / "CronPoller.sol" / "CronPoller.json").exists()
needs_build = pytest.mark.skipif(not BUILT, reason="the measurement reads the ABIs of `forge build`")

LASNA, BASE = "lasna", "base-sepolia"


def address(tag: str) -> str:
    return "0x" + tag.rjust(40, "0")


LASNA_LEDGER, BASE_PROXY = "0x" + "88" * 20, address("b0")
HUB, VAULT, POLLER = address("f1"), address("f2"), address("f3")
RA, RB, RC = address("a1"), address("a2"), address("a3")
KA, KB, KC = address("c1"), address("c2"), address("c3")
OTHER = address("ee")
LOOPING, WAITING, REQUESTER = address("d1"), address("d2"), address("d3")


class StateChain(MockChain):
    """A mock chain with ledger values and balances that change with the block."""

    def __init__(self, *arguments):
        super().__init__(*arguments)
        self.history = {}  # (to, data) -> [(first block, value)]
        self.balance_history = {}  # address -> [(first block, value)]

    @staticmethod
    def _at(entries, block) -> int:
        value = 0
        for first, entry in entries:
            if first <= block:
                value = entry
        return value

    def handle(self, method, params):
        if self.refuse_state or method not in ("eth_call", "eth_getBalance"):
            return super().handle(method, params)
        block = int(params[1], 16)
        if method == "eth_getBalance":
            return "result", hex(self._at(self.balance_history.get(params[0].lower(), []), block))
        key = (params[0]["to"].lower(), params[0]["data"].lower())
        return "result", word(self._at(self.history.get(key, []), block))


class Network:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.lasna = StateChain(5318007, 5000, 1_700_000_000, 1)
        self.base = StateChain(84532, 3000, 1_700_000_000, 2)
        self.chains = {LASNA: self.lasna, BASE: self.base}
        self.ledgers = {LASNA: LASNA_LEDGER, BASE: BASE_PROXY}
        self.node = MockNode(self.chains)
        self.abis = {name: ops.load_artifact(name).abi for name in (
            "LifelineHub", "LifelineVault", "CanaryReactive", "CanaryCallback", "CronPoller", "ICallbackProxy")}
        self._fill()

    # Chain data -------------------------------------------------------------------------------

    def event(self, chain, emitter, contract, name, block, indexed=(), values=()):
        abi = self.abis[contract]
        plain = [ops._type_of(p) for p in abi._item("event", name)["inputs"] if not p.get("indexed")]
        data = "0x" + eth_abi.encode(plain, list(values)).hex()
        return self.chains[chain].add_log(emitter, [abi.topic(name), *(word(value) for value in indexed)], block, data)

    def ledger_event(self, chain, topic0, contract, block):
        self.chains[chain].add_log(self.ledgers[chain], [topic0, word(contract)], block)

    def ledger(self, chain, contract, reserves=(), debt=()):
        """`reserves` and `debt`: [(first block, value)] of a contract at the ledger of a chain."""
        history = self.chains[chain].history
        history[(self.ledgers[chain].lower(), ledger_call(RESERVES, contract))] = list(reserves)
        history[(self.ledgers[chain].lower(), ledger_call(DEBT, contract))] = list(debt)

    def canary(self, reactive, callback, pokes, ticks, pongs):
        for n in pokes:
            self.event(LASNA, reactive, "CanaryReactive", "Poke", 1000 + 100 * n, [n])
        for n in ticks:
            self.event(LASNA, reactive, "CanaryReactive", "Tick", 1001 + 100 * n, [n])
        for n in pongs:
            self.event(BASE, callback, "CanaryCallback", "Pong", 600 + 50 * n, [n])

    def _fill(self):
        pokes = range(1, 7)
        # Arm A: the callback canary stops after Pong(2), the reactive canary after Tick(3). No guardian.
        self.canary(RA, KA, pokes, (1, 2, 3), (1, 2))
        self.ledger_event(LASNA, BLACKLIST, RA, 1300)
        self.ledger_event(BASE, BLACKLIST, KA, 700)
        self.ledger(LASNA, RA, debt=[(1300, 9)])
        self.ledger(BASE, KA, debt=[(700, 4)])

        # Arm B: the poller pays the reactive canary at block 2300. The callback canary has no guardian.
        self.canary(RB, KB, pokes, (1, 2, 3, 6), (1, 2))
        self.ledger_event(LASNA, BLACKLIST, RB, 1300)
        self.ledger_event(LASNA, WHITELIST, RB, 2300)
        self.ledger_event(BASE, BLACKLIST, KB, 700)
        self.ledger(LASNA, RB, reserves=[(2300, 60), (2400, 50)], debt=[(1300, 9), (2300, 0)])
        self.ledger(BASE, KB, debt=[(700, 4)])
        self.event(LASNA, POLLER, "CronPoller", "Paid", 2300, [RB], [700])
        self.event(LASNA, POLLER, "CronPoller", "Paid", 2300, [OTHER], [1])
        self.lasna.history[(POLLER, "0x" + self.abis["CronPoller"].encode("feeFloat").hex())] = [(1050, 1000)]
        self.ledger(LASNA, POLLER, reserves=[(1050, 1000), (2300, 400)])
        self.lasna.balance_history[POLLER] = [(1050, 5700), (2300, 5000)]

        # Arm C: the hub clears the debt of the reactive canary 2 blocks later, the vault the debt of the
        # callback canary 5 blocks later.
        self.canary(RC, KC, pokes, pokes, pokes)
        self.ledger_event(LASNA, BLACKLIST, RC, 1300)
        self.ledger_event(LASNA, WHITELIST, RC, 1302)
        self.ledger_event(BASE, BLACKLIST, KC, 700)
        self.ledger_event(BASE, WHITELIST, KC, 705)
        self.ledger(LASNA, RC, reserves=[(1000, 30), (1300, 0), (1302, 100), (2000, 77)])
        self.ledger(BASE, KC, reserves=[(500, 3), (700, 0), (705, 51), (800, 33)])
        self.event(LASNA, HUB, "LifelineHub", "Rescued", 1302, [RC], [10, 110])
        # A rescue that no debt log started reports the start block 0 in `Gap`. The measurement does not use it.
        self.event(LASNA, HUB, "LifelineHub", "Gap", 1302, [RC], [0, 1302])
        self.event(LASNA, HUB, "LifelineHub", "ToppedUp", 2000, [RC], [40])
        self.event(LASNA, HUB, "LifelineHub", "Rescued", 2100, [OTHER], [1, 5000])
        self.event(LASNA, HUB, "LifelineHub", "SelfCare", 1302, values=[30])
        self.event(LASNA, HUB, "LifelineHub", "SelfCare", 2000, values=[20])
        self.event(LASNA, HUB, "LifelineHub", "SelfCare", 900, values=[77777])  # before `from`
        self.ledger(LASNA, HUB, reserves=[(900, 200), (1302, 190), (2000, 150)])
        self.lasna.balance_history[HUB] = [(900, 9200), (1302, 9060), (2000, 9000)]

        self.event(BASE, VAULT, "LifelineVault", "Result", 705, [7, KC], [0, 55])
        self.event(BASE, VAULT, "LifelineVault", "Result", 900, [8, KC], [2, 0])
        self.event(BASE, VAULT, "LifelineVault", "Result", 1450, [9, OTHER], [0, 7])
        self.event(BASE, VAULT, "LifelineVault", "SelfCare", 705, values=[5])
        self.event(BASE, VAULT, "LifelineVault", "SelfCare", 1460, values=[3])
        # The junk flood: the float is empty at block 1500, and the owner deposits 108 at block 2000.
        self.ledger_event(BASE, BLACKLIST, VAULT, 1500)
        self.ledger_event(BASE, WHITELIST, VAULT, 2000)
        self.ledger(BASE, VAULT, reserves=[(400, 100), (1400, 40), (1500, 0), (2000, 100)],
                    debt=[(1500, 8), (2000, 0)])
        self.base.balance_history[VAULT] = [(400, 860), (705, 800), (1450, 793), (1460, 790)]
        failure = self.abis["ICallbackProxy"].topic("CallbackFailure")
        for block in (1300, 1410, 1420, 1499):
            self.base.add_log(BASE_PROXY, [failure, word(VAULT)], block, "0x" + eth_abi.encode(["bytes"], [b""]).hex())

    # Files and the command --------------------------------------------------------------------

    def manifest(self, change=None):
        def entry(contract, chain, contract_address, block):
            return {"contract": contract, "chain": chain, "chainId": self.chains[chain].chain_id,
                    "address": contract_address, "deployBlock": block, "deployTx": "0x" + "ab" * 32}

        data = {
            "version": 1, "cronTopic": "Cron10000",
            "chains": {name: {"chainId": chain.chain_id, "ledger": self.ledgers[name], "rpc": []}
                       for name, chain in self.chains.items()},
            "contracts": {"hub": {"chain": LASNA, "chainId": 5318007, "address": HUB, "deployBlock": 900},
                          "vaults": {BASE: {"chainId": 84532, "address": VAULT, "deployBlock": 400}}},
            "enrollments": [], "loopCheck": None,
            "fixtures": {
                "armA.reactive": entry("CanaryReactive", LASNA, RA, 1010),
                "armA.callback": entry("CanaryCallback", BASE, KA, 500),
                "armB.reactive": entry("CanaryReactive", LASNA, RB, 1020),
                "armB.callback": entry("CanaryCallback", BASE, KB, 510),
                "armB.poller": entry("CronPoller", LASNA, POLLER, 1050),
                "armC.reactive": entry("CanaryReactive", LASNA, RC, 1000),
                "armC.callback": entry("CanaryCallback", BASE, KC, 520),
                "looping": entry("LoopingCanary", LASNA, LOOPING, 1060),
                "waiting.callback": entry("CanaryCallback", BASE, WAITING, 530),
                "junkRequester": entry("JunkRequester", LASNA, REQUESTER, 1070),
            },
        }
        if change:
            change(data)
        path = self.tmp_path / "manifest.json"
        path.write_text(json.dumps(data))
        return path

    def scenarios(self, junk=None):
        path = self.tmp_path / "scenarios.json"
        path.write_text(json.dumps({"junk": junk} if junk else {}))
        return path

    def measure(self, out, *arguments, manifest=None, scenarios=None) -> int:
        argv = ["measure", "--manifest", str(manifest or self.manifest()), "--out", str(self.tmp_path / out),
                "--cache", str(self.tmp_path / "cache"), "--scenarios", str(scenarios or self.scenarios()),
                "--retries", "2", "--backoff", "0.01", *arguments]
        for name in self.chains:
            argv += ["--rpc", f"{name}={self.node.url(name)}"]
        return experiment.main(argv)

    def data(self, out) -> dict:
        return json.loads((self.tmp_path / out / "data.json").read_text())


@pytest.fixture
def network(tmp_path):
    network = Network(tmp_path)
    yield network
    network.node.close()


FLOOD = {"chain": BASE, "startBlock": 1400, "floodEndBlock": 1600, "endBlock": 2100, "steps": []}


@needs_build
def test_measure_gives_each_metric_of_each_arm_from_the_chain_data(network, capsys):
    assert network.measure("out", scenarios=network.scenarios(FLOOD)) == 0
    data = network.data("out")
    assert data["heights"] == {
        LASNA: {"chainId": 5318007, "from": 1000, "to": 5000, "toTimestamp": 1_700_005_000},
        BASE: {"chainId": 84532, "from": 500, "to": 3000, "toTimestamp": 1_700_006_000},
    }
    a, b, c = (data["arms"][arm] for arm in "ABC")

    # Events missed: Poke(n) with no Tick(n). Callbacks dropped: Tick(n) with no Pong(n).
    assert [(arm["pokes"], arm["ticks"], arm["pongs"]) for arm in (a, b, c)] == [(6, 3, 2), (6, 4, 2), (6, 6, 6)]
    assert [arm["eventsMissed"] for arm in (a, b, c)] == [3, 2, 0]
    assert [arm["callbacksDropped"] for arm in (a, b, c)] == [1, 2, 0]

    # Blocks of downtime: from BlacklistContract to WhitelistContract, or to `to`.
    assert a["downtimeBlocks"] == {LASNA: 5000 - 1300, BASE: 3000 - 700}
    assert b["downtimeBlocks"] == {LASNA: 2300 - 1300, BASE: 3000 - 700}
    # The hub log `Gap(target, 0, 1302)` has the start block 0: the ledger events give 2 blocks.
    assert c["downtimeBlocks"] == {LASNA: 2, BASE: 5}

    # Guardian cost.
    assert a["guardianCost"] == {"principal": {LASNA: "0", BASE: "0"}, "fees": {LASNA: "0", BASE: "0"},
                                 "feeScope": "no guardian", "feeDetail": {}}
    assert b["guardianCost"]["principal"] == {LASNA: "700", BASE: "0"}, "Paid of the ward only"
    assert b["guardianCost"]["fees"] == {LASNA: "600", BASE: "0"}, "fee float 1000 minus reserves 400"
    assert b["guardianCost"]["feeDetail"][LASNA]["exact"] is True
    assert c["guardianCost"]["principal"] == {LASNA: "150", BASE: "55"}, "Rescued 110 + ToppedUp 40; Result 55"
    # Hub: reserves 200 at block 999 and 150 at `to`, self-care 30 + 20 in the range.
    assert c["guardianCost"]["feeDetail"][LASNA] == {
        "opening": "200", "closing": "150", "selfCare": "50", "deposits": "0", "fees": "100", "exact": True}
    # Vault: reserves 100 at both ends, self-care 5 + 3, and the deposit of 108 that cleared the debt of 8.
    assert c["guardianCost"]["feeDetail"][BASE] == {
        "opening": "100", "closing": "100", "selfCare": "8", "deposits": "108", "fees": "116", "exact": False}
    assert c["guardianCost"]["fees"] == {LASNA: "100", BASE: "116"}
    assert "no share is estimated" in c["guardianCost"]["feeScope"]
    assert (a["guardianDowntimeBlocks"], b["guardianDowntimeBlocks"], c["guardianDowntimeBlocks"]) == \
        ({}, {LASNA: 0}, {LASNA: 0, BASE: 500})

    # Capital locked at `to`.
    assert a["capitalLocked"] == {"reactive.reserves": "0", "callback.reserves": "0"}
    assert b["capitalLocked"] == {"reactive.reserves": "50", "callback.reserves": "0", "poller.balance": "5000",
                                  "poller.reserves": "400"}
    assert c["capitalLocked"] == {"reactive.reserves": "77", "callback.reserves": "33", "hub.balance": "9000",
                                  "hub.reserves": "150", "vault.balance": "790", "vault.reserves": "100"}
    assert (a["capitalShared"], b["capitalShared"]) == ([], [])
    assert c["capitalShared"] == ["hub.balance", "hub.reserves", "vault.balance", "vault.reserves"]

    # The junk flood: balance 800 at block 1400 and 790 at block 1600; the vault paid 7 and moved 3 to its float.
    assert data["junk"] == {
        "status": "ok", "chain": BASE, "vault": VAULT, "startBlock": 1400, "endBlock": 1600,
        "balanceStart": "800", "balanceEnd": "790", "resultPaid": "7", "selfCareAdded": "3",
        "unexplainedBalanceChange": "0", "reservesStart": "40", "reservesEnd": "0", "debtEnd": "8",
        "callbackFailures": 3,
    }

    # The table: each arm, each metric, each value from the data file.
    table = (network.tmp_path / "out" / "table.md").read_text()
    for row in (
        "| Metric | A: no guardian | B: cron poller | C: Lifeline |",
        "| Blocks of downtime, lasna | 3700 | 1000 | 2 |",
        "| Blocks of downtime, base-sepolia | 2300 | 2300 | 5 |",
        "| Events missed | 3 | 2 | 0 |",
        "| Callbacks dropped | 1 | 2 | 0 |",
        "| Guardian cost: principal, lasna | 0 | 700 | 150 |",
        "| Guardian cost: principal, base-sepolia | 0 | 0 | 55 |",
        "| Guardian cost: fees, lasna | 0 | 600 | 100 |",
        "| Guardian cost: fees, base-sepolia | 0 | 0 | 116 |",
        "| Balance change that the payments do not explain | 0 |",
    ):
        assert row in table, row
    assert "| Capital locked | reactive.reserves 0<br>callback.reserves 0 |" in table
    for name, text in data["definitions"].items():
        assert text in table and text.endswith(".") and text.count(". ") == 0, name
    assert set(data["definitions"]) >= {"downtimeBlocks", "eventsMissed", "callbacksDropped", "guardianCost",
                                        "capitalLocked"}

    # Read methods only, and no query below `from`.
    for chain in network.chains.values():
        assert chain.method_names() <= {"eth_chainId", "eth_blockNumber", "eth_getLogs", "eth_getBlockByNumber",
                                        "eth_call", "eth_getBalance"}
    assert min(int(query[0]["fromBlock"], 16) for query in network.lasna.methods("eth_getLogs")) == 1000
    assert min(int(query[0]["fromBlock"], 16) for query in network.base.methods("eth_getLogs")) == 500
    output = capsys.readouterr().out
    assert "heights lasna chainId 5318007 from 1000 to 5000" in output


@needs_build
def test_measure_with_heights_reproduces_the_files_when_the_node_has_no_old_state(network, capsys):
    scenarios = network.scenarios(FLOOD)
    assert network.measure("first", scenarios=scenarios) == 0
    capsys.readouterr()

    # The chains go on: new blocks, new logs, other ledger values, no state at the old blocks, and no
    # scenario file. The saved heights fix the result.
    network.lasna.head, network.base.head = 9000, 8000
    network.event(LASNA, RA, "CanaryReactive", "Poke", 6000, [7])
    network.lasna.refuse_state = network.base.refuse_state = True
    heights = network.tmp_path / "first" / "heights.json"
    assert network.measure("second", "--heights", str(heights), scenarios=network.tmp_path / "absent.json") == 0
    for name in ("data.json", "table.md", "heights.json"):
        assert (network.tmp_path / "second" / name).read_bytes() == (network.tmp_path / "first" / name).read_bytes()
    output = capsys.readouterr().out
    # Each value that came from the snapshot is named on standard output, not in the files.
    assert "from snapshot (the node has no state at the old block): reserves(hub) on lasna at block 999" in output
    assert "from snapshot (the node has no state at the old block): balance of the vault on base-sepolia at " \
           "block 1400" in output
    assert "from snapshot" not in (network.tmp_path / "second" / "data.json").read_text()

    # The files hold no RPC URL and no wall-clock time.
    for name in ("data.json", "table.md", "heights.json"):
        text = (network.tmp_path / "first" / name).read_text()
        assert "127.0.0.1" not in text and "http" not in text, name
    saved = json.loads(heights.read_text())
    assert saved["params"] == {"junk": {"chain": BASE, "startBlock": 1400, "endBlock": 1600}}
    assert {(entry["chain"], entry["block"]) for entry in saved["state"]} >= {
        (LASNA, 999), (LASNA, 5000), (BASE, 499), (BASE, 3000), (BASE, 1400), (BASE, 1600), (BASE, 1999), (BASE, 2000)}

    # A value that the snapshot does not hold, with no state at the node: no file.
    assert network.measure("third", "--heights", str(heights),
                           manifest=network.manifest(lambda data: data["fixtures"]["armA.reactive"].update(
                               address=OTHER))) == 1
    assert not (network.tmp_path / "third").exists()


@needs_build
def test_measure_without_a_flood_and_with_a_flood_outside_the_heights(network):
    assert network.measure("none") == 0
    assert network.data("none")["junk"] == {"status": "not recorded"}
    assert "Status: not recorded." in (network.tmp_path / "none" / "table.md").read_text()

    # A scenario that did not reach the end of the flood is not a flood.
    partial = {key: value for key, value in FLOOD.items() if key != "floodEndBlock"}
    assert network.measure("partial", scenarios=network.scenarios(partial)) == 0
    assert network.data("partial")["junk"] == {"status": "not recorded"}

    late = {**FLOOD, "startBlock": 2900, "floodEndBlock": 3100}
    assert network.measure("late", scenarios=network.scenarios(late)) == 0
    assert network.data("late")["junk"] == {"status": "outside the heights", "chain": BASE, "startBlock": 2900,
                                            "endBlock": 3100}


@needs_build
def test_a_fee_figure_with_a_deposit_from_the_ledger_state_is_not_exact(network):
    """The ledger state gives the deposit that cleared a debt only if the ledger took no fee in that block.
    A reaction or a callback can end with no log, so no log proves that. Here the vault and the poller each
    get a deposit of 108 and pay a fee of 7 in the same block, with no log of that work: each looks like a
    deposit of 101."""
    network.ledger(BASE, VAULT, reserves=[(400, 100), (1400, 40), (1500, 0), (2000, 93)], debt=[(1500, 8), (2000, 0)])
    network.ledger_event(LASNA, BLACKLIST, POLLER, 3000)
    network.ledger_event(LASNA, WHITELIST, POLLER, 3100)
    network.ledger(LASNA, POLLER, reserves=[(1050, 1000), (2300, 400), (3000, 0), (3100, 93)],
                   debt=[(3000, 8), (3100, 0)])
    assert network.measure("out") == 0
    arms = network.data("out")["arms"]
    # Vault: 100 - 93 + self-care 8 + deposit 101. The fee of 7 in block 2000 is not in the figure.
    assert arms["C"]["guardianCost"]["feeDetail"][BASE] == {
        "opening": "100", "closing": "93", "selfCare": "8", "deposits": "101", "fees": "116", "exact": False}
    # Poller: 0 - 93 + fee float 1000 + deposit 101 = 1008. The ledger took 1015.
    assert arms["B"]["guardianCost"]["feeDetail"][LASNA] == {
        "opening": "0", "closing": "93", "selfCare": "0", "deposits": "1101", "fees": "1008", "exact": False}
    assert arms["B"]["guardianDowntimeBlocks"] == {LASNA: 100}
    # The hub got no such deposit: its figure is the ledger identity with no inferred part.
    assert arms["C"]["guardianCost"]["feeDetail"][LASNA]["exact"] is True
    assert "`feeDetail.exact` is false" in network.data("out")["definitions"]["guardianCost"]


def test_a_junk_payload_names_its_request_its_flood_and_its_requester():
    abi = ops.load_artifact("LifelineVault").abi
    number = experiment.junk_number
    payload = experiment.junk_payload(abi, 9_050_000, 17)
    assert len(payload) == 100 and payload[:4] == abi.selector("reconcile")
    # The request has the zero address as its first argument.
    assert number(abi, 9_050_000, payload) == 17
    # The node writes the requester there. A delivery counts only with the address of the requester.
    delivered = payload[:4] + bytes(12) + bytes.fromhex(REQUESTER[2:]) + payload[36:]
    assert number(abi, 9_050_000, delivered, REQUESTER) == 17
    assert number(abi, 9_050_000, delivered) is None
    # A copy of the payload that another caller requested: the node writes the address of that caller.
    copied = payload[:4] + bytes(12) + bytes.fromhex(OTHER[2:]) + payload[36:]
    assert number(abi, 9_050_000, copied, REQUESTER) is None and number(abi, 9_050_000, payload, REQUESTER) is None
    # A payload of another flood, of another function, or of another length is not a request of this flood.
    assert number(abi, 9_050_001, payload) is None
    assert number(abi, 9_050_000, abi.encode("reconcile", experiment.ZERO_ADDRESS, VAULT, 1)) is None
    assert number(abi, 9_050_000, b"\x12\x34\x56\x78" + payload[4:]) is None
    assert number(abi, 9_050_000, payload + b"\x00") is None and number(abi, 9_050_000, b"") is None

    # The payload of a request comes back from the `CallbackRequest` log of the system contract.
    configuration = eth_abi.encode(["(uint256,address,uint64,bytes)"], [(84532, VAULT, 250_000, payload)])
    log = {"data": "0x" + eth_abi.encode(["uint8", "bytes"], [0, configuration]).hex()}
    assert experiment.requested_payload(log) == payload


def test_the_bursts_of_the_flood_get_smaller_when_the_float_is_near_its_end():
    burst = experiment.next_burst
    # The fee of a delivery is not known, or self-care refilled the float: one request.
    assert burst(10, 1000, 0, 2000, 2000) == 1
    assert burst(10, 1000, 4, 2000, 2100) == 1
    # 4 deliveries took 400: the float of 1600 covers 16 more. A burst is at most half of them.
    assert burst(10, 1000, 4, 2000, 1600) == 8
    assert burst(5, 1000, 4, 2000, 1600) == 5
    assert burst(10, 3, 4, 2000, 1600) == 3
    # Near the end of the float the requests go out one at a time.
    assert [burst(10, 1000, 17, 2000, 2000 - 100 * 17), burst(10, 1000, 18, 2000, 200),
            burst(10, 1000, 19, 2000, 100), burst(10, 1000, 20, 2000, 0)] == [1, 1, 1, 1]
    # The limit is used: nothing more goes out.
    assert burst(10, 0, 4, 2000, 1600) == 0 and burst(10, 0, 0, 2000, 2000) == 0


def test_the_waiting_rescue_can_be_in_a_sweep_that_is_not_the_last_one():
    def result(request_id, outcome):
        return {"transactionHash": hex(request_id)}, {"id": request_id, "outcome": outcome, "paid": 5}

    earlier, first_sweep, last_sweep = result(7, 0), result(21, 0), result(22, 2)
    # Two catch-up sweeps (21 and 22): the canary that waits was in the first one.
    assert experiment.rescue_of([earlier, first_sweep, last_sweep], {21, 22}) == first_sweep
    # A result of another request (the gauge, a relay before the flood) is not the rescue of this recovery.
    assert experiment.rescue_of([earlier], {21, 22}) is None
    assert experiment.rescue_of([], {21}) is None


@needs_build
def test_measure_refuses_a_manifest_with_no_arm_and_writes_no_file(network, capsys):
    manifest = network.manifest(lambda data: data["fixtures"].pop("armB.poller"))
    assert network.measure("out", manifest=manifest) == 2
    assert "no fixtures entry armB.poller" in capsys.readouterr().err
    assert not (network.tmp_path / "out").exists()

    manifest = network.manifest(lambda data: data["fixtures"]["armC.callback"].update(chain="unichain-sepolia"))
    assert network.measure("out", manifest=manifest) == 2
    assert "not on one chain" in capsys.readouterr().err

    # The retries of a request are used: no number and no file.
    network.base.fail_methods = {"eth_getLogs"}
    assert network.measure("out") == 1
    assert not (network.tmp_path / "out").exists()

    # A local node with the cache or the output directory of the live run is refused.
    argv = ["measure", "--manifest", str(network.manifest()), "--rpc", f"lasna={network.node.url(LASNA)}",
            "--rpc", f"base-sepolia={network.node.url(BASE)}"]
    assert experiment.main(argv) == 2
    assert "a dry run needs its own --cache and --out" in capsys.readouterr().err

    # The same holds for a local URL that comes from the manifest, with no `--rpc`.
    def local_urls(data):
        for name in data["chains"]:
            data["chains"][name]["rpc"] = [network.node.url(name)]

    for chain in network.chains.values():
        chain.requests.clear()
    assert experiment.main(["measure", "--manifest", str(network.manifest(local_urls))]) == 2
    assert "a dry run needs its own --cache and --out" in capsys.readouterr().err
    assert all(chain.requests == [] for chain in network.chains.values()), "a request before the refusal"

    # A scenario file that is not an object.
    scenarios = network.tmp_path / "list.json"
    scenarios.write_text("[]")
    assert network.measure("out", scenarios=scenarios) == 2
    assert "is not a JSON object" in capsys.readouterr().err


# ----------------------------------------------------------------------------------------------
# The fixtures section and the scenario file
# ----------------------------------------------------------------------------------------------


def test_the_fixture_store_writes_only_the_fixtures_section(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "cronTopic": "Cron10000", "chains": {"lasna": {"chainId": 5318007}},
                                "contracts": {"hub": {"address": HUB}, "vaults": {}},
                                "enrollments": [{"label": "first"}], "loopCheck": {"status": "passed"},
                                "fixtures": {}}))
    store = experiment.FixtureStore(path)
    with pytest.raises(ops.OpsError, match="no fixtures entry armA.reactive"):
        store.entry("armA.reactive")

    # An operator command writes its record between the load and the save of the store.
    other = json.loads(path.read_text())
    other["enrollments"].append({"label": "second"})
    other["contracts"]["vaults"]["base-sepolia"] = {"recoveries": [1]}
    path.write_text(json.dumps(other))

    store.entries["armA.reactive"] = {"contract": "CanaryReactive", "address": None}
    store.save()
    with pytest.raises(ops.OpsError, match="no fixtures entry armA.reactive"):
        store.entry("armA.reactive")  # no address yet: the deployment did not finish
    store.entries["armA.reactive"]["address"] = RA
    store.save()

    saved = json.loads(path.read_text())
    assert saved == {**other, "fixtures": {"armA.reactive": {"contract": "CanaryReactive", "address": RA}}}
    assert experiment.FixtureStore(path).address("armA.reactive") == ops.checksum(RA)


def test_the_fixture_names_and_fields_are_the_ones_of_the_manifest_contract():
    assert [name for name, _, _ in experiment.FIXTURES] == [
        "armA.reactive", "armA.callback", "armB.reactive", "armB.callback", "armB.poller", "armC.reactive",
        "armC.callback", "looping", "waiting.callback", "junkRequester"]
    assert experiment.FIXTURE_KEY_VARIABLE == "LIFELINE_FIXTURE_KEY"
    assert experiment.DEFAULT_OUT == ops.ROOT / "evidence" / "experiment"


def test_the_scenario_file_keeps_each_record_and_refuses_other_fixtures(tmp_path):
    path = tmp_path / "evidence" / "scenarios.json"
    scenarios = experiment.Scenarios(path)
    record = scenarios.section("load", LASNA, {"armA.reactive": RA})
    assert record == {"chain": LASNA, "addresses": {"armA.reactive": RA}, "startBlock": None, "endBlock": None,
                      "steps": []}
    experiment.add_step(record, "start", LASNA, 10)
    experiment.add_step(record, "poke 1 armA", LASNA, 11, "0x" + "12" * 32)
    scenarios.save()

    again = experiment.Scenarios(path)
    record = again.section("load", LASNA, {"armA.reactive": RA})
    assert experiment.find_step(record, "poke 1 armA") == {"step": "poke 1 armA", "chain": LASNA, "block": 11,
                                                          "tx": "0x" + "12" * 32}
    assert experiment.find_step(record, "poke 2 armA") is None
    again.section("quarantine", LASNA, {"looping": LOOPING})
    again.save()
    assert set(json.loads(path.read_text())) == {"load", "quarantine"}

    with pytest.raises(ops.OpsError, match="other fixtures"):
        again.section("load", LASNA, {"armA.reactive": RB})


def test_each_driver_stops_before_a_request_without_its_key_or_its_fixtures(tmp_path, monkeypatch, capsys):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "chains": {}, "contracts": {"hub": None, "vaults": {}},
                                "fixtures": {}}))
    monkeypatch.delenv("LIFELINE_FIXTURE_KEY", raising=False)
    monkeypatch.delenv("LIFELINE_OWNER_KEY", raising=False)
    common = ["--manifest", str(path), "--scenarios", str(tmp_path / "scenarios.json")]
    assert experiment.main(["load", "--pokes", "1", "--interval", "0", *common]) == 1
    assert "no fixtures entry armA.reactive" in capsys.readouterr().err
    assert experiment.main(["quarantine", *common]) == 1
    assert "no fixtures entry looping" in capsys.readouterr().err
    assert experiment.main(["junk", "--chain", "base-sepolia", *common]) == 1
    assert "LIFELINE_OWNER_KEY is not set" in capsys.readouterr().err
    assert experiment.main(["load", "--pokes", "0", "--interval", "0", *common]) == 1
    assert not (tmp_path / "scenarios.json").exists()
    with pytest.raises(SystemExit):  # no target address on the command line
        experiment.main(["load", "--pokes", "1", "--interval", "0", "--target", RA, *common])
