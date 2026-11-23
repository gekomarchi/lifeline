"""Operator commands: deployment, Sourcify verification, onboarding, removal, vault recovery, loop check.

    python -m lifeline.ops deploy | verify | enroll | fleet | remove | recover | loop-check

One configuration (`deployments/config.json`) is the input. The manifest
(`deployments/manifest.json`) is the output and the input of each later command.
The owner key comes from the environment variable `LIFELINE_OWNER_KEY`. No
command prints or stores it.

Each ABI and each bytecode comes from the Foundry build output under `out/`.

Reusable parts, for the later units: `load_artifact`, `Abi`, `Rpc`, `send_transaction`,
`wait_receipt`, `transact`, `deploy_contract`, `find_logs`, `wait_for_log`, `Manifest`.
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


class Rpc:
    """A JSON-RPC client for one chain, with an ordered list of URLs. A failed URL gives its place to the next."""

    def __init__(self, name, urls, timeout=30.0, attempts=4):
        if not urls:
            raise OpsError(f"no RPC URL for chain {name}: use --rpc {name}=<url>")
        self.name = name
        self.urls = list(urls)
        self.timeout = timeout
        self.attempts = attempts
        self._id = 0
        self._dev = None
        # The newest block that a receipt of this session named. A node behind a load balancer can be some
        # blocks behind the node that gave the receipt: each read waits for this block.
        self.floor = 0
        self._nonces = {}

    def request(self, method, params=(), attempts=None):
        last = None
        for attempt in range(attempts or self.attempts):
            for url in self.urls:
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
                        last = f"HTTP {error.code}"
                        continue
                except (OSError, ValueError) as error:
                    last = type(error).__name__
                    continue
                if isinstance(body, dict) and body.get("error") is not None:
                    raise RpcError(method, body["error"])
                if isinstance(body, dict) and "result" in body:
                    return body["result"]
                last = "no result"
            if attempt + 1 < (attempts or self.attempts):
                time.sleep(min(2.0**attempt, 8.0) * 0.5)
        raise TransportError(f"{self.name}: no answer for {method} ({last})")

    # Reads

    def chain_id(self) -> int:
        return hex_int(self.request("eth_chainId"))

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
        return bytes.fromhex(self.request("eth_getCode", [address, "latest"])[2:])

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

    def call_at(self, to, data: bytes, block=None):
        """Calls a view at one block: `head()` if none is given. Returns (block, return data)."""
        deadline = time.monotonic() + 30
        while True:
            number = self.head() if block is None else block
            try:
                result = self.request("eth_call", [{"to": to, "data": "0x" + data.hex()}, hex(number)])
                return number, bytes.fromhex(result[2:])
            except RpcError as error:
                # Another node of the same URL does not have the block yet.
                if not _NO_BLOCK.search(error.message.lower()) or time.monotonic() >= deadline:
                    raise
            time.sleep(min(POLL_SECONDS, 1.0))

    def call(self, to, data: bytes, block=None) -> bytes:
        return self.call_at(to, data, block)[1]

    def next_nonce(self, sender) -> int:
        """The nonce of the next transaction of a sender: not below the nonces that this session used."""
        pending = hex_int(self.request("eth_getTransactionCount", [sender, "pending"]))
        return max(pending, self._nonces.get(sender.lower(), 0))

    def used_nonce(self, sender, nonce) -> None:
        self._nonces[sender.lower()] = nonce + 1

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
        """True if the node is a local development node (anvil): it answers `anvil_nodeInfo`."""
        if self._dev is None:
            try:
                self.request("anvil_nodeInfo", attempts=1)
                self._dev = True
            except (RpcError, TransportError):
                self._dev = False
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
        return {i["name"]: v for i, v in zip(inputs, values)}

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


def send_transaction(rpc: Rpc, account, to=None, data=b"", value=0, gas=None, abi: Abi = None, what="") -> str:
    """Signs and sends one transaction. Returns its hash. It sends nothing if the call would revert or if
    the balance does not cover the value and the largest fee."""
    sender = account.address
    what = what or "transaction"
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
    price = fees.get("maxFeePerGas", fees.get("gasPrice"))
    if balance < value + gas * price:
        raise OpsError(
            f"{what} on {rpc.name} was not sent: the balance of {sender} is {amount(balance)}; the transaction "
            f"needs {amount(value)} of value and at most {amount(gas * price)} of fee"
        )
    nonce = rpc.next_nonce(sender)
    signed = account.sign_transaction({
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
    try:
        rpc.request("eth_sendRawTransaction", [raw])
    except RpcError as error:
        # A second send after a lost answer: the node has the transaction already.
        if rpc.transaction(tx_hash) is None and rpc.receipt(tx_hash) is None:
            raise OpsError(f"{what} on {rpc.name} was refused by the node: {error.message}") from None
    rpc.used_nonce(sender, nonce)
    return tx_hash


def wait_receipt(rpc: Rpc, tx_hash: str, timeout: float = None) -> dict:
    """Waits for the receipt of a transaction. A reverted transaction is an error."""
    deadline = time.monotonic() + (RECEIPT_TIMEOUT if timeout is None else timeout)
    while True:
        receipt = rpc.receipt(tx_hash)
        if receipt is not None and receipt.get("blockNumber"):
            block = hex_int(receipt["blockNumber"])
            rpc.floor = max(rpc.floor, block)
            if hex_int(receipt["status"]) != 1:
                raise OpsError(f"transaction {tx_hash} on {rpc.name} reverted in block {block}")
            return receipt
        if time.monotonic() >= deadline:
            raise OpsError(f"no receipt for transaction {tx_hash} on {rpc.name}: check it before the next run")
        time.sleep(min(POLL_SECONDS, 1.0))


def transact(rpc: Rpc, account, to, data=b"", value=0, abi: Abi = None, what="") -> dict:
    """Sends one transaction and waits for its receipt."""
    return wait_receipt(rpc, send_transaction(rpc, account, to, data, value, abi=abi, what=what))


def deploy_contract(rpc: Rpc, account, artifact: Artifact, args=(), value=0) -> dict:
    """Deploys a contract from its build artifact. Returns the receipt: `contractAddress` is the new address."""
    if not artifact.bytecode:
        raise OpsError(f"the build artifact of {artifact.name} has no bytecode")
    data = artifact.bytecode + artifact.abi.encode_constructor(*args)
    receipt = transact(rpc, account, None, data, value, abi=artifact.abi, what=f"the deployment of {artifact.name}")
    if not receipt.get("contractAddress") or not rpc.code(receipt["contractAddress"]):
        raise OpsError(f"the deployment of {artifact.name} on {rpc.name} left no code: {receipt['transactionHash']}")
    return receipt


def find_logs(rpc: Rpc, address, topics, from_block, to_block=None) -> list:
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
        """The newest enrollment record of a target that has no complete removal."""
        for record in reversed(self.data["enrollments"]):
            if record.get("chain") == chain and same_address(record.get("target"), target):
                removed = record.get("removed")
                if not removed or (chain != LASNA and not removed.get("vaultDisarm")):
                    return record
                return None
        return None


@dataclass
class Chain:
    name: str
    chain_id: int
    ledger: str
    rpc: Rpc

    def check_id(self) -> None:
        actual = self.rpc.chain_id()
        if actual != self.chain_id:
            raise OpsError(
                f"{self.name}: the RPC {host_of(self.rpc.urls[0])} answers for chain ID {actual}, "
                f"not {self.chain_id}"
            )


def open_chain(name: str, entry: dict, overrides: dict) -> Chain:
    """One chain of the configuration or of the manifest. `--rpc` URLs replace its RPC list."""
    urls = overrides.get(name) or entry.get("rpc") or []
    return Chain(name, int(entry["chainId"]), parse_address(entry["ledger"], f"chains.{name}.ledger"), Rpc(name, urls))


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

    say("checks before deployment:")
    for name, chain in chains.items():
        actual = chain.rpc.chain_id()
        check(actual == chain.chain_id, f"{name}: eth_chainId is {actual}, the configuration has {chain.chain_id}")
        if actual == chain.chain_id:
            check(bool(chain.rpc.code(chain.ledger)), f"{name}: the ledger {chain.ledger} has code")
    if failures:
        raise OpsError("deploy sent no transaction. Failed checks:\n  - " + "\n  - ".join(failures))

    need = {name: 0 for name in chains}

    lasna = chains[LASNA]
    hub = config["hub"]
    if manifest.hub and manifest.hub.get("deployTx"):
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
        if deployed and deployed.get("deployTx"):
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

    if failures:
        raise OpsError("deploy sent no transaction. Failed checks:\n  - " + "\n  - ".join(failures))


def _deploy_step(manifest: Manifest, entry_of, store, chain: Chain, owner, artifact: Artifact, args, value, what):
    """Deploys one contract, or takes it from the manifest. The transaction hash is in the manifest before
    the receipt, so a run that stops in between does not deploy the contract a second time."""
    entry = entry_of()
    if manifest.deployed(entry):
        if not chain.rpc.code(entry["address"]):
            raise OpsError(
                f"the manifest has the {what} at {entry['address']}, and {chain.name} has no code there: the "
                f"manifest is from another network or from a dry run"
            )
        say(f"{what}: in the manifest at {entry['address']} (block {entry['deployBlock']}); no deployment")
        return entry, False
    if entry and entry.get("deployTx"):
        say(f"{what}: the manifest has the transaction {entry['deployTx']} with no receipt; waiting for it")
        if chain.rpc.transaction(entry["deployTx"]) is None and chain.rpc.receipt(entry["deployTx"]) is None:
            raise OpsError(
                f"{chain.name} does not know the transaction {entry['deployTx']} of the {what}. If it was "
                f"dropped, remove this entry from the manifest and run deploy again"
            )
        tx_hash = entry["deployTx"]
    else:
        data = artifact.bytecode + artifact.abi.encode_constructor(*args)
        tx_hash = send_transaction(chain.rpc, owner, None, data, value, abi=artifact.abi,
                                   what=f"the deployment of the {what}")
        entry = store({"chainId": chain.chain_id, "address": None, "deployBlock": None, "deployTx": tx_hash})
        manifest.save()
    receipt = wait_receipt(chain.rpc, tx_hash)
    entry["address"] = checksum(receipt["contractAddress"])
    entry["deployBlock"] = block_of(receipt)
    manifest.save()
    say(f"{what}: deployed at {entry['address']} on {chain.name}, block {entry['deployBlock']}, tx {tx_hash}, "
        f"value {amount(value)}")
    return entry, True


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

    def store_hub(entry):
        manifest.data["contracts"]["hub"] = {"chain": LASNA, **entry}
        return manifest.data["contracts"]["hub"]

    hub, _ = _deploy_step(
        manifest, lambda: manifest.hub, store_hub, lasna, owner, hub_artifact,
        (lasna.ledger, funds["feeFloat"], funds["selfReserveTarget"], funds["selfCareDailyCap"],
         funds["principalFloor"]),
        funds["value"], "hub",
    )
    hub_address = hub["address"]
    hub_owner = hub_abi.decode("owner", lasna.rpc.call(hub_address, hub_abi.encode("owner")))
    if not same_address(hub_owner, owner.address):
        raise OpsError(f"{OWNER_KEY_VARIABLE} is the key of {owner.address}; the owner of the hub is {hub_owner}")

    sender = hub_address if config["callbackSender"] == "hub" else config["callbackSender"]
    say(f"callbackSender of each vault: {sender}"
        + (" (the hub address)" if config["callbackSender"] == "hub" else " (the address of the configuration)"))

    for name, funds in config["vaults"].items():
        chain = chains[name]

        def store_vault(entry, name=name):
            manifest.vaults[name] = entry
            return entry

        vault, _ = _deploy_step(
            manifest, lambda name=name: manifest.vaults.get(name), store_vault, chain, owner, vault_artifact,
            (chain.ledger, sender, funds["feeFloat"], funds["selfReserveTarget"], funds["selfCareDailyCap"],
             funds["principalFloor"]),
            funds["value"], f"vault of {name}",
        )
        stored = vault_abi.decode("callbackSender",
                                  chain.rpc.call(vault["address"], vault_abi.encode("callbackSender")))
        if not same_address(stored, sender):
            raise OpsError(f"the vault of {name} at {vault['address']} has callbackSender {stored}, not {sender}: "
                           f"the value is immutable, so this vault cannot serve this hub")

    for name in config["vaults"]:
        chain, vault = chains[name], manifest.vaults[name]
        sweep_base = config["chains"][name]["sweepBase"]
        proxy, registered, base, _, _ = hub_abi.decode(
            "vaultInfo", lasna.rpc.call(hub_address, hub_abi.encode("vaultInfo", chain.chain_id)))
        if same_address(proxy, chain.ledger) and same_address(registered, vault["address"]) and base == sweep_base:
            say(f"hub.setVault for {name}: registered already; no transaction")
            continue
        receipt = transact(lasna.rpc, owner, hub_address,
                           hub_abi.encode("setVault", chain.chain_id, chain.ledger, vault["address"], sweep_base),
                           abi=hub_abi, what=f"hub.setVault for {name}")
        vault["setVaultTx"] = receipt["transactionHash"]
        manifest.save()
        say(f"hub.setVault for {name}: tx {receipt['transactionHash']} block {block_of(receipt)} "
            f"(proxy {chain.ledger}, vault {vault['address']}, sweepBase {sweep_base})")

    wanted = int.from_bytes(keccak256(f"{config['cronTopic']}(uint256)".encode()), "big")
    cron = hub.setdefault("cron", {"topic": config["cronTopic"], "setCronTopicTx": None, "resumeCronTx": None})
    cron["topic"] = config["cronTopic"]
    if hub_abi.decode("cronTopic", lasna.rpc.call(hub_address, hub_abi.encode("cronTopic"))) != wanted:
        receipt = transact(lasna.rpc, owner, hub_address, hub_abi.encode("setCronTopic", wanted), abi=hub_abi,
                           what="hub.setCronTopic")
        cron["setCronTopicTx"] = receipt["transactionHash"]
        manifest.save()
        say(f"hub.setCronTopic({config['cronTopic']}): tx {receipt['transactionHash']} block {block_of(receipt)}")
    if hub_abi.decode("cronSubscribed", lasna.rpc.call(hub_address, hub_abi.encode("cronSubscribed"))):
        say(f"cron: subscribed already to {config['cronTopic']}; no transaction")
    else:
        receipt = transact(lasna.rpc, owner, hub_address, hub_abi.encode("resumeCron"), abi=hub_abi,
                           what="hub.resumeCron")
        cron["resumeCronTx"] = receipt["transactionHash"]
        say(f"hub.resumeCron ({config['cronTopic']}): tx {receipt['transactionHash']} block {block_of(receipt)}")
    manifest.save()

    say("state after deploy:")
    say(f"  hub {hub_address} on {LASNA} (chain ID {lasna.chain_id}), deployBlock {hub['deployBlock']}")
    for name in config["vaults"]:
        _, registered, base, up, pending = hub_abi.decode(
            "vaultInfo", lasna.rpc.call(hub_address, hub_abi.encode("vaultInfo", chains[name].chain_id)))
        say(f"  vault {registered} on {name} (chain ID {chains[name].chain_id}), deployBlock "
            f"{manifest.vaults[name]['deployBlock']}: up {str(up).lower()}, pending {pending}, sweepBase {base}")
    subscribed = hub_abi.decode("cronSubscribed", lasna.rpc.call(hub_address, hub_abi.encode("cronSubscribed")))
    say(f"  cronSubscribed {str(subscribed).lower()}")
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

    Each check comes before the first transaction. Returns the record that it added to the manifest."""
    if guest and not consent:
        raise OpsError("a guest needs the written consent of the target owner: give --consent <reference>")
    policy.validate(f"the policy of {label}")
    target = parse_address(target, "--target")
    destination = chain_name != LASNA
    chain = session.destination(chain_name) if destination else session.lasna
    lasna, hub, hub_abi, vault_abi = session.lasna, session.hub, session.hub_abi, session.vault_abi
    session.require_owner(chain_name if destination else None)

    if same_address(target, hub):
        raise OpsError("the target is the hub")
    if not chain.rpc.code(target):
        raise OpsError(f"the target {target} has no code on {chain_name}")
    kind, state, _ = session.read_hub("targetInfo", chain.chain_id, target)
    if kind != 0 and TARGET_STATES[state] != "Disarmed":
        raise OpsError(f"the hub has the target {target} of {chain_name} in state {TARGET_STATES[state]}: "
                       f"remove it before a new enrollment")
    if destination:
        vault = session.vault(chain_name)
        if same_address(target, vault):
            raise OpsError("the target is the vault of its chain")
        _, registered, _, _, _ = session.read_hub("vaultInfo", chain.chain_id)
        if not same_address(registered, vault):
            raise OpsError(f"the hub has the vault {registered} for {chain_name}, the manifest has {vault}: "
                           f"run deploy again")

    say(f"enroll {label}: target {target} on {chain_name} (chain ID {chain.chain_id})"
        + (f", guest with consent {consent}" if guest else ""))
    say(f"  policy: {policy.describe()}")
    txs = {"setPolicy": None, "readBackBlock": None, "enroll": None, "enrollBlock": None}
    step = 1
    if destination:
        receipt = transact(chain.rpc, session.owner, vault, vault_abi.encode("setPolicy", target, policy.as_tuple()),
                           abi=vault_abi, what="vault.setPolicy")
        txs["setPolicy"] = receipt["transactionHash"]
        results = receipt_logs(receipt, vault, vault_abi.topic("Result"))
        outcome = vault_abi.decode_event("Result", results[0]) if results else None
        say(f"  1 setPolicy  on {chain_name}: tx {txs['setPolicy']} block {block_of(receipt)}"
            + (f"; the vault reconciled at once: {OUTCOMES[outcome['outcome']]}, paid {amount(outcome['paid'])}"
               if outcome else ""))

        block, data = chain.rpc.call_at(vault, vault_abi.encode("policyOf", target))
        stored = Policy(*vault_abi.decode("policyOf", data))
        differences = [
            f"{field}: stored {getattr(stored, field)}, input {getattr(policy, field)}"
            for field in POLICY_FIELDS if getattr(stored, field) != getattr(policy, field)
        ]
        if differences:
            raise OpsError(
                f"the policy that vault.policyOf gave at block {block} of {chain_name} differs from the input "
                f"({'; '.join(differences)}). Nothing was sent to Lasna: hub.enroll did not run"
            )
        txs["readBackBlock"] = block
        say(f"  2 read-back  on {chain_name}: block {block}; vault.policyOf equals the input in each field")
        step = 3

    receipt = transact(lasna.rpc, session.owner, hub,
                       hub_abi.encode("enroll", chain.chain_id, target, policy.as_tuple()),
                       abi=hub_abi, what="hub.enroll")
    txs["enroll"], txs["enrollBlock"] = receipt["transactionHash"], block_of(receipt)
    say(f"  {step} enroll     on {LASNA}: tx {txs['enroll']} block {txs['enrollBlock']}; "
        f"hub.enroll({chain.chain_id}, {target}, policy)")

    record = {
        "label": label,
        "chain": chain_name,
        "chainId": chain.chain_id,
        "target": target,
        "policy": policy.to_json(),
        "guest": bool(guest),
        "consent": consent if guest else None,
        "txs": txs,
        "removed": None,
    }
    session.manifest.data["enrollments"].append(record)
    session.manifest.save()
    return record


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
        if kind != 0 and TARGET_STATES[state] != "Disarmed":
            record = session.manifest.active_enrollment(entry["chain"], entry["address"]) or {}
            block = (record.get("txs") or {}).get("enrollBlock")
            say(f"enroll {entry['label']}: the hub has the target in state {TARGET_STATES[state]} already; "
                f"no transaction")
        else:
            block = enroll_target(session, entry["chain"], entry["address"], entry["label"],
                                  entry["policy"])["txs"]["enrollBlock"]
        blocks.append((entry["label"], block))
    say("enrollment blocks on lasna (the start of the 48-hour window):")
    for label, block in blocks:
        say(f"  {label}: block {block if block is not None else 'not in the manifest'}")
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
    session.require_owner(args.chain if destination else None)

    kind, state, _ = session.read_hub("targetInfo", chain.chain_id, target)
    if kind == 0:
        raise OpsError(f"the hub has no target {target} on {args.chain}")
    record = session.manifest.active_enrollment(args.chain, target)
    if record is None:
        say(f"note: the manifest has no open enrollment record of {target} on {args.chain}; the hashes are "
            f"printed only")
    removed = (record or {}).get("removed") or {"hubDisarm": None, "vaultDisarm": None}

    say(f"remove: target {target} on {args.chain} (chain ID {chain.chain_id})")
    in_receipt = False
    if TARGET_STATES[state] == "Disarmed":
        say("  1 hub.disarm: the hub has the target in state Disarmed already; no transaction")
    else:
        receipt = transact(lasna.rpc, session.owner, hub, hub_abi.encode("disarm", chain.chain_id, target),
                           abi=hub_abi, what="hub.disarm")
        removed["hubDisarm"] = receipt["transactionHash"]
        if record is not None:
            record["removed"] = removed
            session.manifest.save()
        say(f"  1 hub.disarm   on {LASNA}: tx {removed['hubDisarm']} block {block_of(receipt)}")
        in_receipt = any(
            log["topics"][1:] == [topic_of_address(hub), topic_of_int(chain.chain_id), topic_of_address(chain.ledger)]
            and _is_debt_tuple(log, session.ledger_abi, target)
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
        vault = session.vault(args.chain)
        if session.read_vault(args.chain, "hasPolicy", target):
            receipt = transact(chain.rpc, session.owner, vault, vault_abi.encode("disarm", target), abi=vault_abi,
                               what="vault.disarm")
            removed["vaultDisarm"] = receipt["transactionHash"]
            say(f"  2 vault.disarm on {args.chain}: tx {removed['vaultDisarm']} block {block_of(receipt)}")
            if session.read_vault(args.chain, "hasPolicy", target):
                raise OpsError("the vault still has a policy for the target")
        else:
            say("  2 vault.disarm: the vault has no policy for the target; no transaction")
    if record is not None:
        record["removed"] = removed
        session.manifest.save()
    say("removed: no subscription and no policy for the target")
    return 0


# ----------------------------------------------------------------------------------------------
# recover
# ----------------------------------------------------------------------------------------------


def cmd_recover(args) -> int:
    session = Session(args)
    chain = session.destination(args.chain)
    lasna, hub, hub_abi = session.lasna, session.hub, session.hub_abi
    vault = session.vault(args.chain)
    session.require_owner(args.chain)

    block, debt = session.read_ledger(chain, "debt", vault)
    say(f"recover: vault {vault} on {args.chain} (chain ID {chain.chain_id})")
    say(f"  debt of the vault at the proxy, block {block}: {amount(debt)}")
    if debt > 0:
        reserve = session.read_vault(args.chain, "selfReserveTarget")
        value = debt + reserve
        try:
            receipt = transact(chain.rpc, session.owner, chain.ledger, session.ledger_abi.encode("depositTo", vault),
                               value=value, abi=session.ledger_abi, what="proxy.depositTo")
        except OpsError as error:
            raise OpsError(f"{error}. hub.vaultUp was not sent") from None
        say(f"  1 proxy.depositTo on {args.chain}: tx {receipt['transactionHash']} block {block_of(receipt)}, "
            f"value {amount(value)} = debt {debt} + float {reserve}")
        block, debt = session.read_ledger(chain, "debt", vault)
        say(f"  debt of the vault at the proxy, block {block}: {amount(debt)}")
    else:
        say("  1 proxy.depositTo: no debt, so no deposit")

    sends = 0
    while True:
        # The hub cannot read the destination ledger: `vaultUp` runs only while the vault debt is 0.
        if debt > 0:
            raise OpsError(f"the debt of the vault is {amount(debt)}, above 0: hub.vaultUp was not sent")
        receipt = transact(lasna.rpc, session.owner, hub, hub_abi.encode("vaultUp", chain.chain_id), abi=hub_abi,
                           what="hub.vaultUp")
        sends += 1
        event = hub_abi.decode_event("VaultUp", receipt_logs(receipt, hub, hub_abi.topic("VaultUp"))[0])
        say(f"  2 hub.vaultUp on {LASNA}: tx {receipt['transactionHash']} block {block_of(receipt)}, "
            f"requestId {event['requestId']}, sent {event['sent']}, remaining {event['remaining']}")
        if event["remaining"] == 0:
            break
        _, debt = session.read_ledger(chain, "debt", vault)
    _, _, _, up, pending = session.read_hub("vaultInfo", chain.chain_id)
    say(f"recovered: hub.vaultInfo reports up {str(up).lower()}, pending {pending}, after {sends} vaultUp")
    return 0


# ----------------------------------------------------------------------------------------------
# loop-check
# ----------------------------------------------------------------------------------------------

LOOP_STEPS = (
    ("debt", "the proxy event BlacklistContract(canary)"),
    ("relayed", "the hub event Relayed on Lasna"),
    ("result", "the vault event Result with outcome Rescued"),
    ("debtZero", "debt(canary) == 0 at the proxy"),
)


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
    if "canaryCallback" not in record:
        receipt = deploy_contract(chain.rpc, session.owner, callback_artifact)
        record["canaryCallback"] = {"address": checksum(receipt["contractAddress"]),
                                    "deployTx": receipt["transactionHash"], "deployBlock": block_of(receipt)}
        save()
    canary = record["canaryCallback"]["address"]
    say(f"  CanaryCallback {canary} on {args.chain}: tx {record['canaryCallback']['deployTx']} "
        f"block {record['canaryCallback']['deployBlock']}")

    if "canaryReactive" not in record:
        receipt = deploy_contract(lasna.rpc, session.owner, reactive_artifact,
                                  (lasna.ledger, chain.chain_id, canary, CANARY_CALLBACK_GAS))
        record["canaryReactive"] = {"address": checksum(receipt["contractAddress"]),
                                    "deployTx": receipt["transactionHash"], "deployBlock": block_of(receipt)}
        save()
    reactive = record["canaryReactive"]["address"]
    say(f"  CanaryReactive {reactive} on {LASNA}: tx {record['canaryReactive']['deployTx']} "
        f"block {record['canaryReactive']['deployBlock']}")

    if "fundTx" not in record:
        if args.canary_reserve is not None:
            reserve = to_wei(args.canary_reserve)
        else:
            coeff, _ = ledger_parameter(lasna, session.ledger_abi, "_gasPriceCoeffPer1000",
                                        manifest.data["chains"][LASNA], "gasPriceCoeffPer1000")
            reserve = trigger_fee(lasna.rpc.gas_price(), coeff or 1000, CANARY_RESERVE_GAS)
        receipt = transact(lasna.rpc, session.owner, lasna.ledger, session.ledger_abi.encode("depositTo", reactive),
                           value=reserve, abi=session.ledger_abi, what="depositTo for the Lasna canary")
        record["fundTx"] = receipt["transactionHash"]
        save()
        say(f"  reserves of the Lasna canary: {amount(reserve)}, tx {record['fundTx']}")

    if "enrolled" not in record:
        # `low` 0: `setPolicy` does not fund the canary, so its first callback puts it into debt.
        reserve_target = session.read_vault(args.chain, "selfReserveTarget")
        high = to_wei(args.high) if args.high is not None else max(reserve_target // 4, 1)
        max_debt = to_wei(args.max_debt) if args.max_debt is not None else max(reserve_target, 1)
        policy = Policy(0, high, max_debt, max_debt + high, 2, 3600)
        enroll_target(session, args.chain, canary, f"loop-check canary ({args.chain})", policy)
        record["enrolled"] = True
        save()

    if "pokeTx" not in record:
        receipt = transact(lasna.rpc, session.owner, reactive, reactive_artifact.abi.encode("poke"),
                           abi=reactive_artifact.abi, what="poke")
        record["pokeTx"], record["pokeBlock"] = receipt["transactionHash"], block_of(receipt)
        record["status"] = "waiting"
        save()
    say(f"  poke() on {LASNA}: tx {record['pokeTx']} block {record['pokeBlock']}")

    deadline = time.monotonic() + args.timeout
    steps = record["steps"]
    canary_topic = topic_of_address(canary)

    def missing(key: str) -> int:
        record["status"], record["missing"] = "timeout", key
        save()
        text = dict(LOOP_STEPS)[key]
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
    except OpsError as error:
        print(f"error: {error}", file=sys.stderr)
    except (RpcError, TransportError) as error:
        print(f"error: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
