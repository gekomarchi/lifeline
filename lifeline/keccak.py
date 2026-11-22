"""Keccak-256 (the Ethereum hash), pure Python.

The standard library has SHA3-256 but not the original Keccak padding that
Ethereum uses for event topics and function selectors.
"""

_MASK = (1 << 64) - 1
_RATE = 136  # bytes, for a 256-bit capacity pair

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# Rotation offsets, indexed [x][y].
_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK


def _permute(a: list) -> None:
    """Keccak-f[1600] on a 5x5 lane matrix, in place."""
    for rc in _ROUND_CONSTANTS:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        for x in range(5):
            d = c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1)
            for y in range(5):
                a[x][y] ^= d
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROTATIONS[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y])
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    padded = bytearray(data)
    pad_len = _RATE - len(padded) % _RATE
    padded += b"\x01" + b"\x00" * (pad_len - 1)
    padded[-1] |= 0x80

    a = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), _RATE):
        block = padded[offset:offset + _RATE]
        for i in range(_RATE // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _permute(a)

    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out
