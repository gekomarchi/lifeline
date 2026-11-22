"""The real reader against the mock JSON-RPC server."""

from __future__ import annotations

import json

import pytest

from lifeline.events import BLACKLIST, DEBT, ledger_call
from lifeline.reader import (
    CallReverted,
    ChainMismatch,
    ChainReader,
    ReaderError,
    Snapshot,
    SnapshotError,
    StateUnavailable,
    first_block_at,
)

from .conftest import A1, LASNA_LEDGER, START

FAST = {"retries": 2, "backoff": 0.01}


def reader_for(network, name="lasna", urls=None, **options):
    urls = urls or [network.node.url(name)]
    return ChainReader(name, urls, network.tmp_path / "cache", **{**FAST, **options})


def log_spans(chain) -> list:
    return [(int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)) for p in chain.methods("eth_getLogs")]


def test_logs_are_in_ascending_order_and_complete(network):
    reader = reader_for(network, chunk=3000)
    logs = reader.logs(LASNA_LEDGER, [], 1000, 21000)
    positions = [(int(log["blockNumber"], 16), int(log["logIndex"], 16)) for log in logs]
    assert positions == sorted(positions)
    assert len(logs) == len(network.lasna.logs)
    assert all(end - start + 1 <= 3000 for start, end in log_spans(network.lasna))


def test_range_error_names_the_limit_and_the_reader_uses_it(network):
    network.lasna.max_range = 3000
    reader = reader_for(network)
    logs = reader.logs(LASNA_LEDGER, [BLACKLIST], 1000, 21000)
    assert len(logs) == 4
    spans = log_spans(network.lasna)
    assert spans[0] == (1000, 21000)  # refused: 20001 blocks
    assert spans[1] == (1000, 3999)  # the same start with the limit of the node
    assert all(end - start + 1 <= 3000 for start, end in spans[1:])
    assert spans[-1][1] == 21000


def test_only_chunks_at_or_below_the_pinned_height_are_cached(network):
    reader = reader_for(network, chunk=5000)
    reader.logs(LASNA_LEDGER, [BLACKLIST], 1000, 6000)  # no pinned height: nothing is cached
    reader.cache_to = 10999
    reader.logs(LASNA_LEDGER, [BLACKLIST], 1000, 15999)  # chunks end at 5999, 10999 and 15999
    network.clear_requests()
    reader.logs(LASNA_LEDGER, [BLACKLIST], 1000, 15999)
    assert log_spans(network.lasna) == [(11000, 15999)]


def test_cache_serves_a_different_range_and_fetches_only_the_gap(network):
    reader = reader_for(network, chunk=4000)
    reader.cache_to = 21000
    reader.logs(LASNA_LEDGER, [BLACKLIST], 3000, 10999)
    network.clear_requests()
    logs = reader_for(network, chunk=50000).logs(LASNA_LEDGER, [BLACKLIST], 1000, 21000)
    assert log_spans(network.lasna) == [(1000, 2999), (11000, 21000)]
    assert len(logs) == 4


def test_cache_key_includes_chain_address_and_topics(network):
    reader = reader_for(network)
    reader.cache_to = 21000
    assert len(reader.logs(LASNA_LEDGER, [BLACKLIST], 1000, 21000)) == 4
    assert len(reader.logs(LASNA_LEDGER, [], 1000, 21000)) == len(network.lasna.logs)
    assert reader.logs(A1, [BLACKLIST], 1000, 21000) == []
    other = reader_for(network, "base-sepolia")
    other.cache_to = 21000
    assert len(other.logs(LASNA_LEDGER, [BLACKLIST], 1000, 11000)) == 0


def test_retry_uses_the_next_url(network):
    dead = "http://127.0.0.1:9/dead"  # nothing listens on the discard port
    reader = reader_for(network, urls=[dead, network.node.url("lasna")])
    assert reader.chain_id() == 5318007
    assert reader.head() == 21000
    assert len(reader.logs(LASNA_LEDGER, [BLACKLIST], 1000, 21000)) == 4


def test_server_errors_are_retried_then_reader_error(network):
    network.lasna.faults = [("eth_blockNumber", "server"), ("eth_blockNumber", "rate")]
    assert reader_for(network).head() == 21000

    network.lasna.always_fail = True
    with pytest.raises(ReaderError) as error:
        reader_for(network).head()
    assert "rpc[0]" in str(error.value) and "127.0.0.1" not in str(error.value)


def test_url_for_another_chain_is_refused(network):
    reader = reader_for(network, urls=[network.node.url("base-sepolia")], chain_id=5318007)
    with pytest.raises(ChainMismatch):
        reader.head()


def test_state_unavailable_and_reverted_call(network):
    reader = reader_for(network)
    data = ledger_call(DEBT, A1)
    assert int(reader.call(LASNA_LEDGER, data, 21000), 16) == 5
    assert reader.balance(A1, 21000) == 0

    network.lasna.reverts.add((LASNA_LEDGER, data))
    with pytest.raises(CallReverted):
        reader.call(LASNA_LEDGER, data, 21000)

    network.lasna.refuse_state = True
    with pytest.raises(StateUnavailable):
        reader.call(LASNA_LEDGER, data, 21000)
    with pytest.raises(StateUnavailable):
        reader.balance(A1, 21000)


def test_reader_sends_read_methods_only(network):
    reader = reader_for(network)
    with pytest.raises(ValueError):
        reader._request("eth_sendRawTransaction", ["0x00"])
    assert network.lasna.requests == []


def test_first_block_search_reads_no_header_below_the_answer_region(network):
    """A node that keeps no old blocks still gives the first block of the window."""
    network.lasna.earliest_block = 900
    reader = reader_for(network)
    assert first_block_at(reader, START, 21000) == 1000
    assert reader.timestamp(1000) >= START > reader.timestamp(999)
    assert min(int(params[0], 16) for params in network.lasna.methods("eth_getBlockByNumber")) >= 900


@pytest.mark.parametrize("pruned_is_null", [False, True])
def test_first_block_search_when_the_block_time_changes_and_old_blocks_are_pruned(network, pruned_is_null):
    """Five blocks per second near the head, one before: the first estimate lands in pruned history."""
    network.lasna.pruned_is_null = pruned_is_null
    network.lasna.timestamp_of = lambda block: (
        START + 500 + (block - 19500) // 5 if block >= 19500 else START + block - 19000)
    network.lasna.earliest_block = 18500
    reader = reader_for(network)
    assert first_block_at(reader, START, 21000) == 19000
    assert any(int(params[0], 16) < 18500 for params in network.lasna.methods("eth_getBlockByNumber"))


def test_first_block_search_fails_when_the_node_has_no_block_before_the_window(network):
    network.lasna.earliest_block = 1500  # the answer, block 1000, is pruned
    with pytest.raises(SnapshotError):
        first_block_at(reader_for(network), START, 21000)


def test_first_block_search_with_two_second_blocks(network):
    assert first_block_at(reader_for(network, "base-sepolia"), START + 1, 11000) == 1001


def test_timestamps_are_cached_at_or_below_the_pinned_height(network):
    reader = reader_for(network)
    reader.cache_to = 5000
    assert reader.timestamps([4000, 4001, 6000]) == {4000: START + 3000, 4001: START + 3001, 6000: START + 5000}
    network.clear_requests()
    again = reader_for(network)
    again.cache_to = 5000
    again.timestamps([4000, 4001, 6000])
    assert [int(p[0], 16) for p in network.lasna.methods("eth_getBlockByNumber")] == [6000]


def test_snapshot_pins_saves_and_loads(network):
    readers = {name: reader_for(network, name) for name in network.chains}
    snapshot = Snapshot.pin(readers, lambda reader, to: first_block_at(reader, START, to), {"margin": 5})
    assert snapshot.chains["lasna"] == {"chainId": 5318007, "from": 1000, "to": 21000, "toTimestamp": START + 20000}
    assert readers["lasna"].cache_to == 21000
    data = ledger_call(DEBT, A1)
    assert int(snapshot.call(readers["lasna"], LASNA_LEDGER, data, "debt of A1"), 16) == 5

    path = network.tmp_path / "heights.json"
    snapshot.save(path)
    document = json.loads(path.read_text())
    assert document["state"] == [{"chain": "lasna", "block": 21000, "kind": "call", "to": LASNA_LEDGER,
                                  "data": data, "result": "0x" + "5".rjust(64, "0")}]

    network.clear_requests()
    network.lasna.refuse_state = True
    readers = {name: reader_for(network, name) for name in network.chains}
    loaded = Snapshot.load(path, readers)
    assert all(chain.methods("eth_blockNumber") == [] for chain in network.chains.values())
    assert int(loaded.call(readers["lasna"], LASNA_LEDGER, data, "debt of A1"), 16) == 5
    assert loaded.from_saved == ["debt of A1"]
    with pytest.raises(StateUnavailable):  # a value that the snapshot does not hold
        loaded.call(readers["lasna"], LASNA_LEDGER, ledger_call(DEBT, LASNA_LEDGER), "other")


def test_rate_limits_keep_the_chunk_and_repeated_server_errors_make_it_smaller(network):
    network.lasna.faults = [("eth_getLogs", "rate")] * 3
    assert len(reader_for(network, retries=4).logs(LASNA_LEDGER, [BLACKLIST], 1000, 21000)) == 4
    assert log_spans(network.lasna) == [(1000, 21000)] * 4

    network.clear_requests()
    network.lasna.faults = [("eth_getLogs", "server")] * 2
    assert len(reader_for(network, retries=4).logs(LASNA_LEDGER, [BLACKLIST], 1000, 21000)) == 4
    assert log_spans(network.lasna)[:3] == [(1000, 21000), (1000, 21000), (1000, 10999)]


def test_batch_with_a_rate_limit_is_sent_again_as_a_batch(network):
    reader = reader_for(network)
    requests = [(LASNA_LEDGER, ledger_call(DEBT, contract)) for contract in (A1, LASNA_LEDGER, A1)]
    reader.chain_id()
    network.clear_requests()
    network.lasna.faults = [("eth_call", "rate"), ("eth_call", "server")]
    assert [int(result, 16) for result in reader.calls(requests, 21000)] == [5, 0, 5]
    assert network.lasna.posts == [3, 3, 3]  # no single request while the node is busy


def test_node_with_no_batch_support_gets_single_requests(network):
    network.lasna.no_batch = True
    reader = reader_for(network)
    reader.chain_id()
    network.clear_requests()
    assert reader.timestamps([4000, 4001, 4002]) == {4000: START + 3000, 4001: START + 3001, 4002: START + 3002}
    assert network.lasna.posts == [3, 1, 1, 1]
    reader.timestamps([5000, 5001])
    assert network.lasna.posts == [3, 1, 1, 1, 1, 1]  # no further batch


def test_batch_with_no_state_is_not_sent_again(network):
    network.lasna.refuse_state = True
    reader = reader_for(network)
    reader.chain_id()
    network.clear_requests()
    requests = [(LASNA_LEDGER, ledger_call(DEBT, contract)) for contract in (A1, LASNA_LEDGER)]
    with pytest.raises(StateUnavailable):
        reader.calls(requests, 21000)
    assert network.lasna.posts == [2, 1, 1]  # one batch, then the first request two times
    with pytest.raises(StateUnavailable):
        reader.calls(requests, 21000)
    assert network.lasna.posts == [2, 1, 1]  # the reader knows that the block has no state


def test_rate_limit_in_one_error_object_for_a_batch_keeps_batching(network):
    """A node can answer a batch with one error object and HTTP 200."""
    reader = reader_for(network)
    reader.chain_id()
    network.clear_requests()
    network.lasna.batch_error = [{"code": -32005, "message": "rate limit exceeded"}]
    assert reader.timestamps([4000, 4001]) == {4000: START + 3000, 4001: START + 3001}
    assert network.lasna.posts == [2, 2]
    assert reader.batch > 1


def test_snapshot_file_with_no_window_end_is_refused(network):
    path = network.tmp_path / "old.json"
    chains = {name: {"chainId": chain.chain_id, "from": 1, "to": 2} for name, chain in network.chains.items()}
    path.write_text(json.dumps({"version": 1, "chains": chains, "params": {}, "state": []}))
    with pytest.raises(SnapshotError, match="toTimestamp"):
        Snapshot.load(path, {name: reader_for(network, name) for name in network.chains})
