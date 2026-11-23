from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ByteSpan:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def validate_span(span: ByteSpan, limit: int) -> ByteSpan:
    if type(span.start) is not int or type(span.end) is not int:
        raise ValueError('byte coordinates must be integers')
    if not 0 <= span.start < span.end <= limit:
        raise ValueError(f'nonempty byte range [{span.start},{span.end}) exceeds volume bounds [0,{limit})')
    return span


def intersect_span(left: ByteSpan, right: ByteSpan) -> ByteSpan | None:
    start = max(left.start, right.start)
    end = min(left.end, right.end)
    return ByteSpan(start, end) if start < end else None
