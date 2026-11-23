import sqlite3

from inventory.manifest import SourceSegment, VerifiedManifest
from inventory.spans import ByteSpan
from storage import BusinessError, require_key, require_number


class InventoryError(BusinessError):
    pass


def initialize_run_index(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS volume_index (
        mount_id INTEGER PRIMARY KEY REFERENCES mounts(mount_id), length INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS run_index (
        mount_id INTEGER NOT NULL REFERENCES volume_index(mount_id), run_id TEXT NOT NULL,
        PRIMARY KEY (mount_id, run_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS source_segments (
        mount_id INTEGER NOT NULL, segment_id TEXT NOT NULL, run_id TEXT NOT NULL,
        start INTEGER NOT NULL, end INTEGER NOT NULL, digest TEXT NOT NULL,
        PRIMARY KEY (mount_id, segment_id),
        FOREIGN KEY (mount_id, run_id) REFERENCES run_index(mount_id, run_id))''')
    db.execute('''CREATE INDEX IF NOT EXISTS source_segment_order
        ON source_segments(mount_id, start, end)''')


def store_verified_manifest(db: sqlite3.Connection, mount_id: int, manifest: VerifiedManifest) -> None:
    db.execute('INSERT INTO volume_index(mount_id, length) VALUES (?, ?)', (mount_id, manifest.length))
    db.executemany('INSERT INTO run_index(mount_id, run_id) VALUES (?, ?)',
                   ((mount_id, run) for run in manifest.runs))
    db.executemany('''INSERT INTO source_segments(mount_id, segment_id, run_id, start, end, digest)
        VALUES (?, ?, ?, ?, ?, ?)''',
        ((mount_id, segment.segment_id, segment.run_id, segment.span.start, segment.span.end, segment.digest)
         for segment in manifest.segments))


def get_volume_length(db: sqlite3.Connection, mount_id: int) -> int:
    require_number(mount_id, 'mount instance')
    row = db.execute('SELECT length FROM volume_index WHERE mount_id = ?', (mount_id,)).fetchone()
    if row is not None:
        return row['length']
    known = db.execute('SELECT 1 FROM mounts WHERE mount_id = ?', (mount_id,)).fetchone()
    if known is None:
        raise InventoryError(f'unknown mount instance {mount_id}')
    raise InventoryError(f'mount {mount_id} has no published manifest')


def lookup_segments(db: sqlite3.Connection, mount_id: int, run_id: str | None = None) -> tuple[SourceSegment, ...]:
    get_volume_length(db, mount_id)
    query = 'SELECT segment_id, run_id, start, end, digest FROM source_segments WHERE mount_id = ?'
    args = (mount_id,)
    if run_id is not None:
        require_key(run_id, 'run ID')
        run = db.execute('SELECT 1 FROM run_index WHERE mount_id = ? AND run_id = ?', (mount_id, run_id))
        if run.fetchone() is None:
            raise InventoryError(f'unknown run {run_id!r} for mount {mount_id}')
        query += ' AND run_id = ?'
        args = (mount_id, run_id)
    rows = db.execute(query + ' ORDER BY start, end, segment_id', args)
    return tuple(SourceSegment(row['segment_id'], row['run_id'], ByteSpan(row['start'], row['end']), row['digest'])
                 for row in rows)


def load_verified_manifest(db: sqlite3.Connection, mount_id: int) -> VerifiedManifest:
    length = get_volume_length(db, mount_id)
    runs = db.execute('SELECT run_id FROM run_index WHERE mount_id = ? ORDER BY run_id', (mount_id,))
    return VerifiedManifest(length, tuple(row['run_id'] for row in runs), lookup_segments(db, mount_id))
