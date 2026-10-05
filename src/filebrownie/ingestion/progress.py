"""Structured scan progress. Presentation turns these into terminal lines."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanProgress:
    stage: str
    index: int = 0
    count: int = 0
    supported: int = 0
    unsupported: int = 0
    skipped: int = 0
    failed: int = 0
    unique: int = 0
    unit: int = 0
    pages: int | None = None
    source: str = ""
    copies: int = 0
    format: str = ""
    cached: bool = False
    step: str = ""
    status: str = ""
    warnings: tuple[str, ...] = ()
    elapsed_s: int = 0
    formats: tuple[str, ...] = ()
    eta_s: int | None = None
    unknown_pdfs: int = 0
    eta_basis: str = ""


ProgressFn = Callable[[ScanProgress], None]
