"""A real browser engine for the tests of the board, and an RPC gate that a page can call.

`Browser` drives Chrome or Chromium through the DevTools protocol with the standard library only: one
WebSocket, no package. It starts a headless browser with a scratch profile, or attaches to a running one
if `LIFELINE_CDP_URL` names its DevTools address (for example `http://127.0.0.1:9222`). Each page records
its network log: every request that the browser sends for it.

`RpcGate` is a local HTTP server between the page and the nodes. It answers the cross-origin requests of a
`file://` page, records each JSON-RPC call, and can take one URL out of service. Behind it is a mock chain
or a real node (an anvil fork): the page and the build are the real ones.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CDP_VARIABLE = "LIFELINE_CDP_URL"
BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


def find_browser():
    """The DevTools address of `LIFELINE_CDP_URL`, or the path of a browser binary, or None."""
    if os.environ.get(CDP_VARIABLE):
        return os.environ[CDP_VARIABLE]
    return next((path for path in map(shutil.which, BROWSERS) if path), None)


class _WebSocket:
    """A client for text frames (RFC 6455), as much as the DevTools protocol needs."""

    def __init__(self, url: str):
        parts = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        self._buffer = b""
        head = self._until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"the browser refused the DevTools connection: {head[:80]!r}")

    def _until(self, mark: bytes) -> bytes:
        while mark not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("the browser closed the DevTools connection")
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(mark)
        return head

    def _take(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self._buffer)))
            if not chunk:
                raise RuntimeError("the browser closed the DevTools connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        size = len(payload)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", size)
        self.sock.sendall(head + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))

    def send(self, text: str) -> None:
        self._frame(1, text.encode())

    def receive(self, timeout: float) -> str:
        """The next text message."""
        self.sock.settimeout(timeout)
        message = b""
        while True:
            first, second = self._take(2)
            size = second & 0x7F
            if size == 126:
                (size,) = struct.unpack(">H", self._take(2))
            elif size == 127:
                (size,) = struct.unpack(">Q", self._take(8))
            payload = self._take(size)
            opcode = first & 0x0F
            if opcode == 9:
                self._frame(10, payload)
            elif opcode == 8:
                raise RuntimeError("the browser closed the DevTools connection")
            elif opcode in (0, 1):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """One browser. `page()` opens a tab."""

    def __init__(self, where: str = None):
        where = where or find_browser()
        if not where:
            raise RuntimeError("no browser: install Chrome or Chromium, or set " + CDP_VARIABLE)
        self.process = self._profile = None
        if where.startswith(("http://", "https://")):
            address = where.rstrip("/")
        else:
            self._profile = tempfile.mkdtemp(prefix="lifeline-board-")
            self.process = subprocess.Popen(
                [where, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
                 "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--no-sandbox", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            port_file, deadline = Path(self._profile) / "DevToolsActivePort", time.monotonic() + 60
            while not port_file.exists() or not port_file.read_text().strip():
                if self.process.poll() is not None or time.monotonic() > deadline:
                    self.close()
                    raise RuntimeError(f"the browser {where} did not start")
                time.sleep(0.1)
            address = "http://127.0.0.1:" + port_file.read_text().split()[0]
        with urllib.request.urlopen(address + "/json/version", timeout=30) as response:
            version = json.loads(response.read())
        self.version = version.get("Browser", "")
        self._socket = _WebSocket(version["webSocketDebuggerUrl"])
        self._next = 0
        self._pages = {}

    def call(self, method: str, params: dict = None, session: str = None, timeout: float = 60.0):
        """Sends one command and returns its result. Events that arrive first go to their page."""
        self._next += 1
        message = {"id": self._next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._socket.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while True:
            answer = json.loads(self._socket.receive(max(deadline - time.monotonic(), 0.1)))
            if answer.get("id") == self._next:
                if "error" in answer:
                    raise RuntimeError(f"{method}: {answer['error']}")
                return answer.get("result", {})
            page = self._pages.get(answer.get("sessionId"))
            if page is not None and "method" in answer:
                page._event(answer["method"], answer.get("params", {}))

    def page(self, width: int = 1440, height: int = 1000) -> "Page":
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        page = self._pages[session] = Page(self, target, session)
        for domain in ("Page", "Runtime", "Network"):
            self.call(domain + ".enable", session=session)
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, session)
        return page

    def close(self) -> None:
        for page in list(self._pages.values()):
            page.close()
        if getattr(self, "_socket", None):
            self._socket.close()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)


class Page:
    """One tab, with its network log, its console errors and its uncaught exceptions."""

    def __init__(self, browser: Browser, target: str, session: str):
        self.browser, self.target, self.session = browser, target, session
        self.requests = []  # {"url", "method", "type", "body"}, in the order that the browser sent them
        self.errors = []    # uncaught exceptions and console errors of the page

    def _event(self, method: str, params: dict) -> None:
        if method == "Network.requestWillBeSent":
            request = params["request"]
            self.requests.append({"url": request["url"], "method": request["method"], "type": params.get("type"),
                                  "body": request.get("postData")})
        elif method == "Runtime.exceptionThrown":
            details = params["exceptionDetails"]
            self.errors.append((details.get("exception") or {}).get("description") or details.get("text"))
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            self.errors.append(" ".join(str(arg.get("value", arg.get("description"))) for arg in params["args"]))

    def call(self, method: str, params: dict = None, timeout: float = 60.0):
        return self.browser.call(method, params, self.session, timeout)

    def goto(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def evaluate(self, expression: str):
        """The JSON value of an expression in the page. A promise is awaited."""
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            raise RuntimeError(f"{expression[:80]}: {result['exceptionDetails']}")
        return result["result"].get("value")

    def wait(self, expression: str, timeout: float = 60.0):
        """Waits until an expression is true in the page, and returns its value."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = self.evaluate(expression)
            except RuntimeError:  # the document is not there yet
                value = None
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"the page did not reach: {expression}")
            time.sleep(0.05)

    def type_into(self, selector: str, text: str, enter: bool = True) -> None:
        """Types a text into a field as a user does: focus, select all, insert, and the Enter key."""
        self.evaluate(f"(() => {{ const field = document.querySelector({json.dumps(selector)}); field.focus(); "
                      f"field.select(); }})()")
        if text:
            self.call("Input.insertText", {"text": text})
        else:
            self.press("Backspace", 8)
        if enter:
            self.press("Enter", 13, "\r")

    def press(self, key: str, code: int, text: str = None) -> None:
        base = {"key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": text} if text else {})})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def click(self, selector: str) -> None:
        self.evaluate(f"document.querySelector({json.dumps(selector)}).click()")

    def screenshot(self, path) -> None:
        data = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})["data"]
        Path(path).write_bytes(base64.b64decode(data))

    def close(self) -> None:
        if self.browser._pages.pop(self.session, None) is not None:
            try:
                self.browser.call("Target.closeTarget", {"targetId": self.target})
            except (RuntimeError, OSError):
                pass


class RpcGate:
    """`http://127.0.0.1:<port>/<route>` answers JSON-RPC for a page of any origin.

    A route is a function `(method, params) -> ("result", value) | ("error", code, message, data)` (the
    `handle` of a mock chain), or the URL of a node. A route name can have a suffix after a second `/`
    (`lasna/b`): it is another URL of the same route. `down` holds the URL paths that give no answer that a
    page can read: status 503 with no cross-origin header. `max_batch` limits the calls of one POST.
    """

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.records = []  # {"route", "path", "method", "params"}, in the order of arrival
        self.posts = []    # (route, number of calls) of each POST that got its answers
        self.down = set()
        # route -> the most calls of one POST. A larger batch gets one error object with status 429, as a
        # public gateway gives it. 0: the route takes no batch at all (one error object, status 200).
        self.max_batch = {}
        self._lock = threading.Lock()
        gate = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "content-type")

            def _reply(self, status, payload=b"", cors=True):
                self.send_response(status)
                if cors:
                    self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_OPTIONS(self):
                self._reply(503 if self.path.strip("/") in gate.down else 204, cors=self.path.strip("/") not in gate.down)

            def do_POST(self):
                path = self.path.strip("/")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if path in gate.down:
                    return self._reply(503, b"{}", cors=False)
                route = path.split("/")[0]
                items = body if isinstance(body, list) else [body]
                limit = gate.max_batch.get(route)
                if limit is not None and isinstance(body, list) and len(items) > max(limit, 1):
                    message = "rate limit exceeded" if limit else "batch requests are not supported"
                    refusal = {"jsonrpc": "2.0", "id": None, "error": {"code": -32005, "message": message}}
                    return self._reply(429 if limit else 200, json.dumps(refusal).encode())
                with gate._lock:
                    gate.posts.append((route, len(items)))
                    for item in items:
                        gate.records.append({"route": route, "path": path, "method": item["method"],
                                             "params": item.get("params")})
                backend = gate.routes[route]
                if callable(backend):
                    answers = [gate._answer(backend, item) for item in items]
                    answer = answers if isinstance(body, list) else answers[0]
                else:
                    request = urllib.request.Request(backend, data=json.dumps(body).encode(),
                                                     headers={"content-type": "application/json"})
                    with urllib.request.urlopen(request, timeout=120) as response:
                        answer = json.loads(response.read())
                self._reply(200, json.dumps(answer).encode())

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @staticmethod
    def _answer(handle, item) -> dict:
        outcome = handle(item["method"], item.get("params") or [])
        answer = {"jsonrpc": "2.0", "id": item["id"]}
        if outcome[0] == "result":
            answer["result"] = outcome[1]
        else:
            answer["error"] = {"code": outcome[1], "message": outcome[2]}
            if len(outcome) > 3 and outcome[3] is not None:
                answer["error"]["data"] = outcome[3]
        return answer

    def url(self, route: str, suffix: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/{route}{suffix}"

    def mark(self) -> int:
        return len(self.records)

    def calls(self, method: str = None, route: str = None, since: int = 0) -> list:
        with self._lock:
            records = list(self.records[since:])
        return [r for r in records if method in (None, r["method"]) and route in (None, r["route"])]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def unused_url() -> str:
    """A URL of this machine with no listener: a request to it gets no connection."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}/"
