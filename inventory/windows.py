import json
import sqlite3
from dataclasses import asdict, dataclass

from inventory.manifest import SourceSegment
from inventory.runs import InventoryError, get_volume_length, lookup_segments
from inventory.spans import ByteSpan, intersect_span, validate_span
from storage import BusinessError


class WindowError(BusinessError):
    pass


@dataclass(frozen=True)
class WindowSlice:
    segment: SourceSegment
    span: ByteSpan


@dataclass(frozen=True)
class WindowPlan:
    mount_id: int
    span: ByteSpan
    slices: tuple[WindowSlice, ...]

    def encode(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))

    @classmethod
    def decode(cls, raw: str) -> 'WindowPlan':
        data = json.loads(raw)
        slices = []
        for item in data['slices']:
            segment = item['segment']
            source = SourceSegment(segment['segment_id'], segment['run_id'],
                                   ByteSpan(**segment['span']), segment['digest'])
            slices.append(WindowSlice(source, ByteSpan(**item['span'])))
        return cls(data['mount_id'], ByteSpan(**data['span']), tuple(slices))


def plan_window(db: sqlite3.Connection, mount_id: int, start: int, end: int) -> WindowPlan:
    try:
        span = validate_span(ByteSpan(start, end), get_volume_length(db, mount_id))
        segments = lookup_segments(db, mount_id)
    except (InventoryError, ValueError) as exc:
        raise WindowError(str(exc)) from exc
    slices = []
    cursor = span.start
    for segment in segments:
        clipped = intersect_span(span, segment.span)
        if clipped is None:
            continue
        if clipped.start != cursor:
            raise WindowError(f'window has a gap or ambiguous source at byte {cursor}')
        slices.append(WindowSlice(segment, clipped))
        cursor = clipped.end
    if cursor != span.end:
        raise WindowError(f'window has no original segment at byte {cursor}')
    return WindowPlan(mount_id, span, tuple(slices))
