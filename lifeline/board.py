"""Build of the board: one self-contained HTML file for the owner.

    python -m lifeline.board [--manifest <file>] [--out <file>] [--lookback <blocks>]

The manifest (`deployments/manifest.json`) is the only input. The build puts into `board/template.html`:

- the manifest content that the page uses: for each chain `chainId`, the ledger and the RPC list; the address
  and `deployBlock` of the hub and of each vault; the label, the chain and the address of each enrollment;
- the ABI fragments that the page uses, with the selector of each function and topic 0 of each event. They
  come from the Foundry build output under `out/`: no selector and no topic is written by hand;
- the default lookback and the chunk size of the event queries.

The output opens from `file://` with no server. It has no external script, style sheet, font or image, and
the same manifest and the same build output give the same bytes. The page sends requests only to the RPC
URLs. It has no wallet action and sends no transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from . import ops
from .ops import OpsError

DEFAULT_TEMPLATE = ops.ROOT / "board" / "template.html"
DEFAULT_OUT = ops.ROOT / "board" / "index.html"

# Blocks before the head that the event queries of the page read. The page can change the value.
DEFAULT_LOOKBACK = 30_000
MAX_LOOKBACK = 5_000_000
# The largest block range of one `eth_getLogs` request. The page makes it smaller for a node that refuses it.
CHUNK_BLOCKS = 10_000

# The place of the inlined data in the template. The template has it one time.
PLACEHOLDER = "__BOARD_CONFIG__"

# The native unit of each chain: lREACT on the chain of the hub, ETH on a destination chain.
HUB_UNIT, DESTINATION_UNIT = "lREACT", "ETH"

# The members that the page uses, by artifact. A member that the build output does not have stops the build.
HUB_FUNCTIONS = ("paused", "principalFloor", "selfReserveTarget", "cronSubscribed", "targetCount", "targetAt",
                 "targetInfo", "vaultInfo")
HUB_EVENTS = ("HubLow", "Rescued", "Gap")
VAULT_FUNCTIONS = ("paused", "principalFloor", "selfReserveTarget", "hasPolicy", "policyOf", "quarantined")
VAULT_EVENTS = ("VaultLow", "Result")
LEDGER_FUNCTIONS = ("debt", "reserves")
# `CallbackFailure` is an event of a callback proxy. The system contract of Lasna does not have it.
LEDGER_EVENTS = ("CallbackFailure",)

# The value types that the page encodes and decodes: one 32-byte word each.
_WORD_TYPE = re.compile(r"^(address|bool|uint(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160"
                        r"|168|176|184|192|200|208|216|224|232|240|248|256))$")
_URL_IN_TEXT = re.compile(r"http", re.IGNORECASE)
_EXTERNAL = (
    (re.compile(r"<script[^>]*\ssrc\s*=", re.IGNORECASE), "a script with a `src`"),
    (re.compile(r"<link\b", re.IGNORECASE), "a `link` element"),
    (re.compile(r"<(img|iframe|object|embed|audio|video|source)\b", re.IGNORECASE), "an embedded resource"),
    (re.compile(r"@import|@font-face|(?<![A-Za-z0-9_.])url\("), "a style sheet reference"),
)


class BoardError(OpsError):
    """The build of the board refuses its input."""


# ----------------------------------------------------------------------------------------------
# ABI fragments
# ----------------------------------------------------------------------------------------------


def _words(parameters, prefix="") -> list:
    """The 32-byte words of a parameter list, in order: [name, type]. A tuple gives the words of its members."""
    words = []
    for index, parameter in enumerate(parameters):
        name = prefix + (parameter.get("name") or ("" if len(parameters) == 1 and not prefix else str(index)))
        kind = parameter["type"]
        if kind == "tuple":
            words += _words(parameter["components"], f"{name}." if name else "")
        elif _WORD_TYPE.match(kind):
            words.append([name, kind])
        else:
            raise BoardError(f"the page does not decode the type {kind} of the parameter {name or index}")
    return words


def _function(abi: ops.Abi, name: str) -> dict:
    item = abi._item("function", name)
    if item.get("stateMutability") not in ("view", "pure"):
        raise BoardError(f"{abi.name}.{name} is not a view function: the page sends only read requests")
    try:
        return {
            "selector": "0x" + abi.selector(name).hex(),
            "inputs": [kind for _, kind in _words(item["inputs"])],
            "outputs": _words(item["outputs"]),
        }
    except BoardError as error:
        raise BoardError(f"{abi.name}.{name}: {error}") from None


def _event(abi: ops.Abi, name: str, count_only=False) -> dict:
    """Topic 0 and the parameters of an event. The page reads no data word of a `count_only` event."""
    item = abi._item("event", name)
    if item.get("anonymous"):
        raise BoardError(f"{abi.name}.{name} is anonymous: it has no topic 0")
    fragment = {"topic": abi.topic(name), "indexed": [], "data": []}
    for parameter in item["inputs"]:
        if parameter.get("indexed"):
            if not _WORD_TYPE.match(parameter["type"]):
                raise BoardError(f"{abi.name}.{name}: the page does not read the indexed type {parameter['type']}")
            fragment["indexed"].append([parameter["name"], parameter["type"]])
        elif not count_only:
            try:
                fragment["data"] += _words([parameter])
            except BoardError as error:
                raise BoardError(f"{abi.name}.{name}: {error}") from None
    return fragment


def abi_fragments(artifacts: Path = None) -> dict:
    """The functions and events that the page uses, from the build output of the hub and the vault."""
    hub = ops.load_artifact("LifelineHub", artifacts).abi
    vault = ops.load_artifact("LifelineVault", artifacts).abi
    ledger = ops.load_artifact("ICallbackProxy", artifacts).abi
    return {
        "hub": {
            "functions": {name: _function(hub, name) for name in HUB_FUNCTIONS},
            "events": {name: _event(hub, name) for name in HUB_EVENTS},
        },
        "vault": {
            "functions": {name: _function(vault, name) for name in VAULT_FUNCTIONS},
            "events": {name: _event(vault, name) for name in VAULT_EVENTS},
        },
        "ledger": {
            "functions": {name: _function(ledger, name) for name in LEDGER_FUNCTIONS},
            "events": {name: _event(ledger, name, count_only=True) for name in LEDGER_EVENTS},
        },
    }


# ----------------------------------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------------------------------


def _block(value, what) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BoardError(f"{what} is not a block number: {value!r}")
    return value


def _rpc_list(value, what) -> list:
    if not isinstance(value, list) or not value:
        raise BoardError(f"{what} has no RPC URL: the page needs one default for each chain")
    for url in value:
        parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.netloc:
            raise BoardError(f"{what} has an entry that is not an http or https URL: {url!r}")
    if len(set(value)) != len(value):
        raise BoardError(f"{what} has the same URL two times")
    return list(value)


def _no_url(text: str, what: str) -> str:
    """The output names no URL other than the RPC URLs, so a label must not hold one."""
    if _URL_IN_TEXT.search(text):
        raise BoardError(f"{what} holds a URL: the built file names no URL other than the RPC URLs")
    return text


def manifest_content(data: dict) -> dict:
    """The manifest content that the page uses. The chain of the hub is first; the vaults keep their order."""
    chains, contracts = data.get("chains") or {}, data.get("contracts") or {}
    hub = contracts.get("hub") or {}
    if not hub.get("address"):
        raise BoardError("the manifest has no hub: run deploy first")
    vaults = {name: entry for name, entry in (contracts.get("vaults") or {}).items() if (entry or {}).get("address")}

    def chain(name, role, contract, what):
        entry = chains.get(name)
        if not isinstance(entry, dict):
            raise BoardError(f"{what} is on the chain {name!r}, and the manifest has no chains.{name}")
        chain_id = entry.get("chainId")
        if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id <= 0:
            raise BoardError(f"chains.{name}.chainId is not a chain ID: {chain_id!r}")
        if contract.get("chainId") != chain_id:
            raise BoardError(f"{what} has chainId {contract.get('chainId')!r}, and chains.{name} has {chain_id}")
        return {
            "name": _no_url(name, f"the chain name {name!r}"),
            "chainId": chain_id,
            "role": role,
            "unit": HUB_UNIT if role == "hub" else DESTINATION_UNIT,
            "ledger": ops.parse_address(entry.get("ledger"), f"chains.{name}.ledger"),
            "rpc": _rpc_list(entry.get("rpc"), f"chains.{name}.rpc"),
            "contract": ops.parse_address(contract["address"], f"the address of {what}"),
            "deployBlock": _block(contract.get("deployBlock"), f"deployBlock of {what}"),
        }

    hub_chain = hub.get("chain")
    if hub_chain in vaults:
        raise BoardError(f"the chain {hub_chain!r} has the hub and a vault")
    listed = [chain(hub_chain, "hub", hub, "the hub")]
    listed += [chain(name, "vault", entry, f"the vault of {name}") for name, entry in vaults.items()]
    if len({entry["chainId"] for entry in listed}) != len(listed):
        raise BoardError("two chains of the manifest have the same chainId")
    names = {entry["name"] for entry in listed}

    # One row for each target of a chain: the newest record of the manifest has the label.
    enrollments = {}
    for index, record in enumerate(data.get("enrollments") or []):
        what = f"enrollments[{index}]"
        if not isinstance(record, dict) or record.get("chain") not in names:
            raise BoardError(f"{what} is on a chain with no contract of the manifest: {(record or {}).get('chain')!r}")
        target = ops.parse_address(record.get("target"), f"{what}.target")
        label = record.get("label")
        if not isinstance(label, str) or not label.strip():
            raise BoardError(f"{what}.label is not a text")
        key = (record["chain"], target.lower())
        enrollments.pop(key, None)
        enrollments[key] = {
            "label": _no_url(label.strip(), f"{what}.label"),
            "chain": record["chain"],
            "target": target,
            "guest": bool(record.get("guest")),
            "removed": bool(record.get("removed")),
        }
    cron = data.get("cronTopic")
    return {
        "cronTopic": _no_url(cron, "cronTopic") if isinstance(cron, str) else None,
        "chains": listed,
        "enrollments": list(enrollments.values()),
    }


# ----------------------------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------------------------


def _lookback(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LOOKBACK:
        raise BoardError(f"the lookback is a number of blocks from 1 to {MAX_LOOKBACK}: {value!r}")
    return value


def _inline(value) -> str:
    """JSON for a script element: no `<`, `>` or `&` can end the element, start a comment or make an entity."""
    text = json.dumps(value, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _check_self_contained(page: str, rpc_urls) -> None:
    """Refuses an output that names an external resource, or a URL other than the RPC URLs."""
    for pattern, what in _EXTERNAL:
        if pattern.search(page):
            raise BoardError(f"the page has {what}: the output must be one self-contained file")
    rest = page
    for url in sorted(rpc_urls, key=len, reverse=True):
        rest = rest.replace(_inline(url)[1:-1], "")
    found = _URL_IN_TEXT.search(rest)
    if found:
        line = rest.count("\n", 0, found.start()) + 1
        raise BoardError(f"the page names a URL other than the RPC URLs (line {line} of the output): "
                         f"{rest[max(found.start() - 30, 0):found.start() + 40]!r}")


def render(manifest: dict, lookback: int = DEFAULT_LOOKBACK, artifacts: Path = None, template: Path = None) -> str:
    """The text of the built page for the data of a manifest."""
    template = Path(template or DEFAULT_TEMPLATE)
    try:
        source = template.read_text(encoding="utf-8")
    except OSError as error:
        raise BoardError(f"template {template}: {error.strerror or error}") from None
    if source.count(PLACEHOLDER) != 1:
        raise BoardError(f"the template {template} must have {PLACEHOLDER} one time")

    content = manifest_content(manifest)
    config = {
        "build": "",
        "lookback": _lookback(lookback),
        "chunk": CHUNK_BLOCKS,
        "maxLookback": MAX_LOOKBACK,
        **content,
        "targetStates": list(ops.TARGET_STATES),
        "outcomes": list(ops.OUTCOMES),
        "abi": abi_fragments(artifacts),
    }
    # The build ID names the inputs: the template and the inlined data. It has no time and no path.
    digest = hashlib.sha256(source.encode() + b"\0" + json.dumps(config, sort_keys=True).encode())
    config["build"] = digest.hexdigest()[:12]
    page = source.replace(PLACEHOLDER, _inline(config))
    _check_self_contained(page, [url for chain in content["chains"] for url in chain["rpc"]])
    return page


def build(manifest_path=None, out=None, lookback: int = DEFAULT_LOOKBACK) -> Path:
    """Writes the page for a manifest file in one step: a failed build leaves the old file."""
    manifest = ops.Manifest.load(manifest_path)
    out = Path(out or DEFAULT_OUT)
    page = render(manifest.data, lookback).encode()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(out.name + ".tmp")
        temporary.write_bytes(page)
        os.replace(temporary, out)
    except OSError as error:
        raise BoardError(f"output {out}: {error.strerror or error}") from None
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lifeline.board",
                                     description="Build the board: one self-contained HTML file.")
    parser.add_argument("--manifest", metavar="<file>", help="default: deployments/manifest.json")
    parser.add_argument("--out", metavar="<file>", help="default: board/index.html")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK, metavar="<blocks>",
                        help=f"default lookback of the event queries (default: {DEFAULT_LOOKBACK})")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = build(args.manifest, args.out, args.lookback)
    except OpsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"board: wrote {out} ({out.stat().st_size} bytes), default lookback {args.lookback} blocks")
    print(f"open it in a browser: {out.resolve().as_uri()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
