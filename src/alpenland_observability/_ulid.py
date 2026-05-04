"""Tiny dependency-free ULID generator.

A ULID is a 26-character lexicographically-sortable identifier built from a
48-bit timestamp + 80 bits of randomness. We don't need full ULID-spec
correctness here — only a unique, sortable, URL-safe ID per run. So we
implement the minimum required (Crockford-base32, monotonicity within the
same millisecond is *not* guaranteed; for our cardinality that's fine).

Avoiding the ``python-ulid`` dependency keeps this library zero-dep.
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bits
    n = (ms << 80) | rand  # 48 + 80 = 128 bits → 26 base32 chars
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[n & 0b11111])
        n >>= 5
    return "".join(reversed(chars))
