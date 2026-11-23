import sqlite3
from dataclasses import dataclass

from inventory.windows import WindowPlan
from orders.intake import OrderRequest, get_order
from orders.receipt import OrderReceipt, ReceiptError, list_receipts
from storage import BusinessError, require_number


class NotAccepted(BusinessError):
    pass


@dataclass(frozen=True)
class AcceptedWindow:
    request: OrderRequest
    plan: WindowPlan
    receipts: tuple[OrderReceipt, ...]

    def require_receipt(self) -> OrderReceipt:
        if not self.receipts:
            raise ReceiptError(f'missing receipt for order {self.request.order_id!r}')
        if len(self.receipts) > 1:
            raise ReceiptError(f'duplicate receipts for order {self.request.order_id!r}')
        receipt = self.receipts[0]
        if not receipt.matches(self.request):
            raise ReceiptError(f'receipt binding mismatch for order {self.request.order_id!r}')
        return receipt

    def require_plan(self) -> WindowPlan:
        if self.plan.mount_id != self.request.mount_id or self.plan.span != self.request.span:
            raise NotAccepted(f'fixed plan binding mismatch for order {self.request.order_id!r}')
        return self.plan


def require_accepted_window(db: sqlite3.Connection, order_id: str) -> AcceptedWindow:
    record = get_order(db, order_id)
    if record is None or record.status != 'accepted':
        raise NotAccepted(f'order {order_id!r} has no formal admission')
    return AcceptedWindow(record.request, record.plan, list_receipts(db, order_id))


def list_accepted_windows(db: sqlite3.Connection, mount_id: int) -> tuple[AcceptedWindow, ...]:
    require_number(mount_id, 'mount instance')
    rows = db.execute("SELECT order_id FROM window_orders WHERE mount_id = ? AND status = 'accepted' ORDER BY order_id",
                      (mount_id,))
    return tuple(require_accepted_window(db, row['order_id']) for row in rows)
