from media.session import MountError
from reconcile.history import UnknownReport
from tests.fixtures import DeskCase


class ReportTests(DeskCase):
    def _returned_pair(self, overlap=True):
        mount = self.mount(body=b'abcdef', spans=((0, 2), (2, 4), (4, 6)))
        a = self.desk.admit(self.order(mount, 'A', 0, 2))
        start, end = (1, 3) if overlap else (4, 6)
        b = self.desk.admit(self.order(mount, 'B', start, end))
        returned = self.desk.retire('return-mount', mount.mount_id)
        return mount, (a, b), returned

    def test_return_and_coverage(self):
        mount, (a, b), returned = self._returned_pair()
        report = self.desk.report(mount.mount_id)
        self.assertEqual(report.snapshot['return']['state'], 'returned')
        self.assertEqual(report.snapshot['return']['receipt']['receipt_id'], returned.receipt_id)
        self.assertEqual([item['receipt']['receipt_id'] for item in report.snapshot['receipts']],
                         [a.receipt_id, b.receipt_id])
        self.assertEqual(report.snapshot['coverage']['requested'], [[0, 3]])
        self.assertEqual(report.snapshot['coverage']['archived'], [[0, 3]])
        self.assertEqual(report.snapshot['coverage']['missing'], [])
        self.remount_desk()
        self.assertEqual(self.desk.history(report.report_id), report)

    def test_gap_between_windows(self):
        mount, _, _ = self._returned_pair(overlap=False)
        report = self.desk.report(mount.mount_id)
        expected = [[0, 2], [4, 6]]
        self.assertEqual(report.snapshot['coverage']['requested'], expected)
        self.assertEqual(report.snapshot['coverage']['archived'], expected)
        self.assertEqual(report.snapshot['coverage']['missing'], [])
        self.remount_desk()
        self.assertEqual(self.desk.history(report.report_id).snapshot['coverage']['archived'], expected)

    def test_missing_window(self):
        mount, _, _ = self._returned_pair()
        self.desk.db.execute("DELETE FROM archive_windows WHERE order_id = 'B'")
        coverage = self.desk.report(mount.mount_id).snapshot['coverage']
        self.assertEqual(coverage['requested'], [[0, 3]])
        self.assertEqual(coverage['archived'], [[0, 2]])
        self.assertEqual(coverage['missing'], [[2, 3]])
        self.assertEqual(coverage['windows'][1], {'order_id': 'B', 'span': [1, 3], 'state': 'missing',
                                                  'detail': 'no local window index'})

    def test_duplicate_receipt(self):
        mount, _, _ = self._returned_pair()
        self.desk.db.execute('''INSERT INTO order_receipts(order_id, receipt_id, mount_id, start, end)
            SELECT order_id, receipt_id, mount_id, start, end FROM order_receipts WHERE order_id = 'B' ''')
        report = self.desk.report(mount.mount_id)
        self.assertEqual([(entry['order_id'], entry['state']) for entry in report.snapshot['receipts']],
                         [('A', 'valid'), ('B', 'duplicate')])
        self.assertEqual(report.snapshot['coverage']['archived'], [[0, 3]])

    def test_old_report(self):
        mount = self.mount()
        receipt = self.desk.admit(self.order(mount))
        before = self.desk.report(mount.mount_id)
        self.assertEqual(before.snapshot['return']['state'], 'unconfirmed')
        self.assertEqual(before.snapshot['coverage']['missing'], [[0, 2]])
        self.desk.retire('return-mount', mount.mount_id)
        after = self.desk.report(mount.mount_id)
        self.assertNotEqual(before.report_id, after.report_id)
        self.assertEqual(after.snapshot['coverage']['archived'], [[0, 2]])
        self.remount_desk()
        self.assertEqual(self.desk.history(before.report_id), before)
        self.assertEqual(self.desk.history(after.report_id), after)
        self.assertEqual(self.desk.fulfill('A').receipt, receipt)

    def test_missing_receipt_and_unknown_report(self):
        mount, _, _ = self._returned_pair()
        self.desk.db.execute("DELETE FROM order_receipts WHERE order_id = 'A'")
        report = self.desk.report(mount.mount_id)
        self.assertEqual(report.snapshot['receipts'][0]['state'], 'missing')
        self.assertEqual(report.snapshot['coverage']['archived'], [[0, 3]])
        with self.assertRaises(UnknownReport):
            self.desk.history(report.report_id + 100)
        with self.assertRaises(MountError):
            self.desk.report(mount.mount_id + 100)
