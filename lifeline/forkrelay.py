"""Node emulation for fork dry runs. For local development nodes (anvil) only.

    python -m lifeline.forkrelay --rpc <chain>=<url> ... [--manifest <file>] [--cron-tick]

On the forks the system contract and the callback proxies are the real deployed code. This command
emulates only the node of Reactive Network, with the two addresses of the node:

- A log that matches an active subscription goes to the subscriber through the real `trigger`, from the
  injected address of the node, with 900,000 gas. A subscriber that is in debt gets no trigger.
- A `CallbackRequest` log of Lasna goes to the real `deliverCallback` of the destination proxy, from the
  authorized delivery account, with the requested gas limit. The first argument of the payload is the
  address of the contract that made the request.

The subscriptions come from the `Subscribe` and `Unsubscribe` logs of the system contract after the fork
block. A Lasna log matches the subscriptions that were active at its place in the log order. A destination
log matches the subscriptions that are active when the relay reads it.

A run ends when no new log remains. With `--watch` the command does not end: it relays each new log until
it gets an interrupt. Each run continues after the blocks that the run before it relayed (`--state`).

The emulation does not prove real delivery or latency.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import signal
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .keccak import keccak256
from .ops import (
    CRON_TOPICS,
    DEFAULT_CONFIG,
    DEFAULT_MANIFEST,
    LASNA,
    REACTIVE_IGNORE,
    ROOT,
    TRIGGER_GAS,
    OpsError,
    Rpc,
    RpcError,
    TransportError,
    _abi_codec,
    _read_json,
    checksum,
    hex_int,
    host_of,
    load_artifact,
    parse_address,
    parse_rpc,
    same_address,
    say,
)

# The sender of the delivery transactions on Base Sepolia and on Unichain Sepolia. The relay reads
# `_operators` of this account from each proxy.
DELIVERY_ACCOUNT = "0xF7E866D726658F6C3F14B8EE537fe268a4D91DDb"
# The node ignores a callback request below this gas limit.
MIN_CALLBACK_GAS = 100_000
# The emitter of the cron logs on Lasna: `CRON_EMITTER` of `src/LifelineHub.sol`.
CRON_EMITTER = "0x0000000000000000000000000000000000fffFfF"
# Stops a relay that does not end.
MAX_ACTIONS = 512
DEFAULT_STATE = ROOT / ".cache" / "forkrelay" / "state.json"
NODE_BALANCE = 10**24


def require_local(name: str, url: str) -> None:
    """Refuses a URL that is not on this machine. No request goes to a refused URL."""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise OpsError(f"forkrelay is for fork dry runs only: the RPC of {name}, {host_of(url)}, is not a "
                       f"local development node")


@dataclass
class Fork:
    name: str
    chain_id: int
    ledger: str
    rpc: Rpc
    fork_block: int = 0
    gas_price: int = 0
    scanned: int = 0  # the last block that the relay read
    start: int = 0  # logs at or before this block were relayed by an earlier run


class Relay:
    """The node emulation for one Lasna fork and the destination forks."""

    def __init__(self, forks: dict, operators=None, gas_prices=None, state_path=None, quiet=False):
        if LASNA not in forks:
            raise OpsError(f"forkrelay needs --rpc {LASNA}=<url>")
        self.forks = forks
        self.lasna = forks[LASNA]
        self.quiet = quiet
        self.state_path = Path(state_path) if state_path else None
        self.node_abi = load_artifact("ISystemContractNode").abi
        self.request_abi = load_artifact("ISystemContractCallbacks").abi
        self.system_abi = load_artifact("ISystemContract").abi
        self.proxy_abi = load_artifact("ICallbackProxy").abi
        self.subscribe_topic = self.system_abi.topic("Subscribe")
        self.unsubscribe_topic = self.system_abi.topic("Unsubscribe")
        self.request_topic = self.request_abi.topic("CallbackRequest")
        self.subscriptions = []
        self.requests = []
        self.triggers = self.deliveries = self.actions = 0
        self.operators = {}

        saved = self._load_state()
        for fork in forks.values():
            for url in fork.rpc.urls:
                require_local(fork.name, url)
            try:
                info = fork.rpc.request("anvil_nodeInfo")
            except (RpcError, TransportError):
                raise OpsError(f"forkrelay is for fork dry runs only: the RPC of {fork.name} is not a local "
                               f"development node (no anvil_nodeInfo)") from None
            actual = fork.rpc.chain_id()
            if actual != fork.chain_id:
                raise OpsError(f"{fork.name}: the node answers for chain ID {actual}, not {fork.chain_id}")
            fork.fork_block = int((info.get("forkConfig") or {}).get("forkBlockNumber") or 0)
            fork.scanned = fork.start = fork.fork_block
            entry = saved.get(fork.name)
            if entry and fork.fork_block <= entry["block"] <= fork.rpc.block_number():
                if fork.rpc.block(entry["block"])["hash"] == entry["hash"]:
                    fork.start = entry["block"]
            fork.gas_price = (gas_prices or {}).get(fork.name) or self._price_at_fork(fork)

        data = self.lasna.rpc.call(self.lasna.ledger, self.node_abi.encode("INJ_ADDR"))
        self.injected = checksum(self.node_abi.decode("INJ_ADDR", data))
        self._impersonate(self.lasna, self.injected)
        for fork in forks.values():
            if fork.name == LASNA:
                continue
            operator = parse_address((operators or {}).get(fork.name) or DELIVERY_ACCOUNT, "the delivery account")
            data = fork.rpc.call(fork.ledger, self.proxy_abi.encode("_operators", operator))
            if not self.proxy_abi.decode("_operators", data):
                raise OpsError(f"{operator} is not an authorized delivery account of the proxy of {fork.name}: "
                               f"give --operator {fork.name}=<address>")
            self.operators[fork.name] = operator
            self._impersonate(fork, operator)

    # State of earlier runs --------------------------------------------------------------------

    def _load_state(self) -> dict:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            return _read_json(self.state_path, "relay state")
        except OpsError:
            return {}

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        state = {}
        for fork in self.forks.values():
            state[fork.name] = {"block": fork.scanned, "hash": fork.rpc.block(fork.scanned)["hash"]}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n")

    # Node accounts ----------------------------------------------------------------------------

    @staticmethod
    def _price_at_fork(fork: Fork) -> int:
        """The gas price of the node transactions: the base fee at the fork point. On the fork the base fee
        goes down in each empty block; the live chain keeps it."""
        prices = [hex_int(fork.rpc.request("eth_gasPrice"))]
        for number in {fork.fork_block, max(fork.fork_block - 1, 0)}:
            base_fee = fork.rpc.block(number).get("baseFeePerGas")
            if base_fee:
                prices.append(hex_int(base_fee))
        return max(prices)

    @staticmethod
    def _impersonate(fork: Fork, account: str) -> None:
        fork.rpc.request("anvil_impersonateAccount", [account])
        fork.rpc.request("anvil_setBalance", [account, hex(NODE_BALANCE)])

    def _send(self, fork: Fork, sender: str, to: str, data: bytes, gas: int) -> dict:
        """One node transaction. A reverted transaction is a result, not an error."""
        self.actions += 1
        if self.actions > MAX_ACTIONS:
            raise OpsError(f"the relay does not end: more than {MAX_ACTIONS} node transactions in one run")
        tx_hash = fork.rpc.request("eth_sendTransaction", [{
            "from": sender, "to": to, "data": "0x" + data.hex(), "gas": hex(gas), "gasPrice": hex(fork.gas_price),
        }])
        deadline = time.monotonic() + 60
        while True:
            receipt = fork.rpc.receipt(tx_hash)
            if receipt is not None and receipt.get("blockNumber"):
                return receipt
            if time.monotonic() >= deadline:
                raise OpsError(f"{fork.name}: no receipt for the node transaction {tx_hash}; the node must mine "
                               f"each transaction at once (automine)")
            time.sleep(0.05)

    def _note(self, text: str) -> None:
        if not self.quiet:
            say(text)

    # Subscriptions ----------------------------------------------------------------------------

    def _update_subscriptions(self, log: dict, position: tuple, table=None) -> None:
        table = self.subscriptions if table is None else table
        topics = log["topics"]
        if not same_address(log["address"], self.lasna.ledger) or len(topics) != 4:
            return
        if topics[0] not in (self.subscribe_topic, self.unsubscribe_topic):
            return
        data = bytes.fromhex(log["data"][2:])
        if len(data) != 128:
            return
        key = (
            checksum("0x" + topics[1][-40:]),
            hex_int(topics[2]),
            checksum("0x" + topics[3][-40:]),
            tuple(int.from_bytes(data[i:i + 32], "big") for i in range(0, 128, 32)),
        )
        active = next((s for s in table if s["key"] == key and s["until"] is None), None)
        if topics[0] == self.subscribe_topic:
            if active is None:
                table.append({"key": key, "since": position, "until": None})
        elif active is not None:
            active["until"] = position

    def _subscribers(self, chain_id: int, log: dict, position=None) -> list:
        """The contracts with a subscription that matches the log. `position` is the place of a Lasna log in
        the log order; None means the subscriptions that are active now."""
        topics = [hex_int(topic) for topic in log["topics"]]
        found = []
        for subscription in self.subscriptions:
            subscriber, sub_chain, emitter, sub_topics = subscription["key"]
            if position is None:
                if subscription["until"] is not None:
                    continue
            elif not (subscription["since"] < position
                      and (subscription["until"] is None or position < subscription["until"])):
                continue
            if sub_chain not in (0, chain_id):
                continue
            if int(emitter, 16) != 0 and not same_address(emitter, log["address"]):
                continue
            if any(wanted != REACTIVE_IGNORE and (i >= len(topics) or topics[i] != wanted)
                   for i, wanted in enumerate(sub_topics)):
                continue
            if subscriber not in found:
                found.append(subscriber)
        return found

    def active_subscriptions(self, subscriber=None) -> list:
        """The keys (subscriber, chain ID, emitter, topics) of the subscriptions that are active now."""
        return [s["key"] for s in self.subscriptions
                if s["until"] is None and (subscriber is None or same_address(s["key"][0], subscriber))]

    def read_subscriptions(self) -> list:
        """The keys of the subscriptions that are active at the head of the Lasna fork, from the logs after
        the fork block. It sends nothing and does not change the state of the relay."""
        table = []
        logs = self.lasna.rpc.logs(self.lasna.ledger, [[self.subscribe_topic, self.unsubscribe_topic]],
                                   self.lasna.fork_block + 1)
        for log in logs:
            self._update_subscriptions(log, (hex_int(log["blockNumber"]), hex_int(log["logIndex"])), table)
        return [s["key"] for s in table if s["until"] is None]

    # Triggers ---------------------------------------------------------------------------------

    def _in_debt(self, subscriber: str) -> bool:
        data = self.lasna.rpc.call(self.lasna.ledger, self.proxy_abi.encode("debt", subscriber))
        return self.proxy_abi.decode("debt", data) != 0

    def trigger(self, subscriber: str, record: tuple, origin: str) -> dict:
        """Sends one log record to `react()` of a contract through the real `trigger`."""
        receipt = self._send(self.lasna, self.injected, self.lasna.ledger,
                             self.node_abi.encode("trigger", subscriber, record), TRIGGER_GAS)
        self.triggers += 1
        reverted = any(log["topics"] and log["topics"][0] == self.node_abi.topic("ReactiveContractReverted")
                       for log in receipt["logs"])
        status = "the trigger reverted" if hex_int(receipt["status"]) != 1 else (
            "react() reverted" if reverted else "ok")
        self._note(f"trigger   {subscriber} <- {origin}: tx {receipt['transactionHash']} block "
                   f"{hex_int(receipt['blockNumber'])}, {status}")
        return receipt

    def _trigger_log(self, chain_id: int, name: str, log: dict, subscribers: list) -> None:
        topics = [hex_int(topic) for topic in log["topics"]] + [0] * (4 - len(log["topics"]))
        record = (
            chain_id, checksum(log["address"]), topics[0], topics[1], topics[2], topics[3],
            bytes.fromhex(log["data"][2:]), hex_int(log["blockNumber"]),
            len(log["topics"]),  # `opCode` is the topic count, as in the trigger transactions on the chain
            hex_int(log["blockHash"]), hex_int(log["transactionHash"]), hex_int(log["logIndex"]),
        )
        origin = f"{name} block {hex_int(log['blockNumber'])} {checksum(log['address'])} {log['topics'][0][:10]}"
        for subscriber in subscribers:
            if self._in_debt(subscriber):
                self._note(f"no trigger {subscriber} <- {origin}: the subscriber is in debt")
                continue
            self.trigger(subscriber, record, origin)

    # Deliveries -------------------------------------------------------------------------------

    def deliver(self, name: str, recipient: str, payload: bytes, gas_limit: int, first_argument: str) -> dict:
        """Delivers one callback through the real `deliverCallback` of the proxy of a destination fork. The
        gas limit of the transaction is the gas limit of the callback."""
        fork = self.forks[name]
        if len(payload) >= 36:
            payload = payload[:4] + bytes(12) + bytes.fromhex(first_argument[2:]) + payload[36:]
        config = _abi_codec().encode(["(uint256,address,uint64,bytes)"],
                                     [(fork.chain_id, recipient, gas_limit, payload)])
        receipt = self._send(fork, self.operators[name], fork.ledger,
                             self.proxy_abi.encode("deliverCallback", 0, config), gas_limit)
        self.deliveries += 1
        failed = any(log["topics"] and log["topics"][0] == self.proxy_abi.topic("CallbackFailure")
                     for log in receipt["logs"])
        status = "the delivery reverted" if hex_int(receipt["status"]) != 1 else (
            "the callback reverted" if failed else "ok")
        self._note(f"delivery  {name} {recipient} gas {gas_limit} for {first_argument}: tx "
                   f"{receipt['transactionHash']} block {hex_int(receipt['blockNumber'])}, {status}")
        return receipt

    def _deliver_request(self, log: dict) -> None:
        chain_id = hex_int(log["topics"][1])
        requester = checksum("0x" + log["topics"][2][-40:])
        _, encoded = _abi_codec().decode(["uint8", "bytes"], bytes.fromhex(log["data"][2:]))
        ((_, recipient, gas_limit, payload),) = _abi_codec().decode(["(uint256,address,uint64,bytes)"], encoded)
        fork = next((f for f in self.forks.values() if f.chain_id == chain_id and f.name != LASNA), None)
        if fork is None:
            self._note(f"no delivery: the request of {requester} is for chain ID {chain_id}, which has no fork")
        elif gas_limit < MIN_CALLBACK_GAS:
            self._note(f"no delivery: the request of {requester} has a gas limit of {gas_limit}, below "
                       f"{MIN_CALLBACK_GAS}")
        else:
            self.deliver(fork.name, checksum(recipient), payload, gas_limit, requester)

    # Relay ------------------------------------------------------------------------------------

    def _new_logs(self, fork: Fork) -> list:
        head = fork.rpc.block_number()
        if head <= fork.scanned:
            return []
        logs = fork.rpc.logs(None, [], fork.scanned + 1, head)
        fork.scanned = head
        return logs

    def _relay_lasna(self) -> int:
        """Reads the new logs of Lasna until none is left. Returns the number of logs."""
        seen = 0
        while True:
            logs = self._new_logs(self.lasna)
            if not logs:
                return seen
            seen += len(logs)
            positions = [(hex_int(log["blockNumber"]), hex_int(log["logIndex"])) for log in logs]
            for log, position in zip(logs, positions):
                self._update_subscriptions(log, position)
            for log, position in zip(logs, positions):
                if position[0] <= self.lasna.start or not log["topics"]:
                    continue
                if same_address(log["address"], self.lasna.ledger) and log["topics"][0] == self.request_topic:
                    self.requests.append(log)
                self._trigger_log(self.lasna.chain_id, LASNA, log,
                                  self._subscribers(self.lasna.chain_id, log, position))

    def scan(self) -> None:
        """Reads the logs that earlier runs relayed: they give the subscriptions and nothing else."""
        if self.lasna.start > self.lasna.scanned:
            for log in self.lasna.rpc.logs(None, [], self.lasna.scanned + 1, self.lasna.start):
                self._update_subscriptions(log, (hex_int(log["blockNumber"]), hex_int(log["logIndex"])))
            self.lasna.scanned = self.lasna.start
        for fork in self.forks.values():
            fork.scanned = max(fork.scanned, fork.start)

    def round(self) -> int:
        """One pass over each fork. Returns the number of new logs: 0 means that no new log remains."""
        seen = self._relay_lasna()
        requests, self.requests = self.requests, []
        for request in requests:
            self._deliver_request(request)
        for fork in self.forks.values():
            if fork.name == LASNA:
                continue
            for log in self._new_logs(fork):
                seen += 1
                if log["topics"]:
                    self._trigger_log(fork.chain_id, fork.name, log, self._subscribers(fork.chain_id, log))
        self._save_state()
        if not seen:
            # No new log remains: the guard counts the node transactions of one burst.
            self.actions = 0
        return seen

    def cron_tick(self) -> int:
        """Sends one cron log record to each cron subscriber. Returns the number of records."""
        self._relay_lasna()
        cron_topics = {int.from_bytes(keccak256(f"{name}(uint256)".encode()), "big") for name in CRON_TOPICS}
        sent = []
        for subscriber, chain_id, emitter, topics in self.active_subscriptions():
            if chain_id != self.lasna.chain_id or not same_address(emitter, CRON_EMITTER):
                continue
            if topics[0] not in cron_topics or (subscriber, topics[0]) in sent:
                continue
            sent.append((subscriber, topics[0]))
            block = self.lasna.rpc.block_number()
            if self._in_debt(subscriber):
                self._note(f"no trigger {subscriber} <- cron tick: the subscriber is in debt")
                continue
            # A cron log has two topics: `topic0` and the indexed block number.
            record = (self.lasna.chain_id, CRON_EMITTER, topics[0], block, 0, 0, b"", block, 2, 0, 0, 0)
            self.trigger(subscriber, record, f"cron tick of block {block}")
        return len(sent)

    def run(self, cron_tick=False) -> tuple:
        """Relays until no new log remains. Returns (triggers, deliveries) of this run."""
        self.scan()
        if cron_tick:
            self.cron_tick()
        while self.round():
            pass
        return self.triggers, self.deliveries


def open_forks(entries: dict, rpc: dict) -> dict:
    """One `Fork` for each chain that has a `--rpc` URL."""
    forks = {}
    for name, urls in rpc.items():
        entry = entries.get(name)
        if not entry:
            raise OpsError(f"--rpc names the chain {name}, which the manifest or the configuration does not have")
        for url in urls:
            require_local(name, url)
        forks[name] = Fork(name, int(entry["chainId"]), parse_address(entry["ledger"], f"chains.{name}.ledger"),
                           Rpc(name, urls))
    return forks


def _pairs(values, what) -> dict:
    pairs = {}
    for value in values or []:
        chain, separator, text = value.partition("=")
        if not separator or not chain or not text:
            raise OpsError(f"{what} needs <chain>=<value>, got {value!r}")
        pairs[chain] = text
    return pairs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lifeline.forkrelay",
        description="Node emulation for fork dry runs: relays triggers and callback deliveries on local forks.",
    )
    parser.add_argument("--rpc", action="append", default=[], metavar="<chain>=<url>", required=True,
                        help="RPC URL of the local fork of one chain; repeatable")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), metavar="<file>",
                        help="manifest with the chain IDs and the ledgers; without the file, the configuration")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), metavar="<file>")
    parser.add_argument("--cron-tick", action="store_true", help="send one cron log record to each cron subscriber")
    parser.add_argument("--watch", action="store_true", help="do not stop: relay each new log until interrupted")
    parser.add_argument("--state", default=str(DEFAULT_STATE), metavar="<file>",
                        help="the blocks that earlier runs relayed (default .cache/forkrelay/state.json)")
    parser.add_argument("--operator", action="append", default=[], metavar="<chain>=<address>",
                        help="authorized delivery account of a proxy, if it is not the default one")
    parser.add_argument("--gas-price", action="append", default=[], metavar="<chain>=<wei>",
                        help="gas price of the node transactions (default: the base fee at the fork point)")
    args = parser.parse_args(argv)
    try:
        rpc = parse_rpc(args.rpc)
        source = Path(args.manifest) if Path(args.manifest).exists() else Path(args.config)
        entries = _read_json(source, "manifest or configuration").get("chains") or {}
        prices = {chain: int(value) for chain, value in _pairs(args.gas_price, "--gas-price").items()}
        relay = Relay(open_forks(entries, rpc), _pairs(args.operator, "--operator"), prices, args.state)
        say(f"forkrelay: {', '.join(f'{f.name} from block {f.start + 1}' for f in relay.forks.values())}")
        relay.run(args.cron_tick)
        if args.watch:
            # A stop request ends the command after the round that is in progress, when no new log remains.
            stop = []
            for number in (signal.SIGINT, signal.SIGTERM):
                signal.signal(number, lambda *_: stop.append(True))
            while not stop:
                if not relay.round():
                    time.sleep(0.3)
            while relay.round():
                pass
        say(f"triggers: {relay.triggers}")
        say(f"deliveries: {relay.deliveries}")
        return 0
    except (OpsError, RpcError, TransportError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
