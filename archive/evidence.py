import sqlite3
from dataclasses import dataclass

from archive.index import ArchiveWindow, load_archive
from archive.segments import ArchiveSegmentError, assemble_window_bytes
from inventory.verify import digest_bytes
from inventory.windows import WindowPlan


@dataclass(frozen=True)
class ArchiveEvidence:
    state: str
    detail: str | None = None
    window: ArchiveWindow | None = None
    body: bytes | None = None


def inspect_archive_window(db: sqlite3.Connection, order_id: str, plan: WindowPlan) -> ArchiveEvidence:
    window = load_archive(db, order_id)
    if window is None:
        return ArchiveEvidence('missing', 'no local window index')
    if window.mount_id != plan.mount_id or window.span != plan.span:
        return ArchiveEvidence('damaged', 'window binding differs from formal order', window)
    if len(window.slices) < len(plan.slices):
        return ArchiveEvidence('incomplete', 'one or more planned slices are missing', window)
    try:
        body = assemble_window_bytes(plan, window.slices)
    except ArchiveSegmentError as exc:
        return ArchiveEvidence('damaged', str(exc), window)
    if digest_bytes(body) != window.body_digest:
        return ArchiveEvidence('damaged', 'assembled window digest mismatch', window)
    return ArchiveEvidence('valid', window=window, body=body)
