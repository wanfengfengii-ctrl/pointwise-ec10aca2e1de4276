"""Incremental SHA-256 whose midstate can be persisted between pages.

The archive may not keep raw bytes outside the formal windows, but it still has
to prove that every touched original segment equals its mount-time digest.  A
page plan slices each segment from its own start in 64-byte pieces, so every
full page ends exactly on a SHA-256 block boundary.  Persisting the eight state
words (plus a possible short tail) is therefore enough to resume verification
after a reopen without storing any extra original bytes.
"""
import struct

_K = (
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
)

_IV = (
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
)


def _rotr(value: int, count: int) -> int:
    return ((value >> count) | (value << (32 - count))) & 0xffffffff


class Sha256State:
    """SHA-256 with exportable intermediate state."""

    __slots__ = ('_h', '_count', '_buf')

    def __init__(self, state=None, count: int = 0, buffer: bytes = b''):
        self._h = list(state) if state is not None else list(_IV)
        self._count = count
        self._buf = bytearray(buffer)

    def copy(self) -> 'Sha256State':
        return Sha256State(self._h, self._count, bytes(self._buf))

    def update(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError('hash input must be bytes-like')
        data = bytes(data)
        self._count += len(data)
        self._buf += data
        while len(self._buf) >= 64:
            self._compress(bytes(self._buf[:64]))
            del self._buf[:64]

    def _compress(self, block: bytes) -> None:
        words = list(struct.unpack('>16I', block))
        words.extend([0] * 48)
        for index in range(16, 64):
            x, y = words[index - 15], words[index - 2]
            s0 = _rotr(x, 7) ^ _rotr(x, 18) ^ (x >> 3)
            s1 = _rotr(y, 17) ^ _rotr(y, 19) ^ (y >> 10)
            words[index] = (words[index - 16] + s0 + words[index - 7] + s1) & 0xffffffff
        a, b, c, d, e, f, g, h = self._h
        for index in range(64):
            s1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
            ch = (e & f) ^ ((e ^ 0xffffffff) & g)
            t1 = (h + s1 + ch + _K[index] + words[index]) & 0xffffffff
            s0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            t2 = (s0 + maj) & 0xffffffff
            h, g, f, e = g, f, e, (d + t1) & 0xffffffff
            d, c, b, a = c, b, a, (t1 + t2) & 0xffffffff
        self._h = [(left + right) & 0xffffffff for left, right in zip(self._h, (a, b, c, d, e, f, g, h))]

    def digest(self) -> bytes:
        clone = self.copy()
        clone._finalize()
        return b''.join(struct.pack('>I', word) for word in clone._h)

    def hexdigest(self) -> str:
        return self.digest().hex()

    def _finalize(self) -> None:
        bit_length = (self._count * 8) & 0xffffffffffffffff
        self._buf.append(0x80)
        while len(self._buf) % 64 != 56:
            self._buf.append(0)
        self._buf += struct.pack('>Q', bit_length)
        while self._buf:
            self._compress(bytes(self._buf[:64]))
            del self._buf[:64]

    def export(self) -> tuple[str, int, str]:
        state_hex = ''.join(f'{word:08x}' for word in self._h)
        return state_hex, self._count, bytes(self._buf).hex()

    @classmethod
    def load(cls, state_hex: str, count: int, buffer_hex: str = '') -> 'Sha256State':
        if not isinstance(state_hex, str) or len(state_hex) != 64:
            raise ValueError('hash state must be 64 lowercase hex digits')
        try:
            state = [int(state_hex[index:index + 8], 16) for index in range(0, 64, 8)]
            buffer = bytes.fromhex(buffer_hex) if buffer_hex else b''
        except ValueError as exc:
            raise ValueError('invalid persisted hash state') from exc
        if type(count) is not int or not 0 <= count <= 2**63 - 1:
            raise ValueError('invalid persisted hash length')
        return cls(state, count, buffer)
