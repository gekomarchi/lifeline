"""Checks of the operator commands that need no chain: amounts, policies, files and the refusals that come
before the first request. The commands run against forks in `test_ops_forks.py`."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("eth_account")

from lifeline import forkrelay, ops

FASTFILL_REACTIVE = "0x05a566959544A55c16012aeeF0a09067da1f204f"
# Nothing listens on this port: a command that sends a request fails with another message.
NO_NODE = [f"--rpc={name}=http://127.0.0.1:9" for name in ops.CHAIN_NAMES]
POLICY = ["--low", "0.05", "--high", "0.25", "--max-debt", "0.5", "--daily-cap", "1", "--max-rescues", "2",
          "--window", "3600"]


def test_amounts_convert_to_wei_and_back():
    assert ops.to_wei("0.05") == 5 * 10**16
    assert ops.to_wei("1") == 10**18
    assert ops.to_wei("0.000000000000000001") == 1
    assert ops.to_wei("123456789012.123456789012345678") == 123456789012123456789012345678
    assert ops.from_wei(25 * 10**16) == "0.25"
    assert ops.amount(5 * 10**16) == "0.05 (50000000000000000 wei)"
    for text in ("0.0000000000000000001", "-1", "abc", "1e400", "nan"):
        with pytest.raises(ops.OpsError):
            ops.to_wei(text)


def test_addresses():
    assert ops.checksum(FASTFILL_REACTIVE.lower()) == FASTFILL_REACTIVE
    assert ops.parse_address(FASTFILL_REACTIVE.lower()) == FASTFILL_REACTIVE
    for text in ("0x1234", FASTFILL_REACTIVE.replace("A", "a", 1), "05a566959544A55c16012aeeF0a09067da1f204f"):
        with pytest.raises(ops.OpsError):
            ops.parse_address(text)


def test_policy_rule():
    valid = ops.Policy(low=0, high=1, maxDebt=1, dailyCap=1, maxRescues=4, window=1)
    assert valid.problems() == []
    for change, text in (
        ({"high": 0}, "high"), ({"low": 2}, "low"), ({"maxDebt": 0}, "maxDebt"), ({"dailyCap": 0}, "dailyCap"),
        ({"maxRescues": 0}, "maxRescues"), ({"maxRescues": 5}, "maxRescues"), ({"window": 0}, "window"),
        ({"window": 2**32}, "window"),
    ):
        policy = ops.Policy(**{**valid.__dict__, **change})
        assert any(text in problem for problem in policy.problems()), change
        with pytest.raises(ops.OpsError):
            policy.validate()
    assert ops.Policy.from_json(valid.to_json()) == valid
    assert valid.to_json() == {"low": "0", "high": "1", "maxDebt": "1", "dailyCap": "1", "maxRescues": 4, "window": 1}


def test_fees():
    # Spec F3: a full trigger of 900,000 gas at 100 gwei is 0.09 lREACT.
    assert ops.trigger_fee(100 * 10**9, 1000) == 9 * 10**16
    # REHEARSAL §2: (`_extraGas` + gas of the call) x price x coefficient / 1000.
    assert ops.sweep_fee(80_000, 619_221, 5_000_000, 1100) == (80_000 + 619_221) * 5_000_000 * 1100 // 1000


def test_repository_configuration_loads_and_names_the_missing_operator_inputs():
    config = ops.load_config()
    assert set(config["chains"]) == set(ops.CHAIN_NAMES)
    assert config["chains"]["lasna"]["chainId"] == 5318007
    assert config["callbackSender"] == "hub" and config["cronTopic"] == "Cron10000"
    for name in ("unichain-sepolia", "base-sepolia"):
        assert config["chains"][name]["sweepBase"] == 220_000 and config["chains"][name]["measuredSweepGas"] > 0
    reactive = next(entry for entry in config["fleet"] if entry["label"] == "FastfillReactive")
    assert reactive["chain"] == "lasna" and reactive["address"] == FASTFILL_REACTIVE
    assert (reactive["policy"].low, reactive["policy"].high) == (5 * 10**16, 25 * 10**16)


def test_configuration_errors(tmp_path):
    good = json.loads(ops.DEFAULT_CONFIG.read_text())

    def load(change):
        data = json.loads(json.dumps(good))
        change(data)
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data))
        return ops.load_config(path)

    for change in (
        lambda d: d["chains"].pop("lasna"),
        lambda d: d["chains"].__setitem__("sepolia", d["chains"]["lasna"]),
        lambda d: d["chains"]["base-sepolia"].__setitem__("ledger", "0x12"),
        lambda d: d["hub"].pop("feeFloat"),
        lambda d: d["hub"].__setitem__("principal", "1.5"),
        lambda d: d["vaults"].__setitem__("lasna", d["hub"]),
        lambda d: d.__setitem__("cronTopic", "Cron1"),
        lambda d: d.__setitem__("callbackSender", "node"),
        lambda d: d["fleet"][0].__setitem__("chain", "ethereum"),
        lambda d: d["fleet"][0]["policy"].pop("window"),
    ):
        with pytest.raises(ops.OpsError):
            load(change)
    assert load(lambda d: d.__setitem__("callbackSender", FASTFILL_REACTIVE))["callbackSender"] == FASTFILL_REACTIVE


def test_manifest_keeps_the_entries_of_other_units(tmp_path):
    path = tmp_path / "deployments" / "manifest.json"
    manifest = ops.Manifest.load(path, create=True)
    assert manifest.data["fixtures"] == {} and manifest.data["loopCheck"] is None and manifest.hub is None
    manifest.data["fixtures"]["unit7"] = {"address": FASTFILL_REACTIVE}
    manifest.data["other"] = [1]
    manifest.save()

    again = ops.Manifest.load(path)
    again.data["enrollments"].append({"chain": "lasna", "target": FASTFILL_REACTIVE, "removed": None})
    again.save()
    data = json.loads(path.read_text())
    assert data["fixtures"] == {"unit7": {"address": FASTFILL_REACTIVE}} and data["other"] == [1]
    assert data["version"] == 1 and len(data["enrollments"]) == 1

    assert again.active_enrollment("lasna", FASTFILL_REACTIVE.lower()) is again.data["enrollments"][0]
    again.data["enrollments"][0]["removed"] = {"hubDisarm": "0x01", "vaultDisarm": None}
    assert again.active_enrollment("lasna", FASTFILL_REACTIVE) is None

    with pytest.raises(ops.OpsError):
        ops.Manifest.load(tmp_path / "absent.json")
    path.write_text(json.dumps({"version": 2}))
    with pytest.raises(ops.OpsError):
        ops.Manifest.load(path)


def test_guest_without_consent_is_refused_before_any_request(capsys, monkeypatch):
    monkeypatch.delenv(ops.OWNER_KEY_VARIABLE, raising=False)
    status = ops.main(["enroll", "--chain", "lasna", "--target", FASTFILL_REACTIVE, "--label", "guest", "--guest",
                       *POLICY, *NO_NODE])
    assert status == 1
    assert "consent" in capsys.readouterr().err


def test_an_invalid_policy_is_refused_before_any_request(capsys, monkeypatch):
    monkeypatch.delenv(ops.OWNER_KEY_VARIABLE, raising=False)
    arguments = ["enroll", "--chain", "lasna", "--target", FASTFILL_REACTIVE, "--label", "x", *NO_NODE]
    assert ops.main([*arguments, *POLICY[:2], "--high", "0.01", *POLICY[4:]]) == 1
    assert "low must not be above high" in capsys.readouterr().err
    assert ops.main([*arguments, *POLICY[:8], "--max-rescues", "5", "--window", "3600"]) == 1
    assert "maxRescues" in capsys.readouterr().err


def test_no_command_runs_without_the_owner_key(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(ops.OWNER_KEY_VARIABLE, raising=False)
    manifest = tmp_path / "manifest.json"
    assert ops.main(["deploy", "--manifest", str(manifest), *NO_NODE]) == 1
    assert ops.OWNER_KEY_VARIABLE in capsys.readouterr().err
    assert not manifest.exists()


def test_a_wrong_key_value_is_not_printed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(ops.OWNER_KEY_VARIABLE, "0xnot-a-key-5ec2e7")
    assert ops.main(["deploy", "--manifest", str(tmp_path / "manifest.json"), *NO_NODE]) == 1
    output = capsys.readouterr()
    assert "is not a private key" in output.err and "5ec2e7" not in output.err + output.out


def test_forkrelay_accepts_only_urls_of_this_machine():
    for url in ("http://127.0.0.1:8545", "http://localhost:8545/lasna", "http://[::1]:8545"):
        forkrelay.require_local("lasna", url)
    for url in ("https://lasna-rpc.rnk.dev/", "http://203.0.113.7:8545", "http://localhost.example.org:8545"):
        with pytest.raises(ops.OpsError):
            forkrelay.require_local("lasna", url)


class FakeNode:
    """A local JSON-RPC server that answers `eth_chainId` and `eth_blockNumber`, and `anvil_nodeInfo` if it
    is a development node."""

    def __init__(self, chain_id, development=False):
        node = self
        self.methods = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                node.methods.append(payload["method"])
                answers = {"eth_chainId": hex(chain_id), "eth_blockNumber": "0x1"}
                if development:
                    answers["anvil_nodeInfo"] = {}
                body = {"jsonrpc": "2.0", "id": payload["id"]}
                if payload["method"] in answers:
                    body["result"] = answers[payload["method"]]
                else:
                    body["error"] = {"code": -32601, "message": "the method does not exist"}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_retry_on_another_url_stays_on_the_chain_of_the_configuration():
    dead = "http://127.0.0.1:9"
    lasna, other, fork = FakeNode(5318007), FakeNode(84532), FakeNode(5318007, development=True)
    try:
        # The first URL gives no answer. The second one answers for the right chain: it is used.
        assert ops.Rpc("lasna", [dead, lasna.url], chain_id=5318007, attempts=1).block_number() == 1
        # The second one answers for another chain: no request but `eth_chainId` goes to it.
        rpc = ops.Rpc("lasna", [dead, other.url], chain_id=5318007, attempts=1)
        with pytest.raises(ops.OpsError, match="answers for chain ID 84532"):
            rpc.block_number()
        assert other.methods == ["eth_chainId"]
        # The chain ID of a signature is the one of the configuration, not an answer of a node.
        assert rpc.chain_id() == 5318007

        chain = ops.Chain("lasna", 5318007, FASTFILL_REACTIVE, ops.Rpc("lasna", [lasna.url, other.url], 5318007))
        with pytest.raises(ops.OpsError, match="answers for chain ID 84532"):
            chain.check_id()
        ops.Chain("lasna", 5318007, FASTFILL_REACTIVE, ops.Rpc("lasna", [dead, lasna.url], 5318007)).check_id()

        # A development node at any place of the list makes the chain a dry-run chain.
        assert ops.Rpc("lasna", [lasna.url, fork.url], 5318007).is_dev_node()
        assert not ops.Rpc("lasna", [dead, lasna.url], 5318007).is_dev_node()
    finally:
        for node in (lasna, other, fork):
            node.close()


def test_create_address():
    # The first contract of the first default account of anvil.
    assert ops.create_address("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266", 0) == \
        "0x5FbDB2315678afecb367f032d93F642f64180aa3"
