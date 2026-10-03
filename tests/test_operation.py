import subprocess
import sys

import pytest

from filebrownie.storage.operation import OperationBusyError, operation_lock


def test_second_operation_is_busy_and_normal_release_recovers(tmp_path):
    with operation_lock(tmp_path):
        with pytest.raises(OperationBusyError, match="^OPERATION_BUSY$"):
            with operation_lock(tmp_path):
                pytest.fail("A second operation acquired the lock")
    with operation_lock(tmp_path):
        pass


def test_process_interruption_releases_lock(tmp_path):
    script = """
import sys
import time
from pathlib import Path
from filebrownie.storage.operation import operation_lock
with operation_lock(Path(sys.argv[1])):
    print('ready', flush=True)
    time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(OperationBusyError):
            with operation_lock(tmp_path):
                pytest.fail("A second process acquired the lock")
    finally:
        child.kill()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
    with operation_lock(tmp_path):
        pass
