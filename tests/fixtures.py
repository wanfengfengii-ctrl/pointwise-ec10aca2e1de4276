from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from desk import ArchiveDesk
from media.port import LocalVolumePort, ResponseLost
from orders.intake import OrderRequest


class EventVolumePort(LocalVolumePort):
    """Suppress one eject response on a chosen side of the real port transition."""

    def __init__(self, root, loss=None):
        super().__init__(root)
        self.loss = loss
        self.calls = []

    def eject(self, port_id):
        self.calls.append(port_id)
        loss, self.loss = self.loss, None
        if loss == 'before':
            raise ResponseLost('operator link dropped before drive action')
        super().eject(port_id)
        if loss == 'after':
            raise ResponseLost('operator link dropped after drive action')


def manifest_for(body, spans=((0, 2), (2, 4)), runs=('R',)):
    return {'length': len(body), 'runs': list(runs),
            'segments': [{'id': f's{index}', 'run': runs[0], 'start': start, 'end': end,
                          'sha256': sha256(body[start:end]).hexdigest()}
                         for index, (start, end) in enumerate(spans)]}


class DeskCase(TestCase):
    def setUp(self):
        self.workspace = self.enterContext(TemporaryDirectory(prefix='metrology-test-'))
        self.root = Path(self.workspace)
        self.desk = self.open_desk()

    def open_desk(self, loss=None):
        return self.enterContext(ArchiveDesk(self.root, EventVolumePort(self.root, loss)))

    def remount_desk(self, loss=None):
        self.desk.close()
        self.desk = self.open_desk(loss)
        return self.desk

    def source(self, name, body):
        path = self.desk.port.supplier_dir / name
        path.write_bytes(body)
        return path

    def mount(self, name='original.bin', body=bytes([7, 11, 13, 17]), spans=((0, 2), (2, 4))):
        self.source(name, body)
        return self.desk.mount('P', name, manifest_for(body, spans))

    @staticmethod
    def order(mount, order_id='A', start=0, end=2):
        return OrderRequest(order_id, mount.mount_id, start, end)

    @staticmethod
    def origins(fulfillment):
        return [(piece.mount_id, piece.segment_id, piece.start, piece.end) for piece in fulfillment.provenance]
