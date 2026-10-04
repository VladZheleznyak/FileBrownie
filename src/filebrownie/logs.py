"""Sanitized local operational log (D38, D39). Counts, timings, opaque IDs, and codes only.

Every event name and field name is allow-listed; values must be short code-like tokens or
numbers. Anything else is dropped, never logged. Failures to log never affect an operation.
Retention is bounded by size-based rotation plus an age limit; `erase derived|all` removes it.
This is not a place for filenames, paths, medical text, prompts, or raw diagnostics.
"""

import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

LOG_NAME = "filebrownie.log"
MAX_BYTES = 256 * 1024
BACKUPS = 3
MAX_AGE_SECONDS = 30 * 24 * 3600

EVENTS = frozenset({"scan_started", "scan_finished", "activation_finished", "operation_failed"})
FIELDS = frozenset(
    {
        "operation",
        "generation",
        "kind",
        "files",
        "unsupported",
        "units",
        "partial_units",
        "facts",
        "status",
        "reason",
        "activated",
        "error",
        "duration_ms",
    }
)
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def clean(fields: dict) -> tuple[dict, int]:
    kept, dropped = {}, 0
    for name, value in fields.items():
        if name not in FIELDS:
            dropped += 1
        elif isinstance(value, bool):
            kept[name] = int(value)
        elif isinstance(value, int | float):
            kept[name] = round(value, 3) if isinstance(value, float) else value
        elif isinstance(value, str) and _TOKEN.fullmatch(value):
            kept[name] = value
        else:
            dropped += 1
    return kept, dropped


def format_line(component: str, event: str, fields: dict, now: datetime | None = None) -> str:
    kept, dropped = clean(fields)
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [stamp, "level=INFO", f"component={component}", f"event={event}"]
    parts += [f"{name}={value}" for name, value in sorted(kept.items())]
    if dropped:
        parts.append(f"dropped_fields={dropped}")
    return " ".join(parts)


def _rotate(directory: Path) -> None:
    current = directory / LOG_NAME
    for index in range(BACKUPS, 0, -1):
        older = directory / f"{LOG_NAME}.{index}"
        source = current if index == 1 else directory / f"{LOG_NAME}.{index - 1}"
        if source.exists():
            os.replace(source, older)


def _expire(directory: Path, now: float) -> None:
    for index in range(1, BACKUPS + 1):
        path = directory / f"{LOG_NAME}.{index}"
        if path.exists() and now - path.stat().st_mtime > MAX_AGE_SECONDS:
            path.unlink()


def log_event(data: Path, component: str, event: str, **fields: object) -> bool:
    """Append one sanitized event; returns False when nothing was written."""
    if event not in EVENTS or not re.fullmatch(r"[a-z_]{1,32}", component):
        return False
    directory = data / "logs"
    try:
        if directory.is_symlink():
            return False
        directory.mkdir(mode=0o700, exist_ok=True)
        line = format_line(component, event, fields) + "\n"
        current = directory / LOG_NAME
        if current.is_symlink():
            return False
        if current.exists() and current.stat().st_size + len(line) > MAX_BYTES:
            _rotate(directory)
        _expire(directory, time.time())
        descriptor = os.open(current, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(line)
        return True
    except OSError:
        return False
