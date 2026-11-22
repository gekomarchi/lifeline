"""Topics and event declarations."""

from __future__ import annotations

import pytest

from lifeline import events
from lifeline.keccak import keccak256
from lifeline.queries import debt_intervals


def test_keccak256_known_values():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert events.topic("Transfer(address,address,uint256)") == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
    # Inputs around the 136-byte block size.
    assert keccak256(b"a" * 135).hex() == "34367dc248bbd832f4e3e69dfaac2f92638bd0bbd18f2912ba4ef454919cf446"
    assert keccak256(b"a" * 136).hex() == "a6c4d403279fe3e0af03729caada8374b5ca54d8065329a3ebcaeb4b60aa386e"


def test_platform_topics_match_the_logs_of_the_deployed_contracts():
    """These values were read from logs on Lasna, Base Sepolia and Unichain Sepolia."""
    assert events.BLACKLIST == "0x930136576d6bd679046e0c2e6212ce4cacec0401db71e9a27f906d2bab5e08f3"
    assert events.WHITELIST == "0xc91181be4112cf78026ec1c30b4f5ecac00faa44c8d5e9523e08325cf186d8a7"
    assert events.SUBSCRIBE == "0xe9b38458922e1af481b2244c7c2bb32e465e90c352946042d2a09472fad6c246"
    assert events.CALLBACK_REQUEST == "0xef94dbc611c4b95bf4b8ba34b413972180c2faf26fbb480befacedd83e07b133"
    assert "0xf02d6ea5c22a71cffe930a4523fcb4f129be6c804db50e4202fb4e0b07ccb514" in events.CRON_TOPICS  # Cron1
    assert "0xe20b31294d84c3661ddc8f423abb9c70310d0cf172aa2714ead78029b325e3f4" in events.CRON_TOPICS  # Cron1000
    assert len(events.CRON_TOPICS) == 5
    assert (events.DEBT, events.RESERVES) == ("0x9b6c56ec", "0xd66bd524")


def test_event_declaration_with_a_tuple_and_a_filter():
    event = events.parse_event(
        "FilledRelay(bytes32 inputToken, bytes32 outputToken, uint256 inputAmount, uint256 outputAmount, "
        "uint256 repaymentChainId, uint256 indexed originChainId, uint256 indexed depositId, uint32 fillDeadline, "
        "uint32 exclusivityDeadline, bytes32 exclusiveRelayer, bytes32 indexed relayer, bytes32 depositor, "
        "bytes32 recipient, bytes32 messageHash, (bytes32,bytes32,uint256,uint8) relayExecutionInfo)"
    )
    assert event.topic0 == "0x44b559f101f8fbcc8a0ea43fa91a05a729a5ea6e14a7c75aa750374690137208"
    assert event.indexed == ("originChainId", "depositId", "relayer")
    assert event.topic_index("depositId") == 2
    assert event.filter({"originChainId": 84532}) == [event.topic0, "0x" + "14a34".rjust(64, "0")]
    relayer = "0xe52ee8943c6e7a982268ce93a78de570c3b68d45"
    assert event.filter({"relayer": relayer}) == [event.topic0, None, None, "0x" + relayer[2:].rjust(64, "0")]
    with pytest.raises(ValueError):
        event.topic_index("inputToken")


def test_event_declaration_aliases_and_errors():
    assert events.parse_event("E(uint indexed a, int b)").topic0 == events.topic("E(uint256,int256)")
    for bad in ("not an event", "E(uint256 indexed)", "E(uint256 a,, uint256 b"):
        with pytest.raises(ValueError):
            events.parse_event(bad)


def test_debt_intervals():
    def log(event, block):
        return {"topics": [event], "blockNumber": hex(block)}

    black, white = events.BLACKLIST, events.WHITELIST
    a, b, c, d, e = log(white, 1), log(white, 2), log(black, 3), log(black, 4), log(white, 5)
    f = log(black, 6)
    # A first WhitelistContract closes an interval that was open at `from`; a repeated event changes nothing.
    assert debt_intervals([a, b, c, d, e, f]) == [(None, a), (c, e), (f, None)]
    assert debt_intervals([]) == []
    assert debt_intervals([c, e, b]) == [(c, e)]
