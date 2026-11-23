import sqlite3
from collections.abc import Callable

from archive.evidence import ArchiveEvidence, inspect_archive_window
from archive.index import ArchiveWindow, store_archive
from archive.segments import assemble_window_bytes, build_archive_segments
from inventory.verify import digest_bytes
from inventory.windows import WindowPlan
from orders.obligations import AcceptedWindow
from storage import BusinessError


class PreservationError(BusinessError):
    pass


def preserve_window(db: sqlite3.Connection, order: AcceptedWindow,
                    read: Callable[[WindowPlan], tuple[bytes, ...]]) -> ArchiveEvidence:
    """Preserve an accepted window inside the caller's write transaction."""
    order_id = order.request.order_id
    try:
        order.require_receipt()
        plan = order.require_plan()
        evidence = inspect_archive_window(db, order_id, plan)
        if evidence.state == 'valid':
            return evidence
        slices = build_archive_segments(plan, read(plan))
        body = assemble_window_bytes(plan, slices)
        store_archive(db, ArchiveWindow(order_id, plan.mount_id, plan.span, digest_bytes(body), slices))
        evidence = inspect_archive_window(db, order_id, plan)
    except (BusinessError, sqlite3.Error) as exc:
        raise PreservationError(f'window {order_id!r} could not be preserved: {exc}') from exc
    if evidence.state != 'valid':
        raise PreservationError(f'window {order_id!r} lacks valid preserved evidence: {evidence.detail}')
    return evidence
