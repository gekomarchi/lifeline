"""Platform event topics, ledger getters and event declarations.

The platform signatures come from the Reactive Network sources:

- `system-smart-contracts` at `72710d2`
  - `src/omni/base/AbstractProxiedPayableBridge.sol`: `BlacklistContract`,
    `WhitelistContract`, `debt(address)`, `reserves(address)`
  - `src/omni/SystemContract.sol`: `Subscribe`, `CallbackRequest`
  - `src/SystemContract.sol`: `Cron1` to `Cron10000`
- `reactive-lib-omni` at `3ade0dc`
  - `src/interfaces/ISystemContract.sol`: `CallbackVersion` is an enum (ABI type `uint8`)
"""

from __future__ import annotations

from dataclasses import dataclass

from .keccak import keccak256


def topic(signature: str) -> str:
    """Topic 0 of an event with this canonical signature."""
    return "0x" + keccak256(signature.encode()).hex()


def selector(signature: str) -> str:
    return topic(signature)[:10]


# event BlacklistContract(address indexed callback_)
BLACKLIST = topic("BlacklistContract(address)")
# event WhitelistContract(address indexed callback_)
WHITELIST = topic("WhitelistContract(address)")
# event Subscribe(address indexed subscriber, uint256 indexed chainId, address indexed contractAddress,
#                 uint256 topic0, uint256 topic1, uint256 topic2, uint256 topic3)
SUBSCRIBE = topic("Subscribe(address,uint256,address,uint256,uint256,uint256,uint256)")
# event CallbackRequest(uint256 indexed chainId_, address indexed sender_, address indexed recipient_,
#                       CallbackVersion version_, bytes configuration_)
CALLBACK_REQUEST = topic("CallbackRequest(uint256,address,address,uint8,bytes)")
# event Cron1(uint256 indexed number) ... event Cron10000(uint256 indexed number)
CRON_TOPICS = frozenset(topic(f"Cron{period}(uint256)") for period in (1, 10, 100, 1000, 10000))

DEBT = selector("debt(address)")
RESERVES = selector("reserves(address)")


def word(value) -> str:
    """A 32-byte topic or ABI word for an address, a hex value or a number."""
    if isinstance(value, str) and value.lower().startswith("0x"):
        digits = value[2:].lower()
    else:
        digits = format(int(value), "x")
    if len(digits) > 64:
        raise ValueError(f"{value} is longer than 32 bytes")
    return "0x" + digits.rjust(64, "0")


def address_of(word_hex: str) -> str:
    """The address in the low 20 bytes of a topic or word."""
    return "0x" + word_hex[-40:].lower()


def ledger_call(getter: str, contract: str) -> str:
    """Call data of `debt(address)` or `reserves(address)`."""
    return getter + word(contract)[2:]


@dataclass(frozen=True)
class Event:
    """An event declaration, for example `Filled(uint256 indexed id, address relayer)`."""

    topic0: str
    indexed: tuple  # names of the indexed parameters, in order

    def topic_index(self, field: str) -> int:
        """Position of an indexed parameter in the topics of a log."""
        if field not in self.indexed:
            raise ValueError(f"{field} is not an indexed parameter of the event")
        return 1 + self.indexed.index(field)

    def filter(self, where: dict) -> list:
        """Topic filter for the event with fixed values for some indexed parameters."""
        topics = [self.topic0] + [None] * len(self.indexed)
        for field, value in where.items():
            topics[self.topic_index(field)] = word(value)
        while topics[-1] is None:
            topics.pop()
        return topics


def parse_event(declaration: str) -> Event:
    """Parse a Solidity event declaration with parameter names.

    Only the text is used: the canonical signature gives topic 0, and the
    `indexed` parameters give the topic positions.
    """
    name, opening, rest = declaration.strip().partition("(")
    if not opening or not rest.endswith(")") or not name.strip().isidentifier():
        raise ValueError(f"not an event declaration: {declaration}")
    types, indexed = [], []
    for parameter in _split(rest[:-1], ","):
        parts = _split(parameter, " ")
        if not parts:
            raise ValueError(f"empty parameter in: {declaration}")
        types.append({"uint": "uint256", "int": "int256"}.get(parts[0], parts[0]))
        if "indexed" in parts[1:]:
            if parts[-1] == "indexed":
                raise ValueError(f"an indexed parameter needs a name in: {declaration}")
            indexed.append(parts[-1])
    return Event(topic(f"{name.strip()}({','.join(types)})"), tuple(indexed))


def _split(text: str, separator: str) -> list:
    """Split at a separator that is outside parentheses (a tuple type has commas of its own)."""
    parts, depth, current = [], 0, ""
    for char in text:
        depth += (char == "(") - (char == ")")
        if depth == 0 and (char == separator or (separator == " " and char.isspace())):
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]
