import sqlite3

from archive.evidence import inspect_archive_window
from archive.segments import ArchivedSlice
from inventory.windows import WindowPlan
from storage import BusinessError


class ArchivedWindowMissing(BusinessError):
    pass


class ArchivedWindowDamaged(BusinessError):
    pass


def read_archived_window(db: sqlite3.Connection, order_id: str,
                         plan: WindowPlan) -> tuple[bytes, tuple[ArchivedSlice, ...]]:
    evidence = inspect_archive_window(db, order_id, plan)
    if evidence.state == 'missing':
        raise ArchivedWindowMissing(f'order {order_id!r}: {evidence.detail}')
    if evidence.state != 'valid':
        raise ArchivedWindowDamaged(f'order {order_id!r}: {evidence.detail}')
    return evidence.body, evidence.window.slices
