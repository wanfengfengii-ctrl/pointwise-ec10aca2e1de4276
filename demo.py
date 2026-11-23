import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from desk import ArchiveDesk, json_value
from media.port import LocalVolumePort, PhysicalPresence, ResponseLost
from orders.intake import OrderRequest


SAMPLES = Path(__file__).resolve().parent / 'examples'
PAGED_BODY = bytes(range(130))
PAGED_MANIFEST = {
    'length': len(PAGED_BODY),
    'runs': ['R'],
    'segments': [{'id': 's0', 'run': 'R', 'start': 0, 'end': len(PAGED_BODY),
                  'sha256': hashlib.sha256(PAGED_BODY).hexdigest()}],
}


class LosingPagePort(LocalVolumePort):
    """Drop one page-confirmation reply after the drive action."""

    def __init__(self, root, loss=None):
        super().__init__(root)
        self.loss = loss

    def ack_page(self, port_id, start, end):
        loss, self.loss = self.loss, None
        if loss == 'before':
            raise ResponseLost('link dropped before page confirmation')
        super().ack_page(port_id, start, end)
        if loss == 'after':
            raise ResponseLost('link dropped after page confirmation')


def _issue(desk, name):
    sample = json.loads((SAMPLES / name).read_text(encoding='utf-8'))
    (desk.port.supplier_dir / sample['file']).write_bytes(bytes(sample['bytes']))
    mount = desk.mount(sample['label'], sample['file'], sample['manifest'])
    segments = desk.segments(mount.mount_id, 'R')
    requests = [OrderRequest(order_id, mount.mount_id, start, end) for order_id, start, end in sample['orders']]
    receipts = [desk.admit(request) for request in requests]
    returned = desk.retire(sample['return'], mount.mount_id)
    if desk.port.probe(mount.port_id) is not PhysicalPresence.EJECTED:
        raise RuntimeError('supplier attachment has not been returned')
    return mount, segments, requests, receipts, returned, sample


def play_shift(root):
    with ArchiveDesk(root) as previous:
        mount, _, requests, _, _, _ = _issue(previous, 'previous.json')
        old_report = previous.report(mount.mount_id)
        old_request = requests[0]
        old_window = previous.fulfill(old_request.order_id)
    with ArchiveDesk(root) as desk:
        mount, segments, requests, receipts, returned, sample = _issue(desk, 'current.json')
        if desk.history(old_report.report_id) != old_report or desk.fulfill(old_request.order_id) != old_window:
            raise RuntimeError('previous shift evidence changed')
        if desk.admit(old_request) != old_window.receipt or list(old_window.body) != [3, 5]:
            raise RuntimeError('previous shift receipt or original bytes differ')
        windows = [desk.fulfill(request.order_id) for request in requests]
        report = desk.report(mount.mount_id)
        print(json.dumps({'mount': mount, 'segments': segments, 'receipts': receipts,
                          'return': returned, 'windows': windows, 'report': report,
                          'previous_report': old_report.report_id}, default=json_value, ensure_ascii=False, sort_keys=True))
        expected = [[[7, 11], [['head', 0, 2]]], [[11, 13], [['head', 1, 2], ['tail', 2, 3]]]]
        actual = [[list(item.body), [[piece.segment_id, piece.start, piece.end] for piece in item.provenance]]
                  for item in windows]
        downstream = {'coverage': report.snapshot['coverage']['archived'],
                      'return': report.snapshot['return']['state']}
        downstream_expected = {'coverage': [[0, 3]], 'return': 'returned'}
        if actual != expected or downstream != downstream_expected:
            raise RuntimeError('fixed sample windows or handoff coverage disagree with the original coordinates')
        if [item.receipt for item in windows] != receipts:
            raise RuntimeError('a receipt changed during the handoff')
        if any(piece.mount_id != mount.mount_id for item in windows for piece in item.provenance):
            raise RuntimeError('a window cites a different mount instance')
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'V8', 'input': {'source': sample['bytes'], 'orders': sample['orders']},
            'expected': expected, 'actual': actual,
            'downstream_expected': downstream_expected, 'downstream_actual': downstream,
            'code_paths': ['media/retire.py', 'orders/fulfill.py', 'reconcile/report.py', 'reconcile/coverage.py'],
            'oracle': 'Read offsets 0,1 and 1,2 from the four supplier bytes. Split at offset 2 and merge the expected coverage to [0,3).',
        }, ensure_ascii=False, sort_keys=True))


def play_paged(root):
    """Demonstrate paged return: lost confirmation, reopen by number, offline pickup."""
    port = LosingPagePort(root, 'after')
    (port.supplier_dir / 'paged.bin').write_bytes(PAGED_BODY)
    with ArchiveDesk(root, port) as desk:
        mount = desk.mount_paged('P', 'paged.bin', PAGED_MANIFEST)
        first_a = desk.admit(OrderRequest('A', mount.mount_id, 62, 66))
        first_b = desk.admit(OrderRequest('B', mount.mount_id, 64, 68))
        lost = None
        try:
            desk.retire('paged-return', mount.mount_id)
        except Exception as exc:  # noqa: BLE001 - demo prints the real pending outcome
            lost = type(exc).__name__

    # Rework the same return number on the same instance; the device reports the
    # actual cursor and the first page is already permanently unreadable.
    with ArchiveDesk(root, LocalVolumePort(root)) as desk:
        cursor = desk.port.current_page(mount.port_id)
        returned = desk.retire('paged-return', mount.mount_id)
        ejected = desk.port.probe(mount.port_id) is PhysicalPresence.EJECTED
        window_a = desk.fulfill('A')
        window_b = desk.fulfill('B')
        report = desk.report(mount.mount_id)
        actual = [[list(window_a.body), [[piece.segment_id, piece.start, piece.end]
                                         for piece in window_a.provenance]],
                  [list(window_b.body), [[piece.segment_id, piece.start, piece.end]
                                         for piece in window_b.provenance]]]
        downstream = {'coverage': report.snapshot['coverage']['archived'],
                      'return': report.snapshot['return']['state']}
        expected = [[[62, 63, 64, 65], [['s0', 62, 66]]],
                    [[64, 65, 66, 67], [['s0', 64, 68]]]]
        downstream_expected = {'coverage': [[62, 68]], 'return': 'returned'}
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'V10',
            'input': {'source': 'bytes(range(130))', 'orders': [['A', 62, 66], ['B', 64, 68]],
                      'lost': 'after_first_page'},
            'first_retire': lost, 'reopen_cursor': list(cursor) if cursor else None,
            'returned': returned.receipt_id, 'ejected': ejected,
            'expected': expected, 'actual': actual,
            'downstream_expected': downstream_expected, 'downstream_actual': downstream,
            'receipts_kept': [window_a.receipt == first_a, window_b.receipt == first_b],
            'code_paths': ['media/paged.py', 'media/retire.py', 'orders/fulfill.py', 'reconcile/report.py'],
            'oracle': 'The first page [0,64) is confirmed but its reply is lost. Reopening resumes at '
                      '[64,128) and finishes segment [0,130). A and B keep their original coordinates '
                      'and the archived coverage merges to [62,68).',
        }, ensure_ascii=False, sort_keys=True))
        if actual != expected or downstream != downstream_expected or not ejected:
            raise RuntimeError('paged sample windows, coverage, or return disagree')
        if not (window_a.receipt == first_a and window_b.receipt == first_b):
            raise RuntimeError('a first receipt changed during the paged return')


if __name__ == '__main__':
    with TemporaryDirectory(prefix='metrology-demo-') as workspace:
        play_shift(workspace)
        play_paged(Path(workspace) / 'paged')
