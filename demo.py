import json
from pathlib import Path
from tempfile import TemporaryDirectory

from desk import ArchiveDesk, json_value
from media.port import PhysicalPresence
from orders.intake import OrderRequest


SAMPLES = Path(__file__).resolve().parent / 'examples'


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


if __name__ == '__main__':
    with TemporaryDirectory(prefix='metrology-demo-') as workspace:
        play_shift(workspace)
