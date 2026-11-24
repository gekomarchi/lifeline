"""The board against the fork dry run of the operator commands.

The contracts, the ledgers, the operator commands, the build and the page are the real ones: the hub and
the vaults are deployed on local anvil forks with `python -m lifeline.ops`, the page is built from the
manifest of that run and opened in a real browser from a `file://` URL. Each value of the page is compared
with a direct read from the forks at the block that the page shows.

The module needs what `test_ops_forks.py` needs (anvil, `forge build`, the three RPC variables) and a
browser. Without them it is skipped. The tests run in the order of the file.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("eth_account")

from lifeline import board, ops
from lifeline.tests import boardcheck, test_ops_forks
from lifeline.tests.browser import Browser, RpcGate, find_browser
from lifeline.tests.test_ops_forks import BASE, LASNA, UNICHAIN, ZERO, DryRun, relayed

pytestmark = [
    test_ops_forks.pytestmark,
    pytest.mark.skipif(not find_browser(), reason="the board needs Chrome or Chromium"),
]

IDLE = "document.body.dataset.phase === 'idle'"
# `low` 0 and `high` 1000 wei: one callback after a rescue makes a new debt. One rescue in the window: the
# second debt puts the target into quarantine on the vault, and the hub (limit of 4 relays) keeps it active.
QUARANTINE_POLICY = ["--low", "0", "--high", "0.000000000000001", "--max-debt", "0.0001", "--daily-cap", "0.001",
                     "--max-rescues", "1", "--window", "3600"]
JUNK = 3


class Run(DryRun):
    """The dry run, a gate that a page can call, and the board of its manifest."""

    def _start(self, directory):
        super()._start(directory)
        self.gate = RpcGate({name: anvil.url for name, anvil in self.anvils.items()})
        self.page_file = directory / "board" / "index.html"
        self.targets = {}

    def close(self):
        if getattr(self, "gate", None):
            self.gate.close()
        super().close()

    def board_manifest(self) -> dict:
        """The manifest of the run, with the URL of the gate as the RPC list of each chain."""
        data = self.data()
        for name in self.anvils:
            data["chains"][name]["rpc"] = [self.gate.url(name)]
        return data

    def open(self, page) -> dict:
        """Builds the board from the manifest, opens it from `file://`, and returns what it shows."""
        manifest = self.dir / "board-manifest.json"
        manifest.write_text(json.dumps(self.board_manifest()))
        board.build(manifest, self.page_file)
        url = self.page_file.resolve().as_uri() + "#auto=0"
        page.goto("about:blank")
        page.goto(url)
        page.wait(f"location.href === {json.dumps(url)} && {IDLE}", timeout=120)
        return boardcheck.read_page(page)


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    run = Run(tmp_path_factory.mktemp("board"))
    yield run
    run.close()


@pytest.fixture(scope="module")
def browser():
    instance = Browser()
    yield instance
    instance.close()


@pytest.fixture
def page(browser):
    instance = browser.page()
    yield instance
    errors = [error for error in instance.errors if "Failed to load resource" not in error]
    instance.close()
    assert not errors, errors


def test_the_board_of_a_new_deployment_equals_direct_reads(net, page):
    result = net.ops("deploy", "--config", str(net.write_config()))
    assert result.returncode == 0, result.stderr + result.stdout

    # One Lasna target, and three callback canaries: one on Unichain Sepolia, two on Base Sepolia.
    receipt = ops.deploy_contract(net.rpcs[LASNA], net.account, net.reactive, (net.ledger[LASNA], 0, ZERO, 0))
    net.targets["lasna"] = ops.checksum(receipt["contractAddress"])
    enrollments = [
        (LASNA, "lasna", "lasna canary", ["--low", "0.01", "--high", "0.02", "--max-debt", "0.05", "--daily-cap", "1",
                                          "--max-rescues", "2", "--window", "3600"]),
        (UNICHAIN, "healthy", "healthy canary", test_ops_forks.CANARY_POLICY),
        (BASE, "quarantine", "quarantine canary", QUARANTINE_POLICY),
        (BASE, "removed", "removed canary", test_ops_forks.CANARY_POLICY),
    ]
    for chain, key, label, policy in enrollments:
        if chain != LASNA:
            net.targets[key] = net.new_callback_canary(chain)
        result = net.ops("enroll", "--chain", chain, "--target", net.targets[key], "--label", label, *policy)
        assert result.returncode == 0, result.stderr + result.stdout
    relayed(net.relay())

    found = net.open(page)
    assert found["states"] == {LASNA: "ok", UNICHAIN: "ok", BASE: "ok"}
    assert found["blocks"] == net.blocks()
    boardcheck.check_page(found, net.data(), net.rpcs)
    fields = found["fields"]
    assert fields[f"funds:{LASNA}.principal"] != "0" and fields[f"funds:{BASE}.float"] != "0"
    assert fields[f"funds:{BASE}.up"] == "true" and fields[f"funds:{UNICHAIN}.pending"] == "0"
    for chain, key in ((LASNA, "lasna"), (UNICHAIN, "healthy"), (BASE, "quarantine"), (BASE, "removed")):
        assert fields[f"target:{chain}|{net.targets[key].lower()}.state"] == "Active"
    # The marks of a destination target come from the vault, the marks of a Lasna target from the hub.
    assert fields[f"target:{BASE}|{net.targets['quarantine'].lower()}.high"] == "1000"
    assert fields[f"target:{LASNA}|{net.targets['lasna'].lower()}.low"] == str(10**16)
    # The reconcile of `setPolicy` is an event of each vault: `Result` with id 0.
    assert [row[1] for row in found["events"] if row[0] == BASE] == ["Result", "Result"]

    # The network log of the browser: the file itself, and requests to the RPC URLs only.
    urls = {request["url"].split("#")[0] for request in page.requests} - {"about:blank"}
    assert urls == {net.page_file.resolve().as_uri(), *(net.gate.url(name) for name in net.anvils)}
    assert {record["method"] for record in net.gate.calls()} <= {
        "eth_chainId", "eth_blockNumber", "eth_call", "eth_getBalance", "eth_getLogs", "eth_getBlockByNumber"}


def test_a_target_removed_with_the_command_shows_disarmed(net, page):
    result = net.ops("remove", "--chain", BASE, "--target", net.targets["removed"])
    assert result.returncode == 0, result.stderr + result.stdout
    found = net.open(page)
    boardcheck.check_page(found, net.data(), net.rpcs)
    base = f"target:{BASE}|{net.targets['removed'].lower()}"
    assert found["fields"][f"{base}.state"] == "Disarmed" and found["fields"][f"{base}.low"] == ""
    row = page.evaluate(f"document.querySelector('#targets-body tr[data-target=\"{BASE}|{net.targets['removed'].lower()}\"]').innerText")
    assert "removed canary" in row and "Disarmed" in row and "hub.disarm" not in row


def test_a_target_that_the_vault_quarantined_has_the_disarm_note(net, page):
    canary = net.targets["quarantine"]
    key = f"{BASE}|{canary.lower()}"
    # The first debt: the hub relays it, and the vault pays (one rescue).
    net.pong(BASE, canary)
    assert net.debt(BASE, canary) > 0
    assert relayed(net.relay()) == (1, 1) and net.debt(BASE, canary) == 0
    # The second debt inside the window: the vault puts the target into quarantine and pays nothing.
    net.pong(BASE, canary, 2)
    assert relayed(net.relay()) == (1, 1) and net.debt(BASE, canary) > 0
    assert net.read(BASE, net.vault(BASE), net.vault_abi, "quarantined", canary) is True
    assert net.target_state(BASE, canary) == "Active"
    count = net.read(LASNA, net.hub, net.hub_abi, "targetCount", net.chain_id[BASE])
    assert canary in [net.read(LASNA, net.hub, net.hub_abi, "targetAt", net.chain_id[BASE], i) for i in range(count)]

    found = net.open(page)
    boardcheck.check_page(found, net.data(), net.rpcs)
    assert found["fields"][f"target:{key}.state"] == "Active" and found["fields"][f"target:{key}.vaultQuarantine"] == "true"
    row = page.evaluate(f"(() => {{ const row = document.querySelector('#targets-body tr[data-target=\"{key}\"]');"
                        f" return [row.innerText, [...row.querySelectorAll('[data-note]')].map((n) => n.textContent)]; }})()")
    assert "Quarantined on the vault" in row[0] and "Active on the hub" in row[0] and "In debt" in row[0]
    assert row[1] == ["Still in the hub sweep list. hub.disarm stops its sweep cost."]
    assert page.evaluate("document.querySelectorAll('#targets-body [data-note]').length") == 1
    outcomes = page.evaluate(f"[...document.querySelectorAll('#events-body tr[data-chain=\"{BASE}\"][data-event=\"Result\"]')]"
                             f".map((row) => row.dataset.outcome)")
    assert outcomes[:2] == ["Quarantined", "Rescued"]


def test_junk_callbacks_show_in_the_diagnostics_and_change_no_target_state(net, page):
    before = net.open(page)
    vault = net.vault(BASE)
    field = f"failures:{BASE}|{vault.lower()}"
    assert before["fields"][field] == "0"
    states = page.evaluate("[...document.querySelectorAll('#targets-body tr[data-target]')].map((row) => "
                           "[row.dataset.target, row.dataset.state, row.dataset.sev, row.querySelector('.tags').innerText])")

    # Callbacks with a wrong first argument, as any caller can request them: each one reverts in the vault.
    payload = net.vault_abi.encode("reconcile", ZERO, vault, 1)
    for _ in range(JUNK):
        net.node.deliver(BASE, vault, payload, 200_000, net.account.address)
    assert net.debt(BASE, vault) == 0

    after = net.open(page)
    boardcheck.check_page(after, net.data(), net.rpcs)
    assert after["fields"][field] == str(JUNK)
    assert page.evaluate(f"Boolean(document.querySelector('[data-field=\"{field}\"]').closest('#diag'))")
    assert "Diagnostics only" in page.evaluate("document.getElementById('diag').innerText")
    # No state of a target changed, and no number of a target: only the float of the vault paid the fees.
    assert page.evaluate("[...document.querySelectorAll('#targets-body tr[data-target]')].map((row) => "
                         "[row.dataset.target, row.dataset.state, row.dataset.sev, row.querySelector('.tags').innerText])") == states
    targets = lambda found: {name: raw for name, raw in found["fields"].items() if name.startswith("target:")}  # noqa: E731
    assert targets(after) == targets(before)
    assert int(after["fields"][f"funds:{BASE}.float"]) < int(before["fields"][f"funds:{BASE}.float"])
    assert after["fields"][f"funds:{BASE}.principal"] == before["fields"][f"funds:{BASE}.principal"]
    assert [row for row in after["events"] if row[1] == "CallbackFailure"] == []


def test_a_fork_that_stops_shows_its_error_and_no_number(net, page):
    net.open(page)
    net.gate.down.add(UNICHAIN)
    try:
        page.click("#refresh")
        page.wait(f"document.querySelector('#chains-body tr[data-chain=\"{UNICHAIN}\"]').dataset.state === 'error'")
        page.wait(IDLE, timeout=120)
        found = boardcheck.read_page(page)
        assert found["states"] == {LASNA: "ok", UNICHAIN: "error", BASE: "ok"} and UNICHAIN not in found["blocks"]
        boardcheck.check_page(found, net.data(), net.rpcs)
        empty = {name: raw for name, raw in boardcheck.chain_values(found["fields"]).items()
                 if f":{UNICHAIN}" in name and not name.endswith(".state")}
        assert empty and set(empty.values()) == {""}
    finally:
        net.gate.down.clear()
