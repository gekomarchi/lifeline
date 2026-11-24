"""Writes `demo/claims.md`: one row for each number, hash, address, block and factual statement of the video.

    python demo/claims.py [--offline]

A row has the time in the video, the text that the video shows or says, the source (file and key, or
section), and the value there. The command does not copy a value by hand:

- The value comes from the evidence file, the manifest, the README or the chain at the time of the run.
- The text of the video is built from that value, and the command then looks for it where the video has it:
  in the narration text, in the terminal recording, or in the text of the framed element of a page, as
  `surfaces.py` read it from the page when it made the picture.

A row that is not equal makes the exit status 1, and `claims.md` shows it. A row with the source "chain" is
read from the RPC of the manifest (5 small reads); with `--offline` such a row is "not checked".
Run it after `python demo/compose.py`: the times are the ones of `demo/build/timeline.json`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import compose  # noqa: E402
from lifeline import ops  # noqa: E402

WORDS = {1: "one", 2: "two", 3: "three", 24: "twenty-four", 25: "twenty-five", 60: "sixty"}
# Words that the narration must not have: the claim limits of the specification.
FORBIDDEN = ("zero loss", "nobody else", "no one else", "first to", "faster", "better", "best", "pitstop",
             "reactivate", "guarantee")


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def eth(wei) -> str:
    return ops.from_wei(int(wei))


class Table:
    def __init__(self, offline: bool):
        self.cut = compose.Cut()
        self.timeline = self.cut.timeline
        self.offline = offline
        self.rows, self.failed = [], 0
        self.narration = {clip["id"]: clip["text"] for clip in self.timeline.clips.values()}
        self.subtitles = " ".join(cue["text"] for cue in self.cut.cues)
        self.terminal = "".join(text for _, text in next(s for s in self.cut.shots if s.is_terminal).terminal.events)
        self.readme = (ROOT / "README.md").read_text()
        self.shots = {shot.id: shot for shot in self.cut.shots}
        self._pages = {}

    def page_text(self, surface: str, region: str) -> str:
        if surface not in self._pages:
            self._pages[surface] = json.loads((compose.WEB / f"{surface}.json").read_text())["texts"]
        return self._pages[surface][region]

    def add(self, when, kind: str, text: str, source: str, value, equal) -> None:
        at = self.timeline.at(when) if isinstance(when, str) else when
        if equal is None:
            verdict = "not checked"
        else:
            verdict = "yes" if equal else "NO"
            self.failed += 0 if equal else 1
        self.rows.append((at, kind, text, source, str(value), verdict))

    # The three places of a text in the video ------------------------------------------------

    def said(self, clip: str, words: str, source: str, value, also=True) -> None:
        """The narration says these words. The time is the time of the words."""
        found = words.lower() in self.narration[clip].lower()
        self.add(f"{clip}@{compose.spoken_map(words, self.timeline.spoken)[0]}" if found else clip, "said",
                 f"\"{words}\"", source, value, None if also is None else found and bool(also))

    def shown(self, shot: str, region: str, text: str, source: str, value=None, surface: str = None,
              when: str = None) -> None:
        """The framed element of a page shows this text."""
        surface = surface or self.shots[shot].surface
        found = text in self.page_text(surface, region)
        self.add(when or self.shots[shot].start + 0.3, "shown", f"`{text}`", source, text if value is None else value,
                 found)

    def typed(self, text: str, source: str, value=None, when: str = "n09$") -> None:
        """The terminal recording shows this text."""
        self.add(when, "shown", f"`{text}`", source, text if value is None else value, text in self.terminal)

    # The chain ------------------------------------------------------------------------------

    def chain(self, read):
        """One read of the chain, or None with `--offline` or with no answer."""
        if self.offline:
            return None
        try:
            return read()
        except (ops.OpsError, ops.RpcError, ops.TransportError):
            return None


def build(table: Table) -> None:
    t = table
    manifest, judge = load("deployments/manifest.json"), load("evidence/judge/data.json")
    network, capture = load("evidence/network/data.json")["queries"], load("evidence/demo/capture.json")
    config = load("deployments/config.json")
    arms, fixtures = judge["experiment"]["arms"], manifest["fixtures"]
    flood = load("evidence/experiment/data.json")["junk"]
    enrollments = {record["label"]: record for record in manifest["enrollments"]}
    rescue = next(r for r in judge["rescues"] if r["requestId"] == capture["requestId"])
    latency = next(r for r in judge["latency"]["rescues"] if r["tx"] == rescue["result"]["tx"])
    J, N, M = "evidence/judge/data.json", "evidence/network/data.json", "deployments/manifest.json"

    chains = {name: ops.open_chain(name, entry, {}) for name, entry in manifest["chains"].items()}
    ledger_abi = None if t.offline else ops.load_artifact("ICallbackProxy").abi

    def debt(chain: str, account: str):
        rpc = chains[chain].rpc
        return t.chain(lambda: ledger_abi.decode("debt", rpc.call(chains[chain].ledger,
                                                                  ledger_abi.encode("debt", account))))

    # Part 1 ---------------------------------------------------------------------------------
    voice = t.timeline.voice
    t.said("n01", "generated with ElevenLabs", "demo/narration.json: voice.engine", voice["engine"],
           voice["engine"] == "ElevenLabs")
    first = min(t.timeline.clips.values(), key=lambda clip: clip["start"])
    t.add(first["start"], "said", "the first sentence is the line about the narration",
          "demo/narration.json: clips[0]", first["id"], first["id"] == "n01" and first["part"] == 1)

    # Part 2: the problem --------------------------------------------------------------------
    arm_a = fixtures["armA.reactive"]["address"]
    t.shown("s01", "head", arm_a, f"{M}: fixtures.armA.reactive.address")
    reserves = arms["A"]["capitalLocked"]["reactive.reserves"]
    t.shown("s01", "reserves", "Contract reserves : 0,0000 REACT",
            f"{J}: experiment.arms.A.capitalLocked[reactive.reserves]", reserves, when="n02|+0.9")
    owed = debt("lasna", arm_a)
    text = "not read" if owed is None else f"Contract debt : {owed // 10**14 / 10**4:.4f} REACT".replace(".", ",")
    t.add("n02|+0.9", "shown", f"`{text}`", "chain: debt(armA.reactive) at the system contract of Lasna, now; "
          "Reactscan cuts the value to 4 decimals", owed,
          None if owed is None else text in t.page_text("reactscan-arm-a", "debt"))
    t.said("n02", "ran out of gas credit", f"{J}: experiment.arms.A.capitalLocked[reactive.reserves], and the debt "
           "of the row above", f"reserves {reserves}, debt {owed}", reserves == "0" and (owed is None or owed > 0))
    status = t.page_text("reactscan-arm-a", "status")
    t.said("n02", "Each transaction to it still shows success",
           "the Reactscan page of armA.reactive: the status column of the picture",
           f"{status.count('Success')} rows with Success, 0 other", status.count("(v) Success") >= 20
           and not re.search(r"fail|revert", status, re.I))
    t.shown("s01", "counts", f"// Transactions [{arms['A']['pokes']}]", f"{J}: experiment.arms.A.pokes",
            arms["A"]["pokes"], when="n02@But the network")
    t.shown("s01", "counts", f"// Reactive Executions [{arms['A']['ticks']}]", f"{J}: experiment.arms.A.ticks",
            arms["A"]["ticks"], when="n02@But the network")
    t.said("n02", "the network gives it no more work", f"{J}: experiment.arms.A.eventsMissed",
           arms["A"]["eventsMissed"], arms["A"]["eventsMissed"] == arms["A"]["pokes"] - arms["A"]["ticks"] > 0)

    build_debt = network["buildathon_debt"]["result"]
    t.shown("s02", "problemBuildathon", f"{build_debt['inDebt']} of {build_debt['submissions']}",
            f"{N}: queries.buildathon_debt.result.inDebt, .submissions", when="n03")
    t.shown("s02", "problemBuildathon", f"Lasna block {build_debt['block']}",
            f"{N}: queries.buildathon_debt.result.block", build_debt["block"], when="n03")
    t.said("n03", "more than a quarter of the submissions", f"{N}: queries.buildathon_debt.result",
           f"{build_debt['inDebt']} of {build_debt['submissions']} = "
           f"{build_debt['inDebt'] / build_debt['submissions']:.1%}",
           0.25 < build_debt["inDebt"] / build_debt["submissions"] < 1 / 3)
    stopped = network["stopped_contracts"]["result"]["byChain"]["lasna"]
    dropped = network["dropped_callbacks"]["result"]["dropped"]
    for text, source in (
        (f"{network['lasna_debt_events']['result']['count']} debt events on Lasna",
         "queries.lasna_debt_events.result.count"),
        (f"{network['destination_debt_events']['result']['total']} on the destination chains",
         "queries.destination_debt_events.result.total"),
        (f"{stopped['stopped']} of {stopped['cohort']}", "queries.stopped_contracts.result.byChain.lasna"),
        (f"{network['repeat_debtors']['result']['byChain']['lasna']['contracts']} ran out 3 times or more",
         "queries.repeat_debtors.result.byChain.lasna.contracts"),
        (f"About {dropped} callbacks were dropped", "queries.dropped_callbacks.result.dropped"),
    ):
        t.shown("s02", "problem", text, f"{N}: {source}")
    own = int(network["fastfill_reactive_reserves"]["result"]["reservesWei"])
    t.shown("s02", "problemOwn", f"{own / 10**18:.2f} lREACT",
            f"{N}: queries.fastfill_reactive_reserves.result.reservesWei", own, when="n04")
    t.said("n04", "about sixty lREACT", f"{N}: queries.fastfill_reactive_reserves.result.reservesWei",
           f"{own} wei = {eth(own)} lREACT", round(own / 10**18) == 60)
    sentence = "in reserves that cannot be withdrawn, because over-funding was the only protection"
    t.said("n04", "cannot be withdrawn", "README.md, section Problem (the sentence is on screen)", sentence,
           sentence in t.readme and sentence in t.page_text("readme", "problemOwn"))
    t.said("n04", "Over-funding was the only protection", "README.md, section Problem", sentence, sentence in t.readme)
    fleet = {entry["label"]: entry for entry in judge["fleet"]}
    reactive = enrollments["FastfillReactive"]
    kept = int(fleet["FastfillReactive"]["reserves"]["value"])
    t.shown("s03", "reserves:FastfillReactive", f"{kept // 10**14 / 10**4:.4f} lREACT",
            f"{J}: fleet[FastfillReactive].reserves.value; the board cuts the value to 4 decimals", kept)
    t.shown("s03", "reserves:FastfillReactive",
            f"low {eth(reactive['policy']['low'])} lREACT · high {eth(reactive['policy']['high'])} lREACT",
            f"{M}: enrollments[FastfillReactive].policy.low, .high",
            f"{reactive['policy']['low']}, {reactive['policy']['high']}")

    # Part 3: what Lifeline is ---------------------------------------------------------------
    for clip, words, sentence in (
        ("n05", "a gas guardian that I set once", "Lifeline is the guardian I set once"),
        ("n05", "listens to the network's own debt event", "The hub subscribes to the network's own debt event"),
        ("n05", "pays the debt, and refills the contract", "The payment is `debt + (high - reserves)`"),
        ("n05", "refills a contract on a cron tick, before it runs out",
         "each target with reserves below `low` is refilled to `high`"),
    ):
        t.said(clip, words, "README.md, first paragraph and section How it works", sentence, sentence in t.readme)
    t.add("n05@Its airbag", "shown", "the sequence diagram: AIRBAG and GAUGE",
          "README.md, section How it works: the Mermaid block, drawn by github.com", "sequenceDiagram",
          "Note over T,V: AIRBAG" in t.readme and "Note over T,V: GAUGE" in t.readme)

    # Part 4: set it once --------------------------------------------------------------------
    executor = enrollments["FastfillExecutor"]
    policy = executor["policy"]
    command = (f"--low {eth(policy['low'])} --high {eth(policy['high'])} --max-debt {eth(policy['maxDebt'])} "
               f"--daily-cap {eth(policy['dailyCap'])} --max-rescues {policy['maxRescues']} "
               f"--window {policy['window']}")
    t.shown("s06", "enroll", command, f"{M}: enrollments[FastfillExecutor].policy (wei as a decimal amount)",
            json.dumps(policy))
    t.said("n06", "Enrollment is one command", "README.md, section Operator guide", "python -m lifeline.ops enroll",
           "python -m lifeline.ops enroll --chain" in t.page_text("readme", "enroll"))
    t.said("n06", "two refill marks, a daily budget, and a limit of rescues",
           f"{M}: the fields of enrollments[FastfillExecutor].policy", ", ".join(policy),
           {"low", "high", "dailyCap", "maxRescues", "window"} <= set(policy))
    labels = [entry["label"] for entry in config["fleet"]]
    active = all("Active" in t.page_text("board", f"row:{label}") for label in labels)
    t.said("n07", "Two of my automations are enrolled", "deployments/config.json: fleet; the board shows each "
           "one with the hub state Active", ", ".join(labels), len(labels) == 2 and active
           and all(not enrollments[label].get("removed") for label in labels))
    for label in labels:
        marks = enrollments[label]["policy"]
        unit = "lREACT" if enrollments[label]["chain"] == "lasna" else "ETH"
        t.shown("s07", f"row:{label}", f"low {eth(marks['low'])} {unit} · high {eth(marks['high'])} {unit}",
                f"{M}: enrollments[{label}].policy.low, .high", f"{marks['low']}, {marks['high']}")
    t.shown("s08", "head", executor["txs"]["enroll"], f"{M}: enrollments[FastfillExecutor].txs.enroll")
    t.shown("s08", "block", f"Block number : {executor['txs']['enrollBlock']:,}",
            f"{M}: enrollments[FastfillExecutor].txs.enrollBlock", executor["txs"]["enrollBlock"])
    shown_policy = ("{" + ", ".join(f"{key}: {policy[key]}" for key in ops.POLICY_FIELDS) + "}")
    t.shown("s08", "input", f"chainId : {executor['chainId']} , target : {executor['target']} , policy : "
            f"{shown_policy}", f"{M}: enrollments[FastfillExecutor].chainId, .target, .policy",
            when="n07@each with|+0.5")

    # Part 5: a live rescue ------------------------------------------------------------------
    t.said("n08", "a live rescue on testnet", "evidence/demo/capture.json: chain, chainId; the terminal recording",
           f"{capture['chain']} ({capture['chainId']})",
           capture["chainId"] == manifest["chains"][capture["chain"]]["chainId"])
    t.said("n08", "its callback contract on Base Sepolia", "evidence/demo/capture.json: target, chain",
           f"{capture['target']} on {capture['chain']}",
           capture["chain"] == "base-sepolia" and capture["target"] == fixtures["armC.callback"]["address"])
    t.typed("python -m lifeline.demo capture", "the command of lifeline/demo.py", when="n08")
    for name in ("armC.reactive", "armC.callback"):
        t.typed(f"{name} {fixtures[name]['address']}", f"{M}: fixtures.{name}.address", when="n08@This command")
    pieces = table.shots["s09"].pieces
    real = table.shots["s09"].real_span
    recording = table.shots["s09"].terminal
    last_poke = recording.when("poke()", sum(1 for _, text in recording.events if "poke()" in text) - 1)
    t.said("n09", "at real speed, with no cut", "demo/build/timeline.json: terminal.pieces; the last piece of the "
           "recording starts before the last poke() and has no cut to the end",
           f"one piece from {pieces[-1][1]:.2f} s of the recording; debt() 0 at {real[1]:.2f} s",
           pieces[-1][1] < last_poke < real[1] and pieces[-1][0] == max(a for a, _ in pieces))
    blocks = capture["blocks"]
    for step, key, entry, chain in (("1 debt event", "debtEvent", "debtEvent", capture["chain"]),
                                    ("2 hub reaction", "relayed", "relayed", "lasna"),
                                    ("3 vault callback", "result", "result", capture["chain"]),
                                    ("4 debt() is 0", "debtZero", "whitelist", capture["chain"])):
        line = f"{step:<18}block {blocks[key]} on {chain}"
        same = blocks[key] == rescue[entry]["block"]
        t.add("n10" if key == "debtEvent" else {"relayed": "n10@The hub", "result": "n10@The vault",
                                                 "debtZero": "n10@The debt is"}[key],
              "shown", f"`{' '.join(line.split())}`",
              f"evidence/demo/capture.json: blocks.{key}; {J}: rescues[requestId].{entry}.block",
              f"{blocks[key]}; {rescue[entry]['block']}", same and line in t.terminal)
    t.typed(f"requestId {capture['requestId']}", f"evidence/demo/capture.json: requestId; {J}: rescues[].requestId",
            when="n10@The hub")
    for key in ("debtEvent", "relayed", "result"):
        t.typed(f"tx {rescue[key]['tx']}", f"{J}: rescues[requestId].{key}.tx", when="n10")
    t.typed(f"outcome {rescue['result']['outcome']}", f"{J}: rescues[requestId].result.outcome", when="n10@The vault")
    paid = int(rescue["result"]["paid"])
    t.typed(f"paid {ops.amount(paid)}", f"{J}: rescues[requestId].result.paid", paid, when="n10@The vault")
    t.said("n10", "The debt is zero", f"{J}: rescues[requestId].whitelist (the proxy emits it in the transaction "
           "of the result)", rescue["whitelist"]["tx"], rescue["whitelist"]["tx"] == rescue["result"]["tx"])

    def senders():
        owner = ops.load_artifact("CanaryReactive").abi
        account = owner.decode("owner", chains["lasna"].rpc.call(fixtures["armC.reactive"]["address"],
                                                                owner.encode("owner")))
        return account, [chains[chain].rpc.transaction(rescue[key]["tx"])["from"]
                         for chain, key in (("lasna", "relayed"), (capture["chain"], "result"))]

    sent = t.chain(senders)
    proof = "Nobody sent a transaction after step 1: the network ran steps 2 and 3."
    t.said("n10", "Nobody sent a transaction after the first step",
           "README.md, section Proof; chain: the senders of the Relayed and the Result transaction are accounts "
           "of the node, not the account of the fixture key",
           "not read" if sent is None else f"senders {', '.join(sent[1])}; fixture account {sent[0]}",
           None if sent is None else proof in t.readme and all(not ops.same_address(a, sent[0]) for a in sent[1]))

    t.shown("s10", "head", rescue["relayed"]["tx"], f"{J}: rescues[requestId].relayed.tx")
    t.shown("s10", "block", f"Block number : {rescue['relayed']['block']:,}", f"{J}: rescues[requestId].relayed.block",
            rescue["relayed"]["block"], when="n11|+0.25")
    t.shown("s10", "callbacks", f"Base Sepolia ( {capture['chainId']} )", "evidence/demo/capture.json: chainId",
            capture["chainId"], when="n11@with its callback")
    vault = manifest["contracts"]["vaults"][capture["chain"]]["address"]
    t.shown("s10", "callbacks", f"Destination : {vault.lower()}", f"{M}: contracts.vaults.{capture['chain']}.address",
            vault, when="n11@with its callback")
    t.shown("s10", "destination", f"Destination tx : {rescue['result']['tx']}", f"{J}: rescues[requestId].result.tx",
            when="n11@with its callback|+0.5")
    t.shown("s10", "callbacks", f"Requested by : {manifest['contracts']['hub']['address']}",
            f"{M}: contracts.hub.address", when="n11@with its callback")

    def stamps():
        rpc = chains[capture["chain"]].rpc
        return [ops.hex_int(rpc.block(rescue[key]["block"])["timestamp"]) for key in ("debtEvent", "result")]

    times = t.chain(stamps)
    for n, (shot, key) in enumerate((("s11", "debtEvent"), ("s12", "result"))):
        when = "n12|+0.35" if n == 0 else "n12@The payment|+0.3"
        t.shown(shot, "details", rescue[key]["tx"], f"{J}: rescues[requestId].{key}.tx", when=when)
        t.shown(shot, "block", f"Block {rescue[key]['block']}", f"{J}: rescues[requestId].{key}.block",
                rescue[key]["block"], when=when)
        text = "not read" if times is None else time.strftime("%b %d %Y %H:%M:%S (+00:00 UTC)", time.gmtime(times[n]))
        t.add(when, "shown", f"`{text}`", f"chain: the timestamp of block {rescue[key]['block']} of "
              f"{capture['chain']}", "not read" if times is None else times[n],
              None if times is None else text in t.page_text(table.shots[shot].surface, "timestamp"))
    t.said("n12", "one second later", f"{J}: latency.rescues[tx of the result].seconds (.blocks {latency['blocks']})",
           latency["seconds"], latency["seconds"] == 1 and latency["trigger"] == "debt event"
           and (times is None or times[1] - times[0] == 1))

    # Part 6: the difference -----------------------------------------------------------------
    for label, key in (("Trigger events sent", "pokes"), ("Reactions that ran on Lasna", "ticks"),
                       ("Callbacks delivered on Base Sepolia", "pongs")):
        row = f"{label} {arms['A'][key]} {arms['B'][key]} {arms['C'][key]}"
        t.shown("s13", "arms", row, f"{J}: experiment.arms.A/B/C.{key}", when="n13|+0.4")
    for chain, label in (("lasna", "Blocks of downtime on Lasna"),
                         ("base-sepolia", "Blocks of downtime on Base Sepolia")):
        row = f"{label} " + " ".join(str(arms[arm]["downtimeBlocks"][chain]) for arm in "ABC")
        t.shown("s13", "arms", row, f"{J}: experiment.arms.A/B/C.downtimeBlocks.{chain}", when="n13|+0.4")
    same = [json.dumps(arms[arm]["pokes"]) for arm in "ABC"]
    t.said("n13", "Three identical automations got the same load", f"{J}: experiment.arms.A/B/C.pokes; README.md, "
           "section Lifeline against the alternatives", ", ".join(same), len(set(same)) == 1
           and "Three identical canary automations got the same load at the same time" in t.readme)
    for words, arm in (("With no guardian, three callbacks arrived", "A"), ("With a cron poller, also three", "B"),
                       ("With Lifeline, all twenty-four", "C")):
        number = arms[arm]["pongs"]
        t.said("n13", words, f"{J}: experiment.arms.{arm}.pongs (guardian: {arms[arm]['guardian']})", number,
               WORDS[number] in words and (arm != "C" or number == arms[arm]["pokes"]))
    dead = network["cron_poller_ward_dead_time"]["result"]["longestSeconds"]
    t.shown("s13", "armB", f"B was dead on Lasna for {dead} s",
            f"{N}: queries.cron_poller_ward_dead_time.result.longestSeconds", dead, when="n14|+0.3")
    t.said("n14", "dead for about two hours", f"{N}: queries.cron_poller_ward_dead_time.result.longestSeconds",
           f"{dead} s = {dead / 3600:.2f} h", round(dead / 3600) == 2 and abs(dead / 3600 - 2) < 0.25)
    summary = judge["latency"]["byChain"]
    for text, source, value in (
        (f"C delivered each of its {arms['C']['pongs']} callbacks", "experiment.arms.C.pongs", arms["C"]["pongs"]),
        (f"Each debt ended {summary['lasna']['largestSeconds']} s later", "latency.byChain.*.largestSeconds",
         [summary[chain]["largestSeconds"] for chain in summary]),
        (f"The {arms['C']['guardianDowntimeBlocks']['base-sepolia']} blocks of vault downtime",
         "experiment.arms.C.guardianDowntimeBlocks.base-sepolia", arms["C"]["guardianDowntimeBlocks"]["base-sepolia"]),
    ):
        ok = text in t.page_text("readme", "armsNotes") and (not isinstance(value, list) or len(set(value)) == 1)
        t.add("n14|+0.3", "shown", f"`{text}`", f"{J}: {source}", value, ok)
    refiller = ("Lifeline is not a wallet refiller.** A wallet refiller sends native currency to an account. Lifeline "
                "pays the debt of a contract at the gas ledger of Reactive Network: a stopped contract starts again "
                "only when that debt is paid.")
    for words in ("Lifeline is not a wallet refiller", "A refiller sends currency to an account",
                  "Lifeline pays a contract's debt at the gas ledger", "only that restarts a stopped contract"):
        t.said("n15", words, "README.md, section Lifeline against the alternatives (the sentence is on screen)",
               refiller.replace("**", ""), refiller in t.readme
               and refiller.replace("**", "") in t.page_text("readme", "notRefiller"))
    t.shown("s13", "comparisonRestart", "Restarts a stopped reactive contract Yes - No: its source has no depositTo "
            "and no debt event", "README.md, the comparison table: the row is shown as the README has it",
            "| Restarts a stopped reactive contract | Yes | - | No: its source has no `depositTo` and no debt event |",
            when="n15@Lifeline pays")
    # Each row of the comparison table of the README, cell by cell, against the table of the page.
    plain = lambda text: " ".join(text.replace("`", "").replace("|", " ").split())  # noqa: E731
    start = t.readme.index("| | Lifeline | Against Reactivate |")
    rows = [line for line in t.readme[start:].split("\n\n")[0].splitlines() if not line.startswith("|---")]
    on_page = plain(t.page_text("readme", "comparison"))
    absent = [plain(row) for row in rows if plain(row) not in on_page]
    t.add("n15|+0.5", "shown", f"the comparison table: {len(rows)} rows",
          "README.md, section Lifeline against the alternatives: each row of the table, cell by cell",
          f"{len(rows) - len(absent)} of {len(rows)} rows are on the page", len(rows) == 11 and not absent)

    # Part 7: when things break --------------------------------------------------------------
    quarantine = judge["quarantine"]
    looping = enrollments["experiment looping"]
    t.shown("s14", "filterQuarantined", "Quarantined 1", f"{J}: quarantine.recorded; `python -m lifeline.ops status`: "
            "one target in the state Quarantined", quarantine["recorded"], surface="board-quarantined-details",
            when="n16|+0.95")
    t.shown("s14", "row:looping", "experiment looping", f"{M}: enrollments[experiment looping].label; {J}: "
            "quarantine.target", quarantine["target"], surface="board-quarantined-details", when="n16|+0.95")
    t.shown("s14", "rule", f"{looping['policy']['maxRescues']} rescues in {looping['policy']['window']:,} s",
            f"{M}: enrollments[experiment looping].policy.maxRescues, .window; {J}: quarantine.maxRescues",
            f"{looping['policy']['maxRescues']}, {looping['policy']['window']}; {quarantine['maxRescues']}",
            surface="board-quarantined-details", when="n16@After two|+0.9")
    t.shown("s14", "state", "Quarantined", f"{J}: quarantine.quarantined.block", quarantine["quarantined"]["block"],
            surface="board-quarantined-details", when="n16@and paid")
    owed = debt("lasna", quarantine["target"])
    text = "not read" if owed is None else f"{eth(owed)} lREACT"
    t.add("n16|+0.95", "shown", f"`{text}`", "chain: debt(looping canary) at the system contract of Lasna, now", owed,
          None if owed is None else text in t.page_text("board-quarantined-details", "row:looping"))
    t.said("n16", "After two rescues", f"{J}: quarantine.rescues (count), quarantine.maxRescues",
           f"{len(quarantine['rescues'])}, {quarantine['maxRescues']}", len(quarantine["rescues"]) == 2)
    t.said("n16", "the hub quarantined this looping test contract", f"{J}: quarantine.quarantined",
           json.dumps(quarantine["quarantined"]), quarantine["recorded"]
           and quarantine["target"] == fixtures["looping"]["address"])
    t.said("n16", "paid nothing more", f"{J}: quarantine.paidAfterQuarantine", quarantine["paidAfterQuarantine"],
           quarantine["paidAfterQuarantine"] == "0")

    E = "evidence/experiment/data.json"
    t.shown("s15", "junkFailures", f"CallbackFailure logs of the vault {flood['callbackFailures']}",
            f"{E}: junk.callbackFailures", flood["callbackFailures"], when="n17|+0.35")
    t.said("n17", "A flood of junk callbacks pushed one vault offline", f"{E}: junk.callbackFailures, .debtEnd; "
           f"{J}: junkRecovery.steps[BlacklistContract(vault)]", f"{flood['callbackFailures']} failed callbacks, "
           f"debt {flood['debtEnd']} at the end", int(flood["debtEnd"]) > 0
           and judge["junkRecovery"]["steps"][0]["step"] == "BlacklistContract(vault)")
    t.shown("s15", "junkReservesEnd", f"Reserves of the vault at the end block {flood['reservesEnd']}",
            f"{E}: junk.reservesEnd (start: junk.reservesStart {flood['reservesStart']})", flood["reservesEnd"],
            when="n17@It lost")
    t.said("n17", "It lost its small fee float", f"{E}: junk.reservesStart, .reservesEnd",
           f"{flood['reservesStart']} to {flood['reservesEnd']} wei ({eth(flood['reservesStart'])} ETH)",
           flood["reservesEnd"] == "0" and int(flood["reservesStart"]) > 0)
    t.shown("s15", "junkSelfCare", f"Sum of SelfCare.added {flood['selfCareAdded']}", f"{E}: junk.selfCareAdded",
            flood["selfCareAdded"], when="n17@and a little")
    t.said("n17", "a little self-care", f"{E}: junk.selfCareAdded; README.md, section Proof: 0.000122 ETH",
           f"{flood['selfCareAdded']} wei = {eth(flood['selfCareAdded'])} ETH",
           f"{int(flood['selfCareAdded']) / 10**18:.6f} ETH" in t.readme)
    t.shown("s15", "junkUnexplained", f"Balance change that the payments do not explain "
            f"{flood['unexplainedBalanceChange']}", f"{E}: junk.unexplainedBalanceChange; {J}: "
            "junkRecovery.unexplainedChange", flood["unexplainedBalanceChange"], when="n17@Its other")
    t.said("n17", "Its other funds were not touched", f"{E}: junk.unexplainedBalanceChange, .resultPaid",
           f"{flood['unexplainedBalanceChange']}, {flood['resultPaid']}",
           flood["unexplainedBalanceChange"] == "0" == judge["junkRecovery"]["unexplainedChange"]
           and int(flood["balanceStart"]) - int(flood["balanceEnd"]) == int(flood["selfCareAdded"]))
    t.shown("s15", "junkFlood", f"Balance of the vault at the start block {flood['balanceStart']}",
            f"{E}: junk.balanceStart")
    t.shown("s15", "junkFlood", f"Balance of the vault at the end block {flood['balanceEnd']}", f"{E}: junk.balanceEnd")

    steps = judge["junkRecovery"]["steps"]
    for step in steps:
        t.shown("s16", "junkRecoverySteps", f"{step['step']} {step['chain']} {step['block']} {step['tx']}",
                f"{J}: junkRecovery.steps[{step['step']}]", when="n18|+0.4")
    recovery = manifest["contracts"]["vaults"]["base-sepolia"]["recoveries"][-1]
    owner_txs = [recovery["deposit"], *(entry["tx"] for entry in recovery["vaultUp"])]
    by_step = {step["step"]: step["tx"] for step in steps}
    t.said("n18", "Recovery needed two transactions from the owner",
           f"{M}: contracts.vaults.base-sepolia.recoveries[-1].deposit, .vaultUp[]", ", ".join(owner_txs),
           len(owner_txs) == 2 and owner_txs == [by_step["WhitelistContract(vault)"], by_step["VaultUp"]])
    waiting = judge["junkRecovery"]["waitingRescue"]
    t.said("n18", "The hub then repeated the waiting rescue", f"{J}: junkRecovery.waitingRescue",
           json.dumps(waiting), waiting["outcome"] == "Rescued"
           and waiting["requestId"] == recovery["vaultUp"][-1]["requestId"])
    sweep = next(r for r in judge["latency"]["rescues"] if r["trigger"] == "sweep")
    t.shown("s17", "latencySweepRow", f"sweep {sweep['chain']} {sweep['target']} {sweep['deadFromBlock']} "
            f"{sweep['aliveAtBlock']} {sweep['blocks']} {sweep['seconds']}",
            f"{J}: latency.rescues[trigger sweep]", when="n18@twenty|+0.1")
    t.said("n18", "twenty-five seconds after its debt event", f"{J}: latency.rescues[trigger sweep].seconds; "
           "latency.sweepByChain.base-sepolia.largestSeconds", sweep["seconds"],
           WORDS[sweep["seconds"]] == "twenty-five" and sweep["tx"] == by_step["Result(waiting.callback)"])
    # The rows above the mark: each row of the page is a row of the evidence, with its blocks and seconds.
    others = [r for r in judge["latency"]["rescues"] if r["trigger"] == "debt event" and r["chain"] == "base-sepolia"]
    page_rows = t.page_text("judge", "latencyRows")
    # The column of the transaction is outside the width of the page: a row is known by its blocks.
    found = [r for r in others if f"debt event {r['chain']} {r['target']} {r['deadFromBlock']} {r['aliveAtBlock']} "
                                  f"{r['blocks']} {r['seconds']} " in page_rows + " "]
    t.add("n18@twenty|+0.1", "shown", f"the {len(found)} rows above the mark: a rescue that a debt event started, "
          "5 blocks, 1 s", f"{J}: latency.rescues[trigger debt event, base-sepolia] ({len(found)} of {len(others)} "
          "are on the page: see the note below)", sorted({(r["blocks"], r["seconds"]) for r in found}),
          len(found) == page_rows.count("debt event base-sepolia") > 0
          and all((r["blocks"], r["seconds"]) == (5, 1) for r in found))

    # Part 8: guests -------------------------------------------------------------------------
    t.shown("s18", "guests", "No guest enrolled.", f"{J}: guests", judge["guests"])
    t.said("n19", "No guest is enrolled yet", f"{J}: guests", judge["guests"], judge["guests"] == []
           and not any(record.get("guest") for record in manifest["enrollments"]))

    # Part 9: limits -------------------------------------------------------------------------
    limit = "Lifeline restores future delivery. Work that arrives while a target is in debt is lost."
    t.said("n20", "Lifeline restores future delivery", "README.md, section Limits (on screen)", limit,
           limit in t.readme and limit in t.page_text("readme", "limits"))
    t.said("n20", "Work that arrives during a debt is lost", "README.md, section Limits (on screen)", limit,
           limit in t.readme)
    t.said("n20", "Testnet only", "README.md, section Demo versus production (on screen)",
           "Demo: testnet, one owner", "Demo: testnet, one owner" in t.page_text("readme", "demoProduction"))
    not_built = t.page_text("readme", "notBuilt")
    t.said("n20", "A public pool and automatic vault recovery are not built", "README.md, section Not built "
           "(on screen)", "Public rescue pool; ...; automatic recovery of a vault that is in debt",
           "Public rescue pool" in not_built and "automatic recovery of a vault that is in debt" in not_built)

    # Part 10: where to check ----------------------------------------------------------------
    t.said("n21", "the repository, the live board, and JUDGE.md", "README.md, line 1: the links of the title",
           "https://gekomarchi.github.io/lifeline/board/, JUDGE.md",
           "https://gekomarchi.github.io/lifeline/board/" in t.readme.splitlines()[0]
           and "(JUDGE.md)" in t.readme.splitlines()[0] and "JUDGE.md" in t.page_text("readme", "top"))

    # The claim limits -----------------------------------------------------------------------
    spoken = " ".join(t.narration.values()).lower()
    found = [word for word in FORBIDDEN if re.search(rf"\b{re.escape(word)}\b", spoken + " " + t.subtitles.lower())]
    t.add(t.timeline.total, "said", "the whole narration and each subtitle", "the claim limits: no \"zero loss\", no "
          "\"nobody else\", no name of another team, no \"faster\" or \"better\"", f"words found: {found or 'none'}",
          not found)
    t.add(t.timeline.total, "said", "each subtitle is a part of the narration text", "demo/narration.json",
          f"{len(t.cut.cues)} subtitles", all(cue["text"] in t.narration[cue["clip"]] for cue in t.cut.cues))


def render(table: Table) -> str:
    def stamp(seconds: float) -> str:
        return f"{int(seconds // 60)}:{seconds % 60:04.1f}"

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    rows = sorted(table.rows, key=lambda row: row[0])
    captured = sorted({(shot.page.url, shot.page.captured) for shot in table.cut.shots if not shot.is_terminal})
    lines = [
        "# Demo video: claims table", "",
        "This file is written by `python demo/claims.py`. Do not edit it by hand.", "",
        "Each row is one number, hash, address, block or factual statement that the video shows or says. The "
        "command builds the text of the video from the value at the source and then looks for that text in the "
        "narration, in the terminal recording, or in the text of the framed element as the page showed it when "
        "its picture was made. \"Equal\" is the result of that check.", "",
        f"Rows: {len(rows)}. Not equal: {table.failed}. Not checked: "
        f"{sum(1 for row in rows if row[5] == 'not checked')}. Length of the cut: {table.timeline.total:.1f} s.", "",
        "| Time | Shown or said | Text in the video | Source | Value there | Equal |", "|---|---|---|---|---|---|",
        *[f"| {stamp(at)} | {kind} | {cell(text)} | {cell(source)} | {cell(value)} | {verdict} |"
          for at, kind, text, source, value, verdict in rows], "",
        "## Notes", "",
        "- **Pages.** A picture of a page is the public page at its URL, not changed. A value in a picture that "
        "has no row above (for example the gas price of a transaction on an explorer, or a board row of another "
        "target) is the value of that page at the time below.",
        "- **`JUDGE.md` on github.com.** The video shows `JUDGE.md` of the branch `main`. The capture of this unit "
        "added rows to `JUDGE.md` (the captured rescue, and two more rescues). Until that file is on `main`, the "
        "page does not have these rows. Each row that the video shows is equal to `evidence/judge/data.json`; the "
        "video shows no count of rescues from that page.",
        "- **Spoken amounts.** The narration says a rounded form (\"about sixty\", \"about two hours\", \"more than a "
        "quarter\"); the exact value is on screen in the same shot.", "",
        "| Page | Picture made (UTC) |", "|---|---|",
        *[f"| {url} | {when} |" for url, when in captured], "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="no read of the chain")
    args = parser.parse_args()
    table = Table(args.offline)
    build(table)
    (HERE / "claims.md").write_text(render(table))
    unchecked = sum(1 for row in table.rows if row[5] == "not checked")
    print(f"claims: {len(table.rows)} rows, {table.failed} not equal, {unchecked} not checked")
    for row in table.rows:
        if row[5] == "NO":
            print(f"  NOT EQUAL at {row[0]:.1f} s: {row[2]} | {row[3]} | {row[4]}")
    return 1 if table.failed else 0


if __name__ == "__main__":
    sys.exit(main())
