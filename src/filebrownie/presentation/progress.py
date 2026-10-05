"""English progress lines for a running scan. Product output, never logged."""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from filebrownie.ingestion.progress import ProgressFn, ScanProgress
from filebrownie.presentation.render import safe

_STEPS = {
    "ocr": "OCR",
    "vision": "vision",
    "text": "text layer",
    "ocr-unavailable": "OCR unavailable",
    "vision-unavailable": "vision unavailable",
    "unavailable": "not processed",
}
# Initial per-page guesses, used only until this scan has timed that file type.
# Full scans start from the middle of the 20–60s model budget; reader scans do not run the model.
_FULL_SCAN_PAGE_S = 30.0
_READER_PDF_S = 5.0
_READER_JPEG_S = 1.0
_CACHED_PAGE_S = 1.0


@dataclass
class _Pace:
    pages: int = 0
    seconds: float = 0.0

    def per_page(self) -> float | None:
        if self.pages <= 0:
            return None
        return self.seconds / self.pages

    def add(self, pages: int, seconds: float) -> None:
        self.pages += pages
        self.seconds += seconds


def _human(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    minutes = (seconds + 59) // 60
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes}m"
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def _decorate(line: str, event: ScanProgress) -> str:
    if event.eta_s is None and not event.unknown_pdfs:
        return line
    if event.eta_s is None:
        return f"{line} ETA waiting on pdf page counts."
    text = f" ETA {_human(event.eta_s)}{event.eta_basis}"
    if event.unknown_pdfs == 1:
        text += ", plus 1 pdf of unknown length"
    elif event.unknown_pdfs > 1:
        text += f", plus {event.unknown_pdfs} pdfs of unknown length"
    return line + text


def format_scan_progress(event: ScanProgress) -> str:
    """One terminal line. Source paths are escaped; document text is never included."""
    match event.stage:
        case "network":
            return "Checking that this scan cannot reach the internet."
        case "discover":
            return "Discovering sources."
        case "inventory":
            extra = ""
            if event.skipped:
                extra += f" skipped files: {event.skipped};"
            if event.failed:
                extra += f" unreadable files: {event.failed};"
            line = (
                f"Supported files: {event.supported}; unsupported files: {event.unsupported};"
                f"{extra} unique contents to process: {event.unique}."
            )
            return _decorate(line, event)
        case "document":
            if event.pages == 1:
                pages = ", 1 page"
            elif event.pages:
                pages = f", {event.pages} pages"
            else:
                pages = ""
            if event.copies == 1:
                copies = ", plus 1 identical file"
            elif event.copies > 1:
                copies = f", plus {event.copies} identical files"
            else:
                copies = ""
            action = "cached reader" if event.cached else "reading"
            line = (
                f"[{event.index}/{event.count}] {action}: {safe(event.source)} "
                f"({event.format}{pages}){copies}."
            )
            return _decorate(line, event)
        case "step":
            total = f"/{event.pages}" if event.pages else ""
            label = _STEPS.get(event.step, event.step)
            cached = " (cached)" if event.cached else ""
            return _decorate(f"  page {event.unit}{total}: {label}{cached}", event)
        case "recorded":
            warnings = _warning_codes(event.warnings)
            extra = f"; warnings: {warnings}" if warnings else ""
            line = f"  recorded {event.status}{extra} ({event.elapsed_s}s)."
            return _decorate(line, event)
        case "mappings":
            return "Recording terminology proposals."
        case "recheck":
            return "Rechecking sources."
        case "activate":
            return "Checking activation."
        case _:
            return event.stage


def _warning_codes(warnings: tuple[str, ...]) -> str:
    codes = [
        item
        for item in warnings
        if item.isascii() and item.replace("_", "").isalnum() and item.isupper()
    ]
    return ", ".join(codes)


class ScanEta:
    """Remaining time from separate pdf and jpeg per-page rates measured in this scan."""

    def __init__(self, full_scan: bool, clock: Callable[[], float]):
        self.full_scan = full_scan
        self.clock = clock
        self.queue: list[str] = []
        self.reader_fresh = {"pdf": _Pace(), "jpeg": _Pace()}
        self.reader_cached = {"pdf": _Pace(), "jpeg": _Pace()}
        self.process_fresh = {"pdf": _Pace(), "jpeg": _Pace()}
        self.process_cached = {"pdf": _Pace(), "jpeg": _Pace()}
        self.observed_pages: dict[str, list[int]] = {"pdf": [], "jpeg": []}
        self.current: str | None = None
        self.current_pages: int | None = None
        self.current_unit = 0
        self.reader_cached_hit = False
        self.reader_accounted = False
        self.page_cached = True
        self.page_has_work = False
        self.pages_noted = False
        self.saw_step = False
        self.doc_mark = 0.0
        self.mark = 0.0

    def note(self, event: ScanProgress) -> ScanProgress:
        if event.stage == "inventory":
            self.queue = [fmt for fmt in event.formats if fmt in ("pdf", "jpeg")]
            return self._with_eta(event)
        if event.stage == "document" and event.format in ("pdf", "jpeg"):
            self.current = event.format
            self.current_pages = None
            self.current_unit = 0
            self.reader_cached_hit = event.cached
            self.reader_accounted = False
            self.page_cached = event.cached
            self.page_has_work = False
            self.pages_noted = False
            self.saw_step = False
            self.doc_mark = self.clock()
            self.mark = self.doc_mark
            self._learn_pages(event.pages)
            return self._with_eta(event)
        if event.stage == "step" and self.current is not None:
            self._learn_pages(event.pages)
            if not self.reader_accounted:
                self._account_reader(self.clock() - self.doc_mark)
                self.mark = self.clock()
                self.current_unit = event.unit
                self.page_cached = True
                self.page_has_work = False
            elif event.unit != self.current_unit:
                self._account_process_page()
                self.mark = self.clock()
                self.current_unit = event.unit
                self.page_cached = True
                self.page_has_work = False
            if event.step in ("ocr", "vision", "text"):
                self.page_has_work = True
                if event.step in ("ocr", "vision") and not event.cached:
                    self.page_cached = False
            self.saw_step = True
            return self._with_eta(event)
        if event.stage == "recorded" and self.current is not None:
            self._learn_pages(event.pages)
            if self.saw_step:
                self._account_process_page()
            elif not self.reader_accounted:
                self._account_reader(float(event.elapsed_s))
            if self.queue and self.queue[0] == self.current:
                self.queue.pop(0)
            self.current = None
            self.current_unit = 0
            self.saw_step = False
            return self._with_eta(event)
        return event

    def _learn_pages(self, pages: int | None) -> None:
        if self.current is None or self.pages_noted or not pages:
            return
        count = 1 if self.current == "jpeg" else pages
        self.current_pages = count
        self.observed_pages[self.current].append(count)
        self.pages_noted = True

    def _account_reader(self, seconds: float) -> None:
        if self.current is None:
            return
        pages = self.current_pages or 1
        pace = self.reader_cached if self.reader_cached_hit else self.reader_fresh
        pace[self.current].add(pages, max(0.0, seconds))
        self.reader_accounted = True

    def _account_process_page(self) -> None:
        if self.current is None or not self.page_has_work:
            return
        pace = self.process_cached if self.page_cached else self.process_fresh
        pace[self.current].add(1, max(0.0, self.clock() - self.mark))

    def _typical_pages(self, fmt: str) -> int | None:
        if fmt == "jpeg":
            return 1
        counts = self.observed_pages["pdf"]
        if not counts:
            return None
        return max(1, round(sum(counts) / len(counts)))

    def _prior(self, fmt: str, cached: bool) -> float:
        if cached:
            return _CACHED_PAGE_S
        if not self.full_scan:
            return _READER_PDF_S if fmt == "pdf" else _READER_JPEG_S
        return _FULL_SCAN_PAGE_S

    def _seen_only_cached(self, fmt: str) -> bool:
        fresh = self.reader_fresh[fmt].pages + self.process_fresh[fmt].pages
        warm = self.reader_cached[fmt].pages + self.process_cached[fmt].pages
        return fresh == 0 and warm > 0

    def _page_seconds(self, fmt: str, cached: bool, include_reader: bool) -> float:
        reader = 0.0
        if include_reader:
            pace = (self.reader_cached if cached else self.reader_fresh)[fmt]
            sample = pace.per_page()
            if sample is not None:
                reader = sample
            elif not self.full_scan:
                reader = self._prior(fmt, cached)
        pace = (self.process_cached if cached else self.process_fresh)[fmt]
        sample = pace.per_page()
        if sample is not None:
            process = sample
        elif self.full_scan:
            process = self._prior(fmt, cached)
        else:
            process = 0.0
        return reader + process

    def _with_eta(self, event: ScanProgress) -> ScanProgress:
        total = 0.0
        known = False
        unknown = 0
        counted: list[str] = []

        def add(fmt: str, pages: int | None, cached: bool, include_reader: bool) -> None:
            nonlocal total, known, unknown
            count = pages if pages is not None else self._typical_pages(fmt)
            if count is None:
                unknown += 1
                return
            known = True
            counted.append(fmt)
            total += max(count, 0) * self._page_seconds(fmt, cached, include_reader)

        if self.current is not None:
            if self.current_pages is None:
                pages_left = self._typical_pages(self.current)
            else:
                done = self.current_unit - 1 if self.current_unit else 0
                pages_left = max(self.current_pages - done, 0)
            cached = self.reader_cached_hit and self.page_cached
            add(self.current, pages_left, cached, include_reader=not self.reader_accounted)
        pending = self.queue[1:] if self.current is not None else self.queue
        for fmt in pending:
            add(fmt, None, self._seen_only_cached(fmt), include_reader=True)
        if not known:
            return replace(event, eta_s=None, unknown_pdfs=unknown, eta_basis="")
        return replace(
            event,
            eta_s=max(0, int(total + 0.999)),
            unknown_pdfs=unknown,
            eta_basis=self._basis(counted),
        )

    def _basis(self, counted: list[str]) -> str:
        parts = []
        for fmt in ("pdf", "jpeg"):
            if fmt not in counted:
                continue
            cached = self._seen_only_cached(fmt)
            if self.current == fmt and fmt not in self.queue[1:]:
                cached = self.reader_cached_hit and self.page_cached
            seconds = self._page_seconds(fmt, cached, include_reader=True)
            pace = (self.process_cached if cached else self.process_fresh)[fmt]
            reader = (self.reader_cached if cached else self.reader_fresh)[fmt]
            guessed = pace.pages == 0 if self.full_scan else reader.pages == 0
            shown = "<1s" if seconds < 1 else f"{max(1, round(seconds))}s"
            parts.append(f"{fmt} {'~' if guessed else ''}{shown}/page")
        if not parts:
            return ""
        return f" ({', '.join(parts)})"


def scanning_progress(full_scan: bool, clock: Callable[[], float] | None = None) -> ProgressFn:
    eta = ScanEta(full_scan, clock or time.monotonic)

    def emit(event: ScanProgress) -> None:
        print(format_scan_progress(eta.note(event)), flush=True)

    return emit
