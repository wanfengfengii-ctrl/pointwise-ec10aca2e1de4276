"""Page-by-page return of a supplier volume.

A paged mount keeps the ordinary admission and receipt model.  At retirement
time the desk first closes admission, freezes the formal windows that already
hold a first receipt, and then walks only the original segments those windows
touch.  Each such segment is processed in full from its own start to its own
end in device-sized pages.

Every page is saved locally (window-covered bytes plus incremental verification
metadata) *before* the page confirmation is sent to the device, so a lost
acknowledgement is always recoverable from the device's actual cursor.  Raw
original bytes are never stored outside the bytes covered by a formal window;
the incremental SHA-256 state is metadata and lets a reopened desk finish the
mount-time digest check without re-reading consumed pages.
"""
import json
import sqlite3

from archive.hashstate import Sha256State
from archive.index import ArchiveWindow, store_archive
from archive.segments import assemble_window_bytes, build_archive_segments
from inventory.manifest import SourceSegment
from inventory.runs import lookup_segments
from inventory.spans import ByteSpan
from inventory.verify import digest_bytes
from media.port import LocalVolumePort
from media.session import get_mount
from orders.obligations import list_accepted_windows
from storage import BusinessError, require_number, transaction


class PagedError(BusinessError):
    pass


class PagedBadPage(PagedError):
    """A touched original segment no longer matches its mount-time digest."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class PagedWriteFailure(PagedError):
    """Local preservation of a page failed before the device confirmation."""


def initialize_paged(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS paged_mounts (
        mount_id INTEGER PRIMARY KEY REFERENCES mounts(mount_id),
        port_id INTEGER NOT NULL,
        plan TEXT,
        state TEXT NOT NULL DEFAULT 'staging'
            CHECK (state IN ('staging', 'bad', 'verified')),
        problem TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS paged_segments (
        mount_id INTEGER NOT NULL REFERENCES paged_mounts(mount_id),
        segment_id TEXT NOT NULL,
        start INTEGER NOT NULL, end INTEGER NOT NULL,
        next_start INTEGER NOT NULL,
        hash_state TEXT NOT NULL, hash_count INTEGER NOT NULL,
        complete INTEGER NOT NULL DEFAULT 0,
        verified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (mount_id, segment_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS paged_contributions (
        mount_id INTEGER NOT NULL REFERENCES paged_mounts(mount_id),
        order_id TEXT NOT NULL,
        segment_id TEXT NOT NULL,
        start INTEGER NOT NULL, end INTEGER NOT NULL,
        body BLOB NOT NULL,
        PRIMARY KEY (mount_id, order_id, segment_id, start, end))''')


def register_paged_mount(db: sqlite3.Connection, mount_id: int, port_id: int) -> None:
    require_number(mount_id, 'mount instance')
    require_number(port_id, 'port attachment')
    db.execute('INSERT INTO paged_mounts(mount_id, port_id) VALUES (?, ?)', (mount_id, port_id))


def is_paged(db: sqlite3.Connection, mount_id: int) -> bool:
    row = db.execute('SELECT 1 FROM paged_mounts WHERE mount_id = ?', (mount_id,)).fetchone()
    return row is not None


def _marker(db: sqlite3.Connection, mount_id: int) -> sqlite3.Row:
    row = db.execute('SELECT * FROM paged_mounts WHERE mount_id = ?', (mount_id,)).fetchone()
    if row is None:
        raise PagedError(f'mount {mount_id} is not a paged attachment')
    return row


def _set_marker(db: sqlite3.Connection, mount_id: int, state: str, problem: str | None) -> None:
    db.execute('UPDATE paged_mounts SET state = ?, problem = ? WHERE mount_id = ?',
               (state, problem, mount_id))


def formal_windows(db: sqlite3.Connection, mount_id: int) -> tuple:
    """Accepted windows that hold a first receipt (prepared-only orders excluded)."""
    return tuple(window for window in list_accepted_windows(db, mount_id) if window.receipts)


def touched_segments(db: sqlite3.Connection, mount_id: int) -> tuple[SourceSegment, ...]:
    """Original segments referenced by the formal first-receipt windows."""
    by_id = {segment.segment_id: segment for segment in lookup_segments(db, mount_id)}
    wanted = {}
    for window in formal_windows(db, mount_id):
        for piece in window.require_plan().slices:
            segment = by_id.get(piece.segment.segment_id, piece.segment)
            wanted[segment.segment_id] = segment
    return tuple(sorted(wanted.values(), key=lambda segment: segment.span))


def _plan_signature(segments: tuple[SourceSegment, ...]) -> str:
    return json.dumps([[segment.span.start, segment.span.end] for segment in segments])


def _sync_segments(db: sqlite3.Connection, mount_id: int,
                   segments: tuple[SourceSegment, ...]) -> None:
    initial = Sha256State().export()
    for segment in segments:
        row = db.execute('SELECT * FROM paged_segments WHERE mount_id = ? AND segment_id = ?',
                         (mount_id, segment.segment_id)).fetchone()
        if row is None:
            db.execute('''INSERT INTO paged_segments(mount_id, segment_id, start, end, next_start,
                hash_state, hash_count) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (mount_id, segment.segment_id, segment.span.start, segment.span.end,
                        segment.span.start, initial[0], initial[1]))
            continue
        if (row['start'], row['end']) != (segment.span.start, segment.span.end):
            raise PagedBadPage(f'segment {segment.segment_id} coordinates changed')


def _segment_for_page(segments: tuple[SourceSegment, ...], start: int, end: int) -> SourceSegment:
    for segment in segments:
        if segment.span.start <= start < end <= segment.span.end:
            return segment
    raise PagedBadPage(f'device page [{start},{end}) lies outside every frozen original segment')


def _page_contributions(db: sqlite3.Connection, mount_id: int, segment: SourceSegment,
                        start: int, end: int, body: bytes):
    for window in formal_windows(db, mount_id):
        plan = window.require_plan()
        order_id = window.request.order_id
        for piece in plan.slices:
            if piece.segment.segment_id != segment.segment_id:
                continue
            left = max(start, piece.span.start)
            right = min(end, piece.span.end)
            if left < right:
                chunk = body[left - start:right - start]
                yield order_id, segment.segment_id, left, right, chunk


def _stage_page(db: sqlite3.Connection, mount_id: int, segment: SourceSegment,
                start: int, end: int, body: bytes) -> bool:
    """Persist one page's window contributions and verification state.

    Returns True when the segment has now been read in full but no longer
    matches its mount-time digest (the ``bad`` marker is set in this same
    transaction, so the caller commits it before deciding what to do).  Returns
    False when the page is safely staged and may be confirmed on the device.
    A page that fails its digest stores none of its own bytes.
    """
    row = db.execute('SELECT * FROM paged_segments WHERE mount_id = ? AND segment_id = ?',
                     (mount_id, segment.segment_id)).fetchone()
    if row['next_start'] != start:
        raise PagedBadPage(f'page [{start},{end}) does not continue segment '
                           f'{segment.segment_id} at byte {row["next_start"]}')
    state = Sha256State.load(row['hash_state'], row['hash_count'])
    state.update(body)
    final = end == segment.span.end
    if final and state.hexdigest() != segment.digest:
        _set_marker(db, mount_id, 'bad',
                    f'original segment {segment.segment_id} no longer matches its mount digest')
        return True
    for order_id, segment_id, left, right, chunk in _page_contributions(
            db, mount_id, segment, start, end, body):
        db.execute('''INSERT OR REPLACE INTO paged_contributions
            (mount_id, order_id, segment_id, start, end, body) VALUES (?, ?, ?, ?, ?, ?)''',
                   (mount_id, order_id, segment_id, left, right, chunk))
    state_hex, count, buffer = state.export()
    if buffer and not final:
        raise PagedBadPage(f'page [{start},{end}) did not end on a digest block boundary')
    if final:
        # Never persist a short trailing tail: it could hold original bytes
        # outside every formal window.  A finished segment keeps only its state
        # words as verification metadata; ``verified`` records the outcome.
        buffer = ''
    db.execute('''UPDATE paged_segments SET next_start = ?, hash_state = ?, hash_count = ?,
        complete = ?, verified = ? WHERE mount_id = ? AND segment_id = ?''',
               (end, state_hex, count, 1 if final else 0, 1 if final else 0,
                mount_id, segment.segment_id))
    return False


def _already_staged(db: sqlite3.Connection, mount_id: int, segment: SourceSegment,
                    start: int) -> bool:
    row = db.execute('SELECT next_start FROM paged_segments WHERE mount_id = ? AND segment_id = ?',
                     (mount_id, segment.segment_id)).fetchone()
    return start < row['next_start']


def _latch_bad(db: sqlite3.Connection, mount_id: int, detail: str) -> 'PagedBadPage':
    with transaction(db):
        _set_marker(db, mount_id, 'bad', detail)
    return PagedBadPage(detail)


def stage_pages(db: sqlite3.Connection, port: LocalVolumePort, mount_id: int) -> None:
    marker = _marker(db, mount_id)
    if marker['state'] == 'bad':
        raise PagedBadPage(marker['problem'] or 'a prior page failed verification')
    if marker['state'] == 'verified':
        return
    segments = touched_segments(db, mount_id)
    signature = _plan_signature(segments)
    if marker['plan'] is not None and marker['plan'] != signature:
        raise _latch_bad(db, mount_id, 'the frozen page plan changed between attempts')
    try:
        with transaction(db):
            db.execute('UPDATE paged_mounts SET plan = ? WHERE mount_id = ?', (signature, mount_id))
            _sync_segments(db, mount_id, segments)
    except PagedBadPage as exc:
        raise _latch_bad(db, mount_id, exc.detail) from exc

    mount = get_mount(db, mount_id)
    port.begin_pages(mount.port_id, [(segment.span.start, segment.span.end) for segment in segments])

    while True:
        page = port.current_page(mount.port_id)
        if page is None:
            break
        start, end = page
        segment = _segment_for_page(segments, start, end)
        if _already_staged(db, mount_id, segment, start):
            # The device is one (or more) pages behind our durable staging, as
            # happens when a confirmation reply was lost before the drive move.
            port.ack_page(mount.port_id, start, end)
            continue
        body = port.read(mount.port_id, start, end)
        if not isinstance(body, bytes) or len(body) != end - start:
            raise PagedBadPage(f'page [{start},{end}) could not be read in full')
        try:
            with transaction(db):
                bad = _stage_page(db, mount_id, segment, start, end, body)
        except sqlite3.Error as exc:
            raise PagedWriteFailure(f'page [{start},{end}) could not be preserved: {exc}') from exc
        if bad:
            # The bad marker was committed above; stop before confirming this
            # page so nothing beyond it is consumed, published, or ejected.
            raise PagedBadPage(f'original segment {segment.segment_id} no longer matches its mount digest')
        port.ack_page(mount.port_id, start, end)

    _publish(db, mount_id, segments)


def _slice_bytes(db: sqlite3.Connection, mount_id: int, order_id: str,
                 segment_id: str, span: ByteSpan) -> bytes:
    rows = db.execute('''SELECT start, end, body FROM paged_contributions
        WHERE mount_id = ? AND order_id = ? AND segment_id = ? AND start >= ? AND end <= ?
        ORDER BY start''', (mount_id, order_id, segment_id, span.start, span.end)).fetchall()
    cursor = span.start
    chunks = []
    for row in rows:
        if row['start'] != cursor:
            raise PagedBadPage(f'window {order_id!r} has a staging gap at byte {cursor}')
        if row['end'] > span.end:
            raise PagedBadPage(f'window {order_id!r} staging exceeds its original slice')
        chunks.append(row['body'])
        cursor = row['end']
    if cursor != span.end:
        raise PagedBadPage(f'window {order_id!r} staging stops at byte {cursor}')
    return b''.join(chunks)


def _check_complete(db: sqlite3.Connection, mount_id: int,
                    segments: tuple[SourceSegment, ...]) -> None:
    """Validate the staged segments and durably latch a ``bad`` verdict.

    Runs in its own write transaction so a bad marker survives the raised
    exception.  A verified, fully staged plan returns normally.
    """
    problem = None
    with transaction(db):
        for segment in segments:
            row = db.execute('SELECT * FROM paged_segments WHERE mount_id = ? AND segment_id = ?',
                             (mount_id, segment.segment_id)).fetchone()
            if row is None or row['next_start'] != segment.span.end or not row['complete']:
                problem = f'segment {segment.segment_id} was not read to its original end'
            elif not row['verified']:
                problem = f'segment {segment.segment_id} failed its mount digest'
            if problem is not None:
                _set_marker(db, mount_id, 'bad', problem)
                break
    if problem is not None:
        raise PagedBadPage(problem)


def _publish(db: sqlite3.Connection, mount_id: int, segments: tuple[SourceSegment, ...]) -> None:
    _check_complete(db, mount_id, segments)
    try:
        with transaction(db):
            for window in formal_windows(db, mount_id):
                window.require_receipt()
                plan = window.require_plan()
                pieces = tuple(_slice_bytes(db, mount_id, window.request.order_id,
                                            piece.segment.segment_id, piece.span)
                               for piece in plan.slices)
                slices = build_archive_segments(plan, pieces)
                body = assemble_window_bytes(plan, slices)
                store_archive(db, ArchiveWindow(window.request.order_id, mount_id, plan.span,
                                                digest_bytes(body), slices))
            _set_marker(db, mount_id, 'verified', None)
    except sqlite3.Error as exc:
        raise PagedWriteFailure(f'paged windows could not be published: {exc}') from exc
    except PagedBadPage as exc:
        # Staging is internally inconsistent (e.g. tampered rows).  Latch the
        # bad verdict durably so no later attempt can publish or eject.
        raise _latch_bad(db, mount_id, exc.detail) from exc
