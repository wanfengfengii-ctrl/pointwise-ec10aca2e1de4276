import json
import sqlite3

from storage import BusinessError, require_number


class UnknownReport(BusinessError):
    pass


def initialize_report_history(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS shift_reports (
        report_id INTEGER PRIMARY KEY AUTOINCREMENT, mount_id INTEGER NOT NULL REFERENCES mounts(mount_id),
        snapshot TEXT NOT NULL)''')


def save_report(db: sqlite3.Connection, mount_id: int, snapshot: dict) -> int:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    row = db.execute('INSERT INTO shift_reports(mount_id, snapshot) VALUES (?, ?)', (mount_id, payload))
    return row.lastrowid


def load_report(db: sqlite3.Connection, report_id: int) -> tuple[int, dict]:
    require_number(report_id, 'report ID')
    row = db.execute('SELECT mount_id, snapshot FROM shift_reports WHERE report_id = ?', (report_id,)).fetchone()
    if row is None:
        raise UnknownReport(f'unknown shift report {report_id}')
    return row['mount_id'], json.loads(row['snapshot'])
