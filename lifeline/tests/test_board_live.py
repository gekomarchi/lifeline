"""The board of the repository manifest against the deployed fleet, in a real browser from `file://`.

The module sends read requests to the public RPC URLs of `deployments/manifest.json`, so it runs only with
`LIFELINE_BOARD_LIVE=1`. It needs the build output and a browser. It sends no transaction and needs no key.
"""

from __future__ import annotations

import json
import os
import urllib.parse

import pytest

pytest.importorskip("eth_account")

from lifeline import board, ops
from lifeline.tests import boardcheck
from lifeline.tests.browser import Browser, find_browser

BUILT = (ops.ARTIFACTS / "LifelineHub.sol" / "LifelineHub.json").exists()
pytestmark = pytest.mark.skipif(
    os.environ.get("LIFELINE_BOARD_LIVE") != "1" or not BUILT or not find_browser(),
    reason="the live check of the board needs LIFELINE_BOARD_LIVE=1, `forge build` and Chrome or Chromium",
)

MANIFEST = json.loads(ops.DEFAULT_MANIFEST.read_text())
IDLE = "document.body.dataset.phase === 'idle'"
DEFAULTS = [(name, url) for name, chain in MANIFEST["chains"].items() for url in chain["rpc"]]


@pytest.fixture(scope="module")
def browser():
    instance = Browser()
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return board.build(ops.DEFAULT_MANIFEST, tmp_path_factory.mktemp("board") / "index.html")


def test_each_number_equals_a_direct_read_at_the_block_of_the_page(browser, built):
    page = browser.page()
    try:
        url = built.resolve().as_uri() + "#auto=0&lookback=3000"
        page.goto(url)
        page.wait(f"location.href === {json.dumps(url)} && {IDLE}", timeout=180)
        found = boardcheck.read_page(page)
        assert set(found["states"].values()) <= {"ok", "fallback"} and set(found["blocks"]) == set(MANIFEST["chains"])
        # The direct reads go to the node that gave the page its values, at the block that the page shows.
        rpcs = {name: ops.Rpc(name, [found["fields"][f"rpc:{name}"]], chain_id=MANIFEST["chains"][name]["chainId"])
                for name in MANIFEST["chains"]}
        boardcheck.check_page(found, MANIFEST, rpcs)
        # The network log: the file itself, and requests to the RPC URLs of the manifest only.
        urls = {request["url"].split("#")[0].rstrip("/") for request in page.requests}
        assert built.resolve().as_uri() in urls
        assert urls - {built.resolve().as_uri()} <= {url.rstrip("/") for _, url in DEFAULTS}
        assert not page.errors, page.errors
    finally:
        page.close()


@pytest.mark.parametrize("name, default", DEFAULTS)
def test_each_default_rpc_url_answers_the_page_opened_from_a_file(browser, built, name, default):
    """One default alone, as the override of its chain: the browser must get its answers from `file://`."""
    page = browser.page()
    try:
        url = f"{built.resolve().as_uri()}#auto=0&lookback=1&rpc.{name}={urllib.parse.quote(default, safe='')}"
        page.goto(url)
        row = f"document.querySelector('#chains-body tr[data-chain=\"{name}\"]')"
        page.wait(f"location.href === {json.dumps(url)} && {row} && ['override', 'error'].includes({row}.dataset.state)",
                  timeout=120)
        assert page.evaluate(f"{row}.dataset.state") == "override", page.evaluate(f"{row}.nextSibling.innerText")
        assert int(page.evaluate(f"document.querySelector('[data-field=\"block:{name}\"]').dataset.raw")) > 0
        # Each request of the chain went to this URL: no other URL of its list got one.
        sent = {request["url"].rstrip("/") for request in page.requests}
        assert default.rstrip("/") in sent
        assert not sent & {url.rstrip("/") for chain, url in DEFAULTS if chain == name and url != default}
    finally:
        page.close()
