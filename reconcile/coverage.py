import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from archive.evidence import inspect_archive_window
from inventory.spans import ByteSpan
from orders.obligations import AcceptedWindow, NotAccepted


@dataclass(frozen=True)
class WindowFinding:
    order_id: str
    span: ByteSpan
    state: str
    detail: str | None


@dataclass(frozen=True)
class CoverageResult:
    requested: tuple[ByteSpan, ...]
    archived: tuple[ByteSpan, ...]
    missing: tuple[ByteSpan, ...]
    windows: tuple[WindowFinding, ...]

    def to_dict(self) -> dict:
        def pairs(spans):
            return [[span.start, span.end] for span in spans]

        return {
            'requested': pairs(self.requested),
            'archived': pairs(self.archived),
            'missing': pairs(self.missing),
            'windows': [{'order_id': finding.order_id, 'span': [finding.span.start, finding.span.end],
                         'state': finding.state, 'detail': finding.detail} for finding in self.windows],
        }


def merge_coverage(spans: Iterable[ByteSpan]) -> tuple[ByteSpan, ...]:
    merged = []
    for span in sorted(spans):
        if not merged or span.start > merged[-1].end:
            merged.append(span)
        else:
            previous = merged[-1]
            merged[-1] = ByteSpan(previous.start, max(previous.end, span.end))
    return tuple(merged)


def _missing(requested: tuple[ByteSpan, ...], archived: tuple[ByteSpan, ...]) -> tuple[ByteSpan, ...]:
    gaps = []
    for wanted in requested:
        cursor = wanted.start
        for available in archived:
            if available.end <= cursor:
                continue
            if available.start >= wanted.end:
                break
            if available.start > cursor:
                gaps.append(ByteSpan(cursor, available.start))
            cursor = max(cursor, available.end)
            if cursor >= wanted.end:
                break
        if cursor < wanted.end:
            gaps.append(ByteSpan(cursor, wanted.end))
    return tuple(gaps)


def check_coverage(db: sqlite3.Connection, obligations: Iterable[AcceptedWindow]) -> CoverageResult:
    findings = []
    for order in obligations:
        order_id = order.request.order_id
        try:
            evidence = inspect_archive_window(db, order_id, order.require_plan())
            finding = WindowFinding(order_id, order.request.span, evidence.state, evidence.detail)
        except NotAccepted as exc:
            finding = WindowFinding(order_id, order.request.span, 'damaged', str(exc))
        findings.append(finding)
    requested = merge_coverage(item.span for item in findings)
    archived = merge_coverage(item.span for item in findings if item.state == 'valid')
    return CoverageResult(requested, archived, _missing(requested, archived), tuple(findings))
