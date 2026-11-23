import sqlite3
from dataclasses import dataclass

from archive.segments import ArchivedSlice
from inventory.spans import ByteSpan
from storage import require_key, require_number


@dataclass(frozen=True)
class ArchiveWindow:
    order_id: str
    mount_id: int
    span: ByteSpan
    body_digest: str
    slices: tuple[ArchivedSlice, ...]


def initialize_archive(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS archive_windows (
        order_id TEXT PRIMARY KEY REFERENCES window_orders(order_id),
        mount_id INTEGER NOT NULL REFERENCES mounts(mount_id),
        start INTEGER NOT NULL, end INTEGER NOT NULL, body_digest TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS archive_slices (
        order_id TEXT NOT NULL REFERENCES archive_windows(order_id) ON DELETE CASCADE,
        position INTEGER NOT NULL, mount_id INTEGER NOT NULL, segment_id TEXT NOT NULL,
        start INTEGER NOT NULL, end INTEGER NOT NULL, length INTEGER NOT NULL,
        digest TEXT NOT NULL, body BLOB NOT NULL, PRIMARY KEY (order_id, position))''')
    db.execute('CREATE INDEX IF NOT EXISTS archive_by_mount ON archive_windows(mount_id, order_id)')


def store_archive(db: sqlite3.Connection, window: ArchiveWindow) -> None:
    db.execute('DELETE FROM archive_windows WHERE order_id = ?', (window.order_id,))
    db.execute('''INSERT INTO archive_windows(order_id, mount_id, start, end, body_digest)
        VALUES (?, ?, ?, ?, ?)''', (window.order_id, window.mount_id, window.span.start,
                                   window.span.end, window.body_digest))
    db.executemany('''INSERT INTO archive_slices(order_id, position, mount_id, segment_id, start, end,
        length, digest, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        ((window.order_id, piece.position, piece.mount_id, piece.segment_id, piece.span.start,
          piece.span.end, piece.length, piece.digest, piece.body) for piece in window.slices))


def load_archive(db: sqlite3.Connection, order_id: str) -> ArchiveWindow | None:
    require_key(order_id, 'order ID')
    head = db.execute('SELECT * FROM archive_windows WHERE order_id = ?', (order_id,)).fetchone()
    if head is None:
        return None
    stored = db.execute('SELECT * FROM archive_slices WHERE order_id = ? ORDER BY position', (order_id,))
    slices = tuple(ArchivedSlice(row['position'], row['mount_id'], row['segment_id'],
                                 ByteSpan(row['start'], row['end']), row['length'], row['digest'], row['body'])
                   for row in stored)
    return ArchiveWindow(order_id, head['mount_id'], ByteSpan(head['start'], head['end']), head['body_digest'], slices)


def list_archive_windows(db: sqlite3.Connection, mount_id: int) -> tuple[ArchiveWindow, ...]:
    require_number(mount_id, 'mount instance')
    rows = db.execute('SELECT order_id FROM archive_windows WHERE mount_id = ? ORDER BY order_id', (mount_id,))
    return tuple(load_archive(db, row['order_id']) for row in rows)
