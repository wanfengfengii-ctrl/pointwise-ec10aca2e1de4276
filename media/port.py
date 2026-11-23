import json
import os
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from storage import BusinessError, connect, require_key, require_number, transaction


class PhysicalPresence(str, Enum):
    PRESENT = 'present'
    EJECTED = 'ejected'
    UNKNOWN = 'unknown'


class PortError(BusinessError):
    pass


class ResponseLost(PortError):
    """A device action did not yield an observable response."""


PAGE_BYTES = 64


@dataclass(frozen=True)
class _PageProgress:
    ranges: list[list[int]]
    index: int
    start: int

    def current(self) -> tuple[int, int] | None:
        if self.index == len(self.ranges):
            return None
        return self.start, min(self.start + PAGE_BYTES, self.ranges[self.index][1])


class LocalVolumePort:
    """Simulated drive with persistent read rights and supplier-owned files."""

    def __init__(self, root: Path):
        root = Path(root)
        self.supplier_dir = root / 'supplier'
        self.supplier_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = root / 'port.sqlite3'
        with closing(connect(self.database_path)) as db, transaction(db):
            db.execute('''CREATE TABLE IF NOT EXISTS port_mounts (
                port_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, file_key TEXT NOT NULL,
                present INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)))''')
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS port_attached_file
                ON port_mounts(file_key) WHERE present = 1''')
            db.execute('''CREATE TABLE IF NOT EXISTS port_page_progress (
                port_id INTEGER PRIMARY KEY REFERENCES port_mounts(port_id),
                ranges TEXT NOT NULL, range_index INTEGER NOT NULL, page_start INTEGER NOT NULL)''')

    @staticmethod
    def _file_key(metadata: os.stat_result) -> str:
        return f'{metadata.st_dev}/{metadata.st_ino}'

    def _metadata(self, name: str) -> os.stat_result:
        try:
            metadata = (self.supplier_dir / name).lstat()
        except OSError as exc:
            raise PortError(f'supplier file {name!r} is unavailable') from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise PortError(f'supplier entry {name!r} must be a regular file')
        return metadata

    def attach(self, name: str) -> int:
        require_key(name, 'supplier file')
        if Path(name).name != name or name in ('.', '..'):
            raise PortError('supply a file name in the supplier directory')
        try:
            with closing(connect(self.database_path)) as db, transaction(db):
                metadata = self._metadata(name)
                row = db.execute('INSERT INTO port_mounts(name, file_key) VALUES (?, ?)',
                                 (name, self._file_key(metadata)))
                return row.lastrowid
        except sqlite3.IntegrityError as exc:
            raise PortError('the same supplier file is already attached') from exc

    @staticmethod
    def _record(db: sqlite3.Connection, port_id: int) -> sqlite3.Row | None:
        require_number(port_id, 'port attachment')
        return db.execute('SELECT * FROM port_mounts WHERE port_id = ?', (port_id,)).fetchone()

    def _presence(self, row: sqlite3.Row | None) -> PhysicalPresence:
        if row is None:
            return PhysicalPresence.UNKNOWN
        if not row['present']:
            return PhysicalPresence.EJECTED
        try:
            metadata = self._metadata(row['name'])
        except PortError:
            return PhysicalPresence.UNKNOWN
        if self._file_key(metadata) != row['file_key']:
            return PhysicalPresence.UNKNOWN
        return PhysicalPresence.PRESENT

    def probe(self, port_id: int) -> PhysicalPresence:
        with closing(connect(self.database_path)) as db:
            return self._presence(self._record(db, port_id))

    def _require_present(self, db: sqlite3.Connection, port_id: int) -> sqlite3.Row:
        row = self._record(db, port_id)
        if self._presence(row) is not PhysicalPresence.PRESENT:
            raise PortError(f'attachment {port_id} is not readable')
        return row

    def size(self, port_id: int) -> int:
        with closing(connect(self.database_path)) as db, transaction(db):
            row = self._require_present(db, port_id)
            metadata = self._metadata(row['name'])
            if self._file_key(metadata) != row['file_key']:
                raise PortError('source file changed during size check')
            return metadata.st_size

    @staticmethod
    def _page_progress(db: sqlite3.Connection, port_id: int) -> _PageProgress | None:
        row = db.execute('SELECT * FROM port_page_progress WHERE port_id = ?', (port_id,)).fetchone()
        if row is None:
            return None
        return _PageProgress(json.loads(row['ranges']), row['range_index'], row['page_start'])

    def begin_pages(self, port_id: int, spans: Iterable[tuple[int, int]]) -> None:
        """Enter page mode for the specified ordered ranges on this attachment."""
        ranges = []
        previous_end = 0
        for start, end in spans:
            if (type(start) is not int or type(end) is not int
                    or not previous_end <= start < end <= 2**63 - 1):
                raise ValueError('page ranges must be ordered, nonempty, and disjoint')
            ranges.append([start, end])
            previous_end = end
        with closing(connect(self.database_path)) as db, transaction(db):
            row = self._require_present(db, port_id)
            progress = self._page_progress(db, port_id)
            if progress is not None:
                if progress.ranges != ranges:
                    raise PortError('attachment already has a different page plan')
                return
            if previous_end > self._metadata(row['name']).st_size:
                raise PortError('page range exceeds attachment length')
            first = ranges[0][0] if ranges else 0
            db.execute('''INSERT INTO port_page_progress(port_id, ranges, range_index, page_start)
                VALUES (?, ?, 0, ?)''', (port_id, json.dumps(ranges), first))

    def current_page(self, port_id: int) -> tuple[int, int] | None:
        """Return the actual current page or None when the active plan is exhausted."""
        with closing(connect(self.database_path)) as db, transaction(db, write=False):
            self._require_present(db, port_id)
            progress = self._page_progress(db, port_id)
            if progress is None:
                raise PortError('attachment has not entered page mode')
            return progress.current()

    def ack_page(self, port_id: int, start: int, end: int) -> None:
        """Release the exact current page in a device transaction."""
        if type(start) is not int or type(end) is not int:
            raise ValueError('page coordinates must be integers')
        with closing(connect(self.database_path)) as db, transaction(db):
            self._require_present(db, port_id)
            progress = self._page_progress(db, port_id)
            if progress is None or progress.current() != (start, end):
                raise PortError('confirmation must name the current page')
            index = progress.index
            next_start = end
            if end == progress.ranges[index][1]:
                index += 1
                if index < len(progress.ranges):
                    next_start = progress.ranges[index][0]
            db.execute('''UPDATE port_page_progress SET range_index = ?, page_start = ?
                WHERE port_id = ?''', (index, next_start, port_id))

    def read(self, port_id: int, start: int, end: int) -> bytes:
        if type(start) is not int or type(end) is not int or not 0 <= start <= end:
            raise ValueError('invalid port byte range')
        with closing(connect(self.database_path)) as db, transaction(db):
            row = self._require_present(db, port_id)
            progress = self._page_progress(db, port_id)
            if progress is not None and progress.current() != (start, end):
                raise PortError('read must name the current page')
            try:
                with (self.supplier_dir / row['name']).open('rb') as source:
                    metadata = os.fstat(source.fileno())
                    if self._file_key(metadata) != row['file_key']:
                        raise PortError('source file changed during read')
                    if end > metadata.st_size:
                        raise PortError(f'short source range [{start},{end})')
                    source.seek(start)
                    return source.read(end - start)
            except OSError as exc:
                raise PortError(f'attachment {port_id} failed to read') from exc

    def eject(self, port_id: int) -> None:
        with closing(connect(self.database_path)) as db, transaction(db):
            presence = self._presence(self._record(db, port_id))
            if presence is PhysicalPresence.EJECTED:
                return
            if presence is not PhysicalPresence.PRESENT:
                raise PortError(f'attachment {port_id} has unknown presence')
            progress = self._page_progress(db, port_id)
            if progress is not None and progress.current() is not None:
                raise PortError('attachment still has unconfirmed pages')
            db.execute('UPDATE port_mounts SET present = 0 WHERE port_id = ?', (port_id,))
