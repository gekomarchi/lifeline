"""A local JSON-RPC server that stands in for the chain nodes.

It is the only fake in the tests: the command, the reader and the HTTP
transport are the real ones. Each chain has its own URL path. A path with a
suffix (`/lasna/b`) is a second URL for the same chain.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ZERO_WORD = "0x" + "0" * 64


class MockChain:
    def __init__(self, chain_id, head, genesis_time, block_time):
        self.chain_id = chain_id
        self.head = head
        self.genesis_time = genesis_time
        self.block_time = block_time
        self.logs = []
        self.calls = {}  # (to, data) -> hex result; any other call returns a zero word
        self.reverts = set()  # (to, data) of calls that revert
        self.balances = {}
        self.requests = []  # (method, params, path) of each request, in order
        # Faults, each used one time for the next request of its method: (method, "rate" | "range" | "server").
        self.faults = []
        self.always_fail = False  # each request gives HTTP 503
        self.refuse_state = False  # eth_call and eth_getBalance give "missing trie node"
        self.earliest_block = 0  # headers below this block are not available
        self.timestamp_of = None  # optional function of the block number, for a block time that changes
        self.max_range = None  # eth_getLogs over more blocks gives a range error that names this limit

    def timestamp(self, block) -> int:
        if self.timestamp_of:
            return self.timestamp_of(block)
        return self.genesis_time + block * self.block_time

    def block_at(self, timestamp) -> int:
        """The first block with a timestamp at or after `timestamp`."""
        return -((self.genesis_time - timestamp) // self.block_time)

    def add_log(self, address, topics, block, data="0x"):
        log = {
            "address": address.lower(),
            "topics": [topic.lower() for topic in topics],
            "data": data,
            "blockNumber": hex(block),
            "logIndex": hex(sum(1 for other in self.logs if other["blockNumber"] == hex(block))),
            "transactionHash": "0x" + format(self.chain_id * 10**9 + len(self.logs), "064x"),
            "removed": False,
        }
        self.logs.append(log)
        return log

    def methods(self, method=None) -> list:
        return [params for name, params, _ in self.requests if method in (None, name)]

    def method_names(self) -> set:
        return {name for name, _, _ in self.requests}

    # ------------------------------------------------------------------ RPC

    def handle(self, method, params):
        """Return ("result", value) or ("error", code, message, data)."""
        if method == "eth_chainId":
            return "result", hex(self.chain_id)
        if method == "eth_blockNumber":
            return "result", hex(self.head)
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            if number < self.earliest_block:
                return "error", -32014, f"block not found with number {params[0]}", None
            if number > self.head:
                return "result", None
            return "result", {"number": hex(number), "timestamp": hex(self.timestamp(number)), "transactions": []}
        if method == "eth_getLogs":
            return self._get_logs(params[0])
        if method in ("eth_call", "eth_getBalance"):
            if self.refuse_state:
                return "error", -32000, "missing trie node 0x00 (path ) state 0x00 is not available", None
            if method == "eth_call" and (params[0]["to"].lower(), params[0]["data"].lower()) in self.reverts:
                return "error", 3, "execution reverted", None
            if method == "eth_call":
                return "result", self.calls.get((params[0]["to"].lower(), params[0]["data"].lower()), ZERO_WORD)
            return "result", hex(self.balances.get(params[0].lower(), 0))
        return "error", -32601, f"the method {method} does not exist", None

    def _get_logs(self, query):
        start, end = int(query["fromBlock"], 16), int(query["toBlock"], 16)
        if self.max_range is not None and end - start + 1 > self.max_range:
            data = {"details": {"requestRange": end - start + 1, "maxAllowedRange": self.max_range}}
            return "error", -32012, "getLogs request exceeded max allowed range", data
        addresses = query.get("address")
        addresses = [addresses] if isinstance(addresses, str) else addresses
        wanted = query.get("topics") or []

        def matches(log):
            if addresses and log["address"] not in [address.lower() for address in addresses]:
                return False
            if not start <= int(log["blockNumber"], 16) <= end:
                return False
            for position, topic in enumerate(wanted):
                if topic is None:
                    continue
                options = [topic] if isinstance(topic, str) else topic
                if position >= len(log["topics"]) or log["topics"][position] not in [o.lower() for o in options]:
                    return False
            return True

        logs = [log for log in self.logs if matches(log)]
        logs.sort(key=lambda log: (int(log["blockNumber"], 16), int(log["logIndex"], 16)))
        return "result", logs


class MockNode:
    """HTTP server for a set of `MockChain` objects, keyed by the first path segment."""

    def __init__(self, chains):
        self.chains = chains
        node = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                chain = node.chains[self.path.strip("/").split("/")[0]]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                items = body if isinstance(body, list) else [body]
                for item in items:
                    chain.requests.append((item["method"], item["params"], self.path))

                status, answer = 200, None
                fault = next((f for f in chain.faults if f[0] == items[0]["method"]), None)
                if chain.always_fail:
                    status, answer = 503, {"error": "service unavailable"}
                elif fault:
                    chain.faults.remove(fault)
                    if fault[1] == "rate":
                        status = 429
                        answer = {"jsonrpc": "2.0", "id": items[0]["id"], "error": {
                            "code": -32016, "message": "Your IP has exceeded its request rate limit."}}
                    elif fault[1] == "range":
                        answer = {"jsonrpc": "2.0", "id": items[0]["id"], "error": {
                            "code": -32005, "message": "query returned more than 10000 results"}}
                    else:
                        status, answer = 503, {"error": "service unavailable"}
                if answer is None:
                    answers = [node._answer(chain, item) for item in items]
                    answer = answers if isinstance(body, list) else answers[0]

                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @staticmethod
    def _answer(chain, item):
        outcome = chain.handle(item["method"], item["params"])
        answer = {"jsonrpc": "2.0", "id": item["id"]}
        if outcome[0] == "result":
            answer["result"] = outcome[1]
        else:
            answer["error"] = {"code": outcome[1], "message": outcome[2]}
            if outcome[3] is not None:
                answer["error"]["data"] = outcome[3]
        return answer

    def url(self, chain, suffix="") -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/{chain}{suffix}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
