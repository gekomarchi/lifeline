"""The build of the board: what goes into the file, and what the build refuses.

These tests need the build output (`forge build`) and no chain. The page itself runs in a real browser in
`test_board_browser.py` (a local JSON-RPC server) and in `test_board_forks.py` (the fork dry run).
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys

import pytest

from lifeline import board, ops
from lifeline.events import parse_event, selector

BUILT = (ops.ARTIFACTS / "LifelineHub.sol" / "LifelineHub.json").exists()
pytestmark = pytest.mark.skipif(not BUILT, reason="the build of the board needs the build output: run `forge build`")

MANIFEST = json.loads(ops.DEFAULT_MANIFEST.read_text())

# The declarations of the events that the page reads, as unit 2, unit 3 and the platform sources give them.
EVENTS = {
    ("hub", "HubLow"): "HubLow(uint256 balance, uint256 principalFloor)",
    ("hub", "Rescued"): "Rescued(address indexed target, uint256 debt, uint256 paid)",
    ("hub", "Gap"): "Gap(address indexed target, uint256 deadFromBlock, uint256 aliveAtBlock)",
    ("vault", "VaultLow"): "VaultLow(uint256 balance, uint256 principalFloor)",
    ("vault", "Result"): "Result(uint256 indexed id, address indexed target, uint8 outcome, uint256 paid)",
    ("ledger", "CallbackFailure"): "CallbackFailure(address indexed target_, bytes payload_)",
}


def config_of(page: str) -> dict:
    """The inlined data of a built page."""
    found = re.search(r'<script id="board-config" type="application/json">(.*?)</script>', page, re.S)
    return json.loads(found.group(1))


def rpc_urls(manifest: dict) -> list:
    return [url for chain in manifest["chains"].values() for url in chain["rpc"]]


def test_two_builds_of_one_manifest_are_the_same_bytes(tmp_path):
    first = board.build(ops.DEFAULT_MANIFEST, tmp_path / "a" / "index.html")
    second = board.build(ops.DEFAULT_MANIFEST, tmp_path / "b" / "other.html")
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == board.render(MANIFEST).encode()
    # Another input gives another file and another build ID.
    other = board.render(MANIFEST, lookback=board.DEFAULT_LOOKBACK + 1)
    assert config_of(other)["build"] != config_of(first.read_text())["build"]


def test_the_command_writes_the_file_with_the_lookback(tmp_path):
    out = tmp_path / "page.html"
    result = subprocess.run(
        [sys.executable, "-m", "lifeline.board", "--manifest", str(ops.DEFAULT_MANIFEST), "--out", str(out),
         "--lookback", "1234"], capture_output=True, text=True, cwd=ops.ROOT)
    assert result.returncode == 0, result.stderr
    assert str(out) in result.stdout and out.resolve().as_uri() in result.stdout
    config = config_of(out.read_text())
    assert config["lookback"] == 1234 and config["chunk"] == board.CHUNK_BLOCKS
    assert board.build_parser().parse_args([]).lookback == board.DEFAULT_LOOKBACK
    assert board.DEFAULT_OUT == ops.ROOT / "board" / "index.html"


def test_the_output_names_no_url_other_than_the_rpc_urls():
    page = board.render(MANIFEST)
    for url in rpc_urls(MANIFEST):
        assert json.dumps(url) in page
    rest = page
    for url in sorted(rpc_urls(MANIFEST), key=len, reverse=True):
        rest = rest.replace(url, "")
    assert not re.search("http", rest, re.IGNORECASE)
    assert "//" not in re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", rest).replace("://", "")  # no scheme-relative URL


def test_an_rpc_url_with_a_query_is_a_url_of_the_output():
    manifest = copy.deepcopy(MANIFEST)
    url = "https://rpc.example.test/v1?key=a&mode=<b>"
    manifest["chains"]["lasna"]["rpc"].append(url)
    page = board.render(manifest)
    assert config_of(page)["chains"][0]["rpc"] == [*MANIFEST["chains"]["lasna"]["rpc"], url]
    assert url not in page and "key=a\\u0026mode=\\u003cb\\u003e" in page


def test_the_output_loads_no_other_resource():
    page = board.render(MANIFEST)
    assert not re.search(r"<(link|img|iframe|object|embed|audio|video|source|base)\b", page, re.IGNORECASE)
    assert not re.search(r"@import|@font-face|[^A-Za-z]url\(", page)
    for script in re.findall(r"<script([^>]*)>", page):
        assert "src" not in script
    # A reference inside the file is the only kind: the marks of the sprite.
    marks = set(re.findall(r'<symbol id="(i-[a-z]+)"', page))
    assert marks and set(re.findall(r'href="([^"]*)"', page)) <= {f"#{mark}" for mark in marks} | {"#i-${name}"}
    # The one reference that the script builds takes its name from a fixed list of the same marks.
    listed = re.search(r"const ICONS = new Set\(\[(.*?)\]\)", page, re.S).group(1)
    assert {f"i-{name}" for name in re.findall(r'"([a-z]+)"', listed)} == marks
    for word in ("localStorage", "sessionStorage", "indexedDB", "XMLHttpRequest", "WebSocket", "sendBeacon",
                 "import(", "eval(", "window.ethereum", "eth_sendTransaction", "eth_sendRawTransaction",
                 "eth_requestAccounts", "personal_sign"):
        assert word not in page, word


def test_selectors_and_topics_come_from_the_build_output():
    abi = config_of(board.render(MANIFEST))["abi"]
    artifacts = {"hub": "LifelineHub", "vault": "LifelineVault", "ledger": "ICallbackProxy"}
    used = {"hub": board.HUB_FUNCTIONS, "vault": board.VAULT_FUNCTIONS, "ledger": board.LEDGER_FUNCTIONS}
    for source, name in artifacts.items():
        data = json.loads(ops.load_artifact(name).path.read_text())
        identifiers = {signature.split("(")[0]: (signature, digits) for signature, digits in data["methodIdentifiers"].items()}
        assert set(abi[source]["functions"]) == set(used[source])
        for function, fragment in abi[source]["functions"].items():
            signature, digits = identifiers[function]
            assert fragment["selector"] == "0x" + digits == selector(signature)
    assert {(source, name) for source in abi for name in abi[source]["events"]} == set(EVENTS)
    for (source, name), declaration in EVENTS.items():
        event = parse_event(declaration)
        fragment = abi[source]["events"][name]
        assert fragment["topic"] == event.topic0
        assert [parameter for parameter, _ in fragment["indexed"]] == list(event.indexed)


def test_the_abi_fragments_have_the_shapes_that_the_page_reads():
    abi = config_of(board.render(MANIFEST))["abi"]
    policy = [[f"policy.{name}", kind] for name, kind in (
        ("low", "uint256"), ("high", "uint256"), ("maxDebt", "uint256"), ("dailyCap", "uint256"),
        ("maxRescues", "uint32"), ("window", "uint32"))]
    hub, vault, ledger = (abi[source]["functions"] for source in ("hub", "vault", "ledger"))
    assert hub["targetInfo"]["inputs"] == ["uint256", "address"]
    assert hub["targetInfo"]["outputs"] == [["kind", "uint8"], ["state", "uint8"], *policy]
    assert hub["vaultInfo"]["outputs"] == [["proxy", "address"], ["vault", "address"], ["sweepBase", "uint256"],
                                           ["up", "bool"], ["pending", "uint256"]]
    assert hub["targetAt"] == {"selector": hub["targetAt"]["selector"], "inputs": ["uint256", "uint256"],
                               "outputs": [["", "address"]]}
    assert vault["policyOf"]["outputs"] == [[name.split(".")[1], kind] for name, kind in policy]
    assert vault["quarantined"]["outputs"] == [["", "bool"]]
    assert [kind for _, kind in ledger["debt"]["outputs"]] == ["uint256"] == [kind for _, kind in ledger["reserves"]["outputs"]]
    assert abi["vault"]["events"]["Result"]["data"] == [["outcome", "uint8"], ["paid", "uint256"]]
    assert abi["ledger"]["events"]["CallbackFailure"]["data"] == []  # the page counts it and reads no payload


def test_the_enum_names_are_the_ones_of_the_contracts():
    config = config_of(board.render(MANIFEST))

    def members(path, name):
        body = re.search(rf"enum {name} \{{(.*?)\}}", (ops.ROOT / path).read_text(), re.S).group(1)
        return re.findall(r"^\s*(\w+)", re.sub(r"//.*", "", body), re.M)

    assert config["targetStates"] == members("src/LifelineTypes.sol", "TargetState")
    assert config["outcomes"] == members("src/LifelineVault.sol", "Outcome")


def test_the_page_gets_only_the_manifest_content_that_it_uses():
    page = board.render(MANIFEST)
    config = config_of(page)
    assert set(config) == {"build", "lookback", "chunk", "maxLookback", "cronTopic", "chains", "enrollments",
                           "targetStates", "outcomes", "abi"}
    hub, vaults = MANIFEST["contracts"]["hub"], MANIFEST["contracts"]["vaults"]
    assert [chain["name"] for chain in config["chains"]] == [hub["chain"], *vaults]
    for chain in config["chains"]:
        entry = MANIFEST["chains"][chain["name"]]
        contract = hub if chain["role"] == "hub" else vaults[chain["name"]]
        assert chain == {
            "name": chain["name"], "chainId": entry["chainId"], "role": chain["role"],
            "unit": "lREACT" if chain["role"] == "hub" else "ETH", "ledger": entry["ledger"], "rpc": entry["rpc"],
            "contract": contract["address"], "deployBlock": contract["deployBlock"],
        }
    assert [(e["label"], e["chain"], e["target"], e["removed"]) for e in config["enrollments"]] == [
        (e["label"], e["chain"], e["target"], bool(e["removed"])) for e in MANIFEST["enrollments"]]
    # No transaction hash, no policy and no fixture of the manifest is in the file.
    assert hub["deployTx"] not in page and "fixtures" not in page and "dailyCap\":\"" not in page
    assert MANIFEST["loopCheck"]["canaryReactive"]["address"] not in page


def test_the_newest_record_of_a_target_gives_its_row():
    manifest = copy.deepcopy(MANIFEST)
    first = manifest["enrollments"][2]
    assert first["removed"]
    manifest["enrollments"].append({**first, "label": "enrolled again", "removed": None})
    rows = [e for e in config_of(board.render(manifest))["enrollments"] if e["target"] == first["target"]]
    assert rows == [{"label": "enrolled again", "chain": first["chain"], "target": first["target"], "guest": False,
                     "removed": False}]


def test_inlined_text_cannot_leave_the_data_element():
    manifest = copy.deepcopy(MANIFEST)
    manifest["enrollments"][0]["label"] = "</script><script>alert(1)</script> & <!--"
    page = board.render(manifest)
    assert page.count("</script>") == 3 and "alert(1)</script>" not in page
    assert config_of(page)["enrollments"][0]["label"] == manifest["enrollments"][0]["label"]


def change(path: str, value):
    def apply(manifest):
        holder = manifest
        *parts, last = path.split(".")
        for part in parts:
            holder = holder[int(part)] if isinstance(holder, list) else holder[part]
        if isinstance(holder, list):
            holder[int(last)] = value
        else:
            holder[last] = value
    return apply


@pytest.mark.parametrize("edit, text", [
    (change("contracts.hub", None), "no hub"),
    (change("contracts.hub.deployBlock", "9039165"), "deployBlock of the hub is not a block number"),
    (change("contracts.hub.deployBlock", True), "deployBlock of the hub is not a block number"),
    (change("contracts.hub.chainId", 1), "the hub has chainId 1"),
    (change("contracts.hub.chain", "nowhere"), "no chains.nowhere"),
    (change("contracts.vaults.base-sepolia.deployBlock", None), "deployBlock of the vault of base-sepolia"),
    (change("contracts.vaults.base-sepolia.address", "0x1234"), "the address of the vault of base-sepolia"),
    (change("chains.lasna.rpc", []), "chains.lasna.rpc has no RPC URL"),
    (change("chains.lasna.rpc", ["ftp://node.example"]), "not an http or https URL"),
    (change("chains.base-sepolia.rpc.1", MANIFEST["chains"]["base-sepolia"]["rpc"][0]), "the same URL two times"),
    (change("chains.unichain-sepolia.ledger", "0x00"), "chains.unichain-sepolia.ledger"),
    (change("enrollments.0.chain", "mainnet"), "enrollments[0] is on a chain with no contract"),
    (change("enrollments.1.target", "0xzz"), "enrollments[1].target"),
    (change("enrollments.0.label", "see https://example.org/x"), "enrollments[0].label holds a URL"),
    (change("enrollments.0.label", ""), "enrollments[0].label is not a text"),
])
def test_the_build_refuses_a_manifest_that_it_cannot_show(edit, text):
    manifest = copy.deepcopy(MANIFEST)
    edit(manifest)
    with pytest.raises(ops.OpsError) as error:  # a `BoardError`, or the refusal of an address by `ops`
        board.render(manifest)
    assert text in str(error.value)


def test_a_vault_that_is_not_deployed_has_no_chain_row():
    manifest = copy.deepcopy(MANIFEST)
    manifest["contracts"]["vaults"]["unichain-sepolia"]["address"] = None
    manifest["enrollments"] = [e for e in manifest["enrollments"] if e["chain"] != "unichain-sepolia"]
    assert [chain["name"] for chain in config_of(board.render(manifest))["chains"]] == ["lasna", "base-sepolia"]


def test_refusals_of_the_command_leave_the_old_file(tmp_path):
    out = tmp_path / "index.html"
    out.write_text("old")
    manifest = tmp_path / "manifest.json"
    broken = copy.deepcopy(MANIFEST)
    broken["chains"]["lasna"]["rpc"] = []
    manifest.write_text(json.dumps(broken))

    def run(*arguments):
        return subprocess.run([sys.executable, "-m", "lifeline.board", "--out", str(out), *arguments],
                              capture_output=True, text=True, cwd=ops.ROOT)

    for arguments, text in (
        (["--manifest", str(manifest)], "error: chains.lasna.rpc has no RPC URL"),
        (["--manifest", str(tmp_path / "absent.json")], "does not exist"),
        (["--manifest", str(ops.DEFAULT_MANIFEST), "--lookback", "0"], "the lookback is a number of blocks"),
        (["--manifest", str(ops.DEFAULT_MANIFEST), "--lookback", str(board.MAX_LOOKBACK + 1)], "the lookback is"),
    ):
        result = run(*arguments)
        assert result.returncode == 1 and text in result.stderr, result.stderr
        assert out.read_text() == "old"


def test_the_build_names_a_seam_that_the_build_output_does_not_have(tmp_path):
    with pytest.raises(ops.OpsError, match="run `forge build`"):
        board.render(MANIFEST, artifacts=tmp_path)
    # A build output with no `vaultInfo` in the hub: the build names the member.
    for name in ("LifelineHub", "LifelineVault", "ICallbackProxy"):
        source = ops.load_artifact(name).path
        data = json.loads(source.read_text())
        if name == "LifelineHub":
            data["abi"] = [entry for entry in data["abi"] if entry.get("name") != "vaultInfo"]
        target = tmp_path / source.parent.name / source.name
        target.parent.mkdir()
        target.write_text(json.dumps(data))
    with pytest.raises(ops.OpsError, match="has no function vaultInfo: the seam of an earlier unit is absent"):
        board.render(MANIFEST, artifacts=tmp_path)


def test_the_template_is_one_file_with_one_place_for_the_data(tmp_path):
    source = board.DEFAULT_TEMPLATE.read_text()
    assert source.count(board.PLACEHOLDER) == 1
    assert not re.search("http", source, re.IGNORECASE)
    twice = tmp_path / "template.html"
    twice.write_text(source + board.PLACEHOLDER)
    with pytest.raises(board.BoardError, match="one time"):
        board.render(MANIFEST, template=twice)
    external = tmp_path / "external.html"
    external.write_text(source.replace("</head>", '<link rel="stylesheet" href="x.css"></head>'))
    with pytest.raises(board.BoardError, match="a `link` element"):
        board.render(MANIFEST, template=external)


def test_the_file_of_the_repository_is_the_build_of_the_manifest():
    if not board.DEFAULT_OUT.exists():
        pytest.skip("board/index.html is not built")
    assert board.DEFAULT_OUT.read_bytes() == board.render(MANIFEST).encode(), \
        "board/index.html is not the build of deployments/manifest.json: run python -m lifeline.board"
