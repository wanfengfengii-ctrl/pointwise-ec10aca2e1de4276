import argparse
import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path

from archive.index import initialize_archive
from inventory.runs import initialize_run_index, lookup_segments
from media.catalog import get_mount_manifest, mount_paged_volume, mount_volume
from media.paged import initialize_paged
from media.port import LocalVolumePort
from media.retire import initialize_retirements, retire_volume
from media.session import initialize_mount_state
from orders.admit import admit_order, commit_admission
from orders.fulfill import fulfill_order
from orders.intake import OrderRequest, initialize_orders, prepare_order
from orders.receipt import initialize_receipts
from reconcile.history import initialize_report_history
from reconcile.report import build_shift_report, read_shift_report
from storage import BusinessError, connect, transaction


class ArchiveDesk:
    def __init__(self, root: str | Path, port: LocalVolumePort | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.port = port if port is not None else LocalVolumePort(self.root)
        self.db = connect(self.root / 'archive.sqlite3')
        try:
            with transaction(self.db):
                initialize_mount_state(self.db)
                initialize_run_index(self.db)
                initialize_orders(self.db)
                initialize_receipts(self.db)
                initialize_archive(self.db)
                initialize_retirements(self.db)
                initialize_paged(self.db)
                initialize_report_history(self.db)
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        self.db.close()

    def mount(self, label, source, manifest):
        return mount_volume(self.db, self.port, label, source, manifest)

    def mount_paged(self, label, source, manifest):
        return mount_paged_volume(self.db, self.port, label, source, manifest)

    def manifest(self, mount_id):
        return get_mount_manifest(self.db, mount_id)

    def segments(self, mount_id, run_id=None):
        return lookup_segments(self.db, mount_id, run_id)

    def prepare(self, request):
        return prepare_order(self.db, request)

    def accept(self, request):
        return commit_admission(self.db, request)

    def admit(self, request):
        return admit_order(self.db, request)

    def fulfill(self, order_id):
        return fulfill_order(self.db, self.port, order_id)

    def retire(self, request_id, mount_id):
        return retire_volume(self.db, self.port, request_id, mount_id)

    def report(self, mount_id):
        return build_shift_report(self.db, mount_id)

    def history(self, report_id):
        return read_shift_report(self.db, report_id)


def json_value(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, bytes):
        return list(value)
    raise TypeError(f'cannot serialize {type(value).__name__}')


def _parser():
    parser = argparse.ArgumentParser(description='Precision metrology archive desk')
    parser.add_argument('--root', type=Path, required=True, help='working directory')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    mount = commands.add_parser('mount')
    mount.add_argument('label')
    mount.add_argument('source')
    mount.add_argument('manifest', type=Path)
    paged = commands.add_parser('mount-paged')
    paged.add_argument('label')
    paged.add_argument('source')
    paged.add_argument('manifest', type=Path)
    segments = commands.add_parser('segments')
    segments.add_argument('mount_id', type=int)
    segments.add_argument('--run')
    admit = commands.add_parser('admit')
    admit.add_argument('order_id')
    admit.add_argument('mount_id', type=int)
    admit.add_argument('start', type=int)
    admit.add_argument('end', type=int)
    fulfill = commands.add_parser('fulfill')
    fulfill.add_argument('order_id')
    retire = commands.add_parser('retire')
    retire.add_argument('request_id')
    retire.add_argument('mount_id', type=int)
    report = commands.add_parser('report')
    report.add_argument('mount_id', type=int)
    history = commands.add_parser('history')
    history.add_argument('report_id', type=int)
    return parser


def _run(desk, args):
    if args.command == 'init':
        return {'archive': str(desk.root / 'archive.sqlite3'), 'supplier': str(desk.port.supplier_dir)}
    if args.command == 'mount':
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        return desk.mount(args.label, args.source, manifest)
    if args.command == 'mount-paged':
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        return desk.mount_paged(args.label, args.source, manifest)
    if args.command == 'segments':
        return desk.segments(args.mount_id, args.run)
    if args.command == 'admit':
        return desk.admit(OrderRequest(args.order_id, args.mount_id, args.start, args.end))
    if args.command == 'fulfill':
        return desk.fulfill(args.order_id)
    if args.command == 'retire':
        return desk.retire(args.request_id, args.mount_id)
    if args.command == 'report':
        return desk.report(args.mount_id)
    return desk.history(args.report_id)


def main():
    parser = _parser()
    args = parser.parse_args()
    try:
        with ArchiveDesk(args.root) as desk:
            result = _run(desk, args)
    except (BusinessError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, default=json_value, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
