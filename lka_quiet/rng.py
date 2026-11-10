"""SplitMix64: the one source of randomness for run order and bootstrap resampling.

Python's `random` makes no promise that a seed yields the same stream across versions; the
board's published orderings and confidence intervals must be reproducible byte for byte by
anyone, so the generator is spelled out here in integer arithmetic.
"""

import hashlib

_MASK = (1 << 64) - 1


class SplitMix64:
    def __init__(self, seed: int):
        self.state = seed & _MASK

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        """Uniform integer in [0, n), by rejection so that no residue is favoured."""
        if n <= 0:
            raise ValueError(f"below() needs a positive bound, got {n}")
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            value = self.next_u64()
            if value < limit:
                return value % n

    def shuffled(self, items) -> list:
        """A Fisher-Yates permutation of `items`, which is left untouched."""
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out


def seed_from(*parts: str) -> int:
    """A 64-bit seed named by its parts (e.g. the round and the pass), unit-separator joined."""
    digest = hashlib.sha256("\x1f".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")
