"""The check "equal to RPC" of the board: each value of an open page against a direct read.

`read_page` takes each field of the page, the block that the page shows for each chain, the block window
of each chain and the event rows. `expected_fields`, `expected_events` and `expected_failures` read the same
values from the nodes with the reader and the ABI of the operator commands, at those blocks. The fork dry
run (`test_board_forks.py`) and the check against the deployed fleet (`test_board_live.py`) use them.
"""

from __future__ import annotations

from lifeline import ops

# The fields of the page that are no value of a chain: addresses and URLs of the manifest, the build ID.
NOT_A_READ = ("rpc", "build", "contract", "ledger")

PAGE = """(() => {
  const all = [...document.querySelectorAll('[data-field]')];
  return {
    fields: all.map((e) => [e.dataset.field, e.dataset.raw === undefined ? null : e.dataset.raw,
                            e.hidden ? null : e.textContent]),
    windows: Object.fromEntries(all.filter((e) => e.dataset.field.startsWith('window:') && e.dataset.to)
      .map((e) => [e.dataset.field.slice(7), [Number(e.dataset.from), Number(e.dataset.to), e.dataset.phase]])),
    events: [...document.querySelectorAll('#events-body tr[data-event]')].map((row) =>
      [row.dataset.chain, row.dataset.event, Number(row.dataset.block), Number(row.dataset.index), row.dataset.tx]),
    states: Object.fromEntries([...document.querySelectorAll('#chains-body tr[data-chain]')]
      .map((row) => [row.dataset.chain, row.dataset.state])),
  };
})()"""


def read_page(page) -> dict:
    """The content of an open page after each read ended: fields, blocks, windows, events, chain states."""
    found = page.evaluate(PAGE)
    fields = {}
    for name, raw, text in found["fields"]:
        assert fields.setdefault(name, (raw, text))[0] == raw, f"the page has two values for {name}"
    found["text"] = {name: text for name, (raw, text) in fields.items()}
    found["fields"] = {name: raw for name, (raw, text) in fields.items()}
    found["blocks"] = {name[6:]: int(raw) for name, raw in found["fields"].items() if name.startswith("block:") and raw}
    found["events"] = [tuple(row) for row in found["events"]]
    return found


def chain_values(fields: dict) -> dict:
    """The fields that hold a value of a chain, with no address of the manifest."""
    return {name: raw for name, raw in fields.items()
            if name.split(":")[0] not in (*NOT_A_READ, "block", "window", "failures") and not name.endswith(".address")}


def brief(wei: int, unit: str) -> str:
    """The short form of an amount, as the page shows it: cut, never rounded up."""
    if wei == 0:
        return "0" + unit
    if wei < 10**9:
        return f"{wei:,}wei"
    whole, fraction = divmod(wei, 10**18)
    digits = f"{fraction:018d}"
    if whole:
        return f"{whole:,}.{digits[:4]}{unit}"
    first = len(digits) - len(digits.lstrip("0"))
    return "0." + digits[:first + 4].rstrip("0") + unit


def _text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def enrolled(manifest: dict) -> dict:
    """(chain, target in lower case) of each enrollment of the manifest."""
    return {(record["chain"], record["target"].lower()): record for record in manifest["enrollments"]}


def expected_fields(manifest: dict, rpcs: dict, blocks: dict) -> dict:
    """The value of each field of the page, from direct reads at the block of each chain in `blocks`.

    A chain that is not in `blocks` gave the page no answer: its values are empty.
    """
    hub_abi = ops.load_artifact("LifelineHub").abi
    vault_abi = ops.load_artifact("LifelineVault").abi
    ledger_abi = ops.load_artifact("ICallbackProxy").abi
    chains, hub = manifest["chains"], manifest["contracts"]["hub"]
    lasna, vaults = hub["chain"], manifest["contracts"]["vaults"]

    def read(name, to, abi, function, *arguments):
        return abi.decode(function, rpcs[name].call(to, abi.encode(function, *arguments), blocks[name]))

    def balance(name, account) -> int:
        return ops.hex_int(rpcs[name].request("eth_getBalance", [account, hex(blocks[name])]))

    def ledger(name, account) -> tuple:
        return (read(name, chains[name]["ledger"], ledger_abi, "debt", account),
                read(name, chains[name]["ledger"], ledger_abi, "reserves", account))

    fields = {}
    if lasna in blocks:
        debt, reserves = ledger(lasna, hub["address"])
        fields.update({f"funds:{lasna}.{key}": _text(value) for key, value in {
            "principal": balance(lasna, hub["address"]), "float": reserves, "debt": debt,
            "floor": read(lasna, hub["address"], hub_abi, "principalFloor"),
            "floatTarget": read(lasna, hub["address"], hub_abi, "selfReserveTarget"),
            "paused": read(lasna, hub["address"], hub_abi, "paused"),
            "cron": read(lasna, hub["address"], hub_abi, "cronSubscribed"),
        }.items()})
    else:
        fields.update({f"funds:{lasna}.{key}": "" for key in ("principal", "float", "debt", "floor", "floatTarget")})
    for name, vault in vaults.items():
        if name in blocks:
            debt, reserves = ledger(name, vault["address"])
            fields.update({f"funds:{name}.{key}": _text(value) for key, value in {
                "principal": balance(name, vault["address"]), "float": reserves, "debt": debt,
                "floor": read(name, vault["address"], vault_abi, "principalFloor"),
                "floatTarget": read(name, vault["address"], vault_abi, "selfReserveTarget"),
                "paused": read(name, vault["address"], vault_abi, "paused"),
            }.items()})
            if lasna in blocks:
                _, _, _, up, pending = read(lasna, hub["address"], hub_abi, "vaultInfo", chains[name]["chainId"])
                fields.update({f"funds:{name}.up": _text(up), f"funds:{name}.pending": _text(pending)})
        else:
            fields.update({f"funds:{name}.{key}": "" for key in ("principal", "float", "debt", "floor", "floatTarget")})

    targets = dict.fromkeys(enrolled(manifest))
    if lasna in blocks:
        for name in (lasna, *vaults):
            count = read(lasna, hub["address"], hub_abi, "targetCount", chains[name]["chainId"])
            for index in range(count):
                target = read(lasna, hub["address"], hub_abi, "targetAt", chains[name]["chainId"], index)
                targets[(name, target.lower())] = None
    for name, target in targets:
        base = f"target:{name}|{target}"
        state, policy = None, None
        if lasna in blocks:
            _, number, hub_policy = read(lasna, hub["address"], hub_abi, "targetInfo", chains[name]["chainId"], target)
            state = ops.TARGET_STATES[number]
            if name == lasna and state != "None":
                policy = hub_policy
        quarantined = ""
        if name != lasna and name in blocks:
            vault = vaults[name]["address"]
            if read(name, vault, vault_abi, "hasPolicy", target):
                policy = read(name, vault, vault_abi, "policyOf", target)
            quarantined = _text(read(name, vault, vault_abi, "quarantined", target))
        debt, reserves = ledger(name, target) if name in blocks else ("", "")
        fields.update({
            f"{base}.state": state or "", f"{base}.vaultQuarantine": quarantined,
            f"{base}.debt": _text(debt), f"{base}.reserves": _text(reserves),
            f"{base}.low": _text(policy[0]) if policy and name in blocks else "",
            f"{base}.high": _text(policy[1]) if policy and name in blocks else "",
        })
    return fields


def _logs(rpc, address, topics, window) -> list:
    return rpc.logs(address, topics, window[0], window[1], chunk=1_000)


def expected_events(manifest: dict, rpcs: dict, windows: dict) -> list:
    """(chain, event, block, log index, transaction) of each event of the page in the windows, newest first."""
    hub_abi = ops.load_artifact("LifelineHub").abi
    vault_abi = ops.load_artifact("LifelineVault").abi
    hub = manifest["contracts"]["hub"]
    sources = [(hub["chain"], hub["address"], hub_abi, ("HubLow", "Rescued", "Gap"))]
    sources += [(name, vault["address"], vault_abi, ("VaultLow", "Result"))
                for name, vault in manifest["contracts"]["vaults"].items()]
    rows = []
    for name, address, abi, events in sources:
        if name not in windows:
            continue
        for event in events:
            for log in _logs(rpcs[name], address, [abi.topic(event)], windows[name]):
                rows.append((name, event, ops.hex_int(log["blockNumber"]), ops.hex_int(log["logIndex"]),
                             log["transactionHash"]))
    return sorted(rows, key=lambda row: (row[0], -row[2], -row[3]))


def expected_failures(manifest: dict, rpcs: dict, windows: dict, recipients: dict) -> dict:
    """`failures:<chain>|<recipient>` -> the number of `CallbackFailure(recipient)` logs of the proxy in the window."""
    ledger_abi = ops.load_artifact("ICallbackProxy").abi
    counts = {}
    for name, accounts in recipients.items():
        for account in accounts:
            logs = _logs(rpcs[name], manifest["chains"][name]["ledger"],
                         [ledger_abi.topic("CallbackFailure"), ops.topic_of_address(account)], windows[name])
            counts[f"failures:{name}|{account.lower()}"] = str(len(logs))
    return counts


def check_page(found: dict, manifest: dict, rpcs: dict) -> None:
    """Each value of the page equals a direct read at the block that the page shows for its chain."""
    blocks, windows = found["blocks"], {name: window[:2] for name, window in found["windows"].items()}
    assert all(window[2] == "done" for window in found["windows"].values()), found["windows"]
    values = chain_values(found["fields"])
    expected = expected_fields(manifest, rpcs, blocks)
    assert values == expected, {name: (values.get(name), expected.get(name))
                                for name in {*values, *expected} if values.get(name) != expected.get(name)}

    # The visible text of each amount is the short form of the same value.
    units = {manifest["contracts"]["hub"]["chain"]: "lREACT"}
    amounts = ("principal", "float", "debt", "floor", "floatTarget", "reserves", "low", "high")
    for name, raw in values.items():
        if raw and name.rsplit(".", 1)[1] in amounts:
            chain = name.split(":")[1].split(".")[0].split("|")[0]
            assert found["text"][name] == brief(int(raw), units.get(chain, "ETH")), name

    # The window of each chain: from the later of deployBlock and head minus the lookback, to the head.
    deploy = {manifest["contracts"]["hub"]["chain"]: manifest["contracts"]["hub"]["deployBlock"],
              **{name: vault["deployBlock"] for name, vault in manifest["contracts"]["vaults"].items()}}
    for name, (start, end) in windows.items():
        assert end == blocks[name] and start >= deploy[name], (name, start, end)

    page_events = sorted(found["events"], key=lambda row: (row[0], -row[2], -row[3]))
    assert page_events == expected_events(manifest, rpcs, windows)
    for name in windows:
        blocks_of_chain = [row[2:4] for row in found["events"] if row[0] == name]
        assert blocks_of_chain == sorted(blocks_of_chain, reverse=True), f"{name}: the events are not newest first"

    shown = {name: raw for name, raw in found["fields"].items() if name.startswith("failures:")}
    recipients = {}
    for name in shown:
        chain, account = name[9:].split("|")
        recipients.setdefault(chain, []).append(account)
    # A chain with no window gave no answer: the page shows no count for it.
    expected = {f"failures:{name}|{account}": "" for name, accounts in recipients.items() if name not in windows
                for account in accounts}
    expected.update(expected_failures(manifest, rpcs, windows, {name: accounts for name, accounts in recipients.items()
                                                                if name in windows}))
    assert shown == expected
