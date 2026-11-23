from collections.abc import Callable
from hashlib import sha256

from inventory.spans import ByteSpan
from storage import BusinessError


class SegmentVerificationError(BusinessError):
    pass


def digest_bytes(body: bytes) -> str:
    return sha256(body).hexdigest()


def verify_source_segment(body: bytes, span: ByteSpan, digest: str) -> None:
    if len(body) != span.length:
        raise SegmentVerificationError(f'short read at [{span.start},{span.end})')
    if digest_bytes(body) != digest:
        raise SegmentVerificationError(f'original segment digest mismatch at [{span.start},{span.end})')


def read_verified_slice(read: Callable[[int, int], bytes], original: ByteSpan,
                        digest: str, clipped: ByteSpan) -> bytes:
    if not original.start <= clipped.start < clipped.end <= original.end:
        raise SegmentVerificationError('slice lies outside its original segment')
    body = read(original.start, original.end)
    verify_source_segment(body, original, digest)
    return body[clipped.start - original.start:clipped.end - original.start]
