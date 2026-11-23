"""Operator commands: deployment, Sourcify verification, onboarding, removal, vault recovery, loop check.

    python -m lifeline.ops deploy | verify | enroll | fleet | remove | recover | status | loop-check

One configuration (`deployments/config.json`) is the input. The manifest
(`deployments/manifest.json`) is the output and the input of each later command.
The owner key comes from the environment variable `LIFELINE_OWNER_KEY`. No
command prints or stores it.

Each ABI and each bytecode comes from the Foundry build output under `out/`.

Reusable parts, for the later units: `load_artifact`, `Artifact`, `Abi`, `Rpc`, `load_owner`,
`send_transaction`, `wait_receipt`, `transact`, `deploy_contract`, `find_logs`, `wait_for_log`, `receipt_logs`,
`Manifest`, `Policy`, `Session`, `enroll_target`, `to_wei`, `from_wei`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path

from .keccak import keccak256

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "out"
DEFAULT_CONFIG = ROOT / "deployments" / "config.json"
DEFAULT_MANIFEST = ROOT / "deployments" / "manifest.json"
OWNER_KEY_VARIABLE = "LIFELINE_OWNER_KEY"

LASNA = "lasna"
CHAIN_NAMES = (LASNA, "unichain-sepolia", "base-sepolia")
MANIFEST_VERSION = 1

# The gas limit of one trigger transaction on Lasna (spec F3).
TRIGGER_GAS = 900_000
# `SWEEP_MAX` of `src/LifelineHub.sol`: a file-level constant, so it is not in the ABI.
SWEEP_MAX = 8
# The vault float covers this number of full sweeps (spec §4.4).
FLOAT_SWEEPS = 4
# Wildcard topic of a subscription: `REACTIVE_IGNORE` of `src/interfaces/ISystemContract.sol`.
REACTIVE_IGNORE = 0xA65F96FC951C35EAD38878E0F0B7A3C744A6F5CCC1476B313353CE31712313AD
CRON_TOPICS = ("Cron10000", "Cron1000")
DEFAULT_CRON_TOPIC = "Cron10000"

# `TargetState` of `src/LifelineTypes.sol` and `Outcome` of `src/LifelineVault.sol`, by position.
TARGET_STATES = ("None", "Active", "Quarantined", "Parked", "Disarmed")
OUTCOMES = (
    "Rescued", "ToppedUp", "NoNeed", "Paused", "NoPolicy", "Quarantined", "NoCode", "DebtAboveCap", "DailyCap",
    "Floor", "Failed",
)
POLICY_FIELDS = ("low", "high", "maxDebt", "dailyCap", "maxRescues", "window")
POLICY_AMOUNTS = ("low", "high", "maxDebt", "dailyCap")
MAX_RESCUES_LIMIT = 4
UINT32_MAX = 2**32 - 1

# Seconds between two reads while a command waits for a receipt or a log.
POLL_SECONDS = float(os.environ.get("LIFELINE_POLL_SECONDS", "2"))
RECEIPT_TIMEOUT = float(os.environ.get("LIFELINE_RECEIPT_TIMEOUT", "180"))
# The gas limit of the callback of the loop-check canary. The proxy charges the gas used, not the limit.
CANARY_CALLBACK_GAS = 300_000
# The reserves of the Lasna canary of the loop check, as the fee of a reaction of this much gas.
CANARY_RESERVE_GAS = 400_000
SOURCIFY_SERVER = "https://sourcify.dev/server"


class OpsError(Exception):
    """A command cannot continue. The message says why. The exit status is 1."""


class RpcError(Exception):
    """The node answered with a JSON-RPC error."""

    def __init__(self, method, error):
        self.method = method
        self.code = error.get("code") if isinstance(error, dict) else None
        self.message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
        self.data = error.get("data") if isinstance(error, dict) else None
        super().__init__(f"{method}: {self.code} {self.message}"[:300])


class TransportError(Exception):
    """No JSON-RPC answer from any URL of the chain."""


# ----------------------------------------------------------------------------------------------
# Numbers and addresses
# ----------------------------------------------------------------------------------------------


def to_wei(text) -> int:
    """A decimal number in the native unit of a chain, as wei. 18 decimals."""
    try:
        value = Decimal(str(text))
    except InvalidOperation:
        raise OpsError(f"not a decimal amount: {text!r}") from None
    if not value.is_finite() or value < 0 or value.adjusted() > 60:
        raise OpsError(f"an amount is a decimal number of 0 or more: {text!r}")
    # Exact: the default context of `decimal` rounds to 28 digits.
    wei = value.scaleb(18, context=Context(prec=200))
    if wei != wei.to_integral_value() or wei >= 2**256:
        raise OpsError(f"an amount has 18 decimals at most and fits 256 bits: {text!r}")
    return int(wei)


def from_wei(wei: int) -> str:
    """Wei as a decimal number in the native unit."""
    whole, fraction = divmod(int(wei), 10**18)
    return f"{whole}.{fraction:018d}".rstrip("0").rstrip(".") if fraction else str(whole)


def amount(wei: int) -> str:
    return f"{from_wei(wei)} ({wei} wei)"


def checksum(address: str) -> str:
    """The EIP-55 form of an address."""
    digits = address[2:].lower()
    digest = keccak256(digits.encode()).hex()
    return "0x" + "".join(c.upper() if int(h, 16) >= 8 else c for c, h in zip(digits, digest))


def parse_address(text, what="address") -> str:
    text = str(text)
    digits = text[2:] if text.lower().startswith("0x") else ""
    if len(digits) != 40 or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise OpsError(f"{what} is not an address: {text!r}")
    if digits != digits.lower() and digits != digits.upper() and "0x" + digits != checksum(text):
        raise OpsError(f"{what} has a wrong checksum: {text!r}")
    return checksum(text)


def same_address(a, b) -> bool:
    return str(a).lower() == str(b).lower()


def hex_int(value) -> int:
    return int(value, 16)


def topic_of_address(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def topic_of_int(value: int) -> str:
    return "0x" + format(value, "064x")


# ----------------------------------------------------------------------------------------------
# JSON-RPC
# ----------------------------------------------------------------------------------------------


def host_of(url: str) -> str:
    """The host of an RPC URL. A URL can hold an API key, so the commands print the host only."""
    return urllib.parse.urlsplit(url).netloc.rpartition("@")[2] or url


_NO_BLOCK = re.compile(r"header not found|unknown block|block not found|block .*not available|blockoutofrange")


class _NoAnswer(Exception):
    """One URL gave no JSON-RPC answer."""


class Rpc:
    """A JSON-RPC client for one chain, with an ordered list of URLs. A failed URL gives its place to the next.

    With `chain_id`, each URL must answer `eth_chainId` with that value before its first use. So a retry on
    another URL can never move a transaction to another network."""

    def __init__(self, name, urls, chain_id=None, timeout=30.0, attempts=4):
        if not urls:
            raise OpsError(f"no RPC URL for chain {name}: use --rpc {name}=<url>")
        self.name = name
        self.urls = list(urls)
        self.expected_chain_id = chain_id
        self.timeout = timeout
        self.attempts = attempts
        self._id = 0
        self._dev = None
        self._verified = set()
        # The newest block that a receipt of this session named. A node behind a load balancer can be some
        # blocks behind the node that gave the receipt: each read waits for this block.
        self.floor = 0
        self._nonces = {}

    def _post(self, url, method, params):
        """One request to one URL. Returns the result. A JSON-RPC error is `RpcError`."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json", "user-agent": "lifeline-ops"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read())
            except ValueError:
                body = None
            if error.code == 429 or error.code >= 500 or not isinstance(body, dict):
                raise _NoAnswer(f"HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise _NoAnswer(type(error).__name__) from None
        if isinstance(body, dict) and body.get("error") is not None:
            raise RpcError(method, body["error"])
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        raise _NoAnswer("no result")

    def _verify(self, url) -> None:
        if self.expected_chain_id is None or url in self._verified:
            return
        actual = hex_int(self._post(url, "eth_chainId", []))
        if actual != self.expected_chain_id:
            raise OpsError(f"{self.name}: the RPC {host_of(url)} answers for chain ID {actual}, not "
                           f"{self.expected_chain_id}")
        self._verified.add(url)

    def request(self, method, params=(), attempts=None):
        attempts = attempts or self.attempts
        last = None
        for attempt in range(attempts):
            for url in self.urls:
                try:
                    self._verify(url)
                    return self._post(url, method, params)
                except _NoAnswer as failure:
                    last = failure
            if attempt + 1 < attempts:
                time.sleep(min(2.0**attempt, 8.0) * 0.5)
        raise TransportError(f"{self.name}: no answer for {method} ({last})")

    def chain_ids(self) -> list:
        """(host, chain ID or None) for each URL: what each one answers now."""
        answers = []
        for url in self.urls:
            try:
                answers.append((host_of(url), hex_int(self._post(url, "eth_chainId", []))))
            except (_NoAnswer, RpcError):
                answers.append((host_of(url), None))
        return answers

    # Reads

    def chain_id(self) -> int:
        return self.expected_chain_id or hex_int(self.request("eth_chainId"))

    def block_number(self) -> int:
        return hex_int(self.request("eth_blockNumber"))

    def block(self, tag="latest") -> dict:
        tag = hex(tag) if isinstance(tag, int) else tag
        block = self.request("eth_getBlockByNumber", [tag, False])
        if block is None:
            raise TransportError(f"{self.name}: the node has no block {tag}")
        return block

    def gas_price(self) -> int:
        """The current gas price: the largest of `eth_gasPrice` and the base fees of the newest blocks. The
        base fee of Lasna changes between two values from block to block."""
        prices = [hex_int(self.request("eth_gasPrice"))]
        base_fee = self.block().get("baseFeePerGas")
        if base_fee:
            prices.append(hex_int(base_fee))
            try:
                history = self.request("eth_feeHistory", ["0x8", "latest", []])
                prices += [hex_int(fee) for fee in history.get("baseFeePerGas") or []]
            except (RpcError, TransportError, AttributeError):
                pass
        return max(prices)

    def balance(self, address) -> int:
        return hex_int(self.request("eth_getBalance", [address, "latest"]))

    def code(self, address) -> bytes:
        return bytes.fromhex(self._read_at("eth_getCode", [address])[1][2:])

    def head(self) -> int:
        """The newest block of the node. It waits until the node has the newest block of a receipt of this
        session."""
        deadline = time.monotonic() + RECEIPT_TIMEOUT
        while True:
            block = self.block_number()
            if block >= self.floor:
                return block
            if time.monotonic() >= deadline:
                raise TransportError(f"{self.name}: the node is at block {block}, behind block {self.floor}")
            time.sleep(min(POLL_SECONDS, 1.0))

    def _read_at(self, method, params, block=None):
        """Sends a state read for one block: `head()` if none is given. Returns (block, result)."""
        deadline = time.monotonic() + 30
        while True:
            number = self.head() if block is None else block
            try:
                return number, self.request(method, [*params, hex(number)])
            except RpcError as error:
                # Another node of the same URL does not have the block yet.
                if not _NO_BLOCK.search(error.message.lower()) or time.monotonic() >= deadline:
                    raise
            time.sleep(min(POLL_SECONDS, 1.0))

    def call_at(self, to, data: bytes, block=None):
        """Calls a view at one block: `head()` if none is given. Returns (block, return data)."""
        number, result = self._read_at("eth_call", [{"to": to, "data": "0x" + data.hex()}], block)
        return number, bytes.fromhex(result[2:])

    def call(self, to, data: bytes, block=None) -> bytes:
        return self.call_at(to, data, block)[1]

    def _idle_count(self, sender):
        """The transaction count of a sender if each of its transactions has a receipt, or None. The count is
        the one of the newest block, not below the block of the newest receipt of this session. It is "idle"
        if it is not below the count with the waiting transactions and not below the nonces that this
        session knows as used."""
        _, count = self._read_at("eth_getTransactionCount", [sender])
        mined = hex_int(count)
        waiting = hex_int(self.request("eth_getTransactionCount", [sender, "pending"]))
        return mined if waiting <= mined and self._nonces.get(sender.lower(), 0) <= mined else None

    def wait_idle(self, sender, timeout: float = None) -> bool:
        """Waits until each transaction of a sender has a receipt. False at the timeout. A command calls it
        before it reads the state that decides its next transaction."""
        deadline = time.monotonic() + (RECEIPT_TIMEOUT if timeout is None else timeout)
        while self._idle_count(sender) is None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(POLL_SECONDS, 1.0))
        return True

    def require_idle(self, sender) -> None:
        """Waits until each transaction of a sender has a receipt. At the timeout it is an error: a command
        must not decide its steps from a state that a waiting transaction can still change."""
        if not self.wait_idle(sender):
            raise self.not_idle(sender)

    def not_idle(self, sender) -> "OpsError":
        return OpsError(f"{sender} has a transaction with no receipt on {self.name}: the state of the chain "
                        f"can still change, so nothing was sent. Run the command again when that transaction "
                        f"has a receipt")

    def fresh_nonce(self, sender) -> int:
        """The nonce of a new transaction of a sender. A command sends a new transaction only when each
        earlier transaction of the sender has a receipt. It waits for that state; then it is an error."""
        self.wait_idle(sender)
        count = self._idle_count(sender)
        if count is None:
            raise OpsError(f"{sender} has a transaction with no receipt on {self.name}: no new transaction was "
                           f"sent. Run the command again when that transaction has a receipt")
        return count

    def used_nonce(self, sender, nonce) -> None:
        """Records that a transaction with this nonce went to the node or has a receipt."""
        key = sender.lower()
        self._nonces[key] = max(self._nonces.get(key, 0), nonce + 1)

    def transaction(self, tx_hash):
        return self.request("eth_getTransactionByHash", [tx_hash])

    def receipt(self, tx_hash):
        return self.request("eth_getTransactionReceipt", [tx_hash])

    def logs(self, address, topics, from_block, to_block=None, chunk=50_000) -> list:
        """The logs of one emitter with a topic filter, in ascending order. A refused range is cut in two."""
        to_block = self.block_number() if to_block is None else to_block
        found, start = [], from_block
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            query = {"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}
            if address is not None:
                query["address"] = address
            try:
                found += self.request("eth_getLogs", [query])
            except RpcError:
                if chunk <= 1:
                    raise
                chunk = max(chunk // 2, 1)
                continue
            start = end + 1
        return sorted(found, key=lambda log: (hex_int(log["blockNumber"]), hex_int(log["logIndex"])))

    def is_dev_node(self) -> bool:
        """True if one of the URLs is a local development node (anvil): it answers `anvil_nodeInfo`."""
        if self._dev is None:
            self._dev = False
            for url in self.urls:
                try:
                    self._post(url, "anvil_nodeInfo", [])
                    self._dev = True
                except (_NoAnswer, RpcError):
                    pass
        return self._dev


# ----------------------------------------------------------------------------------------------
# Build artifacts and ABI
# ----------------------------------------------------------------------------------------------


def _abi_codec():
    try:
        import eth_abi
    except ImportError:
        raise OpsError('the operator commands need eth-account: pip install -e ".[ops]"') from None
    return eth_abi


def _type_of(parameter: dict) -> str:
    """The canonical ABI type of a parameter. A struct is a tuple of its components."""
    kind = parameter["type"]
    if kind.startswith("tuple"):
        return "(" + ",".join(_type_of(c) for c in parameter["components"]) + ")" + kind[5:]
    return kind


class Abi:
    """Encoder and decoder for the functions, events and errors of one ABI of the build output."""

    def __init__(self, entries, name=""):
        self.name = name
        self.entries = entries
        self._items = {}
        for entry in entries:
            if entry.get("type") in ("function", "event", "error"):
                if (entry["type"], entry["name"]) in self._items:
                    raise OpsError(f"{name}: the ABI has two members with the name {entry['name']}")
                self._items[(entry["type"], entry["name"])] = entry
        self._constructor = next((e for e in entries if e.get("type") == "constructor"), {"inputs": []})

    def _item(self, kind, name) -> dict:
        try:
            return self._items[(kind, name)]
        except KeyError:
            raise OpsError(
                f"the build output of {self.name} has no {kind} {name}: the seam of an earlier unit is absent"
            ) from None

    def signature(self, kind, name) -> str:
        return f"{name}({','.join(_type_of(i) for i in self._item(kind, name)['inputs'])})"

    def selector(self, name) -> bytes:
        return keccak256(self.signature("function", name).encode())[:4]

    def encode(self, name, *args) -> bytes:
        """Call data of a function."""
        types = [_type_of(i) for i in self._item("function", name)["inputs"]]
        return self.selector(name) + _abi_codec().encode(types, args)

    def decode(self, name, data: bytes):
        """The return values of a function: one value, or a tuple of values."""
        types = [_type_of(o) for o in self._item("function", name)["outputs"]]
        if not data and types:
            raise OpsError(f"{self.name}.{name} gave no data: there is no such contract at the address")
        values = [
            checksum(value) if kind == "address" else value
            for kind, value in zip(types, _abi_codec().decode(types, data))
        ]
        return values[0] if len(values) == 1 else tuple(values)

    def encode_constructor(self, *args) -> bytes:
        return _abi_codec().encode([_type_of(i) for i in self._constructor["inputs"]], args)

    def decode_constructor(self, data: bytes) -> dict:
        inputs = self._constructor["inputs"]
        values = _abi_codec().decode([_type_of(i) for i in inputs], data)
        return {i["name"]: checksum(v) if i["type"] == "address" else v for i, v in zip(inputs, values)}

    def topic(self, name) -> str:
        """Topic 0 of an event."""
        return "0x" + keccak256(self.signature("event", name).encode()).hex()

    def decode_event(self, name, log: dict) -> dict:
        """The parameters of an event log by name. Indexed parameters of value types only."""
        inputs = self._item("event", name)["inputs"]
        topics = iter(log["topics"][1:])
        values = {}
        for parameter in inputs:
            if parameter.get("indexed"):
                word = bytes.fromhex(next(topics)[2:])
                (values[parameter["name"]],) = _abi_codec().decode([_type_of(parameter)], word)
        plain = [p for p in inputs if not p.get("indexed")]
        decoded = _abi_codec().decode([_type_of(p) for p in plain], bytes.fromhex(log["data"][2:]))
        values.update({p["name"]: v for p, v in zip(plain, decoded)})
        return values

    def describe_error(self, data: bytes) -> str:
        """The name and the arguments of a custom error, or the hex data."""
        if len(data) >= 4:
            for (kind, name), entry in self._items.items():
                if kind == "error" and keccak256(self.signature("error", name).encode())[:4] == data[:4]:
                    try:
                        values = _abi_codec().decode([_type_of(i) for i in entry["inputs"]], data[4:])
                    except Exception:  # the data does not have the shape of this error
                        continue
                    return f"{name}({', '.join(str(v) for v in values)})"
            if data[:4] == bytes.fromhex("08c379a0"):
                try:
                    return "Error(" + _abi_codec().decode(["string"], data[4:])[0] + ")"
                except Exception:  # not a string
                    pass
        return "0x" + data.hex() if data else "no revert data"


@dataclass(frozen=True)
class Artifact:
    """One contract of the Foundry build output."""

    name: str
    path: Path
    abi: Abi
    bytecode: bytes
    metadata: dict
    raw_metadata: str


def load_artifact(name: str, out: Path = None) -> Artifact:
    """Loads `out/<File>.sol/<name>.json`. The build output must exist: run `forge build` first."""
    out = Path(out or ARTIFACTS)
    paths = sorted(out.glob(f"*.sol/{name}.json"))
    if len(paths) != 1:
        problem = "is absent" if not paths else "has more than one file"
        raise OpsError(f"the build artifact of {name} {problem} under {out}: run `forge build`")
    data = json.loads(paths[0].read_text())
    return Artifact(
        name=name,
        path=paths[0],
        abi=Abi(data["abi"], name),
        bytecode=bytes.fromhex(data.get("bytecode", {}).get("object", "0x")[2:]),
        metadata=data.get("metadata") or {},
        raw_metadata=data.get("rawMetadata") or "",
    )


# ----------------------------------------------------------------------------------------------
# Transactions
# ----------------------------------------------------------------------------------------------


def load_owner():
    """The account of `LIFELINE_OWNER_KEY`. The key is never printed and never stored."""
    key = os.environ.get(OWNER_KEY_VARIABLE, "").strip()
    if not key:
        raise OpsError(f"the environment variable {OWNER_KEY_VARIABLE} is not set")
    try:
        from eth_account import Account
    except ImportError:
        raise OpsError('the operator commands need eth-account: pip install -e ".[ops]"') from None
    try:
        return Account.from_key(key)
    except Exception:  # the message of the library can hold the key
        raise OpsError(f"{OWNER_KEY_VARIABLE} is not a private key") from None


def _fees(rpc: Rpc) -> dict:
    """The fee fields of a new transaction: EIP-1559 if the chain has a base fee."""
    base_fee = rpc.block().get("baseFeePerGas")
    if not base_fee:
        return {"gasPrice": hex_int(rpc.request("eth_gasPrice"))}
    try:
        tip = hex_int(rpc.request("eth_maxPriorityFeePerGas"))
    except (RpcError, TransportError):
        tip = 0
    # Lasna answers 0 and refuses a transaction with a tip below 1 wei.
    tip = max(tip, 1)
    return {"maxPriorityFeePerGas": tip, "maxFeePerGas": hex_int(base_fee) * 3 // 2 + tip}


def _revert_reason(error: RpcError, abi: Abi = None) -> str:
    data = error.data
    if isinstance(data, dict):
        data = data.get("data") or data.get("result")
    if isinstance(data, str) and data.startswith("0x"):
        try:
            raw = bytes.fromhex(data[2:])
        except ValueError:
            return error.message
        return (abi or Abi([])).describe_error(raw)
    return error.message


def send_transaction(rpc: Rpc, account, to=None, data=b"", value=0, gas=None, abi: Abi = None, what="",
                     nonce=None, before_send=None, min_fees=None) -> str:
    """Signs and sends one transaction. Returns its hash. It sends nothing if the call would revert or if
    the balance does not cover the value and the largest fee.

    `before_send(hash, nonce, fees)` runs after the signature and before the transaction goes to the node: a
    caller that writes the hash to a file there can find the transaction again after a lost answer.
    `nonce` gives the nonce of a transaction that replaces one with the same nonce; `min_fees` then gives the
    smallest fee fields that a node accepts for the replacement. Without `nonce` the transaction is a new
    one: see `Rpc.fresh_nonce`."""
    sender = account.address
    what = what or "transaction"
    if nonce is None:
        # First: the gas estimate below then runs on the state after each earlier transaction of the sender.
        nonce = rpc.fresh_nonce(sender)
    balance = rpc.balance(sender)
    if balance < value:
        raise OpsError(
            f"{what} on {rpc.name} was not sent: the balance of {sender} is {amount(balance)}; the transaction "
            f"needs {amount(value)} of value"
        )
    transaction = {"from": sender, "data": "0x" + data.hex(), "value": hex(value)}
    if to is not None:
        transaction["to"] = to
    if gas is None:
        rpc.head()
        try:
            estimate = hex_int(rpc.request("eth_estimateGas", [transaction]))
        except RpcError as error:
            raise OpsError(
                f"{what} on {rpc.name} was not sent: it would revert with {_revert_reason(error, abi)}"
            ) from None
        gas = estimate * 6 // 5 + 10_000
    fees = _fees(rpc)
    for field, least in (min_fees or {}).items():
        if field in fees:
            fees[field] = max(fees[field], int(least))
    price = fees.get("maxFeePerGas", fees.get("gasPrice"))
    if balance < value + gas * price:
        raise OpsError(
            f"{what} on {rpc.name} was not sent: the balance of {sender} is {amount(balance)}; the transaction "
            f"needs {amount(value)} of value and at most {amount(gas * price)} of fee"
        )
    signed = account.sign_transaction({
        # The chain ID of the configuration or of the manifest: each URL was checked against it.
        "chainId": rpc.chain_id(),
        "nonce": nonce,
        "to": to or "",
        "data": data,
        "value": value,
        "gas": gas,
        **fees,
        **({"type": 2} if "maxFeePerGas" in fees else {}),
    })
    raw = "0x" + bytes(signed.raw_transaction).hex()
    tx_hash = "0x" + bytes(signed.hash).hex()
    if before_send is not None:
        before_send(tx_hash, nonce, fees)
    try:
        rpc.request("eth_sendRawTransaction", [raw])
    except RpcError as error:
        # A second send after a lost answer: the node has the transaction already.
        if rpc.transaction(tx_hash) is None and rpc.receipt(tx_hash) is None:
            raise OpsError(f"{what} on {rpc.name} was refused by the node: {error.message}") from None
    rpc.used_nonce(sender, nonce)
    return tx_hash


def _receipt(rpc: Rpc, tx_hash: str):
    """The receipt of a transaction if it is in a block, or None."""
    receipt = rpc.receipt(tx_hash)
    if receipt is None or not receipt.get("blockNumber"):
        return None
    rpc.floor = max(rpc.floor, hex_int(receipt["blockNumber"]))
    return receipt


def _known(rpc: Rpc, hashes) -> bool:
    """True if the chain has one of the transactions, in a block or not."""
    return any(_receipt(rpc, h) is not None or rpc.transaction(h) is not None for h in hashes)


def _wait_mined(rpc: Rpc, hashes, timeout: float = None) -> dict:
    """Waits for the receipt of one of the transactions, reverted or not."""
    deadline = time.monotonic() + (RECEIPT_TIMEOUT if timeout is None else timeout)
    while True:
        for candidate in hashes:
            receipt = _receipt(rpc, candidate)
            if receipt is not None:
                return receipt
        if time.monotonic() >= deadline:
            raise OpsError(f"no receipt for transaction {', '.join(hashes)} on {rpc.name}: run the command "
                           f"again to continue")
        time.sleep(min(POLL_SECONDS, 1.0))


def wait_receipt(rpc: Rpc, tx_hash, timeout: float = None) -> dict:
    """Waits for the receipt of a transaction: one hash, or a list of hashes of which one at most can be in
    a block. A reverted transaction is an error."""
    receipt = _wait_mined(rpc, [tx_hash] if isinstance(tx_hash, str) else list(tx_hash), timeout)
    if hex_int(receipt["status"]) != 1:
        raise OpsError(f"transaction {receipt['transactionHash']} on {rpc.name} reverted in block "
                       f"{hex_int(receipt['blockNumber'])}")
    return receipt


def transact(rpc: Rpc, account, to, data=b"", value=0, abi: Abi = None, what="") -> dict:
    """Sends one transaction and waits for its receipt."""
    return wait_receipt(rpc, send_transaction(rpc, account, to, data, value, abi=abi, what=what))


def _replacement_fees(fees) -> dict:
    """The smallest fee fields of a transaction that replaces one with the same nonce: 12.5 % more."""
    return {field: int(value) * 9 // 8 + 1 for field, value in (fees or {}).items()}


def _settle(rpc: Rpc, account, pending: dict, save, send, what, intent=None) -> dict:
    """Sends one transaction with a journal and waits for its receipt.

    `pending` is a dict in a file that `save()` writes: `{"nonce": n, "txs": [hash, ...], "fees": {...},
    "intent": ...}`, or empty for a transaction that no run sent. The journal is in the file before the
    transaction goes to the node. `send(nonce, before_send, min_fees)` signs and sends the transaction.
    `intent` is a digest of the inputs of the transaction.

    An earlier run can have stopped at any point. The rule for a journal with hashes:
    - the inputs are not the inputs of the journal: the command stops;
    - the chain has one of the hashes, in a block or not: wait for it, send nothing;
    - the node knows none of them and the nonce is free: send again with the same nonce and higher fees, so
      one of them at most can get into a block;
    - the node knows none of them and the nonce was used: the result is not known. The command stops. It
      does not send the transaction with a new nonce.
    A recorded transaction that reverted changed nothing: the step starts again."""

    def journal(tx_hash, nonce, fees):
        pending["nonce"] = nonce
        pending["txs"] = list(dict.fromkeys([*pending.get("txs", []), tx_hash]))
        pending["fees"] = {field: str(value) for field, value in fees.items()}
        if intent is not None:
            pending["intent"] = intent
        save()

    hashes = pending.get("txs") or []
    if not hashes:
        send(None, journal, None)
    else:
        say(f"{what}: an earlier run stopped at this transaction (nonce {pending['nonce']}); it continues")
        if intent is not None and pending.get("intent") not in (None, intent):
            raise OpsError(
                f"{what} on {rpc.name}: an earlier run recorded this transaction with other inputs "
                f"({', '.join(hashes)}). Nothing was sent. Give the inputs of that run again. If that "
                f"transaction can never reach the chain, delete its journal entry from the manifest"
            )
        rpc.head()
        reverted = [h for h in hashes if (r := _receipt(rpc, h)) is not None and hex_int(r["status"]) != 1]
        if reverted:
            say(f"{what}: the transaction {reverted[0]} of the earlier run reverted; a new one follows")
            rpc.used_nonce(account.address, pending["nonce"])
            pending.clear()
            save()
            send(None, journal, None)
        elif not _known(rpc, hashes):
            # One block for the nonce and for the second look at the hashes.
            block, count = rpc._read_at("eth_getTransactionCount", [account.address])
            rpc.floor = max(rpc.floor, block)
            if hex_int(count) <= pending["nonce"]:
                send(pending["nonce"], journal, _replacement_fees(pending.get("fees")))
            elif not any(_receipt(rpc, h) is not None for h in hashes):
                raise OpsError(
                    f"{what} on {rpc.name}: the result of an earlier run is not known. The nonce "
                    f"{pending['nonce']} of {account.address} was used, and the node knows none of the "
                    f"recorded transactions ({', '.join(hashes)}). Nothing was sent. Run the command "
                    f"again; if the result is the same, find the transaction of that nonce in a block "
                    f"explorer and correct the manifest"
                )
    receipt = wait_receipt(rpc, pending["txs"])
    rpc.used_nonce(account.address, pending["nonce"])
    return receipt


def _intent(to: str, value: int, data: bytes) -> str:
    """The digest of the inputs of a call: the recipient, the value and the data."""
    return "0x" + keccak256(bytes.fromhex(to[2:]) + value.to_bytes(32, "big") + data).hex()


def _journal(record: dict, key: str) -> dict:
    """The journal of `record[key]`: empty if no transaction of that step waits for a receipt."""
    return (record.get("pending") or {}).get(key) or {}


def _close_journal(record: dict, key: str) -> None:
    pending = record.get("pending") or {}
    pending.pop(key, None)
    if not pending:
        record.pop("pending", None)


def transact_recorded(rpc: Rpc, account, to, data, record: dict, key: str, save, value=0, abi: Abi = None,
                      what="", bind=True) -> dict:
    """Sends one transaction, waits for its receipt, and writes its hash to `record[key]`. `save()` writes
    the file of the record. While the transaction has no receipt, its journal is in
    `record["pending"][key]`: see `_settle`. A `record[key]` with a hash is a transaction with a receipt:
    it is not sent again. With `bind`, the journal holds a digest of the recipient, the data and the value,
    and a later run with other inputs stops."""
    if record.get(key):
        return wait_receipt(rpc, record[key])
    pending = record.setdefault("pending", {}).setdefault(key, {})
    intent = _intent(to, value, data) if bind else None

    def send(nonce, before_send, min_fees):
        send_transaction(rpc, account, to, data, value, abi=abi, what=what, nonce=nonce, before_send=before_send,
                         min_fees=min_fees)

    receipt = _settle(rpc, account, pending, save, send, what or "transaction", intent)
    record[key] = receipt["transactionHash"]
    _close_journal(record, key)
    save()
    return receipt


def reconcile_recorded(rpc: Rpc, record: dict, key: str, save, what, unknown="error", sender=None):
    """Closes the journal of `record[key]` and sends nothing. A command calls it before it decides from the
    state of the chain that a step needs no transaction, so no journal of an earlier run stays open.

    The chain has a recorded transaction: wait for it and write its hash to `record[key]`; return the
    receipt. A reverted one changed nothing: return None. The node knows none of them (`unknown`):
    - "error": stop. The transaction can still get into a block.
    - "drop": delete the journal. Only for a step that is done and that gives the same state if its
      transaction runs a second time.
    - "keep": for a transaction that must not run a second time. If the nonce of the journal was used, the
      transaction can never get into a block: delete the journal. If the nonce is free, keep the journal: a
      later transaction of this step then uses the same nonce. `sender` is the account of the nonce."""
    journal = _journal(record, key)
    hashes = journal.get("txs") or []
    if not hashes:
        _close_journal(record, key)
        return None
    rpc.head()
    receipt = None
    if _known(rpc, hashes):
        receipt = _wait_mined(rpc, hashes)
        if hex_int(receipt["status"]) != 1:
            receipt = None
        else:
            record[key] = receipt["transactionHash"]
    elif unknown == "error":
        raise OpsError(f"{what} on {rpc.name}: an earlier run recorded the transaction {', '.join(hashes)}, "
                       f"and the node does not know it. Its result is not known")
    elif unknown == "keep":
        block, count = rpc._read_at("eth_getTransactionCount", [sender])
        rpc.floor = max(rpc.floor, block)
        if hex_int(count) <= journal["nonce"]:
            say(f"{what}: the node does not know the transaction {', '.join(hashes)} of an earlier run, and its "
                f"nonce {journal['nonce']} is free; the journal stays")
            return None
        say(f"{what}: the node does not know the transaction {', '.join(hashes)} of an earlier run, and its "
            f"nonce {journal['nonce']} was used, so it can never get into a block; its journal is closed")
    else:
        say(f"{what}: the step is done, and the node does not know the transaction {', '.join(hashes)} of an "
            f"earlier run; its journal is closed")
    _close_journal(record, key)
    save()
    return receipt


def create_address(sender: str, nonce: int) -> str:
    """The address of the contract that a sender creates with a nonce."""
    import rlp  # a dependency of eth-account

    return checksum("0x" + keccak256(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[12:].hex())


def deploy_recorded(rpc: Rpc, account, artifact: Artifact, args, value, entry: dict, save, what) -> dict:
    """Deploys one contract and fills its record `entry`: `address`, `deployBlock`, `deployTx`. `save()`
    writes the file of the record. Returns the receipt.

    While the deployment has no receipt, its journal is in `entry["pendingDeploy"]`: see `_settle`. So the
    next run continues a deployment that stopped at any point, and no contract is deployed two times."""
    if not artifact.bytecode:
        raise OpsError(f"the build artifact of {artifact.name} has no bytecode")
    data = artifact.bytecode + artifact.abi.encode_constructor(*args)
    entry.setdefault("address", None)
    pending = entry.setdefault("pendingDeploy", {})

    def send(nonce, before_send, min_fees):
        send_transaction(rpc, account, None, data, value, abi=artifact.abi, what=f"the deployment of the {what}",
                         nonce=nonce, before_send=before_send, min_fees=min_fees)

    intent = "0x" + keccak256(value.to_bytes(32, "big") + data).hex()
    receipt = _settle(rpc, account, pending, save, send, f"the deployment of the {what}", intent)
    address = checksum(receipt["contractAddress"]) if receipt.get("contractAddress") else None
    if not address or not rpc.code(address):
        raise OpsError(f"the deployment of the {what} on {rpc.name} left no code: {receipt['transactionHash']}")
    entry.update(address=address, deployBlock=hex_int(receipt["blockNumber"]), deployTx=receipt["transactionHash"])
    del entry["pendingDeploy"]
    save()
    return receipt


def deploy_contract(rpc: Rpc, account, artifact: Artifact, args=(), value=0) -> dict:
    """Deploys a contract from its build artifact. Returns the receipt: `contractAddress` is the new address."""
    return deploy_recorded(rpc, account, artifact, args, value, {}, lambda: None, artifact.name)


def find_logs(rpc: Rpc, address, topics, from_block, to_block=None) -> list:
    """The logs of one emitter with a topic filter, in ascending order: `Rpc.logs` as a function."""
    return rpc.logs(address, topics, from_block, to_block)


def wait_for_log(rpc: Rpc, address, topics, from_block, deadline: float):
    """Waits until `deadline` (a `time.monotonic()` value) for the first log that matches. None at the deadline."""
    while True:
        logs = rpc.logs(address, topics, from_block)
        if logs:
            return logs[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


def receipt_logs(receipt: dict, address, topic0) -> list:
    """The logs of a receipt that one emitter wrote with one topic 0."""
    return [
        log for log in receipt["logs"]
        if same_address(log["address"], address) and log["topics"] and log["topics"][0] == topic0
    ]


# ----------------------------------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """`Policy` of `src/LifelineTypes.sol`. Amounts are in wei of the chain of the target."""

    low: int
    high: int
    # The field names are the ones of the contract and of the two files.
    maxDebt: int
    dailyCap: int
    maxRescues: int
    window: int

    def as_tuple(self) -> tuple:
        return tuple(getattr(self, field) for field in POLICY_FIELDS)

    def to_json(self) -> dict:
        return {f: str(getattr(self, f)) if f in POLICY_AMOUNTS else getattr(self, f) for f in POLICY_FIELDS}

    @classmethod
    def from_json(cls, data, what="policy") -> "Policy":
        if not isinstance(data, dict) or any(field not in data for field in POLICY_FIELDS):
            raise OpsError(f"{what} needs the fields {', '.join(POLICY_FIELDS)}")
        return cls(*(_integer(data[field], f"{what}.{field}") for field in POLICY_FIELDS))

    def problems(self) -> list:
        """The rules of `validatePolicy` that the policy fails, and the sizes of the fields."""
        found = []
        if self.high <= 0:
            found.append("high must be above 0")
        if self.low > self.high:
            found.append("low must not be above high")
        if self.maxDebt <= 0:
            found.append("maxDebt must be above 0")
        if self.dailyCap <= 0:
            found.append("dailyCap must be above 0")
        if not 1 <= self.maxRescues <= MAX_RESCUES_LIMIT:
            found.append(f"maxRescues must be from 1 to {MAX_RESCUES_LIMIT}")
        if not 0 < self.window <= UINT32_MAX:
            found.append("window must be above 0 seconds and fit 32 bits")
        if any(not 0 <= getattr(self, field) < 2**256 for field in POLICY_AMOUNTS):
            found.append("an amount must be from 0 to 2^256 - 1")
        return found

    def validate(self, what="policy") -> None:
        found = self.problems()
        if found:
            raise OpsError(f"{what} is not valid: {'; '.join(found)}")

    def describe(self) -> str:
        return (
            f"low {amount(self.low)}, high {amount(self.high)}, maxDebt {amount(self.maxDebt)}, "
            f"dailyCap {amount(self.dailyCap)}, maxRescues {self.maxRescues}, window {self.window} s"
        )


def _integer(value, what) -> int:
    """A whole number of 0 or more: a JSON number or a decimal string (amounts in wei are strings)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise OpsError(f"{what} is not a whole number: {value!r}")
    text = str(value).strip()
    if not text.isdigit():
        raise OpsError(f"{what} is not a whole number of 0 or more: {value!r}")
    return int(text)


# ----------------------------------------------------------------------------------------------
# Configuration, manifest and chains
# ----------------------------------------------------------------------------------------------

FUND_FIELDS = ("principal", "feeFloat", "selfReserveTarget", "selfCareDailyCap", "principalFloor")


def parse_rpc(values) -> dict:
    """Turns repeated `<chain>=<url>` arguments into {chain: [url, ...]}, in the given order."""
    urls = {}
    for value in values or []:
        chain, separator, url = value.partition("=")
        if not separator or not chain or not url:
            raise OpsError(f"--rpc needs <chain>=<url>, got {value!r}")
        if chain not in CHAIN_NAMES:
            raise OpsError(f"--rpc names an unknown chain {chain!r}: use one of {', '.join(CHAIN_NAMES)}")
        urls.setdefault(chain, []).append(url)
    return urls


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except OSError as error:
        raise OpsError(f"{what} {path}: {error.strerror or error}") from None
    except ValueError as error:
        raise OpsError(f"{what} {path} is not JSON: {error}") from None
    if not isinstance(data, dict):
        raise OpsError(f"{what} {path} is not a JSON object")
    return data


def _funds(data, what) -> dict:
    if not isinstance(data, dict):
        raise OpsError(f"{what} is missing")
    funds = {field: _integer(data.get(field), f"{what}.{field}") for field in FUND_FIELDS}
    funds["value"] = funds["principal"] + funds["feeFloat"]
    return funds


def load_config(path=None) -> dict:
    """Reads and checks the shape of `deployments/config.json`. Amounts become integers of wei."""
    path = Path(path or DEFAULT_CONFIG)
    raw = _read_json(path, "configuration")
    chains = {}
    for name, entry in (raw.get("chains") or {}).items():
        if name not in CHAIN_NAMES:
            raise OpsError(f"{path}: chains.{name} is not one of {', '.join(CHAIN_NAMES)}")
        chain = {
            "chainId": _integer(entry.get("chainId"), f"chains.{name}.chainId"),
            "ledger": parse_address(entry.get("ledger"), f"chains.{name}.ledger"),
            "rpc": [str(url) for url in entry.get("rpc") or []],
        }
        if name != LASNA:
            chain["sweepBase"] = _integer(entry.get("sweepBase"), f"chains.{name}.sweepBase")
            chain["measuredSweepGas"] = _integer(entry.get("measuredSweepGas"), f"chains.{name}.measuredSweepGas")
        # Used only if the ledger has no getter for the value. The note says how the value was read.
        for key in ("extraGas", "gasPriceCoeffPer1000"):
            if key in entry:
                chain[key] = _integer(entry[key], f"chains.{name}.{key}")
        if "ledgerNote" in entry:
            chain["ledgerNote"] = str(entry["ledgerNote"])
        chains[name] = chain
    if LASNA not in chains:
        raise OpsError(f"{path}: chains.{LASNA} is missing")

    vaults = {}
    for name, entry in (raw.get("vaults") or {}).items():
        if name == LASNA or name not in chains:
            raise OpsError(f"{path}: vaults.{name} needs a destination chain of chains")
        vaults[name] = _funds(entry, f"vaults.{name}")

    sender = raw.get("callbackSender", "hub")
    if sender != "hub":
        sender = parse_address(sender, "callbackSender")
    cron = raw.get("cronTopic", DEFAULT_CRON_TOPIC)
    if cron not in CRON_TOPICS:
        raise OpsError(f"{path}: cronTopic must be one of {', '.join(CRON_TOPICS)}")

    fleet = []
    for index, entry in enumerate(raw.get("fleet") or []):
        what = f"fleet[{index}]"
        if not isinstance(entry, dict) or not entry.get("label"):
            raise OpsError(f"{path}: {what}.label is missing")
        what = f"fleet {entry['label']}"
        if entry.get("chain") != LASNA and entry.get("chain") not in vaults:
            raise OpsError(f"{path}: {what}: chain must be {LASNA} or a chain with a vault")
        fleet.append({
            "label": str(entry["label"]),
            "chain": entry["chain"],
            "address": parse_address(entry.get("address"), f"{what}: address"),
            "policy": Policy.from_json(entry.get("policy"), f"{what}: policy"),
        })
    return {
        "path": path,
        "chains": chains,
        "hub": _funds(raw.get("hub"), "hub"),
        "vaults": vaults,
        "callbackSender": sender,
        "cronTopic": cron,
        "fleet": fleet,
    }


class Manifest:
    """`deployments/manifest.json`: the record of the deployment. A command changes only its own entries."""

    def __init__(self, path, data):
        self.path = Path(path)
        self.data = data
        data.setdefault("version", MANIFEST_VERSION)
        data.setdefault("cronTopic", DEFAULT_CRON_TOPIC)
        data.setdefault("chains", {})
        contracts = data.get("contracts") or {}
        data["contracts"] = {"hub": contracts.get("hub"), "vaults": contracts.get("vaults") or {}, **contracts}
        data.setdefault("enrollments", [])
        data.setdefault("loopCheck", None)
        data.setdefault("fixtures", {})

    @classmethod
    def load(cls, path=None, create=False) -> "Manifest":
        path = Path(path or DEFAULT_MANIFEST)
        if not path.exists():
            if not create:
                raise OpsError(f"the manifest {path} does not exist: run deploy first, or give --manifest")
            return cls(path, {})
        data = _read_json(path, "manifest")
        if data.get("version", MANIFEST_VERSION) != MANIFEST_VERSION:
            raise OpsError(f"the manifest {path} has version {data.get('version')}; this command writes "
                           f"version {MANIFEST_VERSION}")
        return cls(path, data)

    def save(self) -> None:
        """Writes the file in one step: a failed write leaves the old file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(temporary, self.path)

    @property
    def hub(self):
        return self.data["contracts"].get("hub")

    @property
    def vaults(self) -> dict:
        return self.data["contracts"]["vaults"]

    def deployed(self, entry) -> bool:
        return bool(entry and entry.get("address"))

    def active_enrollment(self, chain: str, target: str):
        """The newest enrollment record of a target, if `remove` did not close it. None if there is none."""
        for record in reversed(self.data["enrollments"]):
            if record.get("chain") == chain and same_address(record.get("target"), target):
                return None if record.get("removed") else record
        return None

    @staticmethod
    def enrollment_complete(record) -> bool:
        """True if the enrollment reached its last step: `hub.enroll` is in a block."""
        return bool(record) and (record.get("txs") or {}).get("enrollBlock") is not None


@dataclass
class Chain:
    name: str
    chain_id: int
    ledger: str
    rpc: Rpc

    def check_id(self) -> None:
        """Each URL that answers must answer for this chain, and one URL at least must answer."""
        answers = self.rpc.chain_ids()
        for host, actual in answers:
            if actual is not None and actual != self.chain_id:
                raise OpsError(f"{self.name}: the RPC {host} answers for chain ID {actual}, not {self.chain_id}")
        if all(actual is None for _, actual in answers):
            raise TransportError(f"{self.name}: no RPC answers eth_chainId")


def open_chain(name: str, entry: dict, overrides: dict) -> Chain:
    """One chain of the configuration or of the manifest. `--rpc` URLs replace its RPC list."""
    urls = overrides.get(name) or entry.get("rpc") or []
    chain_id = int(entry["chainId"])
    return Chain(name, chain_id, parse_address(entry["ledger"], f"chains.{name}.ledger"), Rpc(name, urls, chain_id))


def guard_manifest(manifest_path: Path, chains) -> None:
    """A dry run on local development nodes must not write the manifest of the live deployment."""
    if Path(manifest_path).resolve() == DEFAULT_MANIFEST.resolve():
        local = [chain.name for chain in chains if chain.rpc.is_dev_node()]
        if local:
            raise OpsError(
                f"the RPC of {', '.join(local)} is a local development node: a dry run writes to another "
                f"manifest path, given with --manifest"
            )


def say(text="") -> None:
    print(text, flush=True)


# ----------------------------------------------------------------------------------------------
# Fee checks
# ----------------------------------------------------------------------------------------------


def sweep_fee(extra_gas: int, sweep_gas: int, gas_price: int, coeff_per_1000: int) -> int:
    """The fee of one full sweep at a callback proxy: (`_extraGas` + gas of the call) x price x coefficient."""
    return (extra_gas + sweep_gas) * gas_price * coeff_per_1000 // 1000


def trigger_fee(gas_price: int, coeff_per_1000: int, gas: int = TRIGGER_GAS) -> int:
    """The fee of one full trigger of `TRIGGER_GAS` on Lasna."""
    return gas * gas_price * coeff_per_1000 // 1000


def ledger_parameter(chain: Chain, ledger_abi: Abi, getter: str, entry: dict, key: str):
    """Reads `_extraGas` or `_gasPriceCoeffPer1000` from the deployed ledger. If the ledger has no getter,
    the value comes from the configuration, which must then say how the value was read.

    Returns (value, source) or (None, reason)."""
    try:
        data = chain.rpc.call(chain.ledger, ledger_abi.encode(getter))
    except RpcError:
        data = b""
    if len(data) == 32:
        return int.from_bytes(data, "big"), f"{getter}() of the ledger"
    if key in entry and entry.get("ledgerNote"):
        return entry[key], f"chains.{chain.name}.{key} of the configuration ({entry['ledgerNote']})"
    return None, (
        f"the ledger of {chain.name} has no getter {getter}(): give chains.{chain.name}.{key} and "
        f"chains.{chain.name}.ledgerNote (how the value was read) in the configuration"
    )


# ----------------------------------------------------------------------------------------------
# Session: the state that the commands after `deploy` share
# ----------------------------------------------------------------------------------------------


class Session:
    """The manifest, the chains, the owner and the build artifacts of one command."""

    def __init__(self, args, owner=True):
        self.manifest = Manifest.load(args.manifest)
        self.overrides = parse_rpc(args.rpc)
        self.owner = load_owner() if owner else None
        self.hub_artifact = load_artifact("LifelineHub")
        self.vault_artifact = load_artifact("LifelineVault")
        self.hub_abi = self.hub_artifact.abi
        self.vault_abi = self.vault_artifact.abi
        self.ledger_abi = load_artifact("ICallbackProxy").abi
        self.system_abi = load_artifact("ISystemContract").abi
        self._chains = {}
        self._checked = set()

    def chain(self, name: str) -> Chain:
        if name not in self._chains:
            entry = self.manifest.data["chains"].get(name)
            if not entry:
                raise OpsError(f"the manifest {self.manifest.path} has no chain {name}")
            chain = open_chain(name, entry, self.overrides)
            chain.check_id()
            guard_manifest(self.manifest.path, [chain])
            self._chains[name] = chain
        return self._chains[name]

    def destination(self, name: str) -> Chain:
        if name == LASNA or name not in self.manifest.vaults:
            raise OpsError(f"--chain needs a destination chain with a vault in the manifest, got {name!r}")
        return self.chain(name)

    @property
    def lasna(self) -> Chain:
        return self.chain(LASNA)

    def _contract(self, chain: Chain, entry, what: str) -> str:
        if not self.manifest.deployed(entry):
            raise OpsError(f"the manifest {self.manifest.path} has no {what}: run deploy first")
        address = entry["address"]
        if (chain.name, address) not in self._checked:
            if not chain.rpc.code(address):
                raise OpsError(
                    f"no contract code at the address {address} of the {what} on {chain.name}: the manifest "
                    f"is from another network or from a dry run (give the same --rpc as that run)"
                )
            self._checked.add((chain.name, address))
        return address

    @property
    def hub(self) -> str:
        return self._contract(self.lasna, self.manifest.hub, "hub")

    def vault(self, name: str) -> str:
        return self._contract(self.chain(name), self.manifest.vaults.get(name), f"vault of {name}")

    def read_hub(self, function, *args):
        return self.hub_abi.decode(function, self.lasna.rpc.call(self.hub, self.hub_abi.encode(function, *args)))

    def read_vault(self, name, function, *args):
        data = self.chain(name).rpc.call(self.vault(name), self.vault_abi.encode(function, *args))
        return self.vault_abi.decode(function, data)

    def read_ledger(self, chain: Chain, function, *args):
        """A view of the ledger of a chain. Returns (block, value)."""
        block, data = chain.rpc.call_at(chain.ledger, self.ledger_abi.encode(function, *args))
        return block, self.ledger_abi.decode(function, data)

    def require_owner(self, name=None) -> None:
        """The key must be the owner of the hub and, with a chain name, of the vault of that chain."""
        owner = self.read_hub("owner")
        if not same_address(owner, self.owner.address):
            raise OpsError(f"{OWNER_KEY_VARIABLE} is the key of {self.owner.address}; the owner of the hub is {owner}")
        if name is not None:
            owner = self.read_vault(name, "owner")
            if not same_address(owner, self.owner.address):
                raise OpsError(
                    f"{OWNER_KEY_VARIABLE} is the key of {self.owner.address}; the owner of the vault of {name} "
                    f"is {owner}"
                )


def block_of(receipt: dict) -> int:
    return hex_int(receipt["blockNumber"])


# ----------------------------------------------------------------------------------------------
# deploy
# ----------------------------------------------------------------------------------------------


def _deploy_checks(config, chains, manifest: Manifest, ledger_abi: Abi, owner_address: str) -> None:
    """The checks before deployment. Prints each result. Raises `OpsError` with each failure; the caller
    has sent no transaction."""
    failures = []

    def check(ok: bool, text: str) -> None:
        say(f"  {'ok  ' if ok else 'FAIL'} {text}")
        if not ok:
            failures.append(text)

    def stop_on_failures() -> None:
        if failures:
            raise OpsError("deploy sent no transaction. Failed checks:\n  - " + "\n  - ".join(failures))

    say("checks before deployment:")
    for name, chain in chains.items():
        # Each URL of the chain: a retry on another URL must stay on the same network.
        answers = chain.rpc.chain_ids()
        right = True
        for host, actual in answers:
            if actual is not None:
                right &= actual == chain.chain_id
                check(actual == chain.chain_id,
                      f"{name}: eth_chainId of {host} is {actual}, the configuration has {chain.chain_id}")
        if all(actual is None for _, actual in answers):
            right = False
            check(False, f"{name}: no RPC answers eth_chainId")
        if right:
            check(bool(chain.rpc.code(chain.ledger)), f"{name}: the ledger {chain.ledger} has code")
    # The later checks read the ledgers, so they need the right chains.
    stop_on_failures()

    need = {name: 0 for name in chains}

    lasna = chains[LASNA]
    hub = config["hub"]
    if _deployment_started(lasna.rpc, manifest.hub):
        say("  ok   hub: in the manifest; the float checks apply to a new deployment only")
    else:
        price = lasna.rpc.gas_price()
        coeff, source = ledger_parameter(lasna, ledger_abi, "_gasPriceCoeffPer1000", config["chains"][LASNA],
                                         "gasPriceCoeffPer1000")
        if coeff is None:
            check(False, source)
        else:
            fee = trigger_fee(price, coeff)
            say(f"       hub: one full trigger of {TRIGGER_GAS} gas at {price} wei x {coeff}/1000 ({source}) "
                f"costs {amount(fee)}")
            check(hub["feeFloat"] >= fee, f"hub: feeFloat {amount(hub['feeFloat'])} covers one full trigger")
            check(hub["selfReserveTarget"] >= fee,
                  f"hub: selfReserveTarget {amount(hub['selfReserveTarget'])} covers one full trigger")
        need[LASNA] += hub["value"]

    for name, funds in config["vaults"].items():
        chain, entry = chains[name], config["chains"][name]
        check(entry["measuredSweepGas"] > 0, f"{name}: measuredSweepGas is above 0")
        check(entry["sweepBase"] > 0, f"{name}: sweepBase is above 0")
        deployed = manifest.vaults.get(name)
        if _deployment_started(chain.rpc, deployed):
            say(f"  ok   vault of {name}: in the manifest; the float check applies to a new deployment only")
            continue
        need[name] += funds["value"]
        if entry["measuredSweepGas"] <= 0:
            continue
        price = chain.rpc.gas_price()
        extra, extra_source = ledger_parameter(chain, ledger_abi, "_extraGas", entry, "extraGas")
        coeff, coeff_source = ledger_parameter(chain, ledger_abi, "_gasPriceCoeffPer1000", entry,
                                               "gasPriceCoeffPer1000")
        if extra is None or coeff is None:
            check(False, extra_source if extra is None else coeff_source)
            continue
        fee = sweep_fee(extra, entry["measuredSweepGas"], price, coeff)
        say(f"       vault of {name}: one full sweep of {SWEEP_MAX} targets costs ({extra} + "
            f"{entry['measuredSweepGas']}) gas x {price} wei x {coeff}/1000 = {amount(fee)} "
            f"({extra_source}; {coeff_source})")
        check(funds["selfReserveTarget"] >= FLOAT_SWEEPS * fee,
              f"vault of {name}: selfReserveTarget {amount(funds['selfReserveTarget'])} covers "
              f"{FLOAT_SWEEPS} full sweeps, {amount(FLOAT_SWEEPS * fee)}")

    for entry in config["fleet"]:
        found = entry["policy"].problems()
        check(not found, f"fleet {entry['label']}: the policy is valid" + (f" ({'; '.join(found)})" if found else ""))

    for name, value in need.items():
        if value:
            balance = chains[name].rpc.balance(owner_address)
            check(balance > value, f"{name}: the owner balance {amount(balance)} is above the value of the "
                                   f"deployments, {amount(value)}")

    stop_on_failures()


def _deployment_started(rpc: Rpc, entry) -> bool:
    """True if the manifest holds the contract, or a deployment transaction that the chain has and that did
    not revert. A journal with a transaction that the node does not know, or that reverted, is not a
    deployment: the checks apply to the next one."""
    if not entry:
        return False
    if entry.get("address"):
        return True
    for tx_hash in (entry.get("pendingDeploy") or {}).get("txs") or []:
        receipt = _receipt(rpc, tx_hash)
        # In a block and not reverted, or known and not in a block yet.
        if (receipt is not None and hex_int(receipt["status"]) == 1) or (
                receipt is None and rpc.transaction(tx_hash) is not None):
            return True
    return False


def _new_entry(chain_id: int, **first) -> dict:
    """The manifest entry of a contract that is not deployed yet."""
    return {**first, "chainId": chain_id, "address": None, "deployBlock": None, "deployTx": None}


def _deploy_step(manifest: Manifest, entry: dict, chain: Chain, owner, artifact: Artifact, args, value, what):
    """Deploys one contract, or takes it from the manifest. `entry` is its manifest entry: `deploy_recorded`
    fills it. Returns the entry."""
    if manifest.deployed(entry):
        if not chain.rpc.code(entry["address"]):
            raise OpsError(
                f"the manifest has the {what} at {entry['address']}, and {chain.name} has no code there: the "
                f"manifest is from another network or from a dry run"
            )
        say(f"{what}: in the manifest at {entry['address']} (block {entry['deployBlock']}); no deployment")
        return entry
    deploy_recorded(chain.rpc, owner, artifact, args, value, entry, manifest.save, what)
    say(f"{what}: deployed at {entry['address']} on {chain.name}, block {entry['deployBlock']}, tx "
        f"{entry['deployTx']}, value {amount(value)}")
    return entry


def cmd_deploy(args) -> int:
    config = load_config(args.config)
    overrides = parse_rpc(args.rpc)
    owner = load_owner()
    hub_artifact, vault_artifact = load_artifact("LifelineHub"), load_artifact("LifelineVault")
    ledger_abi = load_artifact("ICallbackProxy").abi
    hub_abi, vault_abi = hub_artifact.abi, vault_artifact.abi
    manifest = Manifest.load(args.manifest, create=True)

    chains = {name: open_chain(name, entry, overrides) for name, entry in config["chains"].items()}
    guard_manifest(manifest.path, chains.values())
    for name, recorded in manifest.data["chains"].items():
        entry = config["chains"].get(name)
        if not entry or entry["chainId"] != recorded.get("chainId") or not same_address(entry["ledger"],
                                                                                         recorded.get("ledger")):
            raise OpsError(f"the manifest {manifest.path} has another chain ID or ledger for {name} than the "
                           f"configuration: use a new manifest path for a new deployment")

    say(f"owner {owner.address}; configuration {config['path']}; manifest {manifest.path}")
    _deploy_checks(config, chains, manifest, ledger_abi, owner.address)

    manifest.data["cronTopic"] = config["cronTopic"]
    for name, entry in config["chains"].items():
        manifest.data["chains"][name] = {"chainId": entry["chainId"], "ledger": entry["ledger"], "rpc": entry["rpc"]}
    manifest.save()

    lasna = chains[LASNA]
    funds = config["hub"]

    if manifest.hub is None:
        manifest.data["contracts"]["hub"] = _new_entry(lasna.chain_id, chain=LASNA)
    hub = _deploy_step(
        manifest, manifest.hub, lasna, owner, hub_artifact,
        (lasna.ledger, funds["feeFloat"], funds["selfReserveTarget"], funds["selfCareDailyCap"],
         funds["principalFloor"]),
        funds["value"], "hub",
    )
    hub_address = hub["address"]

    def read_hub(function, *arguments):
        return hub_abi.decode(function, lasna.rpc.call(hub_address, hub_abi.encode(function, *arguments)))

    hub_owner = read_hub("owner")
    if not same_address(hub_owner, owner.address):
        raise OpsError(f"{OWNER_KEY_VARIABLE} is the key of {owner.address}; the owner of the hub is {hub_owner}")

    sender = hub_address if config["callbackSender"] == "hub" else config["callbackSender"]
    say(f"callbackSender of each vault: {sender}"
        + (" (the hub address)" if config["callbackSender"] == "hub" else " (the address of the configuration)"))

    for name, funds in config["vaults"].items():
        chain = chains[name]
        vault = _deploy_step(
            manifest, manifest.vaults.setdefault(name, _new_entry(chain.chain_id)), chain, owner, vault_artifact,
            (chain.ledger, sender, funds["feeFloat"], funds["selfReserveTarget"], funds["selfCareDailyCap"],
             funds["principalFloor"]),
            funds["value"], f"vault of {name}",
        )
        stored = vault_abi.decode("callbackSender",
                                  chain.rpc.call(vault["address"], vault_abi.encode("callbackSender")))
        if not same_address(stored, sender):
            raise OpsError(f"the vault of {name} at {vault['address']} has callbackSender {stored}, not {sender}: "
                           f"the value is immutable, so this vault cannot serve this hub")

    def setup_step(record, key, is_done, data, what):
        """One owner call of the hub with its hash in `record[key]`. `is_done()` reads the state of the hub:
        True if it needs no call. The journal of an earlier run is closed first in each case. Returns
        (done, receipt or None).

        A journal with other values than the values of this run is a call that can change the state after
        this step. A journal with no digest of its values (written by an earlier version) is not known to
        have the values of this run. So each of the two is settled before the state is read: the chain has
        the transaction (wait for it), or its nonce was used (it can never run), or the command stops."""
        journal = _journal(record, key)
        if journal.get("txs") and journal.get("intent") != _intent(hub_address, 0, data):
            values = "other values" if journal.get("intent") else "values that the journal does not hold"
            reconcile_recorded(lasna.rpc, record, key, manifest.save, what, unknown="keep", sender=owner.address)
            if _journal(record, key):
                raise OpsError(
                    f"{what} on {LASNA}: an earlier run recorded this call with {values} "
                    f"({', '.join(journal['txs'])}). The node does not know that transaction and its nonce "
                    f"{journal['nonce']} is free, so it can still get into a block and change the hub after "
                    f"this run. Nothing was sent. Run deploy again with the configuration of that run; then "
                    f"run it with this configuration. If that transaction can never reach a node, use its "
                    f"nonce with another transaction of the owner"
                )
        if is_done():
            # The same call a second time gives the same state: a journal that the node does not know goes.
            return True, reconcile_recorded(lasna.rpc, record, key, manifest.save, what, unknown="drop")
        if not _journal(record, key):
            record[key] = None  # the hash of an earlier call with other values
        return False, transact_recorded(lasna.rpc, owner, hub_address, data, record, key, manifest.save,
                                        abi=hub_abi, what=what)

    for name in config["vaults"]:
        chain, vault = chains[name], manifest.vaults[name]
        sweep_base = config["chains"][name]["sweepBase"]

        def registered(chain=chain, vault=vault, sweep_base=sweep_base) -> bool:
            proxy, address, base, _, _ = read_hub("vaultInfo", chain.chain_id)
            return same_address(proxy, chain.ledger) and same_address(address, vault["address"]) and base == sweep_base

        done, receipt = setup_step(
            vault, "setVaultTx", registered,
            hub_abi.encode("setVault", chain.chain_id, chain.ledger, vault["address"], sweep_base),
            f"hub.setVault for {name}")
        if done:
            say(f"hub.setVault for {name}: registered already; no transaction")
        else:
            say(f"hub.setVault for {name}: tx {receipt['transactionHash']} block {block_of(receipt)} "
                f"(proxy {chain.ledger}, vault {vault['address']}, sweepBase {sweep_base})")

    wanted = int.from_bytes(keccak256(f"{config['cronTopic']}(uint256)".encode()), "big")
    cron = hub.setdefault("cron", {"topic": config["cronTopic"], "setCronTopicTx": None, "resumeCronTx": None})
    cron["topic"] = config["cronTopic"]
    done, receipt = setup_step(cron, "setCronTopicTx", lambda: read_hub("cronTopic") == wanted,
                               hub_abi.encode("setCronTopic", wanted), "hub.setCronTopic")
    if not done:
        say(f"hub.setCronTopic({config['cronTopic']}): tx {receipt['transactionHash']} block {block_of(receipt)}")
    done, receipt = setup_step(cron, "resumeCronTx", lambda: read_hub("cronSubscribed"),
                               hub_abi.encode("resumeCron"), "hub.resumeCron")
    if done:
        say(f"cron: subscribed already to {config['cronTopic']}; no transaction")
    else:
        say(f"hub.resumeCron ({config['cronTopic']}): tx {receipt['transactionHash']} block {block_of(receipt)}")
    manifest.save()

    say("state after deploy:")
    say(f"  hub {hub_address} on {LASNA} (chain ID {lasna.chain_id}), deployBlock {hub['deployBlock']}")
    for name in config["vaults"]:
        _, registered, base, up, pending = read_hub("vaultInfo", chains[name].chain_id)
        say(f"  vault {registered} on {name} (chain ID {chains[name].chain_id}), deployBlock "
            f"{manifest.vaults[name]['deployBlock']}: up {str(up).lower()}, pending {pending}, sweepBase {base}")
    say(f"  cronSubscribed {str(read_hub('cronSubscribed')).lower()}")
    return 0


# ----------------------------------------------------------------------------------------------
# verify
# ----------------------------------------------------------------------------------------------


def _http_json(method: str, url: str, body=None, timeout=60.0):
    """One HTTP request with a JSON body. Returns (status, parsed body or None)."""
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"content-type": "application/json", "user-agent": "lifeline-ops"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except OSError as error:
        raise OpsError(f"no answer from {urllib.parse.urlsplit(url).netloc}: {type(error).__name__}") from None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def _sourcify_match(server: str, chain_id: int, address: str):
    status, body = _http_json("GET", f"{server}/v2/contract/{chain_id}/{address}")
    return body.get("match") if status == 200 and isinstance(body, dict) else None


def sourcify_verify(server: str, chain_id: int, address: str, artifact: Artifact, deploy_tx: str, timeout=300.0):
    """Verifies one contract on Sourcify with the metadata and the sources of the build. Returns
    (verified, text)."""
    match = _sourcify_match(server, chain_id, address)
    if match:
        return True, f"verified already ({match})"

    metadata = json.loads(artifact.raw_metadata)
    sources = {}
    for path, entry in metadata["sources"].items():
        try:
            text = (ROOT / path).read_text()
        except OSError:
            return False, f"the source file {path} of the build is absent"
        if "0x" + keccak256(text.encode()).hex() != entry["keccak256"]:
            return False, f"the source file {path} differs from the build output: run `forge build`"
        sources[path] = text
    status, body = _http_json(
        "POST", f"{server}/v2/verify/metadata/{chain_id}/{address}",
        {"sources": sources, "metadata": metadata, "creationTransactionHash": deploy_tx},
    )
    if status == 409:
        return True, "verified already"
    if status != 202 or not isinstance(body, dict) or "verificationId" not in body:
        reason = body.get("message") if isinstance(body, dict) else None
        return False, f"Sourcify refused the request: HTTP {status} {reason or ''}".rstrip()

    deadline = time.monotonic() + timeout
    while True:
        status, job = _http_json("GET", f"{server}/v2/verify/{body['verificationId']}")
        if status == 200 and isinstance(job, dict) and job.get("isJobCompleted"):
            match = (job.get("contract") or {}).get("match")
            if match:
                return True, f"verified ({match})"
            error = job.get("error") or {}
            return False, f"not verified: {error.get('customCode', '')} {error.get('message', '')}".rstrip()
        if time.monotonic() >= deadline:
            return False, f"Sourcify gave no result in {int(timeout)} s (verification {body['verificationId']})"
        time.sleep(3)


def cmd_verify(args) -> int:
    session = Session(args, owner=False)
    manifest = session.manifest
    contracts = [("hub", LASNA, manifest.hub, session.hub_artifact)]
    contracts += [(f"vault of {name}", name, entry, session.vault_artifact) for name, entry in manifest.vaults.items()]

    for _, name, _, _ in contracts:
        if session.chain(name).rpc.is_dev_node():
            raise OpsError(f"verify needs the live chains: the RPC of {name} is a local development node")

    server = args.sourcify.rstrip("/")
    status, listed = _http_json("GET", f"{server}/chains")
    supported = {int(c["chainId"]) for c in listed if c.get("supported")} if status == 200 and listed else None
    if supported is None:
        raise OpsError(f"Sourcify gave no chain list: HTTP {status}")

    failed = 0
    for what, name, entry, artifact in contracts:
        chain = session.chain(name)
        address = session._contract(chain, entry, what)
        transaction = chain.rpc.transaction(entry["deployTx"])
        code = bytes.fromhex(transaction["input"][2:]) if transaction else b""
        if not code.startswith(artifact.bytecode):
            say(f"{what} {address} on {name} (chain ID {chain.chain_id}): FAIL the build output differs from "
                f"the code of the deployment transaction {entry['deployTx']}")
            failed += 1
            continue
        arguments = artifact.abi.decode_constructor(code[len(artifact.bytecode):])
        say(f"{what} {address} on {name} (chain ID {chain.chain_id}), constructor arguments: "
            + ", ".join(f"{key}={value}" for key, value in arguments.items()))
        if chain.chain_id not in supported:
            say(f"  BLOCKED Sourcify does not support chain ID {chain.chain_id}")
            failed += 1
            continue
        verified, text = sourcify_verify(server, chain.chain_id, address, artifact, entry["deployTx"])
        say(f"  {'ok  ' if verified else 'FAIL'} Sourcify: {text}")
        failed += not verified
    return 1 if failed else 0


# ----------------------------------------------------------------------------------------------
# enroll and fleet
# ----------------------------------------------------------------------------------------------


def enroll_target(session: Session, chain_name, target, label, policy: Policy, guest=False, consent=None) -> dict:
    """The one onboarding path. A destination target: (1) `vault.setPolicy`, (2) read the stored policy
    back, (3) `hub.enroll` with the same policy value. A Lasna target: `hub.enroll` only.

    Each check comes before the first transaction. The record of the enrollment is in the manifest before
    the first transaction, and each hash is in it before its transaction goes to the node. So the next run
    with the same policy continues an enrollment that stopped. Returns the record."""
    if guest and not consent:
        raise OpsError("a guest needs the written consent of the target owner: give --consent <reference>")
    policy.validate(f"the policy of {label}")
    target = parse_address(target, "--target")
    destination = chain_name != LASNA
    chain = session.destination(chain_name) if destination else session.lasna
    lasna, hub, hub_abi, vault_abi = session.lasna, session.hub, session.hub_abi, session.vault_abi
    manifest = session.manifest
    session.require_owner(chain_name if destination else None)

    if same_address(target, hub):
        raise OpsError("the target is the hub")
    if not chain.rpc.code(target):
        raise OpsError(f"the target {target} has no code on {chain_name}")
    vault = None
    if destination:
        vault = session.vault(chain_name)
        if same_address(target, vault):
            raise OpsError("the target is the vault of its chain")
        _, registered, _, _, _ = session.read_hub("vaultInfo", chain.chain_id)
        if not same_address(registered, vault):
            raise OpsError(f"the hub has the vault {registered} for {chain_name}, the manifest has {vault}: "
                           f"run deploy again")

    kind, state, _ = session.read_hub("targetInfo", chain.chain_id, target)
    enrolled = kind != 0 and TARGET_STATES[state] != "Disarmed"
    record = manifest.active_enrollment(chain_name, target)
    if manifest.enrollment_complete(record):
        if enrolled:
            raise OpsError(f"the hub has the target {target} of {chain_name} in state {TARGET_STATES[state]}: "
                           f"remove it before a new enrollment")
        raise OpsError(f"the manifest has an enrollment of {target} on {chain_name}, and the hub does not have "
                       f"the target: run remove to close that record before a new enrollment")
    if record is not None and record["policy"] != policy.to_json():
        raise OpsError(f"the manifest has an enrollment of {target} on {chain_name} that did not finish, with "
                       f"another policy: give that policy again, or run remove")
    # The hub can have the target only if a `hub.enroll` of this record is in the manifest: its hash, or
    # the journal of a transaction with no receipt yet.
    sent_enroll = record is not None and (record["txs"].get("enroll")
                                          or (record["txs"].get("pending") or {}).get("enroll", {}).get("txs"))
    if enrolled and not sent_enroll:
        raise OpsError(f"the hub has the target {target} of {chain_name} in state {TARGET_STATES[state]}, and "
                       f"the manifest has no record of that enrollment: remove it before a new enrollment")

    say(f"enroll {label}: target {target} on {chain_name} (chain ID {chain.chain_id})"
        + (f", guest with consent {consent}" if guest else ""))
    say(f"  policy: {policy.describe()}")
    if record is None:
        record = {
            "label": label,
            "chain": chain_name,
            "chainId": chain.chain_id,
            "target": target,
            "policy": policy.to_json(),
            "guest": bool(guest),
            "consent": consent if guest else None,
            "txs": {"setPolicy": None, "readBackBlock": None, "enroll": None, "enrollBlock": None},
            "removed": None,
        }
        manifest.data["enrollments"].append(record)
    else:
        say("  an earlier run stopped in this enrollment; it continues")
        record.update(label=label, guest=bool(guest), consent=consent if guest else None)
    manifest.save()
    txs = record["txs"]

    step = 1
    if destination:
        receipt = transact_recorded(chain.rpc, session.owner, vault,
                                    vault_abi.encode("setPolicy", target, policy.as_tuple()), txs, "setPolicy",
                                    manifest.save, abi=vault_abi, what="vault.setPolicy")
        results = receipt_logs(receipt, vault, vault_abi.topic("Result"))
        outcome = vault_abi.decode_event("Result", results[0]) if results else None
        say(f"  1 setPolicy  on {chain_name}: tx {txs['setPolicy']} block {block_of(receipt)}"
            + (f"; the vault reconciled at once: {OUTCOMES[outcome['outcome']]}, paid {amount(outcome['paid'])}"
               if outcome else ""))
        txs["readBackBlock"] = _read_back(chain, vault, vault_abi, target, policy)
        manifest.save()
        say(f"  2 read-back  on {chain_name}: block {txs['readBackBlock']}; vault.policyOf equals the input in "
            f"each field")
        step = 3

    receipt = transact_recorded(lasna.rpc, session.owner, hub,
                                hub_abi.encode("enroll", chain.chain_id, target, policy.as_tuple()), txs, "enroll",
                                manifest.save, abi=hub_abi, what="hub.enroll")
    txs["enrollBlock"] = block_of(receipt)
    manifest.save()
    say(f"  {step} enroll     on {LASNA}: tx {txs['enroll']} block {txs['enrollBlock']}; "
        f"hub.enroll({chain.chain_id}, {target}, policy)")
    return record


def _read_back(chain: Chain, vault, vault_abi: Abi, target, policy: Policy) -> int:
    """Reads `vault.policyOf(target)` over RPC and compares each field with the input. Returns the block of
    the read. A difference is an error: the caller then sends nothing to Lasna."""
    block, data = chain.rpc.call_at(vault, vault_abi.encode("policyOf", target))
    stored = Policy(*vault_abi.decode("policyOf", data))
    differences = [
        f"{field}: stored {getattr(stored, field)}, input {getattr(policy, field)}"
        for field in POLICY_FIELDS if getattr(stored, field) != getattr(policy, field)
    ]
    if differences:
        raise OpsError(
            f"the policy that vault.policyOf gave at block {block} of {chain.name} differs from the input "
            f"({'; '.join(differences)}). Nothing was sent to Lasna: hub.enroll did not run"
        )
    return block


def cmd_enroll(args) -> int:
    # The consent rule comes first: a refused guest sends no request at all.
    if args.guest and not args.consent:
        raise OpsError("a guest needs the written consent of the target owner: give --consent <reference>")
    if args.consent and not args.guest:
        raise OpsError("--consent is for a guest: give --guest")
    if args.chain not in CHAIN_NAMES:
        raise OpsError(f"--chain must be one of {', '.join(CHAIN_NAMES)}")
    if not 0 <= args.max_rescues <= UINT32_MAX or not 0 <= args.window <= UINT32_MAX:
        raise OpsError("--max-rescues and --window are whole numbers of 32 bits")
    policy = Policy(to_wei(args.low), to_wei(args.high), to_wei(args.max_debt), to_wei(args.daily_cap),
                    args.max_rescues, args.window)
    policy.validate("the policy")
    target = parse_address(args.target, "--target")
    enroll_target(Session(args), args.chain, target, args.label, policy, args.guest, args.consent)
    return 0


def cmd_fleet(args) -> int:
    config = load_config(args.config)
    if not config["fleet"]:
        raise OpsError(f"{config['path']}: fleet is empty")
    for entry in config["fleet"]:
        entry["policy"].validate(f"the policy of fleet {entry['label']}")
    session = Session(args)
    blocks = []
    for entry in config["fleet"]:
        chain = session.chain(entry["chain"])
        kind, state, _ = session.read_hub("targetInfo", chain.chain_id, entry["address"])
        record = session.manifest.active_enrollment(entry["chain"], entry["address"])
        if kind != 0 and TARGET_STATES[state] != "Disarmed" and session.manifest.enrollment_complete(record):
            say(f"enroll {entry['label']}: the hub has the target in state {TARGET_STATES[state]} already; "
                f"no transaction")
        else:
            record = enroll_target(session, entry["chain"], entry["address"], entry["label"], entry["policy"])
        blocks.append((entry["label"], record["txs"]["enrollBlock"]))
    say("enrollment blocks on lasna (the start of the 48-hour window):")
    for label, block in blocks:
        say(f"  {label}: block {block}")
    return 0


# ----------------------------------------------------------------------------------------------
# remove
# ----------------------------------------------------------------------------------------------


def target_subscription(session: Session, chain: Chain, target: str):
    """The newest `Subscribe` or `Unsubscribe` log of the debt tuple of a target: the hub as subscriber, the
    chain of the target, its ledger, `BlacklistContract(target)`. Returns (active, log or None)."""
    lasna, system_abi, ledger_abi = session.lasna, session.system_abi, session.ledger_abi
    subscribe, unsubscribe = system_abi.topic("Subscribe"), system_abi.topic("Unsubscribe")
    logs = lasna.rpc.logs(
        lasna.ledger,
        [[subscribe, unsubscribe], topic_of_address(session.hub), topic_of_int(chain.chain_id),
         topic_of_address(chain.ledger)],
        session.manifest.hub["deployBlock"],
    )
    logs = [log for log in logs if _is_debt_tuple(log, ledger_abi, target)]
    if not logs:
        return False, None
    return logs[-1]["topics"][0] == subscribe, logs[-1]


def _is_debt_tuple(log: dict, ledger_abi: Abi, target: str) -> bool:
    data = bytes.fromhex(log["data"][2:])
    if len(data) != 128:
        return False
    words = [int.from_bytes(data[i:i + 32], "big") for i in range(0, 128, 32)]
    return words == [hex_int(ledger_abi.topic("BlacklistContract")), int(target, 16), REACTIVE_IGNORE, REACTIVE_IGNORE]


def cmd_remove(args) -> int:
    session = Session(args)
    target = parse_address(args.target, "--target")
    destination = args.chain != LASNA
    chain = session.destination(args.chain) if destination else session.lasna
    lasna, hub, hub_abi, vault_abi = session.lasna, session.hub, session.hub_abi, session.vault_abi
    manifest = session.manifest
    session.require_owner(args.chain if destination else None)

    vault = session.vault(args.chain) if destination else None
    record = manifest.active_enrollment(args.chain, target)
    if record is not None and record["txs"].get("pending"):
        # A transaction of the enrollment with no receipt can still get into a block after the removal.
        try:
            reconcile_recorded(chain.rpc, record["txs"], "setPolicy", manifest.save, "vault.setPolicy")
            receipt = reconcile_recorded(lasna.rpc, record["txs"], "enroll", manifest.save, "hub.enroll")
        except OpsError as error:
            raise OpsError(f"{error}. Run enroll again with the same policy to finish the enrollment; then "
                           f"run remove") from None
        if receipt is not None:
            record["txs"]["enrollBlock"] = block_of(receipt)
            manifest.save()
    # The state decides each step: read it only when each earlier transaction of the owner has a receipt. A
    # transaction that waits (an enrollment that no manifest records, for example) can still change it.
    lasna.rpc.require_idle(session.owner.address)
    chain.rpc.require_idle(session.owner.address)
    kind, state, _ = session.read_hub("targetInfo", chain.chain_id, target)
    has_policy = destination and session.read_vault(args.chain, "hasPolicy", target)
    if kind == 0 and not has_policy and record is None:
        raise OpsError(f"the hub has no target {target} on {args.chain}")
    if record is None:
        say(f"note: the manifest has no open enrollment record of {target} on {args.chain}; the hashes are "
            f"printed only")
    # The hashes go to `removing` of the record first. `removed` is written when the removal is complete.
    removing = (record if record is not None else {}).setdefault("removing", {"hubDisarm": None, "vaultDisarm": None})
    save = manifest.save if record is not None else (lambda: None)

    say(f"remove: target {target} on {args.chain} (chain ID {chain.chain_id})")
    if kind == 0:
        say("  1 hub.disarm: the hub never had the target; no transaction")
    else:
        if TARGET_STATES[state] == "Disarmed":
            # A second `hub.disarm` reverts and changes nothing: the journal of an earlier run can be closed.
            receipt = reconcile_recorded(lasna.rpc, removing, "hubDisarm", save, "hub.disarm", unknown="drop")
            say("  1 hub.disarm: the hub has the target in state Disarmed already; no transaction"
                + (f" (tx {removing['hubDisarm']} of an earlier run)" if receipt else ""))
        else:
            receipt = transact_recorded(lasna.rpc, session.owner, hub,
                                        hub_abi.encode("disarm", chain.chain_id, target), removing, "hubDisarm",
                                        save, abi=hub_abi, what="hub.disarm")
            say(f"  1 hub.disarm   on {LASNA}: tx {removing['hubDisarm']} block {block_of(receipt)}")
        tuple_topics = [topic_of_address(hub), topic_of_int(chain.chain_id), topic_of_address(chain.ledger)]
        in_receipt = receipt is not None and any(
            log["topics"][1:] == tuple_topics and _is_debt_tuple(log, session.ledger_abi, target)
            for log in receipt_logs(receipt, lasna.ledger, session.system_abi.topic("Unsubscribe"))
        )

        active, last = target_subscription(session, chain, target)
        if active:
            raise OpsError(f"the debt tuple of {target} is still subscribed: the newest log is Subscribe in "
                           f"{last['transactionHash']}")
        if in_receipt:
            say("    the receipt holds Unsubscribe of the debt tuple of the target")
        elif last is not None:
            say(f"    the debt tuple was unsubscribed already, in tx {last['transactionHash']} block "
                f"{hex_int(last['blockNumber'])}")
        else:
            say("    the hub has no Subscribe log for the debt tuple of the target")
        _, state, _ = session.read_hub("targetInfo", chain.chain_id, target)
        if TARGET_STATES[state] != "Disarmed":
            raise OpsError(f"hub.targetInfo reports state {TARGET_STATES[state]}, not Disarmed")
        say("    hub.targetInfo reports state Disarmed")

    if destination:
        if has_policy:
            receipt = transact_recorded(chain.rpc, session.owner, vault, vault_abi.encode("disarm", target),
                                        removing, "vaultDisarm", save, abi=vault_abi, what="vault.disarm")
            say(f"  2 vault.disarm on {args.chain}: tx {removing['vaultDisarm']} block {block_of(receipt)}")
            if session.read_vault(args.chain, "hasPolicy", target):
                raise OpsError("the vault still has a policy for the target")
        else:
            # A second `vault.disarm` gives the same state: the journal of an earlier run can be closed.
            receipt = reconcile_recorded(chain.rpc, removing, "vaultDisarm", save, "vault.disarm", unknown="drop")
            say("  2 vault.disarm: the vault has no policy for the target; no transaction"
                + (f" (tx {removing['vaultDisarm']} of an earlier run)" if receipt else ""))
    if record is not None:
        record["removed"] = record.pop("removing")
        manifest.save()
    say("removed: no subscription and no policy for the target")
    return 0


# ----------------------------------------------------------------------------------------------
# recover
# ----------------------------------------------------------------------------------------------


MAX_VAULT_UP = 64  # stops a recovery that does not end


def cmd_recover(args) -> int:
    session = Session(args)
    chain = session.destination(args.chain)
    lasna, hub, hub_abi = session.lasna, session.hub, session.hub_abi
    manifest = session.manifest
    vault = session.vault(args.chain)
    session.require_owner(args.chain)

    # The record of this recovery: `recoveries` of the vault entry. A record with a journal is a recovery
    # that an earlier run did not finish. A new record goes to the manifest with its first transaction.
    recoveries = manifest.vaults[args.chain].setdefault("recoveries", [])

    def new_record() -> dict:
        return {"deposit": None, "depositBlock": None, "vaultUp": []}

    # The newest record continues if it has a journal, or a deposit and no `vaultUp` yet.
    last = recoveries[-1] if recoveries else None
    unfinished = last is not None and (last.get("pending") or (last.get("deposit") and not last.get("vaultUp")))
    record = last if unfinished else new_record()

    def save():
        if not any(entry is record for entry in recoveries):
            recoveries.append(record)
        manifest.save()

    say(f"recover: vault {vault} on {args.chain} (chain ID {chain.chain_id})")
    # The deposit of an earlier run comes first. It is the one transaction that this command waits for: the
    # chain has it (wait for its receipt), or its nonce was used (it can never run), or its nonce is free (the
    # journal stays, and a later deposit uses that nonce). A deposit must not run a second time: reserves
    # cannot be withdrawn.
    earlier = None
    if _journal(record, "deposit").get("txs"):
        say(f"proxy.depositTo: an earlier run stopped at this transaction (nonce "
            f"{_journal(record, 'deposit')['nonce']}); it continues")
        try:
            earlier = reconcile_recorded(chain.rpc, record, "deposit", save, "proxy.depositTo", unknown="keep",
                                         sender=session.owner.address)
        except OpsError as error:
            raise OpsError(f"{error}. hub.vaultUp was not sent") from None
        if earlier is not None:
            record["depositBlock"] = block_of(earlier)
            save()
    # The debt decides the deposit: read it only when each transaction of the owner has a receipt. Another
    # transaction that waits can use the nonce of the journal, or can change the state after this run.
    chain.rpc.require_idle(session.owner.address)
    block, debt = session.read_ledger(chain, "debt", vault)
    say(f"  debt of the vault at the proxy, block {block}: {amount(debt)}")
    if debt > 0:
        if record.get("deposit"):
            # The deposit of the earlier run is in a block, and the vault has a new debt: a new recovery.
            record = new_record()
        reserve = session.read_vault(args.chain, "selfReserveTarget")
        value = debt + reserve
        try:
            # The debt changes between two runs, so the journal is not bound to the value. A journal that
            # stays has a free nonce: this deposit uses that nonce, so one of the two at most gets into a block.
            receipt = transact_recorded(chain.rpc, session.owner, chain.ledger,
                                        session.ledger_abi.encode("depositTo", vault), record, "deposit", save,
                                        value=value, abi=session.ledger_abi, what="proxy.depositTo", bind=False)
        except OpsError as error:
            raise OpsError(f"{error}. hub.vaultUp was not sent") from None
        record["depositBlock"] = block_of(receipt)
        save()
        # The value of a deposit that an earlier run sent can differ from the value of this run.
        sent = hex_int(chain.rpc.transaction(receipt["transactionHash"])["value"])
        say(f"  1 proxy.depositTo on {args.chain}: tx {receipt['transactionHash']} block {block_of(receipt)}, "
            f"value {amount(sent)} = debt {sent - reserve} + float {reserve}")
        block, debt = session.read_ledger(chain, "debt", vault)
        say(f"  debt of the vault at the proxy, block {block}: {amount(debt)}")
    else:
        say("  1 proxy.depositTo: no debt, so no deposit"
            + (f" (tx {record['deposit']} of an earlier run cleared it)" if earlier else ""))

    sends = 0
    while True:
        _, _, _, up, pending = session.read_hub("vaultInfo", chain.chain_id)
        if up and pending == 0:
            break
        # The hub cannot read the destination ledger: `vaultUp` runs only while the vault debt is 0.
        _, debt = session.read_ledger(chain, "debt", vault)
        if debt > 0:
            raise OpsError(f"the debt of the vault is {amount(debt)}, above 0: hub.vaultUp was not sent")
        if sends == MAX_VAULT_UP:
            raise OpsError(f"the hub still reports pending targets after {sends} vaultUp")
        receipt = transact(lasna.rpc, session.owner, hub, hub_abi.encode("vaultUp", chain.chain_id), abi=hub_abi,
                           what="hub.vaultUp")
        sends += 1
        event = hub_abi.decode_event("VaultUp", receipt_logs(receipt, hub, hub_abi.topic("VaultUp"))[0])
        record["vaultUp"].append({"tx": receipt["transactionHash"], "block": block_of(receipt),
                                  "requestId": str(event["requestId"]), "sent": event["sent"],
                                  "remaining": event["remaining"]})
        save()
        say(f"  2 hub.vaultUp on {LASNA}: tx {receipt['transactionHash']} block {block_of(receipt)}, "
            f"requestId {event['requestId']}, sent {event['sent']}, remaining {event['remaining']}")
    if not sends:
        say("  2 hub.vaultUp: the hub has the vault up with no pending target; no transaction")
    # A record with no transaction says nothing.
    if any(entry is record for entry in recoveries) and not (
            record.get("deposit") or record["vaultUp"] or record.get("pending")):
        recoveries[:] = [entry for entry in recoveries if entry is not record]
        manifest.save()
    if not recoveries:
        del manifest.vaults[args.chain]["recoveries"]
    say(f"recovered: hub.vaultInfo reports up true, pending 0, after {sends} vaultUp")
    return 0


# ----------------------------------------------------------------------------------------------
# status
# ----------------------------------------------------------------------------------------------


def cmd_status(args) -> int:
    """Reads the funds and the state of the hub, of each vault and of each enrolled target. It needs no key
    and sends nothing. Exit status 1 if one line needs an action of the owner."""
    session = Session(args, owner=False)
    manifest, lasna, hub, ledger_abi = session.manifest, session.lasna, session.hub, session.ledger_abi
    warnings = []

    def warn(text: str) -> None:
        warnings.append(text)
        say(f"  WARN {text}")

    def ledger(chain: Chain, account: str) -> tuple:
        return session.read_ledger(chain, "debt", account)[1], session.read_ledger(chain, "reserves", account)[1]

    balance, floor = lasna.rpc.balance(hub), session.read_hub("principalFloor")
    debt, reserves = ledger(lasna, hub)
    coeff, _ = ledger_parameter(lasna, ledger_abi, "_gasPriceCoeffPer1000", manifest.data["chains"][LASNA],
                                "gasPriceCoeffPer1000")
    fee = trigger_fee(lasna.rpc.gas_price(), coeff or 1000)
    spendable = max(balance - floor, 0) + reserves
    say(f"hub {hub} on {LASNA}, block {lasna.rpc.head()}")
    say(f"  principal {amount(balance)}, floor {amount(floor)}")
    say(f"  fee float (reserves) {amount(reserves)}, target {amount(session.read_hub('selfReserveTarget'))}, "
        f"daily cap of self-care {amount(session.read_hub('selfCareDailyCap'))}, debt {amount(debt)}")
    say(f"  one full trigger of {TRIGGER_GAS} gas costs {amount(fee)}: the funds above the floor and the float "
        f"cover {spendable // fee if fee else 0} of them")
    say(f"  cron subscribed {str(session.read_hub('cronSubscribed')).lower()}, last tick "
        f"{session.read_hub('lastTick')}, paused {str(session.read_hub('paused')).lower()}")
    if debt:
        warn(f"the hub is in debt and gets no reaction: send depositTo(hub) at the system contract with "
             f"{amount(debt)} or more")
    elif reserves < fee:
        warn("the fee float of the hub does not cover one full trigger")
    # The hub emits HubLow and parks its cron tick only below the floor. Self-care stops at the floor, so the
    # float can go to 0 with no event.
    if balance < floor + fee:
        warn("the principal of the hub above its floor does not cover one full trigger: self-care cannot refill "
             "the float. Send lREACT to the hub")
    if not session.read_hub("cronSubscribed"):
        warn("the hub has no cron subscription: no gauge tick runs. hub.resumeCron starts it")
    if session.read_hub("paused"):
        warn("the hub is paused")

    for name in manifest.vaults:
        chain, vault = session.chain(name), session.vault(name)
        _, _, _, up, pending = session.read_hub("vaultInfo", chain.chain_id)
        debt, reserves = ledger(chain, vault)
        balance, floor = chain.rpc.balance(vault), session.read_vault(name, "principalFloor")
        target = session.read_vault(name, "selfReserveTarget")
        say(f"vault {vault} on {name}, block {chain.rpc.head()}")
        say(f"  principal {amount(balance)}, floor {amount(floor)}")
        say(f"  fee float (reserves) {amount(reserves)}, target {amount(target)}, debt {amount(debt)}")
        vault_paused = session.read_vault(name, "paused")
        say(f"  hub: up {str(up).lower()}, pending {pending}; paused {str(vault_paused).lower()}")
        if vault_paused:
            warn(f"the vault of {name} is paused: it pays no target. vault.unpause starts it")
        if debt or not up or pending:
            warn(f"the vault of {name} needs a recovery (debt {debt}, up {str(up).lower()}, pending {pending}): "
                 f"run recover --chain {name}")
        elif reserves * 2 < target:
            warn(f"the fee float of the vault of {name} is below half of its target")
        if balance <= floor:
            warn(f"the principal of the vault of {name} is at its floor: it pays no target. Send funds to the vault")

    for record in manifest.data["enrollments"]:
        if record.get("removed"):
            continue
        chain = session.chain(record["chain"])
        _, state, _ = session.read_hub("targetInfo", chain.chain_id, record["target"])
        debt, reserves = ledger(chain, record["target"])
        policy = Policy.from_json(record["policy"])
        say(f"target {record['label']} {record['target']} on {record['chain']}: hub state {TARGET_STATES[state]}, "
            f"debt {amount(debt)}, reserves {amount(reserves)} (low {policy.low}, high {policy.high})")
        if TARGET_STATES[state] != "Active":
            warn(f"the hub has {record['label']} in state {TARGET_STATES[state]}")
        if record["chain"] != LASNA and session.read_vault(record["chain"], "quarantined", record["target"]):
            warn(f"the vault has {record['label']} in quarantine: vault.release, or remove the target")
        if debt:
            warn(f"{record['label']} is in debt")
    say(f"status: {len(warnings)} warning(s)" if warnings else "status: ok")
    return 1 if warnings else 0


# ----------------------------------------------------------------------------------------------
# loop-check
# ----------------------------------------------------------------------------------------------

# The steps that the loop check waits for, by their key in `loopCheck.steps` of the manifest.
LOOP_STEPS = {
    "debt": "the proxy event BlacklistContract(canary)",
    "relayed": "the hub event Relayed on Lasna",
    "result": "the vault event Result with outcome Rescued",
    "debtZero": "debt(canary) == 0 at the proxy",
}


def cmd_loop_check(args) -> int:
    session = Session(args)
    manifest = session.manifest
    chain = session.destination(args.chain)
    lasna, hub, hub_abi, vault_abi = session.lasna, session.hub, session.hub_abi, session.vault_abi
    vault = session.vault(args.chain)
    callback_artifact, reactive_artifact = load_artifact("CanaryCallback"), load_artifact("CanaryReactive")
    session.require_owner(args.chain)

    record = manifest.data.get("loopCheck")
    if args.new or not record or record.get("chain") != args.chain or record.get("status") == "passed":
        record = {"chain": args.chain, "chainId": chain.chain_id, "status": "setup", "missing": None, "steps": {}}
        # Checks before the first transaction.
        _, registered, _, up, _ = session.read_hub("vaultInfo", chain.chain_id)
        if not same_address(registered, vault) or not up:
            raise OpsError(f"the hub does not have the vault of {args.chain} in state up: run recover")
        _, vault_debt = session.read_ledger(chain, "debt", vault)
        _, hub_debt = session.read_ledger(lasna, "debt", hub)
        if vault_debt or hub_debt:
            raise OpsError(f"a debt stops the loop: vault {amount(vault_debt)}, hub {amount(hub_debt)}")
    else:
        say(f"loop-check: the manifest has a check of {args.chain} in status {record['status']}; it continues")

    def save():
        manifest.data["loopCheck"] = record
        manifest.save()

    say(f"loop-check on {args.chain} (chain ID {chain.chain_id}), timeout {args.timeout} s")
    # The record is in the manifest before the first transaction, and each hash before its transaction goes
    # to the node: the next run continues a check that stopped at any point.
    save()
    callback = record.setdefault("canaryCallback", {"chain": args.chain, "chainId": chain.chain_id})
    if not callback.get("address"):
        deploy_recorded(chain.rpc, session.owner, callback_artifact, (), 0, callback, save, "CanaryCallback")
    canary = callback["address"]
    say(f"  CanaryCallback {canary} on {args.chain}: tx {callback['deployTx']} block {callback['deployBlock']}")

    lasna_canary = record.setdefault("canaryReactive", {"chain": LASNA, "chainId": lasna.chain_id})
    if not lasna_canary.get("address"):
        deploy_recorded(lasna.rpc, session.owner, reactive_artifact,
                        (lasna.ledger, chain.chain_id, canary, CANARY_CALLBACK_GAS), 0, lasna_canary, save,
                        "CanaryReactive")
    reactive = lasna_canary["address"]
    say(f"  CanaryReactive {reactive} on {LASNA}: tx {lasna_canary['deployTx']} block "
        f"{lasna_canary['deployBlock']}")

    if "reserve" not in record:
        if args.canary_reserve is not None:
            reserve = to_wei(args.canary_reserve)
        else:
            coeff, _ = ledger_parameter(lasna, session.ledger_abi, "_gasPriceCoeffPer1000",
                                        manifest.data["chains"][LASNA], "gasPriceCoeffPer1000")
            reserve = trigger_fee(lasna.rpc.gas_price(), coeff or 1000, CANARY_RESERVE_GAS)
        record["reserve"] = str(reserve)
        save()
    if not record.get("fundTx"):
        transact_recorded(lasna.rpc, session.owner, lasna.ledger, session.ledger_abi.encode("depositTo", reactive),
                          record, "fundTx", save, value=int(record["reserve"]), abi=session.ledger_abi,
                          what="depositTo for the Lasna canary")
    say(f"  reserves of the Lasna canary: {amount(int(record['reserve']))}, tx {record['fundTx']}")

    if "policy" not in record:
        # `low` 0: `setPolicy` does not fund the canary, so its first callback puts it into debt.
        reserve_target = session.read_vault(args.chain, "selfReserveTarget")
        high = to_wei(args.high) if args.high is not None else max(reserve_target // 4, 1)
        max_debt = to_wei(args.max_debt) if args.max_debt is not None else max(reserve_target, 1)
        record["policy"] = Policy(0, high, max_debt, max_debt + high, 2, 3600).to_json()
        save()
    if not manifest.enrollment_complete(manifest.active_enrollment(args.chain, canary)):
        enroll_target(session, args.chain, canary, f"loop-check canary ({args.chain})",
                      Policy.from_json(record["policy"]))

    if record.get("pokeBlock") is None:
        receipt = transact_recorded(lasna.rpc, session.owner, reactive, reactive_artifact.abi.encode("poke"),
                                    record, "pokeTx", save, abi=reactive_artifact.abi, what="poke")
        record["pokeBlock"] = block_of(receipt)
        record["status"] = "waiting"
        save()
    say(f"  poke() on {LASNA}: tx {record['pokeTx']} block {record['pokeBlock']}")

    deadline = time.monotonic() + args.timeout
    steps = record["steps"]
    canary_topic = topic_of_address(canary)

    def missing(key: str) -> int:
        record["status"], record["missing"] = "timeout", key
        save()
        text = LOOP_STEPS[key]
        ticks = lasna.rpc.logs(reactive, [reactive_artifact.abi.topic("Tick")], record["pokeBlock"])
        note = "The Lasna canary reacted (Tick)." if ticks else "The Lasna canary did not react (no Tick)."
        print(f"error: loop-check timed out after {args.timeout} s: the step that did not occur is {text}. "
              f"{note} Run loop-check again to continue the wait.", file=sys.stderr)
        return 1

    if "debt" not in steps:
        log = wait_for_log(chain.rpc, chain.ledger, [session.ledger_abi.topic("BlacklistContract"), canary_topic],
                           record["canaryCallback"]["deployBlock"], deadline)
        if log is None:
            return missing("debt")
        steps["debt"] = {"tx": log["transactionHash"], "block": hex_int(log["blockNumber"])}
        save()
    say(f"  1 proxy debt event BlacklistContract(canary) on {args.chain}: tx {steps['debt']['tx']} "
        f"block {steps['debt']['block']}")

    if "relayed" not in steps:
        log = wait_for_log(lasna.rpc, hub, [hub_abi.topic("Relayed"), None, topic_of_int(chain.chain_id), canary_topic],
                           record["pokeBlock"], deadline)
        if log is None:
            return missing("relayed")
        steps["relayed"] = {"tx": log["transactionHash"], "block": hex_int(log["blockNumber"]),
                            "requestId": str(hex_int(log["topics"][1]))}
        save()
    request_id = int(steps["relayed"]["requestId"])
    say(f"  2 Lasna reaction with Relayed: tx {steps['relayed']['tx']} block {steps['relayed']['block']}, "
        f"requestId {request_id}")

    if "result" not in steps:
        log = wait_for_log(chain.rpc, vault, [vault_abi.topic("Result"), topic_of_int(request_id), canary_topic],
                           steps["debt"]["block"], deadline)
        if log is None:
            return missing("result")
        event = vault_abi.decode_event("Result", log)
        steps["result"] = {"tx": log["transactionHash"], "block": hex_int(log["blockNumber"]),
                           "outcome": OUTCOMES[event["outcome"]], "paid": str(event["paid"])}
        save()
    say(f"  3 vault Result on {args.chain}: tx {steps['result']['tx']} block {steps['result']['block']}, "
        f"requestId {request_id}, outcome {steps['result']['outcome']}, paid {amount(int(steps['result']['paid']))}")
    if steps["result"]["outcome"] != "Rescued":
        record["status"], record["missing"] = "failed", "result"
        save()
        raise OpsError(f"the vault Result has outcome {steps['result']['outcome']}, not Rescued")

    while True:
        chain.rpc.floor = max(chain.rpc.floor, steps["result"]["block"])
        block, debt = session.read_ledger(chain, "debt", canary)
        if debt == 0:
            break
        if time.monotonic() >= deadline:
            return missing("debtZero")
        time.sleep(POLL_SECONDS)
    steps["debtZero"] = {"block": block}
    record["status"], record["missing"] = "passed", None
    save()
    say(f"  4 debt(canary) at the proxy of {args.chain}: 0 at block {block}")
    say("loop-check passed")
    return 0


# ----------------------------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", default=str(DEFAULT_MANIFEST), metavar="<file>",
                        help="manifest file (default deployments/manifest.json)")
    common.add_argument("--rpc", action="append", default=[], metavar="<chain>=<url>",
                        help="RPC URL of one chain; repeatable; replaces the RPC list of that chain")
    with_config = argparse.ArgumentParser(add_help=False)
    with_config.add_argument("--config", default=str(DEFAULT_CONFIG), metavar="<file>",
                             help="configuration file (default deployments/config.json)")

    parser = argparse.ArgumentParser(prog="python -m lifeline.ops", description="Lifeline operator commands.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    deploy = commands.add_parser("deploy", parents=[common, with_config], help="deploy the hub and the vaults")
    deploy.set_defaults(run=cmd_deploy)

    verify = commands.add_parser("verify", parents=[common], help="verify the hub and the vaults on Sourcify")
    verify.add_argument("--sourcify", default=SOURCIFY_SERVER, metavar="<url>", help="Sourcify server")
    verify.set_defaults(run=cmd_verify)

    enroll = commands.add_parser("enroll", parents=[common], help="onboard one target")
    enroll.add_argument("--chain", required=True, metavar="<name>")
    enroll.add_argument("--target", required=True, metavar="<address>")
    enroll.add_argument("--label", required=True, metavar="<text>")
    for flag in ("--low", "--high", "--max-debt", "--daily-cap"):
        enroll.add_argument(flag, required=True, metavar="<amount>", help="decimal, in the native unit of the chain")
    enroll.add_argument("--max-rescues", required=True, type=int, metavar="<n>")
    enroll.add_argument("--window", required=True, type=int, metavar="<seconds>")
    enroll.add_argument("--guest", action="store_true", help="the target belongs to another participant")
    enroll.add_argument("--consent", metavar="<reference>", help="reference to the written consent of the target owner")
    enroll.set_defaults(run=cmd_enroll)

    fleet = commands.add_parser("fleet", parents=[common, with_config], help="onboard each fleet entry")
    fleet.set_defaults(run=cmd_fleet)

    remove = commands.add_parser("remove", parents=[common], help="remove one target: hub first, then vault")
    remove.add_argument("--chain", required=True, metavar="<name>")
    remove.add_argument("--target", required=True, metavar="<address>")
    remove.set_defaults(run=cmd_remove)

    recover = commands.add_parser("recover", parents=[common], help="restore a vault that is in debt")
    recover.add_argument("--chain", required=True, metavar="<name>")
    recover.set_defaults(run=cmd_recover)

    status = commands.add_parser("status", parents=[common],
                                 help="read the funds and the state of the hub, the vaults and the targets")
    status.set_defaults(run=cmd_status)

    loop = commands.add_parser("loop-check", parents=[common], help="show the live destination loop with a canary")
    loop.add_argument("--chain", required=True, metavar="<name>")
    loop.add_argument("--timeout", type=float, default=600.0, metavar="<seconds>")
    loop.add_argument("--new", action="store_true", help="start a new check; do not continue the one in the manifest")
    loop.add_argument("--canary-reserve", metavar="<amount>", help="reserves of the Lasna canary, in lREACT")
    loop.add_argument("--high", metavar="<amount>", help="high mark of the callback canary")
    loop.add_argument("--max-debt", metavar="<amount>", help="maxDebt of the callback canary")
    loop.set_defaults(run=cmd_loop_check)
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
