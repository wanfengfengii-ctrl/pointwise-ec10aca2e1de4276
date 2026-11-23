from inventory.windows import WindowError
from orders.admit import AdmissionRejected
from orders.fulfill import FulfillmentError
from orders.intake import OrderConflict, get_order
from orders.receipt import load_receipt
from tests.fixtures import DeskCase


class OrderTests(DeskCase):
    def test_read_cross_segment(self):
        mount = self.mount()
        request = self.order(mount, start=1, end=4)
        first = self.desk.admit(request)
        window = self.desk.fulfill(request.order_id)
        self.assertEqual(window.body, bytes([11, 13, 17]))
        self.assertEqual(self.origins(window), [(mount.mount_id, 's0', 1, 2), (mount.mount_id, 's1', 2, 4)])
        self.assertEqual(window.receipt, first)

    def test_equal_bytes(self):
        mount = self.mount(body=bytes([7, 11, 11, 17]))
        request = self.order(mount, start=1, end=4)
        receipt = self.desk.admit(request)
        self.desk.retire('return', mount.mount_id)
        window = self.desk.fulfill(request.order_id)
        self.assertEqual(window.body, bytes([11, 11, 17]))
        self.assertEqual(self.origins(window), [(mount.mount_id, 's0', 1, 2), (mount.mount_id, 's1', 2, 4)])
        self.assertEqual(window.receipt, receipt)

    def test_damaged_archive(self):
        mount = self.mount()
        request = self.order(mount, start=1, end=4)
        self.desk.admit(request)
        self.desk.retire('return', mount.mount_id)
        self.desk.db.execute('UPDATE archive_slices SET body = ? WHERE order_id = ? AND position = 1',
                             (bytes([13, 18]), request.order_id))
        with self.assertRaisesRegex(FulfillmentError, 'damaged archive.*digest mismatch'):
            self.desk.fulfill(request.order_id)

    def test_reject_overrun(self):
        mount = self.mount()
        request = self.order(mount, start=2, end=5)
        with self.assertRaisesRegex(WindowError, 'bounds'):
            self.desk.admit(request)
        self.assertIsNone(get_order(self.desk.db, request.order_id))
        self.assertIsNone(load_receipt(self.desk.db, request.order_id))
        with self.assertRaisesRegex(FulfillmentError, 'no formal admission'):
            self.desk.fulfill(request.order_id)

    def test_exact_end(self):
        mount = self.mount()
        request = self.order(mount, start=2, end=4)
        receipt = self.desk.admit(request)
        self.assertEqual(receipt.end, 4)
        self.assertEqual(self.desk.fulfill(request.order_id).body, bytes([13, 17]))

    def test_window_with_gap_or_empty_range_is_rejected(self):
        mount = self.mount(spans=((0, 1), (2, 4)))
        with self.assertRaisesRegex(WindowError, 'gap'):
            self.desk.admit(self.order(mount, start=0, end=4))
        with self.assertRaisesRegex(WindowError, 'nonempty'):
            self.desk.admit(self.order(mount, start=2, end=2))
        self.assertIsNone(load_receipt(self.desk.db, 'A'))

    def test_commit_failure(self):
        mount = self.mount()
        request = self.order(mount)
        self.desk.prepare(request)
        self.desk.db.executescript('''
            CREATE TABLE commit_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE commit_guard (id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED);
            CREATE TRIGGER reject_receipt_commit AFTER INSERT ON order_receipts
                WHEN NEW.order_id = 'A'
                BEGIN INSERT INTO commit_guard VALUES (1); END;
        ''')
        with self.assertRaisesRegex(AdmissionRejected, 'could not commit'):
            self.desk.accept(request)
        self.remount_desk()
        self.assertEqual(get_order(self.desk.db, request.order_id).status, 'prepared')
        self.assertIsNone(load_receipt(self.desk.db, request.order_id))
        with self.assertRaisesRegex(FulfillmentError, 'no formal admission'):
            self.desk.fulfill(request.order_id)
        self.desk.db.execute('DROP TRIGGER reject_receipt_commit')
        receipt = self.desk.accept(request)
        self.assertEqual(self.desk.fulfill(request.order_id).receipt, receipt)

    def test_same_id_replay(self):
        mount = self.mount()
        request = self.order(mount)
        receipt = self.desk.admit(request)
        self.assertEqual(self.desk.admit(request), receipt)
        with self.assertRaises(OrderConflict):
            self.desk.admit(self.order(mount, start=1, end=2))
        self.assertEqual(self.desk.fulfill(request.order_id).receipt, receipt)
        self.assertEqual(self.desk.fulfill(request.order_id).body, bytes([7, 11]))

    def test_old_order(self):
        mount = self.mount()
        request = self.order(mount, 'old', 1, 3)
        receipt = self.desk.admit(request)
        self.desk.retire('old-return', mount.mount_id)
        old = self.desk.fulfill(request.order_id)
        self.remount_desk()
        self.assertEqual(self.desk.admit(request), receipt)
        replayed = self.desk.fulfill(request.order_id)
        self.assertEqual(replayed, old)
        self.assertEqual(replayed.body, bytes([11, 13]))
        with self.assertRaises(OrderConflict):
            self.desk.admit(self.order(mount, 'old', 0, 3))
