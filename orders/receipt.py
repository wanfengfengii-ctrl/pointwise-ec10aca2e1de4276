import sqlite3
from dataclasses import dataclass

from orders.intake import OrderRequest
from storage import BusinessError, require_key


class ReceiptError(BusinessError):
    pass


@dataclass(frozen=True)
class OrderReceipt:
    receipt_id: str
    order_id: str
    mount_id: int
    start: int
    end: int

    def matches(self, request: OrderRequest) -> bool:
        return (self.order_id, self.mount_id, self.start, self.end) == (
            request.order_id, request.mount_id, request.start, request.end)


def initialize_receipts(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS order_receipts (
        evidence_id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES window_orders(order_id),
        receipt_id TEXT NOT NULL, mount_id INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS receipt_order ON order_receipts(order_id, evidence_id)')
    db.execute('CREATE INDEX IF NOT EXISTS receipt_number ON order_receipts(receipt_id)')


def issue_receipt(db: sqlite3.Connection, request: OrderRequest) -> OrderReceipt:
    if list_receipts(db, request.order_id):
        raise ReceiptError(f'order {request.order_id!r} already has receipt evidence')
    receipt = OrderReceipt(f'window/{request.order_id}', request.order_id, request.mount_id,
                           request.start, request.end)
    db.execute('''INSERT INTO order_receipts(order_id, receipt_id, mount_id, start, end)
        VALUES (?, ?, ?, ?, ?)''', (receipt.order_id, receipt.receipt_id, receipt.mount_id,
                                   receipt.start, receipt.end))
    return receipt


def list_receipts(db: sqlite3.Connection, order_id: str) -> tuple[OrderReceipt, ...]:
    require_key(order_id, 'order ID')
    rows = db.execute('''SELECT receipt_id, order_id, mount_id, start, end FROM order_receipts
        WHERE order_id = ? ORDER BY evidence_id''', (order_id,))
    return tuple(OrderReceipt(**dict(row)) for row in rows)


def load_receipt(db: sqlite3.Connection, order_id: str) -> OrderReceipt | None:
    receipts = list_receipts(db, order_id)
    if len(receipts) > 1:
        raise ReceiptError(f'duplicate receipt evidence for order {order_id!r}')
    return receipts[0] if receipts else None
