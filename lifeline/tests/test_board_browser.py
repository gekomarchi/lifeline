"""The built board in a real browser engine, opened from a `file://` URL.

The build and the page are the real ones. The nodes are a local JSON-RPC server (`RpcGate` in front of
`MockChain`): it holds a known fleet, it records each request of the page, and it can take a URL out of
service. The module is skipped without the build output or without a browser (Chrome or Chromium on the
path, or `LIFELINE_CDP_URL` for a running one).
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from lifeline import board, ops
from lifeline.tests.browser import Browser, RpcGate, find_browser, unused_url
from lifeline.tests.mocknode import MockChain

BUILT = (ops.ARTIFACTS / "LifelineHub.sol" / "LifelineHub.json").exists()
pytestmark = pytest.mark.skipif(
    not BUILT or not find_browser(),
    reason="the browser tests of the board need `forge build` and Chrome or Chromium",
)

LASNA, BASE, UNI = "lasna", "base-sepolia", "unichain-sepolia"
ETHER = 10**18
READ_METHODS = {"eth_chainId", "eth_blockNumber", "eth_call", "eth_getBalance", "eth_getLogs", "eth_getBlockByNumber"}

FIELDS = ("Object.fromEntries([...document.querySelectorAll('[data-field]')]"
          ".map((element) => [element.dataset.field, element.dataset.raw]))")
IDLE = "document.body.dataset.phase === 'idle'"


def address(tag: str) -> str:
    return "0x" + tag.rjust(40, "0")


def word(value) -> str:
    if isinstance(value, str):
        return value[2:].lower().rjust(64, "0")
    return format(int(value), "064x")


def words(*values) -> str:
    return "0x" + "".join(word(value) for value in values)


HUB, LASNA_LEDGER = address("a0"), "0x" + "88" * 20
VAULTS = {BASE: address("b0"), UNI: address("c0")}
LEDGERS = {LASNA: LASNA_LEDGER, BASE: address("b1"), UNI: address("c1")}
CHAIN_IDS = {LASNA: 5318007, BASE: 84532, UNI: 1301}
HEADS = {LASNA: 500_000, BASE: 900_000, UNI: 40_000}
# Lasna and Unichain: the window starts at the head minus the lookback. Base: it starts at deployBlock.
DEPLOY = {LASNA: 400_000, BASE: 895_000, UNI: 1_000}

T_ACTIVE, T_LASNA_Q = address("d1"), address("d2")          # Lasna
T_VAULT_Q, T_DISARMED, T_UNLISTED_LABEL = address("e1"), address("e2"), address("e3")  # Base
T_PARKED, T_LOW = address("f1"), address("f2")              # Unichain
STRANGER = address("99")

POLICY = (5 * 10**16, 25 * 10**16, 25 * 10**16, 5 * 10**17, 2, 3600)
NO_POLICY = (0, 0, 0, 0, 0, 0)


class Fleet:
    """A deployment on three mock chains, its manifest, and the built page."""

    def __init__(self, directory, first_url=None, lookback=None):
        self.dir = directory
        self.abi = board.abi_fragments()
        self.chains = {
            LASNA: MockChain(CHAIN_IDS[LASNA], HEADS[LASNA], genesis_time=1_790_000_000, block_time=1),
            BASE: MockChain(CHAIN_IDS[BASE], HEADS[BASE], genesis_time=1_789_000_000, block_time=2),
            UNI: MockChain(CHAIN_IDS[UNI], HEADS[UNI], genesis_time=1_790_400_000, block_time=1),
        }
        self.hooks = {}  # chain -> function (method, params) -> an outcome in place of the answer of the chain, or None
        self.forks = {name: 0 for name in self.chains}  # a new number is a new history: each block has a new hash
        self.gate = RpcGate({name: self._handle(name) for name in self.chains})
        self.lists = {name: [] for name in self.chains}
        self.expected = {}
        self.enrollments = []
        self._fill()
        self.rpc = {name: [self.gate.url(name, "/a"), self.gate.url(name, "/b")] for name in self.chains}
        for name, url in (first_url or {}).items():
            self.rpc[name][0] = url
        self.manifest = directory / "manifest.json"
        self.out = directory / "index.html"
        self.build(lookback)

    def _handle(self, name):
        chain = self.chains[name]

        def handle(method, params):
            outcome = self.hooks[name](method, params) if name in self.hooks else None
            if outcome is None:
                outcome = chain.handle(method, params)
            if method == "eth_getBlockByNumber" and outcome[0] == "result" and outcome[1]:
                seed = f"{name}:{self.forks[name]}:{outcome[1]['number']}".encode()
                outcome[1]["hash"] = "0x" + hashlib.sha256(seed).hexdigest()
            return outcome

        return handle

    # Chain content ----------------------------------------------------------------------------

    def call(self, chain, source, to, function, args, *result):
        fragment = self.abi[source]["functions"][function]
        data = fragment["selector"] + "".join(word(arg) for arg in args)
        self.chains[chain].calls[(to.lower(), data.lower())] = words(*result)

    def ledger(self, chain, account, debt, reserves):
        self.call(chain, "ledger", LEDGERS[chain], "debt", [account], debt)
        self.call(chain, "ledger", LEDGERS[chain], "reserves", [account], reserves)

    def funds(self, chain, principal, float_, debt=0, paused=False, floor=10**15, target=2 * 10**15, cron=True,
              up=True, pending=0):
        contract = HUB if chain == LASNA else VAULTS[chain]
        source = "hub" if chain == LASNA else "vault"
        self.chains[chain].balances[contract.lower()] = principal
        self.ledger(chain, contract, debt, float_)
        self.call(chain, source, contract, "paused", [], paused)
        self.call(chain, source, contract, "principalFloor", [], floor)
        self.call(chain, source, contract, "selfReserveTarget", [], target)
        values = {"principal": principal, "float": float_, "debt": debt, "floor": floor, "floatTarget": target,
                  "paused": paused}
        if chain == LASNA:
            self.call(chain, "hub", contract, "cronSubscribed", [], cron)
            values["cron"] = cron
        else:
            self.call(LASNA, "hub", HUB, "vaultInfo", [CHAIN_IDS[chain]], LEDGERS[chain], contract, 220_000, up, pending)
            values.update(up=up, pending=pending)
        for key, value in values.items():
            self.expected[f"funds:{chain}.{key}"] = str(value).lower() if isinstance(value, bool) else str(value)

    def target(self, chain, target, state, debt=0, reserves=0, listed=True, policy=POLICY, vault_policy=True,
               quarantined=False, label=None, removed=False):
        """One target: its record at the hub, its ledger values, and its record at the vault of its chain."""
        hub_policy = policy if chain == LASNA else (0, 0, 0, 0, policy[4], policy[5])
        self.call(LASNA, "hub", HUB, "targetInfo", [CHAIN_IDS[chain], target],
                  1 if chain == LASNA else 2, ops.TARGET_STATES.index(state), *hub_policy)
        self.ledger(chain, target, debt, reserves)
        marks = policy if chain == LASNA and state != "None" else None
        if chain != LASNA:
            self.call(chain, "vault", VAULTS[chain], "hasPolicy", [target], vault_policy)
            self.call(chain, "vault", VAULTS[chain], "policyOf", [target], *(policy if vault_policy else NO_POLICY))
            self.call(chain, "vault", VAULTS[chain], "quarantined", [target], quarantined)
            marks = policy if vault_policy else None
        if listed and target not in self.lists[chain]:
            self.lists[chain].append(target)
        if label:
            self.enrollments = [e for e in self.enrollments if e["target"] != target]
            self.enrollments.append({"label": label, "chain": chain, "chainId": CHAIN_IDS[chain], "target": target,
                                     "policy": {}, "guest": False, "removed": {"hubDisarm": None} if removed else None})
        base = f"target:{chain}|{target}"
        self.expected.update({
            f"{base}.state": state, f"{base}.debt": str(debt), f"{base}.reserves": str(reserves),
            f"{base}.low": str(marks[0]) if marks else "", f"{base}.high": str(marks[1]) if marks else "",
            f"{base}.vaultQuarantine": "" if chain == LASNA else str(quarantined).lower(),
        })
        self.publish_lists()

    def publish_lists(self):
        for chain, targets in self.lists.items():
            self.call(LASNA, "hub", HUB, "targetCount", [CHAIN_IDS[chain]], len(targets))
            for index, target in enumerate(targets):
                self.call(LASNA, "hub", HUB, "targetAt", [CHAIN_IDS[chain], index], target)

    def log(self, chain, source, event, block, indexed=(), data=(), emitter=None):
        fragment = self.abi[source]["events"][event]
        emitter = emitter or (HUB if source == "hub" else VAULTS[chain] if source == "vault" else LEDGERS[chain])
        return self.chains[chain].add_log(emitter, [fragment["topic"], *("0x" + word(v) for v in indexed)], block, words(*data))

    def _fill(self):
        self.funds(LASNA, principal=4 * ETHER, float_=175 * 10**15, floor=5 * 10**16, target=2 * 10**17)
        self.funds(BASE, principal=2 * 10**15, float_=2 * 10**14, floor=2 * 10**14, target=2 * 10**14)
        self.funds(UNI, principal=5 * 10**15, float_=5 * 10**13, floor=2 * 10**14, target=2 * 10**14, pending=2)
        self.target(LASNA, T_ACTIVE, "Active", reserves=10**17, label="lasna active")
        self.target(LASNA, T_LASNA_Q, "Quarantined", debt=11 * 10**15, label="lasna quarantined")
        # Quarantined on the vault, and the hub still has it in its sweep list (count change 8).
        self.target(BASE, T_VAULT_Q, "Active", debt=3 * 10**12, quarantined=True, label="base vault-quarantined")
        # Removed with the `remove` command: no list entry, the hub keeps the record, the vault has no policy.
        self.target(BASE, T_DISARMED, "Disarmed", reserves=5 * 10**13, listed=False, vault_policy=False,
                    label="base removed", removed=True)
        # In the hub list, and the manifest has no enrollment of it.
        self.target(BASE, T_UNLISTED_LABEL, "Active", reserves=3 * 10**17)
        self.target(UNI, T_PARKED, "Parked", reserves=10**17, label="uni parked")
        self.target(UNI, T_LOW, "Active", reserves=10**16, label="uni below low")

        h = HEADS
        self.log(LASNA, "hub", "HubLow", h[LASNA] - 10, data=[10**16, 5 * 10**16])
        self.log(LASNA, "hub", "Rescued", h[LASNA] - 2_000, [T_ACTIVE], [7 * 10**15, 25 * 10**16])
        self.log(LASNA, "hub", "Gap", h[LASNA] - 2_000, [T_ACTIVE], [h[LASNA] - 2_600, h[LASNA] - 2_000])
        self.log(LASNA, "hub", "Rescued", h[LASNA] - 29_999, [T_LASNA_Q], [10**15, 10**16])
        # Older than the lookback of 30,000 blocks, and still after deployBlock: not in the window.
        self.log(LASNA, "hub", "HubLow", h[LASNA] - 30_500, data=[1, 2])
        self.log(BASE, "vault", "VaultLow", h[BASE] - 100, data=[10**14, 2 * 10**14])
        self.log(BASE, "vault", "Result", h[BASE] - 4_000, [(1 << 128) + 7, T_VAULT_Q], [ops.OUTCOMES.index("Quarantined"), 0])
        self.log(BASE, "vault", "Result", h[BASE] - 4_500, [h[LASNA] - 9_000, T_UNLISTED_LABEL], [ops.OUTCOMES.index("ToppedUp"), 10**14])
        self.log(UNI, "vault", "Result", h[UNI] - 20_000, [0, T_LOW], [ops.OUTCOMES.index("NoNeed"), 0])
        self.failures(BASE, VAULTS[BASE], 3)
        self.failures(BASE, STRANGER, 5)  # a contract of another owner: not in a count of the page

    def failures(self, chain, recipient, count, first_block=None):
        """`CallbackFailure(recipient, payload)` logs of the proxy: the junk callbacks of a caller."""
        first_block = first_block or HEADS[chain] - 3_000
        for index in range(count):
            self.log(chain, "ledger", "CallbackFailure", first_block + index, [recipient], [0x20, 4, 0xDEADBEEF << 224])

    # Build and page ---------------------------------------------------------------------------

    def build(self, lookback=None):
        self.manifest.write_text(json.dumps({
            "version": 1, "cronTopic": "Cron10000",
            "chains": {name: {"chainId": CHAIN_IDS[name], "ledger": LEDGERS[name], "rpc": self.rpc[name]}
                       for name in (LASNA, UNI, BASE)},
            "contracts": {
                "hub": {"chain": LASNA, "chainId": CHAIN_IDS[LASNA], "address": HUB, "deployBlock": DEPLOY[LASNA],
                        "deployTx": "0x" + "11" * 32},
                "vaults": {name: {"chainId": CHAIN_IDS[name], "address": VAULTS[name], "deployBlock": DEPLOY[name],
                                  "deployTx": "0x" + "22" * 32} for name in (UNI, BASE)},
            },
            "enrollments": self.enrollments,
        }))
        board.build(self.manifest, self.out, **({"lookback": lookback} if lookback else {}))

    def url(self, **settings) -> str:
        fragment = "&".join(f"{key}={value}" for key, value in {"auto": 0, **settings}.items())
        return f"{self.out.resolve().as_uri()}#{fragment}"

    def rpc_urls(self) -> set:
        return {url for urls in self.rpc.values() for url in urls}

    def close(self):
        self.gate.close()


@pytest.fixture(scope="module")
def browser():
    instance = Browser()
    yield instance
    instance.close()


@pytest.fixture
def fleet(tmp_path):
    instance = Fleet(tmp_path)
    yield instance
    instance.close()


@pytest.fixture
def page(browser):
    instance = browser.page()
    yield instance
    errors = list(instance.errors)
    instance.close()
    # A failed request is in the console as a network error; an exception of the page script is not allowed.
    assert not [error for error in errors if "Failed to load resource" not in error and "ERR_" not in error], errors


def opened(page, fleet, **settings) -> dict:
    """Opens the built file and returns the values of its fields after each read ended."""
    url = fleet.url(**settings)
    page.goto(url)
    page.wait(f"location.href === {json.dumps(url)} && {IDLE}")
    return page.evaluate(FIELDS)


def reloaded(page, fleet, chain) -> int:
    """Loads the file again and returns the mark of the gate before it. Waits for the reads of the new page."""
    mark = fleet.gate.mark()
    page.call("Page.reload")
    deadline = time.monotonic() + 30
    while not fleet.gate.calls(route=chain, since=mark):
        assert time.monotonic() < deadline, "the page sent no request after the reload"
        time.sleep(0.05)
    page.wait(f"{IDLE} && document.querySelector('#chains-body tr[data-chain=\"{chain}\"]').dataset.state !== 'loading'")
    return mark


def set_rpc(page, chain, text) -> None:
    """Opens the RPC settings of a chain row, and types a URL into its field."""
    page.evaluate(f"(() => {{ if (document.getElementById('chain-detail-{chain}').hidden) "
                  f"document.querySelector('#chains-body button[data-fk=\"cexp:{chain}\"]').click(); }})()")
    page.wait(f"!document.getElementById('chain-detail-{chain}').hidden")
    page.type_into(f"#rpc-{chain}", text)


def choose(page, select, value) -> None:
    page.evaluate(f"(() => {{ const field = document.getElementById({json.dumps(select)}); field.value = {json.dumps(value)}; "
                  f"field.dispatchEvent(new Event('change', {{ bubbles: true }})); }})()")


def state_of(page, chain) -> str:
    return page.evaluate(f"document.querySelector('#chains-body tr[data-chain=\"{chain}\"]').dataset.state")


def log_queries(fleet, chain, since=0) -> list:
    return [record["params"][0] for record in fleet.gate.calls("eth_getLogs", chain, since)]


def window_of(page, chain) -> tuple:
    found = page.evaluate(f"(() => {{ const e = document.querySelector('[data-field=\"window:{chain}\"]');"
                          f" return [e.dataset.from, e.dataset.to, e.dataset.phase]; }})()")
    return int(found[0]), int(found[1]), found[2]


# ----------------------------------------------------------------------------------------------
# Equal to RPC, self-contained
# ----------------------------------------------------------------------------------------------


def test_each_number_is_the_value_of_the_node_at_the_block_that_the_page_shows(fleet, page):
    fields = opened(page, fleet)
    for chain, head in HEADS.items():
        assert fields[f"block:{chain}"] == str(head)
        assert state_of(page, chain) == "ok"
    for field, value in fleet.expected.items():
        assert fields[field] == value, field
    # Each state read names the block that the page shows: no read at "latest".
    for chain, head in HEADS.items():
        for method in ("eth_call", "eth_getBalance"):
            reads = fleet.gate.calls(method, chain)
            assert reads and all(read["params"][1] == hex(head) for read in reads), (chain, method)
    # The short form of a number is its value, cut and not rounded up.
    assert page.evaluate("document.querySelector('[data-field=\"funds:lasna.float\"]').textContent") == "0.175lREACT"
    assert page.evaluate("document.querySelector('[data-field=\"funds:lasna.float\"]').title") \
        == "0.175 lREACT (175000000000000000 wei)"


def test_the_page_loads_itself_and_sends_requests_only_to_the_rpc_urls(fleet, page):
    opened(page, fleet)
    documents = [request for request in page.requests if request["url"].startswith("file:")]
    assert [request["url"].split("#")[0] for request in documents] == [fleet.out.resolve().as_uri()]
    others = [request for request in page.requests if not request["url"].startswith("file:")]
    assert others and {request["url"] for request in others} <= fleet.rpc_urls()
    assert {request["method"] for request in others} <= {"POST", "OPTIONS"}
    # Read methods only: no transaction, no account request.
    assert {record["method"] for record in fleet.gate.calls()} <= READ_METHODS
    # A healthy first URL gets each request: the second one is not in use.
    assert {record["path"] for record in fleet.gate.calls()} == {f"{name}/a" for name in HEADS}


def test_the_page_has_no_wallet_control(fleet, page):
    opened(page, fleet)
    controls = page.evaluate("[...document.querySelectorAll('button, input, select, a')].map((e) => "
                             "(e.id || e.getAttribute('aria-label') || e.textContent).trim())")
    assert controls and not [text for text in controls
                             if any(word in text.lower() for word in ("connect", "wallet", "sign", "enroll", "send"))]


# ----------------------------------------------------------------------------------------------
# Fallback, error state, override
# ----------------------------------------------------------------------------------------------


def test_first_url_unreachable_the_values_come_from_the_second_one(tmp_path, page):
    fleet = Fleet(tmp_path, first_url={LASNA: unused_url()})
    try:
        fleet.gate.down.add(f"{BASE}/a")  # one more kind of failure: an answer that a page cannot read
        fields = opened(page, fleet)
        assert state_of(page, LASNA) == "fallback" and state_of(page, BASE) == "fallback" and state_of(page, UNI) == "ok"
        for field, value in fleet.expected.items():
            assert fields[field] == value, field
        assert fields[f"rpc:{LASNA}"] == fleet.rpc[LASNA][1] and fields[f"rpc:{BASE}"] == fleet.rpc[BASE][1]
        assert {record["path"] for record in fleet.gate.calls(route=LASNA)} == {f"{LASNA}/b"}
        assert {record["path"] for record in fleet.gate.calls(route=BASE)} == {f"{BASE}/b"}
        row = page.evaluate(f"document.querySelector('#chains-body tr[data-chain=\"{LASNA}\"]').innerText")
        assert "Fallback RPC" in row and "URL 2 of 2" in row
        # The reason of the first URL is in the settings of the row.
        page.click(f"#chains-body button[data-fk=\"cexp:{LASNA}\"]")
        detail = page.wait(f"(document.getElementById('chain-detail-{LASNA}').innerText.match(/in use[^]*/) || {{}}).input")
        assert "Failed" in detail and "no answer: the URL is not reachable" in detail and "in use" in detail
        assert "Fallback RPC: lasna, base-sepolia" in page.evaluate("document.getElementById('verdict').innerText")
    finally:
        fleet.close()


def chain_fields(fields: dict, chain: str) -> dict:
    """The fields that hold a number of one chain: its block, its funds, its targets, its window, its counts."""
    return {field: value for field, value in fields.items()
            if field.split(".")[0].split("|")[0] in (f"block:{chain}", f"funds:{chain}", f"target:{chain}",
                                                     f"window:{chain}", f"failures:{chain}", f"rpc:{chain}")
            and not field.endswith((".address", ".state"))}


def test_all_urls_of_one_chain_fail_that_chain_shows_an_error_and_no_number(fleet, page):
    fleet.gate.down.update({f"{BASE}/a", f"{BASE}/b"})
    fields = opened(page, fleet)
    assert state_of(page, BASE) == "error" and state_of(page, LASNA) == "ok" and state_of(page, UNI) == "ok"
    assert chain_fields(fields, BASE) and set(chain_fields(fields, BASE).values()) == {""}
    alert = page.evaluate("document.querySelector('#chains-body tr.alert:not([hidden])').innerText")
    assert f"No RPC URL of {BASE} answered. The page shows no number of this chain." in alert
    # The other chains have each value. A Base target keeps the state that the hub gave, and no number.
    for field, value in fleet.expected.items():
        if not chain_fields({field: value}, BASE) and not field.startswith(f"funds:{BASE}"):
            assert fields[field] == value, field
    assert fields[f"target:{BASE}|{T_VAULT_Q}.state"] == "Active"
    assert not page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{BASE}\"]')].length")
    assert page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{LASNA}\"]')].length") == 4
    assert "No answer: base-sepolia" in page.evaluate("document.getElementById('verdict').innerText")
    # No number of the chain is in the text of its rows either.
    funds = page.evaluate(f"document.querySelector('#funds-body tr[data-funds=\"{BASE}\"]').innerText")
    shown_address = VAULTS[BASE][:6] + "\u2026" + VAULTS[BASE][-4:]
    assert "no data" in funds and not any(character.isdigit() for character in funds.lower().replace(shown_address, ""))


def test_a_hub_with_no_answer_keeps_the_targets_and_values_of_the_other_chains(fleet, page):
    before = opened(page, fleet)
    unlisted = f"target:{BASE}|{T_UNLISTED_LABEL}"  # in the hub list, and not in the manifest
    assert before[f"{unlisted}.reserves"] == str(3 * 10**17)
    fleet.gate.down.update({f"{LASNA}/a", f"{LASNA}/b"})
    page.click("#refresh")
    page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{LASNA}\"]').dataset.state === 'error'")
    page.wait(IDLE)
    after = page.evaluate(FIELDS)
    assert state_of(page, BASE) == "ok" and state_of(page, UNI) == "ok"
    assert set(chain_fields(after, LASNA).values()) == {""}
    # Each destination target keeps its row and the values of its own chain. The hub state is not known.
    for field, value in fleet.expected.items():
        if field.startswith((f"target:{BASE}", f"target:{UNI}", f"funds:{BASE}", f"funds:{UNI}")):
            hub_value = field.endswith((".state", ".up", ".pending"))
            assert after.get(field, "") == ("" if hub_value else value), field
    row = page.evaluate(f"document.querySelector('#targets-body tr[data-target=\"{BASE}|{T_UNLISTED_LABEL}\"]').innerText")
    assert "Hub state not known" in row and "0.3" in row
    assert page.evaluate(f"document.querySelectorAll('#events-body tr[data-chain=\"{BASE}\"]').length") == 3
    # The proxy query still names the target of the last hub list.
    proxy = [q for q in log_queries(fleet, BASE) if q["address"].lower() == LEDGERS[BASE]][-1]
    assert "0x" + word(T_UNLISTED_LABEL) in proxy["topics"][1]


def test_a_chain_that_stops_does_not_keep_its_last_values(fleet, page):
    fields = opened(page, fleet)
    assert fields[f"funds:{UNI}.principal"] == fleet.expected[f"funds:{UNI}.principal"]
    assert page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{UNI}\"]')].length") == 1
    fleet.gate.down.update({f"{UNI}/a", f"{UNI}/b"})
    page.click("#refresh")
    page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{UNI}\"]').dataset.state === 'error'")
    page.wait(IDLE)
    fields = page.evaluate(FIELDS)
    assert set(chain_fields(fields, UNI).values()) == {""}
    assert not page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{UNI}\"]')].length")
    assert str(HEADS[UNI]) not in page.evaluate("document.body.innerText").replace(",", "")
    # The chain answers again: the next read shows its values again.
    fleet.gate.down.clear()
    page.click(f"#chains-body button[data-fk=\"retry:{UNI}\"]")
    page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{UNI}\"]').dataset.state === 'ok'")
    page.wait(IDLE)
    assert page.evaluate(FIELDS)[f"funds:{UNI}.principal"] == fleet.expected[f"funds:{UNI}.principal"]


def test_an_override_takes_each_request_of_its_chain(fleet, page):
    opened(page, fleet)
    other = fleet.gate.url(LASNA, "/own")
    mark = fleet.gate.mark()
    set_rpc(page, LASNA, other)
    page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{LASNA}\"]').dataset.state === 'override'")
    page.wait(IDLE)
    after = fleet.gate.calls(route=LASNA, since=mark)
    assert after and {record["path"] for record in after} == {f"{LASNA}/own"}
    assert {"eth_chainId", "eth_blockNumber", "eth_call", "eth_getBalance", "eth_getLogs"} <= {r["method"] for r in after}
    fields = page.evaluate(FIELDS)
    assert fields[f"rpc:{LASNA}"] == other and fields[f"funds:{LASNA}.principal"] == fleet.expected[f"funds:{LASNA}.principal"]
    assert "rpc.lasna=" in page.evaluate("location.hash")
    row = page.evaluate(f"document.querySelector('#chains-body tr[data-chain=\"{LASNA}\"]').innerText")
    assert "Override" in row and "set in this page" in row

    # A reload keeps the override: the first request of the chain goes to it.
    mark = reloaded(page, fleet, LASNA)
    assert state_of(page, LASNA) == "override"
    assert {record["path"] for record in fleet.gate.calls(route=LASNA, since=mark)} == {f"{LASNA}/own"}

    # An override that does not answer has no fallback to the list: the chain shows its error.
    fleet.gate.down.add(f"{LASNA}/own")
    page.click("#refresh")
    page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{LASNA}\"]').dataset.state === 'error'")
    page.wait(IDLE)
    assert set(chain_fields(page.evaluate(FIELDS), LASNA).values()) == {""}

    # "Clear" goes back to the list of the manifest.
    mark = fleet.gate.mark()
    page.click(f"#chain-detail-{LASNA} form button[type=button]")
    page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{LASNA}\"]').dataset.state === 'ok'")
    page.wait(IDLE)
    assert {record["path"] for record in fleet.gate.calls(route=LASNA, since=mark)} == {f"{LASNA}/a"}
    assert "rpc.lasna" not in page.evaluate("location.hash")


def test_an_override_for_another_chain_is_refused(fleet, page):
    opened(page, fleet)
    set_rpc(page, UNI, fleet.gate.url(BASE, "/a"))
    page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{UNI}\"]').dataset.state === 'error'")
    alert = page.evaluate("document.querySelector('#chains-body tr.alert:not([hidden])').innerText")
    assert f"the URL answers for chain {CHAIN_IDS[BASE]}, and the manifest has {CHAIN_IDS[UNI]}" in alert
    assert set(chain_fields(page.evaluate(FIELDS), UNI).values()) == {""}
    # A text that is not a URL changes nothing.
    set_rpc(page, BASE, "not a url")
    assert page.evaluate(f"document.getElementById('rpc-{BASE}').getAttribute('aria-invalid')") == "true"
    assert state_of(page, BASE) == "ok"


def test_a_node_that_refuses_a_large_batch_gets_smaller_ones(fleet, page):
    fleet.gate.max_batch.update({BASE: 6, UNI: 0})  # Base: 6 calls for each POST. Unichain: no batch.
    fields = opened(page, fleet)
    for chain in HEADS:
        assert state_of(page, chain) == "ok"
    for field, value in fleet.expected.items():
        assert fields[field] == value, field
    sizes = {chain: {count for route, count in fleet.gate.posts if route == chain} for chain in HEADS}
    assert 1 < max(sizes[BASE]) <= 6 and sizes[UNI] == {1} and max(sizes[LASNA]) > 6
    assert window_of(page, BASE)[2] == "done" and window_of(page, UNI)[2] == "done"


# ----------------------------------------------------------------------------------------------
# Log bounds and events
# ----------------------------------------------------------------------------------------------


def check_bounds(fleet, page, chain, lookback, since=0, chunk=board.CHUNK_BLOCKS):
    head = HEADS[chain]
    start = max(DEPLOY[chain], head - lookback)
    queries = log_queries(fleet, chain, since)
    assert queries, chain
    for query in queries:
        low, high = int(query["fromBlock"], 16), int(query["toBlock"], 16)
        assert start <= low <= high <= head, (chain, low, high)
        assert high - low + 1 <= chunk, (chain, low, high)
    assert window_of(page, chain) == (start, head, "done")
    by_filter = {}
    for query in queries:
        key = json.dumps([query["address"], query["topics"]])
        by_filter.setdefault(key, []).append((int(query["fromBlock"], 16), int(query["toBlock"], 16)))
    chunks, high = [], head
    while high >= start:
        chunks.append((max(start, high - chunk + 1), high))
        high = chunks[-1][0] - 1
    for ranges in by_filter.values():
        # Each chunk one time. The page sends three requests at a time, from the newest chunk down.
        assert sorted(ranges) == sorted(chunks), (chain, ranges)
        assert chunks[0] in ranges[:3], (chain, ranges)
    return queries


def test_log_queries_stay_inside_the_window_and_inside_the_chunk(fleet, page):
    opened(page, fleet)
    for chain in HEADS:
        queries = check_bounds(fleet, page, chain, board.DEFAULT_LOOKBACK)
        contract = HUB if chain == LASNA else VAULTS[chain]
        assert {query["address"].lower() for query in queries} == ({contract} if chain == LASNA else {contract, LEDGERS[chain]})
    # Base: the window starts at deployBlock, and the page says so.
    assert window_of(page, BASE)[0] == DEPLOY[BASE]
    assert "from deployBlock" in page.evaluate(f"document.querySelector('#chains-body tr[data-chain=\"{BASE}\"]').innerText")
    row = page.evaluate(f"document.querySelector('#chains-body tr[data-chain=\"{LASNA}\"]').innerText")
    assert "470,000 \u2013 500,000" in row and "30,001 blocks" in row and "from deployBlock" not in row
    # The proxy query names the recipients: the vault and the targets of the chain, no other contract.
    proxy = [query for query in log_queries(fleet, BASE) if query["address"].lower() == LEDGERS[BASE]]
    assert proxy[0]["topics"] == [[fleet.abi["ledger"]["events"]["CallbackFailure"]["topic"]],
                                  ["0x" + word(a) for a in (VAULTS[BASE], T_VAULT_Q, T_DISARMED, T_UNLISTED_LABEL)]]


def test_the_lookback_of_the_page_changes_the_window(fleet, page):
    opened(page, fleet)
    assert page.evaluate("[...document.querySelectorAll('#events-body tr[data-chain=\"lasna\"]')].length") == 4
    mark = fleet.gate.mark()
    page.type_into("#lookback", "2500")
    page.wait(f"document.querySelector('[data-field=\"window:{LASNA}\"]').dataset.from === '{HEADS[LASNA] - 2500}'")
    page.wait(IDLE)
    for chain in HEADS:
        check_bounds(fleet, page, chain, 2500, since=mark)
    # The state is not read again for a new window, and the events of the old window are gone.
    assert not fleet.gate.calls("eth_call", since=mark)
    assert page.evaluate("[...document.querySelectorAll('#events-body tr[data-chain=\"lasna\"]')].map((r) => r.dataset.event)") \
        == ["HubLow", "Gap", "Rescued"]
    assert "lookback=2500" in page.evaluate("location.hash")
    # A value that is not a block count changes nothing.
    mark = fleet.gate.mark()
    page.type_into("#lookback", "0")
    assert page.evaluate("document.getElementById('lookback').getAttribute('aria-invalid')") == "true"
    assert not fleet.gate.calls(since=mark) and window_of(page, LASNA)[0] == HEADS[LASNA] - 2500


def test_the_default_lookback_of_the_build_and_of_the_url(tmp_path, page):
    fleet = Fleet(tmp_path, lookback=700)
    try:
        opened(page, fleet)
        for chain in HEADS:
            check_bounds(fleet, page, chain, 700)
        # A new fragment of the URL is a new setting of the open page; a reload keeps it.
        mark = fleet.gate.mark()
        page.goto(fleet.url(lookback=50_000))
        page.wait(f"document.querySelector('[data-field=\"window:{UNI}\"]').dataset.from === '{DEPLOY[UNI]}'")
        page.wait(IDLE)
        for chain in HEADS:
            check_bounds(fleet, page, chain, 50_000, since=mark)
        assert page.evaluate("document.getElementById('lookback').value") == "50,000"
        mark = reloaded(page, fleet, UNI)
        for chain in HEADS:
            check_bounds(fleet, page, chain, 50_000, since=mark)
    finally:
        fleet.close()


def test_a_node_with_a_smaller_log_range_gets_smaller_requests(fleet, page):
    fleet.chains[UNI].max_range = 1_000
    opened(page, fleet)
    queries = log_queries(fleet, UNI)
    refused = [query for query in queries if int(query["toBlock"], 16) - int(query["fromBlock"], 16) + 1 > 1_000]
    assert refused and all(int(q["toBlock"], 16) - int(q["fromBlock"], 16) + 1 <= board.CHUNK_BLOCKS for q in refused)
    assert window_of(page, UNI) == (HEADS[UNI] - board.DEFAULT_LOOKBACK, HEADS[UNI], "done")
    assert page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{UNI}\"]')].length") == 1
    # The next read asks only for new blocks, with the range that the node takes.
    fleet.chains[UNI].head += 50
    mark = fleet.gate.mark()
    page.click("#refresh")
    page.wait(f"document.querySelector('[data-field=\"block:{UNI}\"]').dataset.raw === '{HEADS[UNI] + 50}'")
    page.wait(IDLE)
    again = log_queries(fleet, UNI, mark)
    assert {(int(q["fromBlock"], 16), int(q["toBlock"], 16)) for q in again} == {(HEADS[UNI] + 1, HEADS[UNI] + 50)}
    assert window_of(page, UNI) == (HEADS[UNI] + 50 - board.DEFAULT_LOOKBACK, HEADS[UNI] + 50, "done")


def test_log_queries_that_each_node_refuses_leave_the_state_and_no_event_number(fleet, page):
    fleet.hooks[BASE] = lambda method, params: (("error", -32000, "the log index is not ready", None)
                                                if method == "eth_getLogs" else None)
    fields = opened(page, fleet)
    assert state_of(page, BASE) == "ok" and fields[f"funds:{BASE}.principal"] == fleet.expected[f"funds:{BASE}.principal"]
    assert fields[f"failures:{BASE}|{VAULTS[BASE]}"] == ""
    assert not page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{BASE}\"]')].length")
    row = page.evaluate(f"document.querySelector('#chains-body tr[data-chain=\"{BASE}\"]').innerText")
    assert "Event queries failed" in row and "the log index is not ready" in row
    assert {record["path"] for record in fleet.gate.calls("eth_getLogs", BASE)} == {f"{BASE}/a", f"{BASE}/b"}


def test_one_refused_log_request_leaves_no_event_of_a_request_that_ends_later(fleet, page):
    """Three requests run at a time. One fails; an answer that comes after it adds no row to the cleared scan."""
    head, start = HEADS[LASNA], HEADS[LASNA] - board.DEFAULT_LOOKBACK
    fleet.log(LASNA, "hub", "HubLow", head - 15_000, data=[1, 2])

    def hook(method, params):
        if method != "eth_getLogs":
            return None
        low = int(params[0]["fromBlock"], 16)
        if low == start:  # the oldest chunk: one block
            return "error", -32000, "the log index is not ready", None
        if low == head - 20_000 + 1:  # the second chunk, with the new log: a slow answer
            time.sleep(1.5)
        return None

    fleet.hooks[LASNA] = hook
    opened(page, fleet)
    assert window_of(page, LASNA)[2] == "error" and state_of(page, LASNA) == "ok"
    time.sleep(2.5)
    assert window_of(page, LASNA)[2] == "error"
    assert not page.evaluate(f"document.querySelectorAll('#events-body tr[data-chain=\"{LASNA}\"]').length")
    assert "HubLow in the window" not in page.evaluate("document.getElementById('verdict').innerText")
    assert page.evaluate(FIELDS)[f"funds:{LASNA}.principal"] == fleet.expected[f"funds:{LASNA}.principal"]


def lasna_events(page) -> list:
    return page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{LASNA}\"]')]"
                         f".map((row) => [row.dataset.event, Number(row.dataset.block)])")


@pytest.mark.parametrize("blocks", [0, 7])
def test_a_reorganization_makes_the_page_read_the_window_again(fleet, page, blocks):
    """The page keeps the logs of a window only while the last block of that window has the same hash."""
    opened(page, fleet)
    head = HEADS[LASNA]
    assert (["HubLow", head - 10] in lasna_events(page)) and len(lasna_events(page)) == 4
    # Without a reorganization a read asks for no log of a block that the page has.
    mark = fleet.gate.mark()
    page.click("#refresh")
    page.wait(IDLE)
    assert fleet.gate.calls("eth_call", LASNA, mark) and log_queries(fleet, LASNA, mark) == []

    # Another history: the HubLow log is gone, at the same head or at a head that is 7 blocks later.
    chain = fleet.chains[LASNA]
    chain.logs = [log for log in chain.logs if int(log["blockNumber"], 16) != head - 10]
    chain.head += blocks
    chain.genesis_time += 3_600  # the blocks of the new history have other times
    fleet.forks[LASNA] += 1
    mark = fleet.gate.mark()
    page.click("#refresh")
    page.wait(f"!document.querySelector('#events-body tr[data-chain=\"{LASNA}\"][data-event=\"HubLow\"]')")
    page.wait(IDLE)
    # The window moved with the head: with 7 more blocks the oldest event is before its first block.
    assert lasna_events(page) == [["Gap", head - 2_000], ["Rescued", head - 2_000]] + (
        [] if blocks else [["Rescued", head - 29_999]])
    ranges = [(int(q["fromBlock"], 16), int(q["toBlock"], 16)) for q in log_queries(fleet, LASNA, mark)]
    assert min(low for low, _ in ranges) == head + blocks - board.DEFAULT_LOOKBACK
    assert max(high for _, high in ranges) == head + blocks
    assert "HubLow in the window" not in page.evaluate("document.getElementById('verdict').innerText")
    # Each row has the block time of the new history, not the time that the page had for that block number.
    shown = page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{LASNA}\"]')]"
                          f".map((row) => row.cells[0].textContent)")
    assert shown[0] == time.strftime("%b %d %H:%M:%S", time.gmtime(chain.timestamp(head - 2_000)))


def test_each_event_block_gets_its_time(fleet, page):
    """Many more event blocks than one batch of header requests: each row has a time, and the order is by time."""
    for index in range(230):
        fleet.log(UNI, "vault", "Result", HEADS[UNI] - 100 - 3 * index, [0, T_LOW], [ops.OUTCOMES.index("NoNeed"), 0])
    opened(page, fleet)
    rows = page.evaluate("[...document.querySelectorAll('#events-body tbody tr')].map((row) => "
                         "[row.dataset.chain, Number(row.dataset.block), row.cells[0].textContent])")
    assert len([row for row in rows if row[0] == UNI]) == 231
    assert all(text != "\u2014" and ":" in text for _, _, text in rows)
    times = [fleet.chains[name].timestamp(block) for name, block, _ in rows]
    assert times == sorted(times, reverse=True)
    assert "a block time is not known" not in page.evaluate("document.getElementById('events-meta').textContent")


def test_events_are_newest_first_with_their_values(fleet, page):
    opened(page, fleet)
    rows = page.evaluate("[...document.querySelectorAll('#events-body tbody tr')].map((row) => "
                         "[row.dataset.chain, row.dataset.event, Number(row.dataset.block), row.dataset.outcome, row.innerText])")
    for chain in HEADS:
        blocks = [block for name, _, block, _, _ in rows if name == chain]
        assert blocks == sorted(blocks, reverse=True)
    # Each chain has a block time here, so the rows of all chains are in the order of time.
    by_time = [fleet.chains[name].timestamp(block) for name, _, block, _, _ in rows]
    assert by_time == sorted(by_time, reverse=True)
    assert [(name, event, block) for name, event, block, _, _ in rows if name == LASNA] == [
        (LASNA, "HubLow", HEADS[LASNA] - 10), (LASNA, "Gap", HEADS[LASNA] - 2_000),
        (LASNA, "Rescued", HEADS[LASNA] - 2_000), (LASNA, "Rescued", HEADS[LASNA] - 29_999)]
    assert [(event, outcome) for name, event, _, outcome, _ in rows if name == BASE] == [
        ("VaultLow", ""), ("Result", "Quarantined"), ("Result", "ToppedUp")]
    text = {(name, event, block): text for name, event, block, _, text in rows}
    assert "base vault-quarantined" in text[(BASE, "Result", HEADS[BASE] - 4_000)]
    assert "request 7" in text[(BASE, "Result", HEADS[BASE] - 4_000)]
    assert f"sweep of tick {HEADS[LASNA] - 9_000:,}" in text[(BASE, "Result", HEADS[BASE] - 4_500)]
    assert "setPolicy" in text[(UNI, "Result", HEADS[UNI] - 20_000)]
    assert "600 blocks" in text[(LASNA, "Gap", HEADS[LASNA] - 2_000)] and "lasna active" in text[(LASNA, "Gap", HEADS[LASNA] - 2_000)]
    verdict = page.evaluate("document.getElementById('verdict').innerText")
    assert "HubLow in the window: 1" in verdict and "VaultLow in the window: 1" in verdict
    # A filter is a button: one type, one chain.
    page.click("#events-type button[data-fk=\"etype:Result\"]")
    page.wait("document.querySelectorAll('#events-body tbody tr').length === 3")
    choose(page, "events-chain", UNI)
    page.wait("document.querySelectorAll('#events-body tbody tr').length === 1")


# ----------------------------------------------------------------------------------------------
# Target states, diagnostics
# ----------------------------------------------------------------------------------------------


def target_row(page, chain, target) -> dict:
    return page.evaluate(f"(() => {{ const row = document.querySelector('#targets-body tr[data-target=\"{chain}|{target}\"]');"
                         f" return {{ text: row.innerText, sev: row.dataset.sev, state: row.dataset.state,"
                         f" notes: [...row.querySelectorAll('[data-note]')].map((n) => n.textContent) }}; }})()")


def test_a_target_quarantined_on_the_vault_and_in_the_hub_list_has_the_disarm_note(fleet, page):
    opened(page, fleet)
    row = target_row(page, BASE, T_VAULT_Q)
    assert "Quarantined on the vault" in row["text"] and "Active on the hub" in row["text"]
    assert row["notes"] == ["Still in the hub sweep list. hub.disarm stops its sweep cost."]
    assert row["state"] == "Active" and row["sev"] == "3"  # it is in debt too
    # No other row has the note: not the Lasna target that the hub quarantined, not a healthy one.
    assert page.evaluate("document.querySelectorAll('#targets-body [data-note]').length") == 1
    lasna = target_row(page, LASNA, T_LASNA_Q)
    assert "Quarantined" in lasna["text"] and "hub.release" in lasna["text"] and "hub.disarm" not in lasna["text"]
    assert "1 target quarantined on a vault and still swept by the hub" in page.evaluate("document.getElementById('verdict').innerText")
    # The count is before the noun, so a plural reads "2 targets in debt" and not "2 target in debts".
    assert "2 targets in debt" in page.evaluate("document.getElementById('verdict').innerText")
    # After hub.disarm the target is in no list: the note goes, the vault quarantine stays visible.
    fleet.lists[BASE].remove(T_VAULT_Q)
    fleet.target(BASE, T_VAULT_Q, "Disarmed", debt=3 * 10**12, quarantined=True, listed=False)
    page.click("#refresh")
    page.wait(f"document.querySelector('#targets-body tr[data-target=\"{BASE}|{T_VAULT_Q}\"]').dataset.state === 'Disarmed'")
    row = target_row(page, BASE, T_VAULT_Q)
    assert row["notes"] == [] and "Disarmed" in row["text"] and "quarantined on the vault" in row["text"]


def test_states_of_the_other_targets(fleet, page):
    fields = opened(page, fleet)
    disarmed = target_row(page, BASE, T_DISARMED)
    assert disarmed["state"] == "Disarmed" and "Disarmed" in disarmed["text"] and "base removed" in disarmed["text"]
    assert disarmed["sev"] == "0" and fields[f"target:{BASE}|{T_DISARMED}.low"] == ""
    parked = target_row(page, UNI, T_PARKED)
    assert "Parked" in parked["text"] and "hub.rearm" in parked["text"] and parked["sev"] == "2"
    low = target_row(page, UNI, T_LOW)
    assert "Active" in low["text"] and "Below the low mark" in low["text"] and low["sev"] == "2"
    assert "In debt" in target_row(page, LASNA, T_LASNA_Q)["text"]
    unlisted = target_row(page, BASE, T_UNLISTED_LABEL)
    assert "No label" in unlisted["text"] and unlisted["state"] == "Active"
    assert target_row(page, LASNA, T_ACTIVE)["sev"] == "0"
    # The first rows are the ones that need the owner.
    order = page.evaluate("[...document.querySelectorAll('#targets-body tr[data-target]')].map((row) => row.dataset.sev)")
    assert order == sorted(order, reverse=True)
    # Filters and the detail of a row are reachable with the keyboard: a button each.
    page.click("#targets-state button[data-fk=\"tstate:disarmed\"]")
    page.wait("document.querySelectorAll('#targets-body tr[data-target]').length === 1")
    page.evaluate(f"document.querySelector('#targets-body button[data-fk=\"texp:{BASE}|{T_DISARMED}\"]').focus()")
    page.press("Enter", 13, "\r")
    detail = page.wait("(document.querySelector('#targets-body tr.detail') || {}).innerText")
    assert T_DISARMED in detail.lower() and "Manifest enrollment, removed" in detail and "Not in the hub list" in detail
    assert page.evaluate("document.activeElement.getAttribute('aria-expanded')") == "true"


def test_failure_counts_are_diagnostics_and_change_no_target_state(fleet, page):
    before = opened(page, fleet)
    rows_before = {key: target_row(page, *key) for key in ((BASE, T_VAULT_Q), (BASE, T_UNLISTED_LABEL), (UNI, T_LOW))}
    assert before[f"failures:{BASE}|{VAULTS[BASE]}"] == "3" and before[f"failures:{UNI}|{VAULTS[UNI]}"] == "0"
    assert f"failures:{BASE}|{STRANGER}" not in before
    diagnostics = page.evaluate("document.getElementById('diag').innerText")
    assert "Diagnostics only" in diagnostics and "a count here is not a state" in diagnostics
    assert not page.evaluate("document.querySelector('#events-body tr[data-event=\"CallbackFailure\"]')")

    # More junk callbacks: to the vault, and to two targets (a caller can make these logs for any contract).
    fleet.chains[BASE].head += 40
    fleet.failures(BASE, VAULTS[BASE], 4, HEADS[BASE] + 1)
    fleet.failures(BASE, T_UNLISTED_LABEL, 6, HEADS[BASE] + 10)
    fleet.chains[UNI].head += 20
    fleet.failures(UNI, T_LOW, 2, HEADS[UNI] + 1)
    page.click("#refresh")
    page.wait(f"document.querySelector('[data-field=\"failures:{BASE}|{VAULTS[BASE]}\"]').dataset.raw === '7'")
    page.wait(IDLE)
    after = page.evaluate(FIELDS)
    assert after[f"failures:{BASE}|{T_UNLISTED_LABEL}"] == "6" and after[f"failures:{UNI}|{T_LOW}"] == "2"
    # The counts are in the diagnostics part only, and no state or number of a target changed.
    assert page.evaluate("[...document.querySelectorAll('[data-field^=\"failures:\"]')].every((e) => e.closest('#diag'))")
    for key, row in rows_before.items():
        assert target_row(page, *key) == row, key
    changed = {field for field in before if before[field] != after.get(field)}
    assert changed == {f"failures:{BASE}|{VAULTS[BASE]}", f"block:{BASE}", f"block:{UNI}"}
    assert "CallbackFailure" not in page.evaluate("document.getElementById('verdict').innerText")


@pytest.mark.parametrize("width, height", [(390, 844), (720, 500)])
def test_a_narrow_or_zoomed_window_shows_each_column(fleet, browser, width, height):
    """390px is a phone. 720px is a 1440px window at a browser zoom of 200%. No column is out of view."""
    narrow = browser.page(width=width, height=height)
    try:
        opened(narrow, fleet, theme="dark")
        assert narrow.evaluate("document.documentElement.dataset.theme") == "dark"
        # Nothing scrolls sideways: not the page, and no table inside its panel.
        assert narrow.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert narrow.evaluate("[...document.querySelectorAll('.panel__body')].every((body) => "
                               "body.scrollWidth <= body.clientWidth + 1)")
        # Each value and each control of a row is inside the window, with the name of its column.
        assert narrow.evaluate("[...document.querySelectorAll('[data-field], #chains-body input, #chains-body button')]"
                               ".filter((e) => !e.hidden && e.getClientRects().length)"
                               ".every((e) => e.getBoundingClientRect().right <= window.innerWidth + 1)")
        cell = (f"document.querySelector('[data-field=\"funds:{LASNA}.float\"]').closest('td')")
        assert narrow.evaluate(f"{cell}.dataset.th") == "Fee float (reserves)"
        assert narrow.evaluate(f"getComputedStyle({cell}, '::before').content") == '"Fee float (reserves)"'
        assert not narrow.errors
    finally:
        narrow.close()
