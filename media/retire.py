import sqlite3
from dataclasses import dataclass

from archive.write import PreservationError, preserve_window
from media.paged import PagedBadPage, PagedWriteFailure, is_paged, stage_pages
from media.port import LocalVolumePort, PhysicalPresence, PortError
from media.read import probe_source, read_source_window
from media.session import close_admission, get_mount, record_return
from orders.obligations import list_accepted_windows
from storage import BusinessError, require_key, require_number, transaction


class RetirementError(BusinessError):
    pass


class RetirementPending(RetirementError):
    pass


@dataclass(frozen=True)
class RetireReceipt:
    request_id: str
    mount_id: int
    receipt_id: str


@dataclass(frozen=True)
class Retirement:
    request_id: str
    mount_id: int
    phase: str
    receipt_id: str | None
    problem: str | None

    @property
    def receipt(self) -> RetireReceipt | None:
        if self.phase == 'returned' and self.receipt_id is not None:
            return RetireReceipt(self.request_id, self.mount_id, self.receipt_id)
        return None


def initialize_retirements(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS retirements (
        request_id TEXT PRIMARY KEY, mount_id INTEGER NOT NULL UNIQUE REFERENCES mounts(mount_id),
        phase TEXT NOT NULL CHECK (phase IN ('preserving', 'write_failed', 'permitted', 'attempting', 'returned')),
        receipt_id TEXT UNIQUE, problem TEXT,
        CHECK ((phase = 'returned') = (receipt_id IS NOT NULL)))''')


def get_retirement(db: sqlite3.Connection, request_id: str) -> Retirement | None:
    require_key(request_id, 'retirement ID')
    row = db.execute('SELECT * FROM retirements WHERE request_id = ?', (request_id,)).fetchone()
    return Retirement(**dict(row)) if row is not None else None


def retirement_for_mount(db: sqlite3.Connection, mount_id: int) -> Retirement | None:
    row = db.execute('SELECT * FROM retirements WHERE mount_id = ?', (mount_id,)).fetchone()
    return Retirement(**dict(row)) if row is not None else None


def _begin(db: sqlite3.Connection, request_id: str, mount_id: int) -> None:
    with transaction(db):
        previous = get_retirement(db, request_id)
        if previous is not None:
            if previous.mount_id != mount_id:
                # A return number stays bound to its original mount instance.
                raise RetirementError(f'retirement {request_id!r} belongs to mount {previous.mount_id}')
            return
        get_mount(db, mount_id)
        if retirement_for_mount(db, mount_id) is not None:
            raise RetirementError(f'mount {mount_id} already has a retirement ID')
        close_admission(db, mount_id)
        db.execute("INSERT INTO retirements(request_id, mount_id, phase) VALUES (?, ?, 'preserving')",
                   (request_id, mount_id))


def _confirm(db: sqlite3.Connection, retirement: Retirement) -> RetireReceipt:
    # Called in a write transaction after probing the original attachment.
    receipt = RetireReceipt(retirement.request_id, retirement.mount_id, f'return/{retirement.request_id}')
    record_return(db, retirement.mount_id)
    db.execute("UPDATE retirements SET phase = 'returned', receipt_id = ?, problem = NULL WHERE request_id = ?",
               (receipt.receipt_id, receipt.request_id))
    return receipt


def _note_problem(db: sqlite3.Connection, request_id: str, detail: str) -> None:
    with transaction(db):
        db.execute("""UPDATE retirements SET problem = ?
            WHERE request_id = ? AND phase != 'returned'""", (detail, request_id))


def _permit(db: sqlite3.Connection, request_id: str) -> None:
    with transaction(db):
        # Never downgrade an in-flight eject attempt; only clear preservation
        # states once paging has verified every touched segment.
        db.execute("""UPDATE retirements SET phase = 'permitted', problem = NULL
            WHERE request_id = ? AND phase IN ('preserving', 'write_failed')""",
                   (request_id,))


def _preserve_regular(db: sqlite3.Connection, port: LocalVolumePort, request_id: str) -> None:
    """Read-and-preserve every formal window at once; used by ordinary volumes."""
    with transaction(db):
        current = get_retirement(db, request_id)
        if current.receipt is not None:
            return
        if probe_source(db, port, current.mount_id) is not PhysicalPresence.PRESENT:
            raise RetirementPending(f'original attachment {current.mount_id} cannot be confirmed in place')

        def read(plan):
            return read_source_window(db, port, plan)

        for window in list_accepted_windows(db, current.mount_id):
            preserve_window(db, window, read)
        if current.phase != 'attempting':
            db.execute("UPDATE retirements SET phase = 'permitted', problem = NULL WHERE request_id = ?",
                       (request_id,))


def _authorize(db: sqlite3.Connection, port: LocalVolumePort, request_id: str) -> RetireReceipt | None:
    with transaction(db):
        current = get_retirement(db, request_id)
        if current.receipt is not None:
            return current.receipt
        presence = probe_source(db, port, current.mount_id)
        if presence is PhysicalPresence.EJECTED and current.phase == 'attempting':
            return _confirm(db, current)
        if presence is not PhysicalPresence.PRESENT:
            raise RetirementPending(f'original attachment {current.mount_id} cannot be confirmed in place')
        paged = is_paged(db, current.mount_id)

    if paged:
        # Pages are staged in their own transactions and confirmed on the device
        # only after a durable local save.  A bad page blocks the whole return.
        stage_pages(db, port, current.mount_id)
        _permit(db, request_id)
        return None

    _preserve_regular(db, port, request_id)
    return None


def _mark_write_failure(db: sqlite3.Connection, request_id: str, detail: str) -> None:
    with transaction(db):
        db.execute("""UPDATE retirements SET phase = 'write_failed', problem = ?
            WHERE request_id = ? AND phase IN ('preserving', 'write_failed')""", (detail, request_id))


def _record_attempt(db: sqlite3.Connection, request_id: str) -> tuple[Retirement, int]:
    with transaction(db):
        current = get_retirement(db, request_id)
        if current.receipt is None:
            updated = db.execute("""UPDATE retirements SET phase = 'attempting'
                WHERE request_id = ? AND phase IN ('permitted', 'attempting')""", (request_id,))
            if updated.rowcount != 1:
                raise RetirementPending(f'retirement {request_id!r} has no valid eject permit')
        return get_retirement(db, request_id), get_mount(db, current.mount_id).port_id


def retire_volume(db: sqlite3.Connection, port: LocalVolumePort, request_id: str, mount_id: int) -> RetireReceipt:
    require_key(request_id, 'retirement ID')
    require_number(mount_id, 'mount instance')
    _begin(db, request_id, mount_id)
    try:
        receipt = _authorize(db, port, request_id)
    except PagedWriteFailure as exc:
        _mark_write_failure(db, request_id, str(exc))
        raise PreservationError(f'retirement {request_id!r} has no confirmed preservation: {exc}') from exc
    except PagedBadPage as exc:
        _note_problem(db, request_id, exc.detail)
        raise
    except PortError as exc:
        if is_paged(db, mount_id):
            _note_problem(db, request_id, str(exc))
            raise RetirementPending(f'retirement {request_id!r} is awaiting the page cursor: {exc}') from exc
        raise
    except (PreservationError, sqlite3.Error) as exc:
        _mark_write_failure(db, request_id, str(exc))
        raise PreservationError(f'retirement {request_id!r} has no confirmed preservation: {exc}') from exc
    if receipt is not None:
        return receipt
    current, port_id = _record_attempt(db, request_id)
    if current.receipt is not None:
        return current.receipt
    try:
        port.eject(port_id)
    except (PortError, sqlite3.Error) as exc:
        raise RetirementPending(f'retirement {request_id!r} is awaiting the original eject outcome') from exc
    with transaction(db):
        current = get_retirement(db, request_id)
        if current.receipt is not None:
            return current.receipt
        if probe_source(db, port, mount_id) is not PhysicalPresence.EJECTED:
            raise RetirementPending(f'retirement {request_id!r} has no confirmed eject')
        return _confirm(db, current)
