import json

from archive.index import list_archive_windows
from archive.write import PreservationError
from media.port import PhysicalPresence, PortError
from media.read import read_source_window
from media.retire import RetirementError, RetirementPending, get_retirement
from orders.admit import AdmissionRejected
from orders.intake import OrderConflict, get_order
from orders.receipt import load_receipt
from tests.fixtures import DeskCase


class RetireTests(DeskCase):
    def test_lost_reply_then_remount(self):
        desk = self.remount_desk('after')
        original = self.mount()
        a = self.order(original, 'A', 0, 2)
        b = self.order(original, 'B', 1, 3)
        first_a = desk.admit(a)
        first_b = desk.admit(b)
        self.assertEqual(list_archive_windows(desk.db, original.mount_id), ())
        with self.assertRaises(RetirementPending):
            desk.retire('R', original.mount_id)
        self.assertEqual(desk.port.calls, [original.port_id])
        self.assertIs(desk.port.probe(original.port_id), PhysicalPresence.EJECTED)
        self.assertIsNone(get_retirement(desk.db, 'R').receipt)
        desk = self.remount_desk()
        returned = desk.retire('R', original.mount_id)
        self.assertEqual(returned.mount_id, original.mount_id)
        self.assertEqual(desk.port.calls, [])
        replacement = self.mount('replacement.bin', bytes([19, 23, 29, 31]))
        self.assertEqual(replacement.label, original.label)
        self.assertNotEqual(replacement.mount_id, original.mount_id)
        self.assertIs(desk.port.probe(replacement.port_id), PhysicalPresence.PRESENT)
        with self.assertRaises(PortError):
            desk.port.read(original.port_id, 0, 1)
        with self.assertRaises(RetirementError):
            desk.retire('R', replacement.mount_id)
        window_a = desk.fulfill('A')
        self.assertEqual(desk.admit(b), first_b)
        window_b = desk.fulfill('B')
        expected = [[7, 11], [11, 13]]
        actual = [list(window_a.body), list(window_b.body)]
        self.assertEqual(actual, expected)
        self.assertEqual(self.origins(window_a), [(original.mount_id, 's0', 0, 2)])
        self.assertEqual(self.origins(window_b), [(original.mount_id, 's0', 1, 2), (original.mount_id, 's1', 2, 3)])
        self.assertEqual((window_a.receipt, window_b.receipt), (first_a, first_b))
        with self.assertRaises(OrderConflict):
            desk.admit(self.order(replacement, 'B', 1, 3))
        self.assertEqual(desk.retire('R', original.mount_id), returned)
        archived = list_archive_windows(desk.db, original.mount_id)
        self.assertEqual([entry.order_id for entry in archived], ['A', 'B'])
        positions = sorted({offset for entry in archived for piece in entry.slices
                            for offset in range(piece.span.start, piece.span.end)})
        self.assertEqual(positions, [0, 1, 2])
        self.assertEqual([list(piece.body) for entry in archived for piece in entry.slices], [[7, 11], [11], [13]])
        report = desk.report(original.mount_id)
        downstream_expected = {'return': 'returned', 'coverage': [[0, 3]], 'stored_positions': [0, 1, 2]}
        downstream_actual = {'return': report.snapshot['return']['state'],
                             'coverage': report.snapshot['coverage']['archived'], 'stored_positions': positions}
        self.assertEqual(downstream_actual, downstream_expected)
        self.assertEqual([row['receipt']['receipt_id'] for row in report.snapshot['receipts']],
                         [first_a.receipt_id, first_b.receipt_id])
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'V6', 'input': {'original': [7, 11, 13, 17], 'replacement': [19, 23, 29, 31],
                                       'windows': [[0, 2], [1, 3]], 'lost': 'after_eject'},
            'expected': expected, 'actual': actual, 'downstream_expected': downstream_expected,
            'downstream_actual': downstream_actual,
            'code_paths': ['orders/admit.py', 'media/retire.py', 'archive/write.py', 'orders/fulfill.py', 'reconcile/report.py'],
            'oracle': 'Index the original four input bytes. A uses offsets 0 and 1, B uses offsets 1 and 2. Offset 3 has no order.',
        }, sort_keys=True))

    def test_same_id_remount(self):
        original = self.mount()
        request = self.order(original, 'B', 1, 3)
        first = self.desk.admit(request)
        self.desk.retire('R', original.mount_id)
        self.remount_desk()
        replacement = self.mount('replacement.bin', bytes([19, 23, 29, 31]))
        self.assertEqual(self.desk.admit(request), first)
        window = self.desk.fulfill('B')
        self.assertEqual(window.body, bytes([11, 13]))
        self.assertEqual(self.origins(window), [(original.mount_id, 's0', 1, 2), (original.mount_id, 's1', 2, 3)])
        self.assertEqual(window.receipt, first)
        with self.assertRaises(OrderConflict):
            self.desk.admit(self.order(replacement, 'B', 1, 3))

    def test_lost_before_eject(self):
        desk = self.remount_desk('before')
        original = self.mount()
        request = self.order(original)
        first = desk.admit(request)
        with self.assertRaises(RetirementPending):
            desk.retire('R', original.mount_id)
        self.assertIs(desk.port.probe(original.port_id), PhysicalPresence.PRESENT)
        self.assertEqual(desk.port.read(original.port_id, 0, 4), bytes([7, 11, 13, 17]))
        self.assertIsNone(get_retirement(desk.db, 'R').receipt)
        desk = self.remount_desk()
        result = desk.retire('R', original.mount_id)
        self.assertEqual(desk.port.calls, [original.port_id])
        self.assertIs(desk.port.probe(original.port_id), PhysicalPresence.EJECTED)
        self.assertEqual(result, get_retirement(desk.db, 'R').receipt)
        self.assertEqual(desk.fulfill(request.order_id).receipt, first)

    def test_unknown_presence_keeps_original_request_pending(self):
        desk = self.remount_desk('before')
        original = self.mount()
        desk.admit(self.order(original))
        with self.assertRaises(RetirementPending):
            desk.retire('R', original.mount_id)
        supplier_file = desk.port.supplier_dir / 'original.bin'
        moved = self.root / 'temporarily-offline.bin'
        supplier_file.rename(moved)
        desk = self.remount_desk()
        self.assertIs(desk.port.probe(original.port_id), PhysicalPresence.UNKNOWN)
        with self.assertRaises(RetirementPending):
            desk.retire('R', original.mount_id)
        self.assertIsNone(get_retirement(desk.db, 'R').receipt)
        self.assertEqual(desk.port.calls, [])
        moved.rename(supplier_file)
        desk.retire('R', original.mount_id)
        self.assertIs(desk.port.probe(original.port_id), PhysicalPresence.EJECTED)

    def test_write_failure(self):
        original = self.mount()
        a = self.order(original, 'A', 0, 2)
        b = self.order(original, 'B', 1, 3)
        first = self.desk.admit(a)
        self.desk.admit(b)
        self.assertEqual(list_archive_windows(self.desk.db, original.mount_id), ())
        self.desk.db.execute('''CREATE TRIGGER reject_slice BEFORE INSERT ON archive_slices
            WHEN NEW.order_id = 'B' BEGIN SELECT RAISE(ABORT, 'archive device write failed'); END''')
        with self.assertRaisesRegex(PreservationError, 'archive device write failed'):
            self.desk.retire('R', original.mount_id)
        pending = get_retirement(self.desk.db, 'R')
        self.assertEqual(pending.phase, 'write_failed')
        self.assertIsNone(pending.receipt)
        self.assertEqual(list_archive_windows(self.desk.db, original.mount_id), ())
        self.assertEqual(self.desk.port.calls, [])
        self.assertIs(self.desk.port.probe(original.port_id), PhysicalPresence.PRESENT)
        plan = get_order(self.desk.db, 'A').plan
        self.assertEqual(read_source_window(self.desk.db, self.desk.port, plan), (bytes([7, 11]),))
        self.assertEqual(self.desk.fulfill('A').receipt, first)
        with self.assertRaises(AdmissionRejected):
            self.desk.admit(self.order(original, 'C', 2, 4))
        self.remount_desk()
        self.desk.db.execute('DROP TRIGGER reject_slice')
        returned = self.desk.retire('R', original.mount_id)
        self.assertEqual(returned, get_retirement(self.desk.db, 'R').receipt)
        self.assertIs(self.desk.port.probe(original.port_id), PhysicalPresence.EJECTED)
        self.assertEqual(self.desk.fulfill('B').body, bytes([11, 13]))

    def test_close_before_admission(self):
        original = self.mount()
        request = self.order(original)
        prepared = self.desk.prepare(request)
        self.assertEqual(prepared.status, 'prepared')
        self.assertIsNone(load_receipt(self.desk.db, request.order_id))
        operator = self.open_desk()
        operator.retire('R', original.mount_id)
        with self.assertRaisesRegex(AdmissionRejected, 'closed'):
            self.desk.accept(request)
        self.assertIsNone(load_receipt(self.desk.db, request.order_id))
        self.assertEqual(get_order(self.desk.db, request.order_id).status, 'rejected')
        self.assertEqual(list_archive_windows(self.desk.db, original.mount_id), ())
        self.assertIs(operator.port.probe(original.port_id), PhysicalPresence.EJECTED)

    def test_admission_wins(self):
        original = self.mount()
        request = self.order(original, start=1, end=3)
        self.desk.prepare(request)
        first = self.desk.accept(request)
        operator = self.open_desk()
        result = operator.retire('R', original.mount_id)
        self.assertEqual(result.mount_id, original.mount_id)
        self.assertIs(operator.port.probe(original.port_id), PhysicalPresence.EJECTED)
        window = self.desk.fulfill(request.order_id)
        self.assertEqual(window.body, bytes([11, 13]))
        self.assertEqual(self.origins(window), [(original.mount_id, 's0', 1, 2), (original.mount_id, 's1', 2, 3)])
        self.assertEqual(window.receipt, first)
