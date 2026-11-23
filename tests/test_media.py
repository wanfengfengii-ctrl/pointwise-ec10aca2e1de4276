from inventory.runs import InventoryError
from inventory.spans import ByteSpan
from media.catalog import MountRejected
from media.port import PhysicalPresence, PortError
from media.session import MountState, get_mount
from tests.fixtures import DeskCase, manifest_for


class MediaTests(DeskCase):
    def test_mount_twice_same_label(self):
        first = self.mount()
        second = self.mount('second.bin', b'abcd', ((0, 1), (1, 4)))
        self.assertEqual(first.label, second.label)
        self.assertNotEqual(first.mount_id, second.mount_id)
        self.assertNotEqual(first.port_id, second.port_id)
        self.assertEqual([s.span for s in self.desk.segments(first.mount_id)], [ByteSpan(0, 2), ByteSpan(2, 4)])
        self.assertEqual([s.span for s in self.desk.segments(second.mount_id)], [ByteSpan(0, 1), ByteSpan(1, 4)])

    def test_same_run_id(self):
        first = self.mount(spans=((0, 2),))
        second = self.mount('second.bin', b'abcd', ((2, 4),))
        left, = self.desk.segments(first.mount_id, 'R')
        right, = self.desk.segments(second.mount_id, 'R')
        self.assertEqual((left.run_id, right.run_id), ('R', 'R'))
        self.assertEqual((left.span, right.span), (ByteSpan(0, 2), ByteSpan(2, 4)))
        self.assertNotEqual(left.digest, right.digest)

    def test_initialize_again(self):
        mount = self.mount()
        original = self.desk.manifest(mount.mount_id)
        self.desk.db.execute('UPDATE mounts SET label = ? WHERE mount_id = ?', ('updated display name', mount.mount_id))
        self.remount_desk()
        self.assertEqual(self.desk.manifest(mount.mount_id), original)
        self.assertEqual(get_mount(self.desk.db, mount.mount_id).label, 'updated display name')
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.PRESENT)

    def test_bad_manifest(self):
        good = self.mount()
        data = b'abcd'
        self.source('bad.bin', data)
        bad = manifest_for(data, ((0, 2), (2, 5)))
        with self.assertRaisesRegex(MountRejected, 'bounds') as caught:
            self.desk.mount('P', 'bad.bin', bad)
        mount = get_mount(self.desk.db, caught.exception.mount_id)
        self.assertIs(mount.state, MountState.REJECTED)
        with self.assertRaisesRegex(InventoryError, 'no published manifest'):
            self.desk.segments(mount.mount_id, 'R')
        self.assertEqual(len(self.desk.segments(good.mount_id, 'R')), 2)

    def test_same_length_bad_digest(self):
        data = bytes([7, 11, 13, 17])
        self.source('damaged.bin', bytes([7, 12, 13, 17]))
        with self.assertRaisesRegex(MountRejected, 'digest mismatch') as caught:
            self.desk.mount('P', 'damaged.bin', manifest_for(data))
        with self.assertRaisesRegex(InventoryError, 'no published manifest'):
            self.desk.segments(caught.exception.mount_id)

    def test_empty_known_run_and_unknown_identifiers(self):
        data = b'abcd'
        self.source('empty-run.bin', data)
        mount = self.desk.mount('P', 'empty-run.bin', manifest_for(data, runs=('R', 'EMPTY')))
        self.assertEqual(self.desk.segments(mount.mount_id, 'EMPTY'), ())
        with self.assertRaisesRegex(InventoryError, 'unknown run'):
            self.desk.segments(mount.mount_id, 'UNKNOWN')
        with self.assertRaisesRegex(InventoryError, 'unknown mount'):
            self.desk.segments(99)

    def test_same_file_cannot_be_double_attached(self):
        mount = self.mount()
        data = bytes([7, 11, 13, 17])
        with self.assertRaisesRegex(PortError, 'already attached'):
            self.desk.mount('different display name', 'original.bin', manifest_for(data))
        self.desk.retire('old', mount.mount_id)
        again = self.desk.mount('P', 'original.bin', manifest_for(data))
        self.assertNotEqual(mount.port_id, again.port_id)
        self.assertIs(self.desk.port.probe(mount.port_id), PhysicalPresence.EJECTED)
