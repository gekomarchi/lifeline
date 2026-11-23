"""Local anvil forks and a recording JSON-RPC proxy for the fork dry run of the operator commands.

The proxy stands between a command and the forks. It records each request in the order of arrival, so a
test can show the order of the transactions on two chains. A test can also give it a function that
changes one answer: the read-back case needs an RPC answer that differs from the chain.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ANVIL = shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")

# The RPC variables of the forge fork tests, and one more for Unichain Sepolia.
FORK_VARIABLES = {
    "lasna": "LASNA_RPC_URL",
    "unichain-sepolia": "UNICHAIN_SEPOLIA_RPC_URL",
    "base-sepolia": "BASE_SEPOLIA_RPC_URL",
}


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def rpc(url: str, method: str, params=()):
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read())
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


class Anvil:
    """One anvil fork of a chain."""

    def __init__(self, name: str, upstream: str, log_dir: Path):
        self.name = name
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._log = open(log_dir / f"anvil-{name}.log", "w")
        self.process = subprocess.Popen(
            [ANVIL, "--fork-url", upstream, "--port", str(self.port), "--disable-min-priority-fee"],
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 90
        while True:
            try:
                rpc(self.url, "eth_chainId")
                break
            except OSError:
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"anvil did not start for {name}: see {self._log.name}") from None
                time.sleep(0.2)
        # The block header of Lasna has no blob gas field: anvil refuses a call at the fork block itself.
        rpc(self.url, "anvil_mine", ["0x1"])

    def request(self, method, params=()):
        return rpc(self.url, method, params)

    def block_number(self) -> int:
        return int(self.request("eth_blockNumber"), 16)

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._log.close()


class RecordingProxy:
    """One HTTP server. `http://127.0.0.1:<port>/<chain>` forwards to the fork of that chain."""

    def __init__(self, upstreams: dict):
        self.upstreams = dict(upstreams)
        self.records = []  # {"chain", "method", "params", "result"}, in the order of arrival
        self.tamper = None  # function(chain, method, params, result) -> result
        self.refuse = ()  # method prefixes that get a "method not found" error, with no request to the fork
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                chain = self.path.strip("/")
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if payload["method"].startswith(tuple(proxy.refuse)) and proxy.refuse:
                    body = {"jsonrpc": "2.0", "id": payload.get("id"),
                            "error": {"code": -32601, "message": "the method does not exist"}}
                else:
                    request = urllib.request.Request(
                        proxy.upstreams[chain], data=json.dumps(payload).encode(),
                        headers={"content-type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=120) as response:
                        body = json.loads(response.read())
                if proxy.tamper is not None and "result" in body:
                    body["result"] = proxy.tamper(chain, payload["method"], payload.get("params"), body["result"])
                with proxy._lock:
                    proxy.records.append({
                        "chain": chain, "method": payload["method"], "params": payload.get("params"),
                        "result": body.get("result"),
                    })
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, chain: str) -> str:
        return f"http://127.0.0.1:{self.port}/{chain}"

    def mark(self) -> int:
        return len(self.records)

    def sent(self, since: int = 0) -> list:
        """The transactions that the commands sent after a mark: (chain, transaction hash), in order."""
        return [(r["chain"], r["result"]) for r in self.records[since:] if r["method"] == "eth_sendRawTransaction"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def fork_urls():
    """The upstream RPC URL of each chain from the environment, or None if one is not set."""
    urls = {name: os.environ.get(variable) for name, variable in FORK_VARIABLES.items()}
    return urls if all(urls.values()) else None
