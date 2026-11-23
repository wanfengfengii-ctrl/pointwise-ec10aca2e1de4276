import sqlite3
from dataclasses import dataclass

from inventory.spans import ByteSpan
from inventory.windows import WindowError, WindowPlan, plan_window
from storage import BusinessError, require_key, require_number, transaction


class OrderConflict(BusinessError):
    pass


@dataclass(frozen=True)
class OrderRequest:
    order_id: str
    mount_id: int
    start: int
    end: int

    @property
    def span(self) -> ByteSpan:
        return ByteSpan(self.start, self.end)

    def validate_parameters(self) -> None:
        require_key(self.order_id, 'order ID')
        require_number(self.mount_id, 'mount instance')
        if type(self.start) is not int or type(self.end) is not int:
            raise WindowError('byte coordinates must be integers')


@dataclass(frozen=True)
class PreparedOrder:
    request: OrderRequest
    plan: WindowPlan
    status: str


def initialize_orders(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS window_orders (
        order_id TEXT PRIMARY KEY, mount_id INTEGER NOT NULL REFERENCES volume_index(mount_id),
        start INTEGER NOT NULL, end INTEGER NOT NULL, plan TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('prepared', 'accepted', 'rejected')))''')
    db.execute('''CREATE INDEX IF NOT EXISTS order_obligation_index
        ON window_orders(mount_id, status, order_id)''')


def get_order(db: sqlite3.Connection, order_id: str) -> PreparedOrder | None:
    require_key(order_id, 'order ID')
    row = db.execute('SELECT * FROM window_orders WHERE order_id = ?', (order_id,)).fetchone()
    if row is None:
        return None
    request = OrderRequest(row['order_id'], row['mount_id'], row['start'], row['end'])
    return PreparedOrder(request, WindowPlan.decode(row['plan']), row['status'])


def match_existing_request(db: sqlite3.Connection, request: OrderRequest) -> PreparedOrder | None:
    request.validate_parameters()
    existing = get_order(db, request.order_id)
    if existing is not None and existing.request != request:
        raise OrderConflict(f'order {request.order_id!r} has different fixed parameters')
    return existing


def prepare_order(db: sqlite3.Connection, request: OrderRequest) -> PreparedOrder:
    with transaction(db):
        existing = match_existing_request(db, request)
        if existing is not None:
            return existing
        plan = plan_window(db, request.mount_id, request.start, request.end)
        db.execute('''INSERT INTO window_orders(order_id, mount_id, start, end, plan, status)
            VALUES (?, ?, ?, ?, ?, 'prepared')''',
            (request.order_id, request.mount_id, request.start, request.end, plan.encode()))
        return PreparedOrder(request, plan, 'prepared')
