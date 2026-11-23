import sqlite3
from dataclasses import dataclass
from enum import Enum

from storage import BusinessError, require_key, require_number


class MountState(str, Enum):
    CANDIDATE = 'candidate'
    OPEN = 'open'
    REJECTED = 'rejected'
    CLOSING = 'closing'
    RETURNED = 'returned'


class MountError(BusinessError):
    pass


@dataclass(frozen=True)
class Mount:
    mount_id: int
    label: str
    port_id: int
    state: MountState
    rejection: str | None


def initialize_mount_state(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS mounts (
        mount_id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL, port_id INTEGER NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('candidate', 'open', 'rejected', 'closing', 'returned')),
        rejection TEXT)''')


def register_mount(db: sqlite3.Connection, label: str, port_id: int) -> int:
    require_key(label, 'volume label')
    require_number(port_id, 'port attachment')
    row = db.execute("INSERT INTO mounts(label, port_id, state) VALUES (?, ?, 'candidate')",
                     (label, port_id))
    return row.lastrowid


def get_mount(db: sqlite3.Connection, mount_id: int) -> Mount:
    require_number(mount_id, 'mount instance')
    row = db.execute('SELECT * FROM mounts WHERE mount_id = ?', (mount_id,)).fetchone()
    if row is None:
        raise MountError(f'unknown mount instance {mount_id}')
    return Mount(row['mount_id'], row['label'], row['port_id'], MountState(row['state']), row['rejection'])


def _transition(db: sqlite3.Connection, mount_id: int, before: MountState,
                after: MountState, reason: str | None = None) -> None:
    changed = db.execute('UPDATE mounts SET state = ?, rejection = ? WHERE mount_id = ? AND state = ?',
                         (after.value, reason, mount_id, before.value))
    if changed.rowcount != 1:
        mount = get_mount(db, mount_id)
        raise MountError(f'mount {mount_id} in state {mount.state.value} cannot become {after.value}')


def publish_mount(db: sqlite3.Connection, mount_id: int) -> None:
    _transition(db, mount_id, MountState.CANDIDATE, MountState.OPEN)


def reject_mount(db: sqlite3.Connection, mount_id: int, reason: str) -> None:
    _transition(db, mount_id, MountState.CANDIDATE, MountState.REJECTED, reason)


def close_admission(db: sqlite3.Connection, mount_id: int) -> None:
    _transition(db, mount_id, MountState.OPEN, MountState.CLOSING)


def record_return(db: sqlite3.Connection, mount_id: int) -> None:
    _transition(db, mount_id, MountState.CLOSING, MountState.RETURNED)
