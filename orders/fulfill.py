import sqlite3
from dataclasses import dataclass

from archive.read import ArchivedWindowDamaged, ArchivedWindowMissing, read_archived_window
from archive.segments import ArchivedSlice, assemble_window_bytes, build_archive_segments
from media.port import LocalVolumePort
from media.read import SourceUnavailable, read_source_window
from orders.obligations import require_accepted_window
from orders.receipt import OrderReceipt
from storage import BusinessError, transaction


class FulfillmentError(BusinessError):
    pass


@dataclass(frozen=True)
class Provenance:
    mount_id: int
    segment_id: str
    start: int
    end: int

    @classmethod
    def from_slice(cls, piece: ArchivedSlice) -> 'Provenance':
        return cls(piece.mount_id, piece.segment_id, piece.span.start, piece.span.end)


@dataclass(frozen=True)
class FulfilledWindow:
    receipt: OrderReceipt
    body: bytes
    provenance: tuple[Provenance, ...]


def fulfill_order(db: sqlite3.Connection, port: LocalVolumePort, order_id: str) -> FulfilledWindow:
    try:
        with transaction(db, write=False):
            accepted = require_accepted_window(db, order_id)
            receipt = accepted.require_receipt()
            plan = accepted.require_plan()
            try:
                body, slices = read_archived_window(db, order_id, plan)
            except ArchivedWindowMissing:
                pieces = read_source_window(db, port, plan)
                slices = build_archive_segments(plan, pieces)
                body = assemble_window_bytes(plan, slices)
            return FulfilledWindow(receipt, body, tuple(Provenance.from_slice(piece) for piece in slices))
    except ArchivedWindowDamaged as exc:
        raise FulfillmentError(f'damaged archive: {exc}') from exc
    except SourceUnavailable as exc:
        raise FulfillmentError(f'archive missing and original unavailable for {order_id!r}: {exc}') from exc
    except BusinessError as exc:
        raise FulfillmentError(str(exc)) from exc
