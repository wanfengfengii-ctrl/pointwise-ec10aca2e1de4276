from dataclasses import dataclass

from inventory.spans import ByteSpan
from inventory.verify import digest_bytes
from inventory.windows import WindowPlan
from storage import BusinessError


class ArchiveSegmentError(BusinessError):
    pass


@dataclass(frozen=True)
class ArchivedSlice:
    position: int
    mount_id: int
    segment_id: str
    span: ByteSpan
    length: int
    digest: str
    body: bytes


def build_archive_segments(plan: WindowPlan, pieces: tuple[bytes, ...]) -> tuple[ArchivedSlice, ...]:
    if len(pieces) != len(plan.slices):
        raise ArchiveSegmentError('slice count differs from fixed plan')
    result = []
    for position, (piece, planned) in enumerate(zip(pieces, plan.slices)):
        if not isinstance(piece, bytes) or len(piece) != planned.span.length:
            raise ArchiveSegmentError(f'wrong source slice length at position {position}')
        result.append(ArchivedSlice(position, plan.mount_id, planned.segment.segment_id,
                                    planned.span, len(piece), digest_bytes(piece), piece))
    return tuple(result)


def assemble_window_bytes(plan: WindowPlan, slices: tuple[ArchivedSlice, ...]) -> bytes:
    if len(slices) != len(plan.slices):
        raise ArchiveSegmentError('archived slice count differs from fixed plan')
    cursor = plan.span.start
    for position, (stored, planned) in enumerate(zip(slices, plan.slices)):
        if (stored.position, stored.mount_id, stored.segment_id, stored.span) != (
                position, plan.mount_id, planned.segment.segment_id, planned.span):
            raise ArchiveSegmentError(f'slice {position} has incorrect provenance')
        if stored.span.start != cursor or not cursor < stored.span.end <= plan.span.end:
            raise ArchiveSegmentError(f'slice {position} is outside the contiguous window')
        if (not isinstance(stored.body, bytes) or stored.length != stored.span.length
                or len(stored.body) != stored.length):
            raise ArchiveSegmentError(f'slice {position} length mismatch')
        if digest_bytes(stored.body) != stored.digest:
            raise ArchiveSegmentError(f'slice {position} digest mismatch')
        cursor = stored.span.end
    if cursor != plan.span.end:
        raise ArchiveSegmentError('archive does not reach the end of the fixed window')
    return b''.join(stored.body for stored in slices)
