import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class BusinessError(RuntimeError):
    """A business operation could not deliver its requested result."""


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection, *, write: bool = True) -> Iterator[None]:
    connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def require_key(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{field} must be a nonempty identifier')
    return value


def require_number(value: int, field: str) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError(f'{field} must be a positive SQLite integer')
    return value
