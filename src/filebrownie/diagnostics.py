"""`doctor`: runtime self-checks of the isolation and readiness guarantees.

Output is fixed labels and codes only, never paths, names, or content. Checks never write to
source documents or generated data.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from filebrownie.ingestion.discovery import InventoryError

from filebrownie import provisioning
from filebrownie.evidence.network import (
    NetworkIsolationError,
    ensure_isolated_network,
    ensure_no_outbound,
)
from filebrownie.interpretation.llama_vision import LlamaVisionClient


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    code: str = "-"


def mounted_read_only(path: Path) -> bool:
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _guard(name: str, action: Callable[[], None]) -> Check:
    try:
        action()
    except (NetworkIsolationError, provisioning.SetupError, InventoryError) as error:
        return Check(name, False, str(error).split(":")[0])
    except OSError:
        return Check(name, False, "UNAVAILABLE")
    return Check(name, True)


def _host_paths_distinct() -> None:
    host_source = os.environ.get("FILEBROWNIE_HOST_SOURCE_DIR")
    host_data = os.environ.get("FILEBROWNIE_HOST_DATA_DIR")
    if not host_source or not host_data:
        return
    left, right = Path(host_source).resolve(), Path(host_data).resolve()
    if left == right or left.is_relative_to(right) or right.is_relative_to(left):
        raise InventoryError("SOURCE_DATA_OVERLAP")


def run_checks(source: Path, models: Path, model_url: str, database: Callable[[], None]) -> list:
    checks = [
        _guard("host source and data folders are separate", _host_paths_distinct),
        _guard("no route to the internet", ensure_isolated_network),
        _guard("internet unreachable", ensure_no_outbound),
        Check(
            "source folder mounted read-only",
            mounted_read_only(source),
            "-" if mounted_read_only(source) else "SOURCES_WRITABLE",
        ),
        Check(
            "model storage mounted read-only",
            mounted_read_only(models),
            "-" if mounted_read_only(models) else "MODELS_WRITABLE",
        ),
        _guard("database reachable, schema current", database),
        _guard("OCR language data provisioned", lambda: provisioning.require(models, "tesseract")),
        _guard("vision weights provisioned", lambda: provisioning.require(models, "vision")),
        _guard("inference service ready", lambda: LlamaVisionClient(model_url, models)),
    ]
    return checks
