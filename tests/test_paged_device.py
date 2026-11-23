from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from media.port import LocalVolumePort, PhysicalPresence, PortError, ResponseLost
from tests.fixtures import manifest_for


class PageReplyPort(LocalVolumePort):
    """Drop a single page confirmation reply before or after the device action."""

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


def install_sample(port):
    body = bytes(range(130))
    name = 'paged.bin'
    (port.supplier_dir / name).write_bytes(body)
    return name, manifest_for(body, ((0, 130),))


class PageDeviceTests(TestCase):
    def test_lost_confirmations_and_reopen(self):
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            port = PageReplyPort(root, 'before')
            source, _ = install_sample(port)
            attachment = port.attach(source)
            self.assertEqual(port.read(attachment, 0, 130), bytes(range(130)))
            port.begin_pages(attachment, [(0, 130)])
            self.assertEqual(port.current_page(attachment), (0, 64))
            with self.assertRaises(ResponseLost):
                port.ack_page(attachment, 0, 64)

            port = PageReplyPort(root, 'after')
            self.assertEqual(port.current_page(attachment), (0, 64))
            self.assertEqual(port.read(attachment, 0, 64), bytes(range(64)))
            with self.assertRaises(ResponseLost):
                port.ack_page(attachment, 0, 64)

            port = LocalVolumePort(root)
            port.begin_pages(attachment, [(0, 130)])
            self.assertEqual(port.current_page(attachment), (64, 128))
            with self.assertRaises(PortError):
                port.read(attachment, 0, 64)
            with self.assertRaises(PortError):
                port.ack_page(attachment, 0, 64)
            with self.assertRaises(PortError):
                port.begin_pages(attachment, [(0, 128)])
            with self.assertRaises(PortError):
                port.eject(attachment)
            self.assertEqual(port.current_page(attachment), (64, 128))
            self.assertEqual(port.read(attachment, 64, 128), bytes(range(64, 128)))
            port.ack_page(attachment, 64, 128)
            self.assertEqual(port.current_page(attachment), (128, 130))
            self.assertEqual(port.read(attachment, 128, 130), bytes([128, 129]))
            port.ack_page(attachment, 128, 130)
            self.assertIsNone(port.current_page(attachment))
            port.eject(attachment)
            self.assertIs(LocalVolumePort(root).probe(attachment), PhysicalPresence.EJECTED)
            with self.assertRaises(PortError):
                port.read(attachment, 128, 130)

    def test_segment_relative_pages_and_independent_attachments(self):
        with TemporaryDirectory() as workspace:
            port = LocalVolumePort(Path(workspace))
            source, _ = install_sample(port)
            first = port.attach(source)
            (port.supplier_dir / 'other.bin').write_bytes(b'abc')
            other = port.attach('other.bin')
            port.begin_pages(first, [(3, 69), (80, 85)])
            for start, end in [(3, 67), (67, 69), (80, 85)]:
                self.assertEqual(port.current_page(first), (start, end))
                self.assertEqual(port.read(first, start, end), bytes(range(start, end)))
                port.ack_page(first, start, end)
            self.assertIsNone(port.current_page(first))
            self.assertEqual(port.read(other, 0, 3), b'abc')
            port.begin_pages(other, [])
            self.assertIsNone(port.current_page(other))
            port.eject(other)
            self.assertIs(port.probe(first), PhysicalPresence.PRESENT)
