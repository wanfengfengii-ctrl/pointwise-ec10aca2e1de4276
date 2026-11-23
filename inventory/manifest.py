from collections.abc import Callable, Mapping
from dataclasses import dataclass
from string import hexdigits

from inventory.spans import ByteSpan, validate_span
from inventory.verify import SegmentVerificationError, verify_source_segment
from storage import BusinessError, require_key


class ManifestError(BusinessError):
    pass


@dataclass(frozen=True)
class SourceSegment:
    segment_id: str
    run_id: str
    span: ByteSpan
    digest: str


@dataclass(frozen=True)
class VerifiedManifest:
    length: int
    runs: tuple[str, ...]
    segments: tuple[SourceSegment, ...]


def _parse_segment(item: Mapping, length: int, run_ids: set[str]) -> SourceSegment:
    if not isinstance(item, Mapping):
        raise ValueError('segment must be an object')
    segment_id = require_key(item['id'], 'segment ID')
    run_id = require_key(item['run'], 'run ID')
    if run_id not in run_ids:
        raise ValueError(f'segment {segment_id} refers to unknown run {run_id}')
    span = validate_span(ByteSpan(item['start'], item['end']), length)
    digest = item['sha256']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in hexdigits[:16] for c in digest):
        raise ValueError(f'segment {segment_id} has an invalid SHA-256 digest')
    return SourceSegment(segment_id, run_id, span, digest)


def verify_manifest(raw: Mapping, actual_length: int,
                    read: Callable[[int, int], bytes]) -> VerifiedManifest:
    if not isinstance(raw, Mapping):
        raise ManifestError('manifest must be an object')
    try:
        length = raw['length']
        if type(length) is not int or not 0 <= length <= 2**63 - 1 or length != actual_length:
            raise ValueError('declared volume length does not match the attachment')
        if not isinstance(raw['runs'], list) or not isinstance(raw['segments'], list):
            raise ValueError('runs and segments must be arrays')
        runs = tuple(require_key(item, 'run ID') for item in raw['runs'])
        run_ids = set(runs)
        if len(run_ids) != len(runs):
            raise ValueError('duplicate run ID')
        segments = sorted((_parse_segment(item, length, run_ids) for item in raw['segments']),
                          key=lambda segment: (segment.span, segment.segment_id))
        if len({segment.segment_id for segment in segments}) != len(segments):
            raise ValueError('duplicate segment ID')
        previous_end = 0
        for segment in segments:
            if segment.span.start < previous_end:
                raise ValueError(f'overlapping original segment {segment.segment_id}')
            previous_end = segment.span.end
        for segment in segments:
            body = read(segment.span.start, segment.span.end)
            verify_source_segment(body, segment.span, segment.digest)
    except (KeyError, ValueError, SegmentVerificationError) as exc:
        raise ManifestError(str(exc)) from exc
    return VerifiedManifest(length, tuple(sorted(runs)), tuple(segments))
