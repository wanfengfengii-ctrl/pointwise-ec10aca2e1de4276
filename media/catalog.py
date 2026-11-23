import sqlite3
from collections.abc import Mapping

from inventory.manifest import ManifestError, VerifiedManifest, verify_manifest
from inventory.runs import load_verified_manifest, store_verified_manifest
from media.port import LocalVolumePort, PhysicalPresence, PortError
from media.session import Mount, MountError, get_mount, publish_mount, register_mount, reject_mount
from storage import require_key, transaction


class MountRejected(MountError):
    def __init__(self, mount_id: int, reason: str):
        super().__init__(f'mount {mount_id} rejected: {reason}')
        self.mount_id = mount_id


def mount_volume(db: sqlite3.Connection, port: LocalVolumePort, label: str,
                 source: str, manifest: Mapping) -> Mount:
    require_key(label, 'volume label')
    port_id = port.attach(source)
    try:
        with transaction(db):
            mount_id = register_mount(db, label, port_id)
    except sqlite3.Error as exc:
        try:
            port.eject(port_id)
        except PortError as detach_error:
            raise MountError('candidate registration failed and attachment release is unconfirmed') from detach_error
        raise MountError('candidate registration failed') from exc

    try:
        checked = verify_manifest(manifest, port.size(port_id),
                                  lambda start, end: port.read(port_id, start, end))
        if port.probe(port_id) is not PhysicalPresence.PRESENT:
            raise PortError('candidate no longer in place')
        with transaction(db):
            store_verified_manifest(db, mount_id, checked)
            publish_mount(db, mount_id)
            return get_mount(db, mount_id)
    except (ManifestError, PortError, sqlite3.Error) as exc:
        with transaction(db):
            reject_mount(db, mount_id, str(exc))
        # A rejected candidate has no window obligations. Release its drive attachment.
        try:
            port.eject(port_id)
        except PortError as release_error:
            raise MountRejected(mount_id, f'{exc}; candidate release unconfirmed') from release_error
        raise MountRejected(mount_id, str(exc)) from exc


def get_mount_manifest(db: sqlite3.Connection, mount_id: int) -> VerifiedManifest:
    get_mount(db, mount_id)
    return load_verified_manifest(db, mount_id)
