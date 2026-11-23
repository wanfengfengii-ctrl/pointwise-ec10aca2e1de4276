import sqlite3
from dataclasses import dataclass

from media.retire import RetireReceipt, retirement_for_mount
from media.session import MountState, get_mount


@dataclass(frozen=True)
class ReturnFinding:
    state: str
    receipt: RetireReceipt | None
    detail: str | None


def check_return(db: sqlite3.Connection, mount_id: int) -> ReturnFinding:
    mount = get_mount(db, mount_id)
    retirement = retirement_for_mount(db, mount_id)
    if retirement is None:
        return ReturnFinding('unconfirmed', None, 'no retirement record for this mount')
    if mount.state is MountState.RETURNED and retirement.receipt is not None:
        return ReturnFinding('returned', retirement.receipt, None)
    if mount.state is MountState.RETURNED or retirement.receipt is not None:
        return ReturnFinding('unconfirmed', None, 'retirement evidence disagrees with mount state')
    detail = retirement.problem or f'retirement phase {retirement.phase} is unconfirmed'
    return ReturnFinding('unconfirmed', None, detail)
