import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from orders.obligations import AcceptedWindow, list_accepted_windows
from orders.receipt import OrderReceipt
from reconcile.coverage import check_coverage
from reconcile.history import load_report, save_report
from reconcile.returns import check_return
from storage import transaction


@dataclass(frozen=True)
class ReceiptFinding:
    order_id: str
    state: str
    receipt: OrderReceipt | None
    detail: str | None


@dataclass(frozen=True)
class ShiftReport:
    report_id: int
    mount_id: int
    snapshot: dict


def check_receipts(db: sqlite3.Connection, obligations: Iterable[AcceptedWindow]) -> tuple[ReceiptFinding, ...]:
    repeated = {row['receipt_id'] for row in db.execute('''SELECT receipt_id FROM order_receipts
        GROUP BY receipt_id HAVING COUNT(*) > 1''')}
    findings = []
    for window in obligations:
        order_id = window.request.order_id
        if not window.receipts:
            finding = ReceiptFinding(order_id, 'missing', None, 'no first receipt evidence')
        elif len(window.receipts) > 1 or window.receipts[0].receipt_id in repeated:
            finding = ReceiptFinding(order_id, 'duplicate', None, 'first receipt evidence is repeated')
        else:
            receipt = window.receipts[0]
            if receipt.matches(window.request):
                finding = ReceiptFinding(order_id, 'valid', receipt, None)
            else:
                finding = ReceiptFinding(order_id, 'damaged', None, 'first receipt binding differs from order')
        findings.append(finding)
    return tuple(findings)


def build_shift_report(db: sqlite3.Connection, mount_id: int) -> ShiftReport:
    with transaction(db):
        returned = check_return(db, mount_id)
        windows = list_accepted_windows(db, mount_id)
        snapshot = {
            'return': asdict(returned),
            'receipts': [asdict(finding) for finding in check_receipts(db, windows)],
            'coverage': check_coverage(db, windows).to_dict(),
        }
        return ShiftReport(save_report(db, mount_id, snapshot), mount_id, snapshot)


def read_shift_report(db: sqlite3.Connection, report_id: int) -> ShiftReport:
    mount_id, snapshot = load_report(db, report_id)
    return ShiftReport(report_id, mount_id, snapshot)
