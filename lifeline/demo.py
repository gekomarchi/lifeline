"""The capture step of the demo video.

    python -m lifeline.demo capture --rpc <chain>=<url> ... [--timeout <seconds>] [--manifest <file>]

`capture` drives the fixture automation of arm C into debt on its destination chain, with the fixture key
(`LIFELINE_FIXTURE_KEY`): it calls `poke()` on `armC.reactive` until the callback proxy emits
`BlacklistContract(armC.callback)`. Then it waits for the rescue and prints each step when it occurs:

    1  the block of the debt event;
    2  the block of the hub's `Relayed` for that target on Lasna, with its `requestId`;
    3  the block of the vault's `Result` with the same `requestId`;
    4  the block at which `debt(armC.callback)` reads 0 at the callback proxy.

On success it writes `evidence/demo/capture.json`. At the timeout it exits with status 1, names the step
that did not occur and writes nothing: the file of an earlier run stays as it was.

The command acts only on the two arm C fixtures of the manifest. It sends `poke()` and no other
transaction. It measures no latency and it states no time limit: the seconds of a rescue are in `JUDGE.md`,
from block timestamps (`python -m lifeline.judge`, run again after a capture).

Each capture is a new rescue. A target allows `maxRescues` rescues inside `window` seconds, and the next
debt inside the window quarantines it. So the command sends no `poke()` while a new debt of one of the two
contracts would quarantine it, and while the hub or the vault cannot serve.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import experiment, ops
from .experiment import FixtureStore, idle_block, logs_now, observe, poke, wait_for_event
from .ops import (
    LASNA,
    OUTCOMES,
    Manifest,
    OpsError,
    Policy,
    RpcError,
    TransportError,
    amount,
    block_of,
    hex_int,
    say,
    topic_of_address,
    topic_of_int,
)
from .reader import _write_atomic

DEFAULT_CAPTURE = ops.ROOT / "evidence" / "demo" / "capture.json"
REACTIVE, CALLBACK = "armC.reactive", "armC.callback"
# The four steps that the capture waits for, by their key in `blocks` of the capture file.
STEPS = {
    "debtEvent": f"the debt event BlacklistContract({CALLBACK}) of the callback proxy",
    "relayed": f"the hub event Relayed for {CALLBACK} on Lasna",
    "result": "the vault event Result with the requestId of that Relayed",
    "debtZero": f"debt({CALLBACK}) == 0 at the callback proxy",
}


# The output is for a terminal with a large font: each line has 92 characters or fewer.
INDENT = " " * 20


class StepMissing(Exception):
    """The timeout came before one of the four steps."""

    def __init__(self, step: str):
        super().__init__(step)
        self.step = step


def rescues_in_window(rpc: ops.Rpc, emitter: str, topics: list, is_rescue, window: int, first_block: int) -> int:
    """The number of rescue logs of one target that are less than `window` seconds old at the newest block:
    the rescues that the sliding window of the hub or of the vault still counts."""
    head = rpc.head()
    stamps = {}

    def stamp(block: int) -> int:
        if block not in stamps:
            stamps[block] = hex_int(rpc.block(block)["timestamp"])
        return stamps[block]

    oldest = stamp(head) - window
    # The first block of the query: back from the newest block, in steps that double, to a block that is not
    # inside the window.
    span, start = 1024, head
    while start > first_block and stamp(start) > oldest:
        start = max(head - span, first_block)
        span *= 2
    logs = observe(rpc, rpc.logs(emitter, topics, start, head))
    return sum(1 for log in logs if is_rescue(log) and stamp(block_of(log)) > oldest)


class Capture:
    """One capture: the two arm C fixtures, their guardian, and the wait for the four steps."""

    def __init__(self, args):
        store = FixtureStore(args.manifest)
        # Each address comes from the fixtures section. An absent entry stops the command before any request.
        store.entry(REACTIVE)
        self.dest_name = store.entry(CALLBACK)["chain"]
        self.account = experiment.load_fixture_account()
        self.session = session = ops.Session(args, owner=False)
        self.lasna, self.dest = session.lasna, session.destination(self.dest_name)
        self.reactive = experiment._owned_fixture(store, self.lasna.rpc, REACTIVE, self.account)
        self.callback = store.address(CALLBACK)
        self.hub, self.vault = session.hub, session.vault(self.dest_name)
        self.out = Path(args.out)
        experiment.guard_local_outputs([*self.lasna.rpc.urls, *self.dest.rpc.urls],
                                       [("--out", self.out, DEFAULT_CAPTURE)])
        self.policies = {
            REACTIVE: self._policy(session.manifest, LASNA, self.reactive, REACTIVE),
            CALLBACK: self._policy(session.manifest, self.dest_name, self.callback, CALLBACK),
        }
        self.timeout, self.poke_wait = args.timeout, args.poke_wait
        self.deadline = None

    @staticmethod
    def _policy(manifest: Manifest, chain: str, target: str, name: str) -> Policy:
        record = manifest.active_enrollment(chain, target)
        if not manifest.enrollment_complete(record):
            raise OpsError(f"the manifest {manifest.path} has no complete enrollment of {name} ({target}): run "
                           f"`python -m lifeline.experiment fixtures` first. No transaction was sent")
        return Policy.from_json(record["policy"], f"the policy of {name}")

    # The guard ----------------------------------------------------------------------------------

    def debt(self, chain: ops.Chain, account: str, block: int = None) -> tuple:
        """(block, debt) of an account at the ledger of its chain: at `block`, or at the newest block."""
        abi = self.session.ledger_abi
        block, data = chain.rpc.call_at(chain.ledger, abi.encode("debt", account), block)
        return block, abi.decode("debt", data)

    def problems(self) -> list:
        """The reasons why the capture must not send a `poke()` now. Empty if a new debt of each of the two
        contracts would be rescued."""
        session, problems = self.session, experiment.guardian_problems(self.session, self.dest_name)
        hub_abi, vault_abi = session.hub_abi, session.vault_abi
        for name, chain, target in ((REACTIVE, self.lasna, self.reactive), (CALLBACK, self.dest, self.callback)):
            _, state = experiment.target_state(session, chain, target)
            if state != "Active":
                problems.append(f"the hub has {name} in state {state}")
        if session.read_vault(self.dest_name, "quarantined", self.callback):
            problems.append(f"the vault of {self.dest_name} has {CALLBACK} in quarantine")
        rescued = OUTCOMES.index("Rescued")
        counts = (
            (REACTIVE, self.lasna, self.hub, [hub_abi.topic("Rescued"), topic_of_address(self.reactive)],
             lambda log: True, session.manifest.hub["deployBlock"]),
            (CALLBACK, self.dest, self.vault, [vault_abi.topic("Result"), None, topic_of_address(self.callback)],
             lambda log: vault_abi.decode_event("Result", log)["outcome"] == rescued,
             session.manifest.vaults[self.dest_name]["deployBlock"]),
        )
        for name, chain, emitter, topics, is_rescue, first_block in counts:
            policy = self.policies[name]
            count = rescues_in_window(chain.rpc, emitter, topics, is_rescue, policy.window, first_block)
            if count >= policy.maxRescues:
                problems.append(f"{name} has {count} rescue(s) inside its window of {policy.window} s "
                                f"(maxRescues {policy.maxRescues}): its next debt quarantines it")
        return problems

    def require_room(self) -> None:
        problems = self.problems()
        if problems:
            raise OpsError("the capture stops and sends no poke(): " + "; ".join(problems)
                           + ". No capture file was written. Run `python -m lifeline.ops status`, and run the "
                             "capture again when the window has room")

    # The wait -----------------------------------------------------------------------------------

    def left(self, step: str) -> float:
        """The seconds to the timeout. At the timeout the step that the capture waits for is missing."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise StepMissing(step)
        return left

    def wait(self, step: str, chain: ops.Chain, emitter: str, topics: list, from_block: int) -> dict:
        log = wait_for_event(chain.rpc, emitter, topics, from_block, self.left(step))
        if log is None:
            raise StepMissing(step)
        return log

    def drive_into_debt(self, dest_start: int, nonce: int) -> dict:
        """Calls `poke()` until the callback proxy emits `BlacklistContract(armC.callback)`. One poke is one
        reaction on Lasna and one `pong` callback: the command waits for that callback, or for the debt
        event, before the next poke. Returns the debt log."""
        ledger_abi, callback_abi = self.session.ledger_abi, ops.load_artifact("CanaryCallback").abi
        debt_topics = [ledger_abi.topic("BlacklistContract"), topic_of_address(self.callback)]
        dest = self.dest.rpc
        while True:
            logs = logs_now(dest, self.dest.ledger, debt_topics, dest_start)
            if logs:
                return logs[0]
            self.require_room()
            # The check of the guard takes time: no poke() goes to the node after the timeout.
            self.left("debtEvent")
            # The nonce binds each poke to the state that decided it: see `experiment.poke`.
            sent = poke(self.lasna.rpc, self.account, self.reactive, nonce)
            nonce += 1
            say(f"  poke()            block {sent['block']} on {LASNA}: Poke({sent['n']})")
            pong_topics = [callback_abi.topic("Pong"), topic_of_int(sent["n"])]
            patience = time.monotonic() + min(self.poke_wait, self.left("debtEvent"))
            while time.monotonic() < patience:
                if logs_now(dest, self.dest.ledger, debt_topics, dest_start):
                    break
                pongs = logs_now(dest, self.callback, pong_topics, dest_start)
                if pongs:
                    say(f"{INDENT}block {block_of(pongs[0])} on {self.dest_name}: callback Pong({sent['n']})")
                    break
                time.sleep(min(ops.POLL_SECONDS, 1.0))
            else:
                say(f"{INDENT}no callback for Poke({sent['n']}) after {self.poke_wait:g} s")

    def run(self) -> dict:
        session, lasna, dest = self.session, self.lasna, self.dest
        say(f"capture: arm C of the experiment, timeout {self.timeout:g} s")
        say(f"  {REACTIVE} {self.reactive} on {LASNA}")
        say(f"  {CALLBACK} {self.callback} on {self.dest_name} (chain ID {dest.chain_id})")

        # One block for the start: the fixture account has no transaction that waits, and each log of the
        # capture is after the two start blocks.
        lasna_start, nonce = idle_block(lasna.rpc, self.account.address)
        dest_start, debt = self.debt(dest, self.callback)
        # No later read of the destination chain is below this block: a node that is behind then gives no
        # answer in place of an older state, in which a rescue of the window is not counted yet.
        dest.rpc.floor = max(dest.rpc.floor, dest_start)
        if debt:
            raise OpsError(f"{CALLBACK} is in debt already ({amount(debt)}) at block {dest_start} of "
                           f"{self.dest_name}: the capture needs a new debt event. No transaction was sent")
        self.deadline = time.monotonic() + self.timeout
        blocks = {}

        log = self.drive_into_debt(dest_start + 1, nonce)
        blocks["debtEvent"] = block_of(log)
        say(f"  1 debt event      block {blocks['debtEvent']} on {self.dest_name}: BlacklistContract({CALLBACK})")
        say(f"{INDENT}tx {log['transactionHash']}")

        log = self.wait("relayed", lasna, self.hub,
                        [session.hub_abi.topic("Relayed"), None, topic_of_int(dest.chain_id),
                         topic_of_address(self.callback)], lasna_start + 1)
        request_id = hex_int(log["topics"][1])
        blocks["relayed"] = block_of(log)
        say(f"  2 hub reaction    block {blocks['relayed']} on {LASNA}: Relayed")
        say(f"{INDENT}requestId {request_id}")
        say(f"{INDENT}tx {log['transactionHash']}")

        log = self.wait("result", dest, self.vault,
                        [session.vault_abi.topic("Result"), topic_of_int(request_id),
                         topic_of_address(self.callback)], blocks["debtEvent"])
        event = session.vault_abi.decode_event("Result", log)
        blocks["result"] = block_of(log)
        outcome = OUTCOMES[event["outcome"]]
        say(f"  3 vault callback  block {blocks['result']} on {self.dest_name}: Result, outcome {outcome}")
        say(f"{INDENT}paid {amount(event['paid'])}")
        say(f"{INDENT}tx {log['transactionHash']}")
        if outcome != "Rescued":
            raise OpsError(f"the vault Result of requestId {request_id} has outcome {outcome}, not Rescued. No "
                           f"capture file was written")

        # The first block at which the proxy reads no debt: the block of the result, or a later one.
        block = blocks["result"]
        while True:
            block, debt = self.debt(dest, self.callback, block)
            # A read that ends after the timeout is not a success: the capture then saves nothing.
            self.left("debtZero")
            if debt == 0:
                break
            time.sleep(ops.POLL_SECONDS)
            block = max(block, dest.rpc.head())
        blocks["debtZero"] = block
        say(f"  4 debt() is 0     block {block} on {self.dest_name}: debt({CALLBACK}) reads 0")
        return {"requestId": str(request_id), "chain": self.dest_name, "chainId": dest.chain_id,
                "target": self.callback, "blocks": blocks}


def cmd_capture(args) -> int:
    if args.timeout <= 0 or args.poke_wait <= 0:
        raise OpsError("--timeout and --poke-wait are above 0")
    capture = Capture(args)
    try:
        record = capture.run()
    except StepMissing as missing:
        print(f"error: capture timed out after {args.timeout:g} s: the step that did not occur is "
              f"{STEPS[missing.step]}. No capture file was written.", file=sys.stderr)
        return 1
    _write_atomic(capture.out, json.dumps(record, indent=2) + "\n")
    say(f"capture: saved {capture.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lifeline.demo", description="Lifeline demo: the capture step.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    capture = commands.add_parser("capture", help="drive arm C into destination debt and save the rescue loop")
    capture.add_argument("--manifest", default=str(ops.DEFAULT_MANIFEST), metavar="<file>",
                         help="manifest file (default deployments/manifest.json)")
    capture.add_argument("--rpc", action="append", default=[], metavar="<chain>=<url>",
                         help="RPC URL of one chain; repeatable; replaces the RPC list of that chain")
    capture.add_argument("--timeout", type=float, default=900.0, metavar="<seconds>",
                         help="longest time of the whole capture (default 900)")
    capture.add_argument("--poke-wait", type=float, default=60.0, metavar="<seconds>",
                         help="longest wait for the callback of one poke before the next poke (default 60)")
    capture.add_argument("--out", default=str(DEFAULT_CAPTURE), metavar="<file>",
                         help="capture file (default evidence/demo/capture.json)")
    capture.set_defaults(run=cmd_capture)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (OpsError, RpcError, TransportError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
