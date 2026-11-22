"""The real command against the mock JSON-RPC server."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from lifeline.config import load_config, open_readers
from lifeline.queries import Scan, dropped_callbacks
from lifeline.reader import Snapshot
from lifeline.stats import main

from .conftest import A1, A2, A3, A4, HEIGHTS, R1, R2, R3, R5, R6, SUBJECTS

REPOSITORY = Path(__file__).resolve().parents[2]
FILES = ("data.json", "table.md", "heights.json")


def block_numbers(logs) -> set:
    return {int(log["blockNumber"], 16) for log in logs}


def header_reads(chain) -> set:
    return {int(params[0], 16) for params in chain.methods("eth_getBlockByNumber")}


def test_first_run_prints_each_number_and_writes_three_files(network):
    """The command as an operator runs it: `python -m lifeline.stats` with only RPC URLs (and test paths)."""
    completed = subprocess.run(
        [sys.executable, "-m", "lifeline.stats", *network.arguments("out", subjects=SUBJECTS)],
        cwd=REPOSITORY, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    out = network.tmp_path / "out"
    assert sorted(path.name for path in out.iterdir()) == sorted(FILES)

    data = network.data("out")
    assert data["version"] == 1
    assert data["heights"] == HEIGHTS
    assert data["params"]["windowStart"] == "2026-11-05T00:00:00+02:00"
    assert data["params"]["latencyMarginSeconds"] == 5
    queries = data["queries"]
    assert all(query["status"] == "ok" and query["definition"] for query in queries.values())

    assert queries["lasna_debt_events"]["result"] == {"count": 4}
    assert queries["lasna_debt_events"]["chains"] == ["lasna"]
    assert queries["destination_debt_events"]["result"] == {
        "byChain": {"base-sepolia": 3, "unichain-sepolia": 1}, "total": 4}
    stopped = queries["stopped_contracts"]["result"]["byChain"]
    assert stopped["lasna"] == {"cohort": 2, "stopped": 1, "contracts": [A1]}
    assert stopped["base-sepolia"] == {"cohort": 3, "stopped": 2, "contracts": [R2, R6]}
    assert stopped["unichain-sepolia"] == {"cohort": 1, "stopped": 1, "contracts": [R5]}
    assert queries["cron_stopped_share"]["result"] == {
        "cron": {"contracts": 2, "stopped": 1, "share": 0.5},
        "others": {"contracts": 2, "stopped": 1, "share": 0.5},
    }
    repeat = queries["repeat_debtors"]["result"]["byChain"]
    assert repeat["lasna"] == {"contracts": 1, "largest": 3, "largestContract": A1}
    assert repeat["base-sepolia"] == {"contracts": 0, "largest": 1, "largestContract": R1}

    fills = queries["fastfill_fill_times"]["result"]
    assert (fills["fills"], fills["medianSeconds"], fills["p90Seconds"], fills["maximumSeconds"]) == (3, 3, 5, 5)
    assert (fills["depositLogs"], fills["fillLogs"]) == (4, 4)
    assert queries["fastfill_fill_times"]["chains"] == ["base-sepolia", "unichain-sepolia"]
    ward = queries["cron_poller_ward_dead_time"]["result"]
    assert (ward["intervals"], ward["longestSeconds"], ward["longestBlocks"]) == (3, 17000, 17000)
    assert ward["longest"] == {"fromBlock": 4000, "toBlock": 21000, "openAtFrom": False, "openAtTo": True}
    assert queries["fastfill_reactive_reserves"]["result"] == {
        "contract": A3, "reservesWei": str(10**18), "block": 21000}

    guests = queries["guest_candidates"]["result"]
    assert [(entry["chain"], entry["address"]) for entry in guests["candidates"]] == [
        ("lasna", A1), ("lasna", A2), ("lasna", A4),
        ("base-sepolia", R1), ("base-sepolia", R2), ("base-sepolia", R6), ("base-sepolia", R3),
        ("unichain-sepolia", R5),
    ]
    first, last_lasna, low_only = guests["candidates"][0], guests["candidates"][2], guests["candidates"][6]
    assert first["reasons"] == ["debt_event", "in_debt_at_to", "low_reserve"]
    assert first["debtEvents"] == 3 and first["lastDebtBlock"] == 4000 and first["lastDebtTx"].startswith("0x")
    assert (first["debtWei"], first["reservesWei"], first["readBlock"]) == ("5", "0", 21000)
    assert last_lasna["reasons"] == ["in_debt_at_to", "low_reserve"] and "lastDebtTx" not in last_lasna
    assert low_only["reasons"] == ["low_reserve"] and low_only["readBlock"] == 11000
    assert guests["lowReserveThresholdWei"] == {
        "lasna": "37500000000000000", "base-sepolia": "2100000000000", "unichain-sepolia": "2100000000000"}
    assert "samples, not protocol constants" in guests["sampleFeeNote"]

    # Standard output: each number with its query name, chain and heights.
    assert "lasna_debt_events | lasna | from 1000 to 21000 | debt events = 4" in completed.stdout
    assert ("fastfill_fill_times | base-sepolia,unichain-sepolia | from 1000,500 to 11000,20500 | fills = 3"
            in completed.stdout)
    for name in queries:
        assert re.search(rf"^{name} \| .* \| from [\d,]+ to [\d,]+ \| .+ = ", completed.stdout, re.M), name

    table = (out / "table.md").read_text()
    assert "| `lasna_debt_events` | debt events | 4 | lasna | 1000 | 21000 |" in table
    assert "| `stopped_contracts` | stopped contracts | 2 | base-sepolia | 1000 | 11000 |" in table
    for name, query in queries.items():
        assert f"- `{name}`: {query['definition']}" in table

    heights = json.loads((out / "heights.json").read_text())
    assert heights["version"] == 1 and heights["chains"] == HEIGHTS and heights["params"] == data["params"]
    assert {entry["kind"] for entry in heights["state"]} == {"call"}
    assert all(set(entry) == {"chain", "block", "kind", "to", "data", "result"} for entry in heights["state"])


def test_rate_limit_and_range_errors_are_retried_and_second_run_uses_cache(network, capsys):
    network.lasna.faults = [("eth_getLogs", "rate"), ("eth_getLogs", "range")]
    assert network.run("out") == 0
    assert all((network.tmp_path / "out" / name).exists() for name in FILES)

    scans = [params[0] for params in network.lasna.methods("eth_getLogs")]
    spans = [(int(scan["fromBlock"], 16), int(scan["toBlock"], 16)) for scan in scans]
    # Request 1 got the rate-limit error, request 2 (the same range) the range error,
    # request 3 is the same start with a smaller chunk.
    assert spans[0] == spans[1] == (1000, 21000)
    assert spans[2][0] == 1000 and spans[2][1] < 21000
    assert network.data("out")["queries"]["lasna_debt_events"]["result"] == {"count": 4}

    first = (network.tmp_path / "out" / "data.json").read_bytes()
    network.clear_requests()
    assert network.run("again", heights=network.tmp_path / "out" / "heights.json") == 0
    for chain in network.chains.values():
        assert chain.methods("eth_getLogs") == []
        assert chain.methods("eth_blockNumber") == []  # a run with --heights reads no head
    assert (network.tmp_path / "again" / "data.json").read_bytes() == first
    assert "none (cache)" in capsys.readouterr().out


def test_retries_used_gives_nonzero_exit_and_no_output(network, capsys):
    network.base.always_fail = True
    assert network.run("out") == 1
    assert not (network.tmp_path / "out").exists()
    captured = capsys.readouterr()
    assert captured.out == ""  # no number
    assert "base-sepolia" in captured.err and "failed after 2 retries" in captured.err
    assert "127.0.0.1" not in captured.err


def test_new_run_continues_from_the_cache_after_a_failed_run(network):
    network.uni.fail_methods = {"eth_getLogs"}
    assert network.run("out") == 1  # the ledger scans of lasna and base-sepolia finished before the failure
    assert len(network.lasna.methods("eth_getLogs")) == 1
    assert len(network.base.methods("eth_getLogs")) == 1
    assert not (network.tmp_path / "out").exists()

    network.uni.fail_methods = set()
    network.clear_requests()
    assert network.run("out") == 0
    # Only the scans that the failed run did not reach: Subscribe and CallbackRequest on lasna, none on base-sepolia.
    assert len(network.lasna.methods("eth_getLogs")) == 2
    assert network.base.methods("eth_getLogs") == []
    assert network.data("out")["queries"]["lasna_debt_events"]["result"] == {"count": 4}


def test_failure_in_a_late_query_writes_no_file(network, capsys):
    """State reads come after the scans. A failure there must still leave no partial output."""
    network.uni.refuse_state = True
    assert network.run("out") == 1
    assert not (network.tmp_path / "out").exists()
    assert capsys.readouterr().out == ""


def test_dropped_callback_join(network, capsys):
    assert network.run("out", heights=network.heights_file()) == 0
    result = network.data("out")["queries"]["dropped_callbacks"]["result"]

    assert result["requests"] == 9
    assert result["byChain"] == {
        "base-sepolia": {"requests": 7, "dropped": 3, "uncertain": 1, "notInDebt": 3},
        "unichain-sepolia": {"requests": 1, "dropped": 1, "uncertain": 0, "notInDebt": 0},
    }
    assert (result["dropped"], result["uncertain"], result["notInDebt"]) == (4, 1, 3)
    assert result["droppedInIntervalOpenAtFrom"] == 1
    assert result["notScanned"] == {"requests": 1, "byChainId": {"999": 1}}
    assert result["delaySeconds"] == {"count": 3, "smallest": 13, "within60": 2, "median": 30, "below10800": 3}
    assert result["latencyMarginSeconds"] == 5
    assert (result["specificationFigure"], result["differenceFromSpecification"]) == (206, -202)
    assert "not a count of observed reverted transactions" in result["derivation"]
    assert "not visible" in result["limit"]

    # The command reports the requests of each query: this one sent log queries and block-header reads only.
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("rpc requests of dropped_callbacks:"))
    assert re.fullmatch(r"rpc requests of dropped_callbacks: eth_getBlockByNumber x\d+, eth_getLogs x\d+", line)
    assert "dropped_callbacks | lasna,base-sepolia,unichain-sepolia | from 1000,1000,500 to 21000,11000,20500 | " \
           "dropped callbacks (derived from debt intervals and InDebt) = 4" in out

    # With no subject configured, no other query reads a header: headers were read only for matched logs.
    requests = network.requests
    matched = [requests[name] for name in ("inside", "before", "after", "margin", "open_to_to", "open_at_from",
                                           "other_chain")]
    assert header_reads(network.lasna) == block_numbers(matched)
    unmatched = block_numbers([requests["no_debt_event"], requests["not_configured"]])
    assert unmatched.isdisjoint(header_reads(network.lasna))
    assert header_reads(network.base) == block_numbers(
        [network.r1_start, network.r1_end, network.r2_start, network.r3_end])
    assert header_reads(network.uni) == block_numbers([network.r5_start])


def test_dropped_callback_query_sends_log_queries_and_header_reads_only(network):
    """The query alone, with the real reader: no eth_call, no transaction, no receipt, no block body."""
    config = load_config(network.config())
    urls = {name: [network.node.url(name)] for name in network.chains}
    readers = open_readers(config, urls, network.tmp_path / "cache")
    snapshot = Snapshot.load(network.heights_file(), readers)
    result, _ = dropped_callbacks(Scan(config, readers, snapshot))

    assert result["dropped"] == 4
    allowed = {"eth_chainId", "eth_blockNumber", "eth_getLogs", "eth_getBlockByNumber"}
    for chain in network.chains.values():
        assert chain.method_names() <= allowed
        assert all(params[1] is False for params in chain.methods("eth_getBlockByNumber"))  # header, no transactions


def test_same_heights_and_different_url_order_give_identical_data(network):
    assert network.run("first") == 0
    heights = network.tmp_path / "first" / "heights.json"
    time.sleep(1.1)  # a later wall-clock second must not change any file
    assert network.run("ab", heights=heights, cache="cache-ab", suffixes=("/a", "/b")) == 0
    assert network.run("ba", heights=heights, cache="cache-ba", suffixes=("/b", "/a")) == 0

    first = (network.tmp_path / "first" / "data.json").read_bytes()
    assert (network.tmp_path / "ab" / "data.json").read_bytes() == first
    assert (network.tmp_path / "ba" / "data.json").read_bytes() == first
    for out in ("first", "ab", "ba"):
        for name in FILES:
            text = (network.tmp_path / out / name).read_text()
            assert "127.0.0.1" not in text and "http" not in text
            assert time.strftime("%Y-%m-%d") not in text
        assert (network.tmp_path / out / "table.md").read_bytes() == heights.with_name("table.md").read_bytes()
        assert (network.tmp_path / out / "heights.json").read_bytes() == heights.read_bytes()


def test_old_state_uses_saved_values_and_names_them(network, capsys):
    assert network.run("first", subjects=SUBJECTS) == 0
    first = (network.tmp_path / "first" / "data.json").read_bytes()
    capsys.readouterr()

    for chain in network.chains.values():
        chain.refuse_state = True
    heights = network.tmp_path / "first" / "heights.json"
    assert network.run("later", subjects=SUBJECTS, heights=heights, cache="fresh-cache") == 0

    out = capsys.readouterr().out
    assert f"from snapshot (the node has no state at the old block): debt({A1}) on lasna" in out
    assert f"from snapshot (the node has no state at the old block): reserves({A3}) on lasna" in out
    assert f"from snapshot (the node has no state at the old block): debt({R5}) on unichain-sepolia" in out
    assert (network.tmp_path / "later" / "data.json").read_bytes() == first
    assert b"from snapshot" not in (network.tmp_path / "later" / "data.json").read_bytes()


def test_query_with_no_subject_is_not_configured(network, capsys):
    subjects = {key: value for key, value in SUBJECTS.items() if key != "fastfill"}
    assert network.run("out", subjects=subjects) == 0
    queries = network.data("out")["queries"]
    assert queries["fastfill_fill_times"] == {
        "chains": [],
        "definition": queries["fastfill_fill_times"]["definition"],
        "status": "not_configured",
        "result": {"missing": "subjects.fastfill.deposit.chain"},
    }
    others = [query for name, query in queries.items() if name != "fastfill_fill_times"]
    assert len(others) == 9 and all(query["status"] == "ok" and query["result"] for query in others)
    out = capsys.readouterr().out
    assert "not configured: fastfill_fill_times did not run; subjects.fastfill.deposit.chain is missing" in out
    assert "| `fastfill_fill_times` | not configured:" in (network.tmp_path / "out" / "table.md").read_text()


def test_state_that_differs_from_the_snapshot_is_reported_and_the_saved_value_is_used(network, capsys):
    assert network.run("first") == 0
    first = (network.tmp_path / "first" / "data.json").read_bytes()
    capsys.readouterr()
    network.set_ledger("lasna", A2, debt=1)
    assert network.run("later", heights=network.tmp_path / "first" / "heights.json") == 0
    out = capsys.readouterr().out
    assert f"differs from snapshot (the saved value is used): debt({A2}) on lasna: the node gives" in out
    assert (network.tmp_path / "later" / "data.json").read_bytes() == first
    assert (network.tmp_path / "later" / "heights.json").read_bytes() == (
        network.tmp_path / "first" / "heights.json").read_bytes()


def test_output_that_cannot_be_written_leaves_no_file(network, capsys):
    out = network.tmp_path / "out"
    (out / "table.md.tmp").mkdir(parents=True)  # the table cannot be written
    assert network.run("out") == 1
    assert [path.name for path in out.iterdir()] == ["table.md.tmp"]
    assert "cannot write the output files" in capsys.readouterr().err


def test_wrong_chain_behind_a_url_is_refused(network, capsys):
    arguments = network.arguments("out")
    arguments[arguments.index(f"lasna={network.node.url('lasna')}")] = f"lasna={network.node.url('base-sepolia')}"
    assert main(arguments) == 2
    assert "answers for chain 84532, expected 5318007" in capsys.readouterr().err
    assert not (network.tmp_path / "out").exists()
