import sqlite3

from media.session import MountState, get_mount
from orders.intake import OrderRequest, match_existing_request, prepare_order
from orders.receipt import OrderReceipt, ReceiptError, issue_receipt, load_receipt
from storage import BusinessError, transaction


class AdmissionRejected(BusinessError):
    pass


def commit_admission(db: sqlite3.Connection, request: OrderRequest) -> OrderReceipt:
    try:
        with transaction(db):
            order = match_existing_request(db, request)
            if order is None:
                raise AdmissionRejected(f'order {request.order_id!r} has no preparation')
            if order.status == 'accepted':
                receipt = load_receipt(db, request.order_id)
                if receipt is None or not receipt.matches(order.request):
                    raise ReceiptError(f'order {request.order_id!r} lacks its fixed receipt')
                return receipt
            if order.status == 'prepared':
                mount = get_mount(db, order.request.mount_id)
                if mount.state is MountState.OPEN:
                    db.execute("UPDATE window_orders SET status = 'accepted' WHERE order_id = ?",
                               (request.order_id,))
                    return issue_receipt(db, order.request)
                db.execute("UPDATE window_orders SET status = 'rejected' WHERE order_id = ?",
                           (request.order_id,))
    except sqlite3.Error as exc:
        raise AdmissionRejected(f'order {request.order_id!r} admission could not commit') from exc
    raise AdmissionRejected(f'mount {request.mount_id} is closed to new admissions')


def admit_order(db: sqlite3.Connection, request: OrderRequest) -> OrderReceipt:
    prepare_order(db, request)
    return commit_admission(db, request)
