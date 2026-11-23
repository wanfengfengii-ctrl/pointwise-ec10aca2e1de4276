import sqlite3

from inventory.verify import SegmentVerificationError, read_verified_slice
from inventory.windows import WindowPlan
from media.port import LocalVolumePort, PhysicalPresence, PortError
from media.session import MountError, get_mount
from storage import BusinessError


class SourceUnavailable(BusinessError):
    pass


def probe_source(db: sqlite3.Connection, port: LocalVolumePort, mount_id: int) -> PhysicalPresence:
    mount = get_mount(db, mount_id)
    return port.probe(mount.port_id)


def read_source_window(db: sqlite3.Connection, port: LocalVolumePort, plan: WindowPlan) -> tuple[bytes, ...]:
    try:
        mount = get_mount(db, plan.mount_id)
        if port.probe(mount.port_id) is not PhysicalPresence.PRESENT:
            raise SourceUnavailable(f'original mount {plan.mount_id} is not readable')

        def read(start: int, end: int) -> bytes:
            return port.read(mount.port_id, start, end)

        return tuple(read_verified_slice(read, piece.segment.span, piece.segment.digest, piece.span)
                     for piece in plan.slices)
    except (MountError, PortError, SegmentVerificationError) as exc:
        raise SourceUnavailable(f'original mount {plan.mount_id}: {exc}') from exc
