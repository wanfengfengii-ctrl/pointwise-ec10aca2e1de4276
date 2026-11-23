import json
from hashlib import sha256

from archive.index import list_archive_windows
from archive.write import PreservationError
from media.paged import PagedBadPage
from media.port import LocalVolumePort, PhysicalPresence, PortError
from media.retire import RetirementError, RetirementPending, get_retirement
from orders.admit import AdmissionRejected
from orders.intake import OrderRequest, get_order
from tests.fixtures import DeskCase, manifest_for
from tests.test_paged_device import PageReplyPort, install_sample
from desk import ArchiveDesk


class PagedCase(DeskCase):
    def open_paged(self, loss=None):
        self.desk.close()
        port = PageReplyPort(self.root, loss) if loss else LocalVolumePort(self.root)
        self.desk = self.open_desk_port(port)
        return self.desk

    def open_desk_port(self, port):
        return self.enterContext(ArchiveDesk(self.root, port))
    def paged_mount(self, loss=None, admit=(('A', 62, 66), ('B', 64, 68))):
        port = PageReplyPort(self.root, loss) if loss else LocalVolumePort(self.root)
        self.desk.close()
        self.desk = self.open_desk_port(port)
        source, manifest = install_sample(port)
        mount = self.desk.mount_paged('P', source, manifest)
        receipts = [self.desk.admit(OrderRequest(order_id, mount.mount_id, start, end))
                    for order_id, start, end in admit]
        return mount, receipts

    @staticmethod
    def origins(fulfillment):
        return [(piece.segment_id, piece.start, piece.end) for piece in fulfillment.provenance]


class PagedReturnTests(PagedCase):
    def test_lost_after_ack_resume_and_recover(self):
        mount, _ = self.paged_mount(loss='after')
        with self.assertRaises(RetirementPending):
            self.desk.retire('RET', mount.mount_id)
        # Only the first page was staged, and staging is not coverage yet.
        self.assertEqual(list_archive_windows(self.desk.db, mount.mount_id), ())
        self.assertEqual(self.desk.port.current_page(mount.port_id), (64, 128))

        self.desk = self.open_paged()
        self.assertEqual(self.desk.port.current_page(mount.port_id), (64, 128))
        with self.assertRaises(PortError):
            self.desk.port.read(mount.port_id, 0, 64)
        receipt = self.desk.retire('RET', mount.mount_id)
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.EJECTED)
        self.assertEqual(receipt.mount_id, mount.mount_id)

        a = self.desk.fulfill('A')
        b = self.desk.fulfill('B')
        self.assertEqual(list(a.body), [62, 63, 64, 65])
        self.assertEqual(list(b.body), [64, 65, 66, 67])
        self.assertEqual(self.origins(a), [('s0', 62, 66)])
        self.assertEqual(self.origins(b), [('s0', 64, 68)])
        report = self.desk.report(mount.mount_id)
        self.assertEqual(report.snapshot['coverage']['archived'], [[62, 68]])
        self.assertEqual(report.snapshot['return']['state'], 'returned')
        positions = sorted({offset for entry in list_archive_windows(self.desk.db, mount.mount_id)
                            for piece in entry.slices
                            for offset in range(piece.span.start, piece.span.end)})
        self.assertEqual(positions, [62, 63, 64, 65, 66, 67])
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'V9',
            'input': {'source': list(bytes(range(130))), 'orders': [['A', 62, 66], ['B', 64, 68]],
                      'lost': 'after_first_page'},
            'expected': [[62, 63, 64, 65], [64, 65, 66, 67]],
            'actual': [list(a.body), list(b.body)],
            'downstream_expected': {'coverage': [[62, 68]], 'return': 'returned'},
            'downstream_actual': {'coverage': report.snapshot['coverage']['archived'],
                                  'return': report.snapshot['return']['state']},
            'code_paths': ['media/paged.py', 'media/retire.py', 'orders/fulfill.py', 'reconcile/report.py'],
            'oracle': 'Page [0,64) is confirmed then the reply is lost. Reopening at [64,128) finishes the '
                      'only touched segment [0,130); A and B keep their original segment coordinates.',
        }, sort_keys=True))

    def test_lost_before_ack_keeps_page(self):
        mount, _ = self.paged_mount(loss='before')
        with self.assertRaises(RetirementPending):
            self.desk.retire('RET', mount.mount_id)
        self.assertEqual(self.desk.port.current_page(mount.port_id), (0, 64))
        self.assertEqual(list_archive_windows(self.desk.db, mount.mount_id), ())
        self.desk = self.open_paged()
        self.assertEqual(self.desk.port.current_page(mount.port_id), (0, 64))
        self.desk.retire('RET', mount.mount_id)
        self.assertEqual(list(self.desk.fulfill('A').body), [62, 63, 64, 65])

    def test_first_page_write_failure_blocks_confirmation(self):
        mount, _ = self.paged_mount()
        # Fail the durable staging of the first page (which holds no window bytes).
        self.desk.db.execute('''CREATE TRIGGER reject_contrib BEFORE INSERT ON paged_contributions
            WHEN NEW.segment_id = 's0' AND NEW.start < 64
            BEGIN SELECT RAISE(ABORT, 'staging device write failed'); END''')
        with self.assertRaises(PreservationError):
            self.desk.retire('RET', mount.mount_id)
        self.assertEqual(get_retirement(self.desk.db, 'RET').phase, 'write_failed')
        # The device page was never confirmed, so it is still the current page.
        self.assertEqual(self.desk.port.current_page(mount.port_id), (0, 64))
        self.assertEqual(list_archive_windows(self.desk.db, mount.mount_id), ())
        self.desk.db.execute('DROP TRIGGER reject_contrib')
        self.desk.retire('RET', mount.mount_id)
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.EJECTED)

    def test_corrupted_tail_blocks_publish_and_eject(self):
        mount, _ = self.paged_mount()
        path = self.desk.port.supplier_dir / 'paged.bin'
        data = bytearray(path.read_bytes())
        data[129] = 0  # byte 129 lies in the short tail page [128,130), outside both windows
        path.write_bytes(data)
        with self.assertRaises(PagedBadPage):
            self.desk.retire('RET', mount.mount_id)
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.PRESENT)
        self.assertEqual(list_archive_windows(self.desk.db, mount.mount_id), ())
        retirement = get_retirement(self.desk.db, 'RET')
        self.assertIsNone(retirement.receipt)
        self.assertIn('s0', retirement.problem)
        with self.assertRaises(PagedBadPage):
            self.desk.retire('RET', mount.mount_id)
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.PRESENT)

    def test_prepared_only_window_uses_empty_page_plan(self):
        port = LocalVolumePort(self.root)
        self.desk.close()
        self.desk = self.open_desk_port(port)
        source, manifest = install_sample(port)
        mount = self.desk.mount_paged('P', source, manifest)
        self.desk.prepare(OrderRequest('A', mount.mount_id, 62, 66))
        receipt = self.desk.retire('RET', mount.mount_id)
        self.assertEqual(receipt.mount_id, mount.mount_id)
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.EJECTED)
        self.assertEqual(get_order(self.desk.db, 'A').status, 'prepared')
        self.assertEqual(list_archive_windows(self.desk.db, mount.mount_id), ())

    def test_rebinding_retirement_number_is_rejected(self):
        mount, _ = self.paged_mount(admit=(('A', 62, 66),))
        self.desk.retire('RET', mount.mount_id)
        # A fresh paged instance under the same display label is a different instance.
        path = self.desk.port.supplier_dir / 'other.bin'
        path.write_bytes(bytes(range(130)))
        replacement = self.desk.mount_paged('P', 'other.bin', manifest_for(bytes(range(130)), ((0, 130),)))
        with self.assertRaises(RetirementError):
            self.desk.retire('RET', replacement.mount_id)

    def test_touched_segments_only(self):
        # Only s1 is touched; s0 must remain untouched and the mount still verifies fully.
        body = bytes(range(200))
        port = LocalVolumePort(self.root)
        self.desk.close()
        self.desk = self.open_desk_port(port)
        (port.supplier_dir / 'two.bin').write_bytes(body)
        mount = self.desk.mount_paged('P', 'two.bin', manifest_for(body, ((0, 130), (130, 200))))
        self.desk.admit(OrderRequest('A', mount.mount_id, 130, 140))
        self.desk.retire('RET', mount.mount_id)
        self.assertEqual(list(self.desk.fulfill('A').body), list(range(130, 140)))
        self.assertEqual(self.origins(self.desk.fulfill('A')), [('s1', 130, 140)])
        # Nothing from the untouched segment [0,130) is stored outside the volume.
        positions = sorted({offset for entry in list_archive_windows(self.desk.db, mount.mount_id)
                            for piece in entry.slices
                            for offset in range(piece.span.start, piece.span.end)})
        self.assertEqual(positions, list(range(130, 140)))

    def test_gapped_segments_keep_gaps(self):
        body = bytes(range(150))
        manifest = {
            'length': 150, 'runs': ['R'],
            'segments': [
                {'id': 's0', 'run': 'R', 'start': 0, 'end': 40,
                 'sha256': sha256(body[0:40]).hexdigest()},
                {'id': 's1', 'run': 'R', 'start': 100, 'end': 150,
                 'sha256': sha256(body[100:150]).hexdigest()},
            ],
        }
        port = LocalVolumePort(self.root)
        self.desk.close()
        self.desk = self.open_desk_port(port)
        (port.supplier_dir / 'gap.bin').write_bytes(body)
        mount = self.desk.mount_paged('P', 'gap.bin', manifest)
        self.desk.admit(OrderRequest('A', mount.mount_id, 10, 30))
        self.desk.admit(OrderRequest('B', mount.mount_id, 110, 140))
        self.desk.retire('RET', mount.mount_id)
        self.assertEqual(list(self.desk.fulfill('A').body), list(body[10:30]))
        self.assertEqual(list(self.desk.fulfill('B').body), list(body[110:140]))
        report = self.desk.report(mount.mount_id)
        self.assertEqual(report.snapshot['coverage']['archived'], [[10, 30], [110, 140]])

    def test_publish_failure_retries_without_rereading_pages(self):
        mount, _ = self.paged_mount()
        self.desk.db.execute('''CREATE TRIGGER reject_publish BEFORE INSERT ON archive_windows
            WHEN NEW.order_id = 'B' BEGIN SELECT RAISE(ABORT, 'publish device write failed'); END''')
        with self.assertRaises(PreservationError):
            self.desk.retire('RET', mount.mount_id)
        # All device pages were consumed before publish, but nothing is published.
        self.assertIsNone(self.desk.port.current_page(mount.port_id))
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.PRESENT)
        self.assertEqual(list_archive_windows(self.desk.db, mount.mount_id), ())
        self.desk.db.execute('DROP TRIGGER reject_publish')
        receipt = self.desk.retire('RET', mount.mount_id)
        self.assertEqual(receipt.mount_id, mount.mount_id)
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.EJECTED)
        self.assertEqual(list(self.desk.fulfill('B').body), [64, 65, 66, 67])

    def test_tampered_staging_latches_bad_and_blocks_eject(self):
        mount, _ = self.paged_mount()
        import media.paged as paged_module
        port = self.desk.port
        staged = paged_module._publish
        paged_module._publish = lambda *args, **kwargs: None
        try:
            # Stage all pages but suppress the atomic publish step.
            paged_module.stage_pages(self.desk.db, port, mount.mount_id)
        finally:
            paged_module._publish = staged
        self.desk.db.execute('DELETE FROM paged_contributions WHERE start = 64 AND end = 66')
        with self.assertRaises(PagedBadPage):
            paged_module._publish(self.desk.db, mount.mount_id,
                                  paged_module.touched_segments(self.desk.db, mount.mount_id))
        state = tuple(self.desk.db.execute('SELECT state FROM paged_mounts WHERE mount_id = ?',
                                           (mount.mount_id,)).fetchone())
        self.assertEqual(state, ('bad',))
        with self.assertRaises(PagedBadPage):
            self.desk.retire('RET', mount.mount_id)
        self.assertIs(port.probe(mount.port_id), PhysicalPresence.PRESENT)
        self.assertEqual(list_archive_windows(self.desk.db, mount.mount_id), ())

    def test_admission_closed_before_paging(self):
        mount, _ = self.paged_mount(admit=(('A', 62, 66),))
        prepared = self.desk.prepare(OrderRequest('C', mount.mount_id, 60, 62))
        operator = self.open_desk_port(LocalVolumePort(self.root))
        operator.retire('RET', mount.mount_id)
        with self.assertRaises(AdmissionRejected):
            self.desk.accept(prepared.request)
        self.assertEqual(get_order(self.desk.db, 'C').status, 'rejected')
