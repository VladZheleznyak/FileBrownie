"""Process-wide operation exclusion using a shared local-data lock file."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class OperationBusyError(Exception):
    pass


class OperationLockError(Exception):
    pass


@contextmanager
def operation_lock(data_directory: Path) -> Iterator[None]:
    try:
        descriptor = os.open(
            data_directory / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
    except OSError:
        raise OperationLockError("LOCAL_DATA_UNAVAILABLE") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperationBusyError("OPERATION_BUSY") from None
        except OSError:
            raise OperationLockError("LOCK_UNAVAILABLE") from None
        yield
    finally:
        # Closing releases the kernel lock even after interruption. Keep the file:
        # unlinking it would allow different processes to lock different inodes.
        os.close(descriptor)
