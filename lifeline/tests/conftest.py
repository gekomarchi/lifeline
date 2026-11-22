"""The test network: three mock chains with a known set of logs and ledger values."""

from __future__ import annotations

import json

import pytest

from lifeline.events import (
    BLACKLIST,
    CALLBACK_REQUEST,
    DEBT,
    RESERVES,
    SUBSCRIBE,
    WHITELIST,
    ledger_call,
    topic,
    word,
)
from lifeline.stats import DEFAULT_PARAMS, main

from .mocknode import MockChain, MockNode

START = DEFAULT_PARAMS["windowStartTimestamp"]


def address(tag: str) -> str:
    return "0x" + tag.rjust(40, "0")


LASNA_LEDGER = "0x" + "88" * 20
BASE_PROXY = address("b0")
UNI_PROXY = address("c0")
A1, A2, A3, A4 = (address(f"a{n}") for n in range(1, 5))
R1, R2, R3, R4, R5, R6 = (address(f"d{n}") for n in range(1, 7))
DEPOSITS, FILLS, RELAYER, OTHER_RELAYER = address("e1"), address("e2"), address("e3"), address("e4")

DEPOSIT_EVENT = "Deposited(uint256 indexed destination, uint256 indexed id, address sender)"
FILL_EVENT = "Filled(uint256 indexed origin, uint256 indexed id, address indexed relayer, (uint256,bytes) info)"
OTHER_TOPIC = topic("Ping(uint256)")
CRON100 = topic("Cron100(uint256)")

# The heights that a first run pins on the test network.
HEIGHTS = {
    "lasna": {"chainId": 5318007, "from": 1000, "to": 21000},
    "base-sepolia": {"chainId": 84532, "from": 1000, "to": 11000},
    "unichain-sepolia": {"chainId": 1301, "from": 500, "to": 20500},
}

SUBJECTS = {
    "fastfillReactive": {"chain": "lasna", "address": A3},
    "cronPoller": {"chain": "lasna", "address": A4, "ward": A1},
    "fastfill": {
        "deposit": {"chain": "base-sepolia", "address": DEPOSITS, "event": DEPOSIT_EVENT,
                    "where": {"destination": 1301}},
        "fill": {"chain": "unichain-sepolia", "address": FILLS, "event": FILL_EVENT,
                 "where": {"origin": 84532, "relayer": RELAYER}},
        "joinField": "id",
    },
}


class Network:
    """The mock chains, their server, and helpers to run the real command against them."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        # Block times of 1 s and 2 s. `from` is block 1000 on lasna and base-sepolia, block 500 on unichain-sepolia.
        self.lasna = MockChain(5318007, head=21000, genesis_time=START - 1000, block_time=1)
        self.base = MockChain(84532, head=11000, genesis_time=START - 2000, block_time=2)
        self.uni = MockChain(1301, head=20500, genesis_time=START - 500, block_time=1)
        self.chains = {"lasna": self.lasna, "base-sepolia": self.base, "unichain-sepolia": self.uni}
        self.ledgers = {"lasna": LASNA_LEDGER, "base-sepolia": BASE_PROXY, "unichain-sepolia": UNI_PROXY}
        self.node = MockNode(self.chains)
        self._fill()

    # A time is in seconds after the start of the window.

    def block(self, chain, time) -> int:
        return self.chains[chain].block_at(START + time)

    def ledger_event(self, chain, event, contract, time):
        return self.chains[chain].add_log(self.ledgers[chain], [event, word(contract)], self.block(chain, time))

    def subscribe(self, subscriber, topic0, time):
        data = "0x" + topic0[2:] + "0" * 192
        self.lasna.add_log(LASNA_LEDGER, [SUBSCRIBE, word(subscriber), word(5318007), word(LASNA_LEDGER)],
                           self.block("lasna", time), data)

    def request(self, chain_id, recipient, time):
        return self.lasna.add_log(LASNA_LEDGER, [CALLBACK_REQUEST, word(chain_id), word(A3), word(recipient)],
                                  self.block("lasna", time))

    def set_ledger(self, chain, contract, debt=0, reserves=0):
        calls = self.chains[chain].calls
        calls[(self.ledgers[chain], ledger_call(DEBT, contract))] = word(debt)
        calls[(self.ledgers[chain], ledger_call(RESERVES, contract))] = word(reserves)

    def _fill(self):
        # Lasna ledger: A1 goes into debt three times and stays in debt; A2 one time and recovers.
        for event, time in ((BLACKLIST, 1000), (WHITELIST, 1100), (BLACKLIST, 2000), (WHITELIST, 2100),
                            (BLACKLIST, 3000)):
            self.ledger_event("lasna", event, A1, time)
        self.ledger_event("lasna", BLACKLIST, A2, 4000)
        self.ledger_event("lasna", WHITELIST, A2, 4200)
        self.set_ledger("lasna", A1, debt=5)
        self.set_ledger("lasna", A3, reserves=10**18)
        self.set_ledger("lasna", A4, debt=7)  # in debt at `to`, with no debt event in the range
        self.subscribe(A1, CRON100, 100)
        self.subscribe(A2, OTHER_TOPIC, 200)
        self.subscribe(A3, CRON100, 300)
        self.subscribe(A4, OTHER_TOPIC, 400)

        # Base Sepolia proxy. R1: one closed interval. R2: open to `to`. R3: open at `from`. R6: no request.
        self.r1_start = self.ledger_event("base-sepolia", BLACKLIST, R1, 4000)
        self.r1_end = self.ledger_event("base-sepolia", WHITELIST, R1, 8000)
        self.r2_start = self.ledger_event("base-sepolia", BLACKLIST, R2, 10000)
        self.r3_end = self.ledger_event("base-sepolia", WHITELIST, R3, 6000)
        self.r6_start = self.ledger_event("base-sepolia", BLACKLIST, R6, 2000)
        self.set_ledger("base-sepolia", R2, debt=9)
        self.set_ledger("base-sepolia", R6, debt=3)
        self.set_ledger("base-sepolia", R4, reserves=10**15)
        # Unichain Sepolia proxy. R5: open to `to`.
        self.r5_start = self.ledger_event("unichain-sepolia", BLACKLIST, R5, 12000)
        self.set_ledger("unichain-sepolia", R5, debt=4)

        # Callback requests on Lasna, by the class that the join must give.
        self.requests = {
            "inside": self.request(84532, R1, 4013),
            "before": self.request(84532, R1, 3900),
            "after": self.request(84532, R1, 8050),
            "margin": self.request(84532, R1, 7997),
            "open_to_to": self.request(84532, R2, 17200),
            "open_at_from": self.request(84532, R3, 5500),
            "no_debt_event": self.request(84532, R4, 9000),
            "not_configured": self.request(999, R1, 9100),
            "other_chain": self.request(1301, R5, 12030),
        }

        # fastfill: three fills by the relayer (2 s, 3 s, 5 s), one deposit with no fill,
        # one fill by another relayer, one fill with no deposit, one deposit to another chain.
        for deposit_id, time in ((1, 3000), (2, 3100), (3, 3200), (4, 3300)):
            self.base.add_log(DEPOSITS, [topic("Deposited(uint256,uint256,address)"), word(1301), word(deposit_id)],
                              self.block("base-sepolia", time))
        self.base.add_log(DEPOSITS, [topic("Deposited(uint256,uint256,address)"), word(10), word(5)],
                          self.block("base-sepolia", 3400))
        fill_topic = topic("Filled(uint256,uint256,address,(uint256,bytes))")
        for deposit_id, time, relayer in ((1, 3002, RELAYER), (2, 3103, RELAYER), (3, 3205, RELAYER),
                                          (4, 3309, OTHER_RELAYER), (99, 3500, RELAYER)):
            self.uni.add_log(FILLS, [fill_topic, word(84532), word(deposit_id), word(relayer)],
                             self.block("unichain-sepolia", time))

    # ------------------------------------------------------------- command

    def config(self, subjects=None):
        chains = {
            name: {"chainId": chain.chain_id, "role": "reactive" if name == "lasna" else "destination",
                   "ledger": self.ledgers[name], "rpc": []}
            for name, chain in self.chains.items()
        }
        path = self.tmp_path / "config.json"
        path.write_text(json.dumps({"chains": chains, "subjects": subjects or {}}))
        return path

    def arguments(self, out, *, subjects=None, heights=None, cache="cache", suffixes=("",)):
        """Command arguments. `suffixes` gives one RPC URL of each chain for each suffix, in that order."""
        arguments = ["--config", str(self.config(subjects)), "--cache", str(self.tmp_path / cache),
                     "--out", str(self.tmp_path / out), "--retries", "2", "--backoff", "0.01"]
        if heights:
            arguments += ["--heights", str(heights)]
        for name in self.chains:
            for suffix in suffixes:
                arguments += ["--rpc", f"{name}={self.node.url(name, suffix)}"]
        return arguments

    def run(self, out, **options) -> int:
        return main(self.arguments(out, **options))

    def data(self, out) -> dict:
        return json.loads((self.tmp_path / out / "data.json").read_text())

    def heights_file(self):
        """A snapshot with the heights that a first run pins, written without a run."""
        path = self.tmp_path / "pinned.json"
        path.write_text(json.dumps({"version": 1, "chains": HEIGHTS, "params": DEFAULT_PARAMS, "state": []}))
        return path

    def clear_requests(self):
        for chain in self.chains.values():
            chain.requests.clear()
            chain.posts.clear()


@pytest.fixture
def network(tmp_path):
    network = Network(tmp_path)
    yield network
    network.node.close()
