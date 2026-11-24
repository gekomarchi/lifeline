"""Fork dry run of the capture step of the demo video.

The command, the fixtures, the contracts and the ledgers are real: the tests run `python -m lifeline.demo`
against the local anvil forks of the experiment dry run, with throwaway keys and a scratch manifest. Only the
node is emulated (the fork relay). Each block of the capture file is compared with the logs that the test
reads directly from the forks.

The tests need what the fork dry run of the experiment needs (see `test_experiment_forks.py`). They run in
the order of the file: each one continues on the state that the tests before it left.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("eth_account")

from lifeline import demo, experiment, ops
from lifeline.tests import forknet
from lifeline.tests.test_experiment_forks import KEYS, Experiment, amount_flags
from lifeline.tests.test_ops_forks import BASE, BUILT, LASNA, UPSTREAMS

pytestmark = pytest.mark.skipif(
    UPSTREAMS is None or not Path(forknet.ANVIL).exists() or not BUILT
    or not (ops.ARTIFACTS / "CronPoller.sol" / "CronPoller.json").exists(),
    reason="the fork dry run needs anvil, `forge build` and the three RPC variables "
           + ", ".join(forknet.FORK_VARIABLES.values()),
)

STEP_LINE = re.compile(r"^  ([1-4]) .*block (\d+)", re.MULTILINE)


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    run = Experiment(tmp_path_factory.mktemp("demo"))
    try:
        result = run.experiment("fixtures", *amount_flags(run))
        assert result.returncode == 0, result.stderr + result.stdout
        yield run
    finally:
        run.close()


def capture(net, out, *arguments, manifest=None, keys=True) -> subprocess.CompletedProcess:
    """Runs `python -m lifeline.demo capture`. Each request of the command goes through the recording proxy."""
    command = [sys.executable, "-m", "lifeline.demo", "capture", "--manifest", str(manifest or net.manifest),
               *net._rpc_arguments(), *(["--out", str(out)] if out else []), *arguments]
    environment = net.env if keys else {name: value for name, value in net.env.items() if name not in KEYS}
    return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=600, cwd=ops.ROOT)


def logs(net, chain, emitter, abi, event, *indexed, start) -> list:
    """The logs of one event after the start blocks of a test: an independent read of the fork."""
    return net.rpcs[chain].logs(emitter, [abi.topic(event), *indexed], start[chain] + 1)


def pokes_only(net, mark) -> None:
    """Each transaction that the command sent after the mark is `poke()` of `armC.reactive` on Lasna."""
    reactive, selector = net.address(demo.REACTIVE), "0x" + net.canary_abi.encode("poke").hex()
    sent = net.proxy.sent(mark)
    assert sent, "the capture sent no transaction"
    for chain, tx_hash in sent:
        transaction = net.rpcs[chain].transaction(tx_hash)
        assert chain == LASNA and ops.same_address(transaction["to"], reactive) and transaction["input"] == selector
        assert ops.same_address(transaction["from"], net.fixture.address)


def test_capture_refuses_before_any_transaction(net, tmp_path):
    blocks, mark = net.blocks(), net.proxy.mark()
    out = tmp_path / "capture.json"

    # A dry run does not write the capture file of the live run.
    result = capture(net, None)
    assert result.returncode == 1 and "an RPC is a local node" in result.stderr and "--out" in result.stderr

    # No argument names a target, and a manifest with no arm C entry stops the command.
    result = capture(net, out, "--target", net.address("armA.reactive"))
    assert result.returncode == 2 and "unrecognized arguments" in result.stderr
    for absent in (demo.REACTIVE, demo.CALLBACK):
        manifest = net.dir / "absent.json"
        data = net.data()
        del data["fixtures"][absent]
        manifest.write_text(json.dumps(data))
        result = capture(net, out, manifest=manifest)
        assert result.returncode == 1, result.stdout
        assert f"no fixtures entry {absent}" in result.stderr and "No transaction was sent" in result.stderr

    # Without the fixture key the command sends nothing.
    result = capture(net, out, keys=False)
    assert result.returncode == 1 and experiment.FIXTURE_KEY_VARIABLE in result.stderr

    result = capture(net, out, "--timeout", "0")
    assert result.returncode == 1 and "--timeout" in result.stderr
    assert net.blocks() == blocks and net.proxy.sent(mark) == [] and not out.exists()


def test_capture_prints_the_four_blocks_in_order_and_saves_the_request(net, tmp_path):
    out = tmp_path / "capture.json"
    callback, hub, vault = net.address(demo.CALLBACK), net.hub, net.vault(BASE)
    target = ops.topic_of_address(callback)
    start, mark = net.blocks(), net.proxy.mark()
    with net.watch():
        result = capture(net, out, "--timeout", "300", "--poke-wait", "30")
    assert result.returncode == 0, result.stderr + result.stdout
    print(result.stdout)  # shown by pytest when the test fails

    record = json.loads(out.read_text())
    assert list(record) == ["requestId", "chain", "chainId", "target", "blocks"]
    assert list(record["blocks"]) == ["debtEvent", "relayed", "result", "debtZero"]
    assert (record["chain"], record["chainId"], record["target"]) == (BASE, 84532, callback)
    blocks = record["blocks"]

    # The logs of the forks, read directly: one debt event, one relay request, one result, one whitelist.
    debts = logs(net, BASE, net.ledger[BASE], net.ledger_abi, "BlacklistContract", target, start=start)
    relays = logs(net, LASNA, hub, net.hub_abi, "Relayed", None, ops.topic_of_int(84532), target, start=start)
    assert len(debts) == 1 and len(relays) == 1
    request_id = int(relays[0]["topics"][1], 16)
    results = logs(net, BASE, vault, net.vault_abi, "Result", ops.topic_of_int(request_id), target, start=start)
    whitelists = logs(net, BASE, net.ledger[BASE], net.ledger_abi, "WhitelistContract", target, start=start)
    assert len(results) == 1 and len(whitelists) == 1
    assert record["requestId"] == str(request_id)
    assert blocks == {"debtEvent": ops.block_of(debts[0]), "relayed": ops.block_of(relays[0]),
                      "result": ops.block_of(results[0]), "debtZero": ops.block_of(whitelists[0])}
    event = net.vault_abi.decode_event("Result", results[0])
    assert ops.OUTCOMES[event["outcome"]] == "Rescued" and event["paid"] > 0
    assert whitelists[0]["transactionHash"] == results[0]["transactionHash"]
    assert blocks["debtEvent"] < blocks["result"] == blocks["debtZero"]
    # The debt is 0 from the block of the result, and it was above 0 in the block before.
    assert net.debt(BASE, callback) == 0
    debt_at = lambda block: net.ledger_abi.decode("debt", net.rpcs[BASE].call(  # noqa: E731
        net.ledger[BASE], net.ledger_abi.encode("debt", callback), block))
    assert debt_at(blocks["debtZero"]) == 0 and debt_at(blocks["debtZero"] - 1) > 0

    # The output has the four steps in their order, each with its block, and the requestId.
    assert [(int(step), int(block)) for step, block in STEP_LINE.findall(result.stdout)] == \
        [(1, blocks["debtEvent"]), (2, blocks["relayed"]), (3, blocks["result"]), (4, blocks["debtZero"])]
    assert f"requestId {request_id}\n" in result.stdout and f"capture: saved {out}" in result.stdout
    for log in (debts[0], relays[0], results[0]):
        assert f"tx {log['transactionHash']}" in result.stdout
    assert "second" not in result.stdout.split("timeout")[1].split("\n", 1)[1], "the capture states no latency"
    pokes_only(net, mark)


def test_capture_sends_no_poke_when_the_next_debt_would_quarantine(net, tmp_path):
    # The capture before this test was one rescue of `armC.callback`. With `maxRescues` 1 in the policy of
    # the manifest, the next debt inside the window would quarantine the target.
    out = tmp_path / "capture.json"
    manifest = net.dir / "one-rescue.json"
    data = net.data()
    record = next(r for r in data["enrollments"] if r["label"] == "experiment armC.callback")
    record["policy"]["maxRescues"] = 1
    manifest.write_text(json.dumps(data))
    blocks, mark = net.blocks(), net.proxy.mark()
    result = capture(net, out, manifest=manifest)
    assert result.returncode == 1, result.stdout
    assert f"{demo.CALLBACK} has 1 rescue(s) inside its window of 3600 s" in result.stderr
    assert "its next debt quarantines it" in result.stderr and "sends no poke()" in result.stderr
    assert net.blocks() == blocks and net.proxy.sent(mark) == [] and not out.exists()


def test_capture_times_out_without_the_node_and_keeps_the_earlier_file(net, tmp_path):
    out = tmp_path / "capture.json"
    out.write_text("the capture of an earlier run")
    mark = net.proxy.mark()
    # No relay runs: no reaction, no callback, so no debt event.
    result = capture(net, out, "--timeout", "8", "--poke-wait", "3")
    assert result.returncode == 1, result.stdout
    assert "capture timed out after 8 s" in result.stderr
    assert f"the step that did not occur is {demo.STEPS['debtEvent']}" in result.stderr
    assert "No capture file was written" in result.stderr
    assert out.read_text() == "the capture of an earlier run"
    assert "no callback for Poke(" in result.stdout and "  1 debt event" not in result.stdout
    pokes_only(net, mark)

    # With no earlier file, the timeout leaves no file.
    absent = tmp_path / "absent.json"
    result = capture(net, absent, "--timeout", "4", "--poke-wait", "2")
    assert result.returncode == 1 and not absent.exists() and not list(tmp_path.glob("*.tmp"))
