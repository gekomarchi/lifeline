"""Keyless, read-only chain reader and the heights snapshot.

`ChainReader` sends only read methods. It scans logs in bounded block chunks,
uses a smaller chunk after a block-range or response-size error, retries
rate-limit, timeout and server errors with exponential backoff, and keeps a
local cache so that a new run continues where the last run stopped.

`Snapshot` pins one `from` and one `to` block for each chain, saves each state
value that it reads at `to`, and reproduces a run from the saved file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

READ_METHODS = frozenset({
    "eth_chainId",
    "eth_blockNumber",
    "eth_getLogs",
    "eth_getBlockByNumber",
    "eth_call",
    "eth_getBalance",
})
# Methods that a node refuses for a block that it has pruned.
_BLOCK_METHODS = frozenset({"eth_call", "eth_getBalance", "eth_getBlockByNumber"})


class ReaderError(Exception):
    """The retries are used and the node still gave no answer."""


class StateUnavailable(Exception):
    """No configured node can give state, or the header, at the requested block."""


class CallReverted(Exception):
    """The node executed `eth_call` and the call reverted."""


class ChainMismatch(Exception):
    """An RPC URL answers for a different chain than the configured one."""


class SnapshotError(Exception):
    """A heights snapshot cannot be pinned or used."""


class TransportError(Exception):
    """No HTTP response: connection failure or timeout."""


class _Failure(Exception):
    """One failed attempt. `kind` is rate, timeout, retry, range, state or revert.

    `rate` is a rate limit. `timeout` is no answer or a server error: a retry
    of a log request then also uses a smaller chunk. `retry` is any other
    failure that a retry can cure. A request is sent again after each of
    these three.
    """

    def __init__(self, kind, message, hint=None, retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.hint = hint
        self.retry_after = retry_after


def http_transport(url, payload, timeout):
    """POST one JSON-RPC request. Return (HTTP status, parsed body or None, Retry-After seconds or None)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "user-agent": "lifeline-reader"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw, headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        status, raw, headers = error.code, error.read(), error.headers
    except OSError as error:  # URLError, connection errors and timeouts
        raise TransportError(type(error).__name__) from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    try:
        retry_after = float(headers.get("Retry-After", ""))
    except ValueError:
        retry_after = None
    return status, body, retry_after


_RATE_LIMIT = re.compile(r"rate.?limit|too many requests|request limit|throttl|capacity|compute units|credits")
_RANGE = re.compile(
    r"range|too many (logs|results|blocks)|more than \d+ (logs|results)|response size|size exceeded"
    r"|too large|limited to|exceeds? (the )?(max|limit)|block count|query (timeout|timed out)"
)
_STATE = re.compile(
    r"missing trie node|pruned|historical state|state histor|insufficient changesets"
    r"|state (is |was )?(not available|unavailable)|no state available|proof window"
    r"|header not found|block not found|unknown block|does not have this data|archive"
)
_REVERT = re.compile(r"execution reverted|\brevert")


def _classify(method, status, body, retry_after):
    """Return the JSON-RPC result, or raise `_Failure` with the kind of the failure."""
    if not isinstance(body, dict):
        body = None
    error = body.get("error") if body else None
    transient = "timeout" if status == 408 or status >= 500 else "retry"
    if error is None:
        if status == 200 and body is not None and body.get("result") is not None:
            return body["result"]
        if status == 200 and body is not None and "result" in body:
            # A null header: the node has pruned the block, or does not have it yet.
            raise _Failure("state", "the node has no such block")
        if status == 413 and method == "eth_getLogs":
            raise _Failure("range", "HTTP 413")
        raise _Failure(transient, f"HTTP {status}", retry_after=retry_after)

    if not isinstance(error, dict):
        error = {"message": str(error)}
    message = str(error.get("message", ""))
    text = (message + " " + json.dumps(error.get("data", ""))).lower()
    summary = f"{error.get('code', 'error')} {message}"[:200]

    if method == "eth_call" and (error.get("code") == 3 or _REVERT.search(text)):
        raise _Failure("revert", summary)
    if status == 429 or _RATE_LIMIT.search(text):
        raise _Failure("rate", summary, retry_after=retry_after)
    if method == "eth_getLogs" and (status == 413 or _RANGE.search(text)):
        numbers = [int(n) for n in re.findall(r"\b\d+\b", text)]
        raise _Failure("range", summary, hint=numbers)
    if method in _BLOCK_METHODS and _STATE.search(text):
        raise _Failure("state", summary)
    raise _Failure(transient, summary, retry_after=retry_after)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _log_position(log):
    return int(log["blockNumber"], 16), int(log["logIndex"], 16)


class ChainReader:
    """Reader for one chain.

    `urls` is an ordered list. A request starts at the first URL that did not
    fail in the last `cooldown` seconds, and a retry uses the next URL.
    Messages name a URL by its position in the list, never by its text.
    """

    def __init__(
        self,
        name,
        urls,
        cache_dir=".cache/chain",
        *,
        chain_id=None,
        chunk=50_000,
        batch=50,
        retries=8,
        backoff=1.0,
        max_backoff=60.0,
        cooldown=60.0,
        timeout=30.0,
    ):
        if not urls:
            raise ValueError(f"{name}: no RPC URL")
        self.name = name
        self.urls = list(urls)
        self.cache_dir = Path(cache_dir)
        self.batch = batch
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.cooldown = cooldown
        self.timeout = timeout
        self.cache_to = None  # set by Snapshot: only data at or below this block is cached
        self.sent = Counter()  # requests sent, by method

        self._chain_id = chain_id
        self._verified = set()
        self._bad_until = [0.0] * len(self.urls)
        self._chunk = [chunk] * len(self.urls)
        self._request_id = 0
        self._timestamps = None
        self._no_state = set()

    # ------------------------------------------------------------------ API

    def chain_id(self) -> int:
        if not self._verified:
            self._request("eth_chainId", [])
        return self._chain_id

    def head(self) -> int:
        return int(self._request("eth_blockNumber", []), 16)

    def logs(self, address, topics, from_block, to_block) -> list:
        """Raw log objects of `address` in `from_block..to_block`, in ascending order of block and log index."""
        address = address.lower()
        topics = [_lower(topic) for topic in topics]
        directory = self._log_dir(address, topics)
        cached = sorted(
            (int(a), int(b), path)
            for path in directory.glob("*-*.json")
            for a, b in [path.stem.split("-")]
        )
        out = []
        cursor = from_block
        while cursor <= to_block:
            hit = next((seg for seg in cached if seg[0] <= cursor <= seg[1]), None)
            if hit:
                chunk_logs = json.loads(hit[2].read_text())
                chunk_end = hit[1]
            else:
                later = [seg[0] for seg in cached if seg[0] > cursor]
                limit = min([to_block] + [start - 1 for start in later])
                chunk_end, chunk_logs = self._fetch_logs(address, topics, cursor, limit)
                if self.cache_to is not None and chunk_end <= self.cache_to:
                    _write_atomic(directory / f"{cursor}-{chunk_end}.json", json.dumps(chunk_logs))
            out += [log for log in chunk_logs if cursor <= int(log["blockNumber"], 16) <= to_block]
            cursor = chunk_end + 1
        out.sort(key=_log_position)
        return out

    def timestamp(self, block) -> int:
        """Timestamp of one block, from its header."""
        return self.timestamps([block])[block]

    def timestamps(self, blocks) -> dict:
        """Timestamps of many blocks, from their headers. Returns {block: timestamp}."""
        if self._timestamps is None:
            self._timestamps = self._load_timestamps()
        missing = sorted(set(blocks) - self._timestamps.keys())
        headers = self._request_many("eth_getBlockByNumber", [[hex(block), False] for block in missing])
        lines = []
        for block, header in zip(missing, headers):
            self._timestamps[block] = int(header["timestamp"], 16)
            if self.cache_to is not None and block <= self.cache_to:
                lines.append(f"{block} {self._timestamps[block]}\n")
        if lines:
            path = self._timestamp_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.writelines(lines)
        return {block: self._timestamps[block] for block in blocks}

    def call(self, to, data, block) -> str:
        """`eth_call` at one block. Returns the hex result."""
        return self.calls([(to, data)], block)[0]

    def calls(self, requests, block) -> list:
        """`eth_call` for many (to, data) pairs at one block. Returns the hex results in order."""
        params = [[{"to": to, "data": data}, hex(block)] for to, data in requests]
        return [result.lower() for result in self._state_requests("eth_call", params, block)]

    def balance(self, address, block) -> int:
        return int(self._state_requests("eth_getBalance", [[address, hex(block)]], block)[0], 16)

    # ------------------------------------------------------------ requests

    def _state_requests(self, method, params_list, block):
        if block in self._no_state:
            raise StateUnavailable(f"{self.name}: no state at block {block}")
        try:
            return self._request_many(method, params_list)
        except StateUnavailable:
            self._no_state.add(block)
            raise

    def _request_many(self, method, params_list) -> list:
        """Results for many requests of one method, in order.

        JSON-RPC batches are the fast path. A batch that fails with a
        rate-limit, timeout or server error is sent again after the same
        backoff as a single request. For any other failure, and when the
        retries are used, each open request goes through `_request`, which
        owns the error rules.
        """
        results = {}
        for start in range(0, len(params_list), self.batch):
            group = range(start, min(start + self.batch, len(params_list)))
            failures = 0
            tried = set()
            while self.batch > 1 and len(group) > 1:
                pending = [i for i in group if i not in results]
                if not pending:
                    break
                index = self._pick(tried)
                answers, failure = self._attempt_batch(index, method, [params_list[i] for i in pending])
                for position, result in answers.items():
                    results[pending[position]] = result
                if failure is None:
                    continue
                if failure.kind == "no batch":
                    self.batch = 1
                if failure.kind not in ("rate", "timeout", "retry"):
                    break
                self._bad_until[index] = time.monotonic() + self.cooldown
                tried.add(index)
                failures += 1
                if failures > self.retries:
                    break
                time.sleep(self._delay(failures, failure))
        return [
            results[i] if i in results else self._request(method, params)
            for i, params in enumerate(params_list)
        ]

    def _attempt_batch(self, index, method, params_list):
        """Send one batch to one URL. Return ({position: result}, the first failure or None)."""
        first_id = self._request_id + 1
        payload = [
            {"jsonrpc": "2.0", "id": first_id + position, "method": method, "params": params}
            for position, params in enumerate(params_list)
        ]
        try:
            self._verify(index)
            self._request_id += len(params_list)
            self.sent[method] += len(params_list)
            status, body, retry_after = http_transport(self.urls[index], payload, self.timeout)
        except TransportError as error:
            return {}, _Failure("timeout", f"no response ({error})")
        except _Failure as failure:
            return {}, failure
        if not isinstance(body, list):
            try:
                _classify(method, status, body, retry_after)
            except _Failure as failure:
                if failure.kind not in ("rate", "timeout"):
                    failure.kind = "no batch"  # the node answers, but not to a batch
                return {}, failure
            return {}, _Failure("no batch", "one answer for a batch")
        answers, first = {}, None
        for item in body:
            position = item.get("id", 0) - first_id if isinstance(item, dict) else -1
            if 0 <= position < len(params_list):
                try:
                    answers[position] = _classify(method, status, item, retry_after)
                except _Failure as failure:
                    first = first or failure
        if first is None and len(answers) < len(params_list):
            first = _Failure("retry", "the batch answer is not complete")
        return answers, first

    def _delay(self, failures, failure) -> float:
        """Exponential backoff, and at least the Retry-After time of the node."""
        delay = min(self.max_backoff, self.backoff * 2 ** (failures - 1))
        if failure.retry_after:
            delay = max(delay, min(failure.retry_after, self.max_backoff))
        return delay

    def _fetch_logs(self, address, topics, start, limit):
        """Fetch one chunk that starts at `start` and ends at or below `limit`. Return (end, logs)."""
        end = start

        def params(index):
            nonlocal end
            end = min(limit, start + self._chunk[index] - 1)
            return [{"address": address, "topics": topics, "fromBlock": hex(start), "toBlock": hex(end)}]

        def smaller(index, failure):
            span = end - start + 1
            if span <= 1:
                return False
            # A range error often names the limit of the node. A number below 100 is
            # more likely a code or a duration than a block count.
            hints = [n for n in (failure.hint or []) if 100 <= n < span]
            self._chunk[index] = max(hints) if hints else span // 2
            return True

        result = self._request("eth_getLogs", params, smaller=smaller)
        return end, [log for log in result if not log.get("removed")]

    def _request(self, method, params, smaller=None):
        """Send one read request with retries. `params` is a list, or a function of the URL index.

        `smaller(index, failure)` is called for a log request that failed with a
        range error or with repeated timeouts. It makes the chunk of that URL
        smaller and returns True if a smaller chunk is possible.
        """
        if method not in READ_METHODS:
            raise ValueError(f"{method} is not a read method")
        failures = 0
        state_failures = 0
        tried = set()
        while True:
            index = self._pick(tried)
            try:
                return self._attempt(index, method, params(index) if callable(params) else params)
            except _Failure as failure:
                last = f"rpc[{index}]: {failure}"
                if failure.kind == "revert":
                    raise CallReverted(f"{self.name}: {method}: {last}") from None
                if failure.kind == "range" and smaller and smaller(index, failure):
                    continue  # the same range again with a smaller chunk
                self._bad_until[index] = time.monotonic() + self.cooldown
                tried.add(index)
                if failure.kind == "state":
                    # Ask each URL two times: a node behind the head answers on the second round.
                    state_failures += 1
                    if state_failures >= 2 * len(self.urls):
                        raise StateUnavailable(f"{self.name}: {method}: {last}") from None
                    if state_failures % len(self.urls) == 0:
                        time.sleep(self.backoff)
                    continue
                failures += 1
                if failures > self.retries:
                    raise ReaderError(
                        f"{self.name}: {method} failed after {self.retries} retries; last error {last}"
                    ) from None
                if failures > 1 and failure.kind == "timeout" and smaller:
                    smaller(index, failure)  # a heavy range can cause timeouts
                time.sleep(self._delay(failures, failure))

    def _pick(self, tried):
        """The first URL that this request did not use and that is not in its cooldown."""
        if len(tried) >= len(self.urls):
            tried.clear()
        now = time.monotonic()
        candidates = [index for index in range(len(self.urls)) if index not in tried]
        return min(candidates, key=lambda index: (self._bad_until[index] > now, index))

    def _attempt(self, index, method, params):
        self._verify(index)
        if method == "eth_chainId":
            return hex(self._chain_id)
        return self._send(index, method, params)

    def _verify(self, index):
        """Check one time that the URL answers for the chain of this reader."""
        if index in self._verified:
            return
        answer = int(self._send(index, "eth_chainId", []), 16)
        if self._chain_id is None:
            self._chain_id = answer
        elif answer != self._chain_id:
            raise ChainMismatch(
                f"{self.name}: rpc[{index}] answers for chain {answer}, expected {self._chain_id}"
            )
        self._verified.add(index)

    def _send(self, index, method, params):
        self._request_id += 1
        self.sent[method] += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        try:
            status, body, retry_after = http_transport(self.urls[index], payload, self.timeout)
        except TransportError as error:
            raise _Failure("timeout", f"no response ({error})") from None
        return _classify(method, status, body, retry_after)

    # --------------------------------------------------------------- cache

    def _chain_dir(self) -> Path:
        return self.cache_dir / str(self.chain_id())

    def _log_dir(self, address, topics) -> Path:
        key = json.dumps([self.chain_id(), address, topics])
        directory = self._chain_dir() / "logs" / hashlib.sha256(key.encode()).hexdigest()[:24]
        if self.cache_to is not None and not (directory / "filter.json").exists():
            _write_atomic(directory / "filter.json", key)
        return directory

    def _timestamp_file(self) -> Path:
        return self._chain_dir() / "timestamps.txt"

    def _load_timestamps(self) -> dict:
        values = {}
        try:
            lines = self._timestamp_file().read_text().splitlines()
        except FileNotFoundError:
            return values
        for line in lines:
            parts = line.split()
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                values[int(parts[0])] = int(parts[1])
        return values


def _lower(topic):
    if topic is None:
        return None
    if isinstance(topic, list):
        return [item.lower() for item in topic]
    return topic.lower()


def first_block_at(reader: ChainReader, timestamp: int, head: int) -> int:
    """The first block with a timestamp at or after `timestamp`, at or below `head`.

    The search starts at the head and estimates the block from the block
    rate, so that it reads few headers far below the answer: a node can keep
    no old blocks. A block with no header on the node counts as below the
    answer only while a later probe confirms a real block before `timestamp`.
    """

    def is_before(block):
        """True or False from the header; None if the node has no header for the block."""
        try:
            return reader.timestamp(block) < timestamp
        except StateUnavailable:
            return None

    if reader.timestamp(head) < timestamp:
        raise SnapshotError(f"{reader.name}: the head block is older than the start of the window")
    low, confirmed = -1, True  # block -1 stands for "before the chain": block 0 can be the answer
    high = head
    step = min(1000, head)
    while high > 0:
        probe = max(0, high - step)
        before = is_before(probe)
        if before is not False:
            low, confirmed = probe, before is True
            break
        # Blocks per second between the two known points give the next estimate.
        seconds = max(1, reader.timestamp(high) - reader.timestamp(probe))
        step = max(1, int((reader.timestamp(probe) - timestamp) * (high - probe) / seconds) + 1)
        high = probe
    while high - low > 1:
        middle = (low + high) // 2
        before = is_before(middle)
        if before is False:
            high = middle
        else:
            low, confirmed = middle, before is True
    if not confirmed:
        raise SnapshotError(
            f"{reader.name}: the node has no block header before the start of the window "
            f"(its first header in the search is block {high})"
        )
    return high


class Snapshot:
    """Heights for each chain, the parameters, and each state value read at `to`.

    `chains[name]` has `chainId`, `from`, `to` and `toTimestamp`, the block
    timestamp of `to`: the end of the window in time, with no header read in
    a later run.
    """

    VERSION = 1

    def __init__(self, chains, params, state=()):
        self.chains = chains
        self.params = params
        self.from_saved = []  # names of values that the node could not serve and the snapshot supplied
        self.differs = []  # values that the node now gives differently; the saved value is used
        self._saved = {self._key(entry): entry["result"] for entry in state}
        self._state = {}  # values read in this run

    @classmethod
    def pin(cls, readers, from_block, params):
        """Pin `to` at the head of each chain, then `from` with `from_block(reader, to)`."""
        chains = {}
        for name, reader in readers.items():
            chains[name] = {"chainId": reader.chain_id(), "from": 0, "to": reader.head()}
            reader.cache_to = chains[name]["to"]
        for name, reader in readers.items():
            chains[name]["toTimestamp"] = reader.timestamp(chains[name]["to"])
            chains[name]["from"] = from_block(reader, chains[name]["to"])
        return cls(chains, params)

    @classmethod
    def load(cls, path, readers):
        """Load a saved snapshot and check it against the readers. Reads no head."""
        try:
            document = json.loads(Path(path).read_text())
        except (OSError, ValueError) as error:
            raise SnapshotError(f"{path}: {error}") from None
        if document.get("version") != cls.VERSION:
            raise SnapshotError(f"{path}: unsupported snapshot version {document.get('version')}")
        chains = document["chains"]
        if set(chains) != set(readers):
            raise SnapshotError(
                f"{path}: chains {sorted(chains)} do not match the configured chains {sorted(readers)}"
            )
        for name, reader in readers.items():
            absent = {"chainId", "from", "to", "toTimestamp"} - chains[name].keys()
            if absent:
                raise SnapshotError(f"{path}: chains.{name} has no {', '.join(sorted(absent))}")
            if reader.chain_id() != chains[name]["chainId"]:
                raise ChainMismatch(
                    f"{name}: the node answers for chain {reader.chain_id()}, "
                    f"the snapshot has {chains[name]['chainId']}"
                )
            reader.cache_to = chains[name]["to"]
        return cls(chains, document.get("params", {}), document.get("state", ()))

    def text(self) -> str:
        """The snapshot file: the saved state of a loaded file and each value read in this run."""
        state = [
            {"chain": chain, "block": block, "kind": kind, "to": to, "data": data, "result": result}
            for (chain, block, kind, to, data), result in sorted({**self._saved, **self._state}.items())
        ]
        document = {"version": self.VERSION, "chains": self.chains, "params": self.params, "state": state}
        return json.dumps(document, indent=1, sort_keys=True) + "\n"

    def save(self, path) -> None:
        _write_atomic(Path(path), self.text())

    def call(self, reader, to, data, name, block=None) -> str:
        """`eth_call` at the pinned `to` of the reader's chain, or at `block`. `name` labels the value in reports."""
        return self.calls(reader, [(to, data, name)], block)[0]

    def calls(self, reader, requests, block=None) -> list:
        """`eth_call` for many (to, data, name) requests at the pinned `to`, or at `block`. Returns the hex
        results in order."""
        return self._read_state(reader, "call", requests, block)

    def balance(self, reader, address, name, block=None) -> int:
        return int(self._read_state(reader, "balance", [(address, "0x", name)], block)[0], 16)

    def _read_state(self, reader, kind, requests, block=None):
        """State values at one block: the pinned `to`, or a block of `from - 1..to` that the caller gives.
        Each value is saved with its block, so a later run reproduces it."""
        heights = self.chains[reader.name]
        if block is None:
            block = heights["to"]
        elif not max(heights["from"] - 1, 0) <= block <= heights["to"]:
            raise SnapshotError(
                f"{reader.name}: block {block} is outside the heights {heights['from']}..{heights['to']}"
            )
        keys = [(reader.name, block, kind, to.lower(), data.lower()) for to, data, _ in requests]
        missing = {key: name for key, (_, _, name) in zip(keys, requests) if key not in self._state}
        if missing:
            try:
                if kind == "call":
                    results = reader.calls([(key[3], key[4]) for key in missing], block)
                else:
                    results = [hex(reader.balance(key[3], block)) for key in missing]
            except StateUnavailable:
                # The node cannot give state at the old block: use the saved values.
                absent = [name for key, name in missing.items() if key not in self._saved]
                if absent:
                    raise StateUnavailable(
                        f"{reader.name}: no state at block {block} and no saved value for {absent[0]}"
                    ) from None
                self.from_saved += missing.values()
                results = [self._saved[key] for key in missing]
            for (key, name), result in zip(missing.items(), results):
                # A saved value stays: a run with a snapshot must reproduce the same data.
                saved = self._saved.get(key, result)
                if saved != result:
                    self.differs.append(f"{name}: the node gives {result}, the snapshot has {saved}")
                self._state[key] = saved
        return [self._state[key] for key in keys]

    @staticmethod
    def _key(entry):
        return entry["chain"], entry["block"], entry["kind"], entry["to"].lower(), entry.get("data", "0x").lower()
