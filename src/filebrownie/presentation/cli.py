"""English CLI output; source references are intentional output, never logs."""

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID, uuid4

from filebrownie import __version__, diagnostics, logs, provisioning
from filebrownie.evidence.network import NetworkIsolationError, ensure_isolated_network
from filebrownie.evidence.ocr import TesseractOcr
from filebrownie.evidence.readers import ReaderError, inspect_evidence, read_document
from filebrownie.ingestion.consistency import InventoryDifference
from filebrownie.ingestion.discovery import DiscoveryStatus, InventoryError, discover_sources
from filebrownie.ingestion.scan import run_full_scan, scan_sources
from filebrownie.interpretation.llama_vision import LlamaVisionClient
from filebrownie.presentation.progress import scanning_progress
from filebrownie.presentation.render import render, safe
from filebrownie.query.history import QueryError, run_query, source_text
from filebrownie.query.timeline import RangeError, parse_range
from filebrownie.storage import erasure
from filebrownie.storage.checks import MAX_NOTE, VERDICTS
from filebrownie.storage.database import DatabaseError, open_repository
from filebrownie.storage.operation import OperationBusyError, OperationLockError, operation_lock

_SUMMARY_SAMPLE_PATHS = 3
_UNIT_DETAIL_WARNINGS = frozenset({"DOCUMENT_RESOURCE_LIMIT", "VISION_OUTPUT_INVALID"})
_COVERAGE_WARNING_PART = re.compile(r"^(.+?) \((unit \d+|file)\)$")


def configured_paths() -> tuple[Path, Path]:
    source = Path(os.environ.get("FILEBROWNIE_SOURCE_DIR", "/sources"))
    data = Path(os.environ.get("FILEBROWNIE_DATA_DIR", "/data"))
    if not source.is_absolute() or not data.is_absolute():
        raise InventoryError("ABSOLUTE_DIRECTORIES_REQUIRED")
    source_resolved, data_resolved = source.resolve(), data.resolve()
    if source_resolved.is_relative_to(data_resolved) or data_resolved.is_relative_to(
        source_resolved
    ):
        raise InventoryError("SOURCE_DATA_OVERLAP")
    host_source = os.environ.get("FILEBROWNIE_HOST_SOURCE_DIR")
    host_data = os.environ.get("FILEBROWNIE_HOST_DATA_DIR")
    if host_source and host_data:
        host_source_resolved = Path(host_source).resolve()
        host_data_resolved = Path(host_data).resolve()
        if (
            host_source_resolved == host_data_resolved
            or host_source_resolved.is_relative_to(host_data_resolved)
            or host_data_resolved.is_relative_to(host_source_resolved)
        ):
            raise InventoryError("SOURCE_DATA_OVERLAP")
    return source, data


def inventory(save: bool = False) -> int:
    try:
        source, data = configured_paths()
        with operation_lock(data):
            with open_repository() if save else nullcontext() as repository:
                generation_id = None
                if repository is not None:
                    repository.require_schema()
                    repository.recover_interrupted()
                    generation_id = repository.begin_inventory()
                try:
                    result = discover_sources(source)
                except (InventoryError, OSError, KeyboardInterrupt):
                    if repository is not None:
                        repository.interrupt(generation_id)
                    raise
                if repository is not None:
                    repository.save_inventory(generation_id, result)
                    print(f"Generation: {generation_id} (staged; inventory only)")
            print(f"Inventory timestamp: {result.created_at.isoformat()}")
            print(f"Supported files: {result.supported_file_count}")
            print(f"Unsupported files: {result.unsupported_file_count}")
            print(f"Unique fingerprinted contents: {len(result.content_sources())}")
            print("Discovery only. Documents were not parsed; extraction coverage is unknown.")
            print("Status\tFormat\tSource\tWarnings")
            for record in result.records:
                # Escape controls and Unicode formatting characters in terminal filenames.
                print(
                    f"{record.status}\t{record.format or '-'}\t{safe(record.relative_path)}\t"
                    f"{', '.join(record.warnings) or '-'}"
                )
            incomplete = any(
                record.status in (DiscoveryStatus.FAILED, DiscoveryStatus.SKIPPED)
                for record in result.records
            )
            return 1 if incomplete else 0
    except (InventoryError, OperationLockError, OperationBusyError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("INVENTORY_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def _print_inventory_difference(difference: InventoryDifference) -> None:
    if difference.consistent:
        return
    print("Source inventory no longer matches the start of this operation:")
    for label in ("added", "removed", "changed", "unverifiable"):
        paths = getattr(difference, label)
        if not paths:
            continue
        print(f"  {label}: {len(paths)}")
        detail_map = dict(difference.changed_details)
        for path in paths:
            extra = detail_map.get(path)
            suffix = f" ({extra})" if extra else ""
            print(f"    {safe(path)}{suffix}")


def _sample_paths(paths: Sequence[str], limit: int = _SUMMARY_SAMPLE_PATHS) -> str:
    shown = [safe(path) for path in paths[:limit]]
    if not shown:
        return ""
    text = "; e.g. " + ", ".join(shown)
    if len(paths) > limit:
        text += f" (+{len(paths) - limit} more)"
    return text


def show_generation(
    repository, generation_id: UUID, step_cache_stats: dict[str, int] | None = None
) -> None:
    inventory = repository.load_inventory(generation_id)
    generation = next(item for item in repository.generations() if item.id == generation_id)
    print(f"Generation: {generation.id}; kind: {generation.kind}; state: {generation.state}")
    print(f"Reason: {generation.reason}; finished at: {generation.finished_at or 'not completed'}")
    print(
        f"Supported files: {inventory.supported_file_count}; "
        f"unsupported files: {inventory.unsupported_file_count}"
    )
    if generation.kind != "scan":
        print("Reader foundations only; no OCR, vision, or medical interpretation was run.")
    for record in inventory.records:
        if record.status != DiscoveryStatus.READY:
            print(
                f"Source inventory: {record.status}; {safe(record.relative_path)}; "
                f"warnings: {', '.join(record.warnings) or '-'}"
            )
    reads = repository.generation_reads(generation_id)
    if reads:
        file_status: dict[str, int] = {}
        cache_hits = 0
        for item in reads:
            file_status[item.status] = file_status.get(item.status, 0) + 1
            if item.cache_hit:
                cache_hits += 1
        parts = ", ".join(f"{count} {status}" for status, count in sorted(file_status.items()))
        line = f"Reader files: {parts}; reader cache hits: {cache_hits}/{len(reads)}"
        if step_cache_stats:
            ocr_pages = step_cache_stats.get("ocr_pages", 0)
            vision_pages = step_cache_stats.get("vision_pages", 0)
            if ocr_pages:
                line += (
                    f"; OCR cache hits: {step_cache_stats.get('ocr_hits', 0)}/{ocr_pages}"
                )
            if vision_pages:
                line += (
                    f"; vision cache hits: {step_cache_stats.get('vision_hits', 0)}/{vision_pages}"
                )
        print(line)
        for item in reads:
            if item.status == "completed":
                continue
            paths = list(dict.fromkeys(item.sources))
            path_text = ", ".join(safe(name) for name in paths[:2])
            if len(paths) > 2:
                path_text += f" (+{len(paths) - 2} more)"
            extra = f"; warnings: {', '.join(item.warnings)}" if item.warnings else ""
            print(f"  Reader {item.status}: {path_text}{extra}")


def _summarize_coverage_detail(detail: str) -> str:
    """Collapse per-unit warning tokens into code counts for terminal output."""
    code_counts: dict[str, int] = {}
    other: list[str] = []
    for part in detail.split(", "):
        if match := _COVERAGE_WARNING_PART.match(part):
            code_counts[match[1]] = code_counts.get(match[1], 0) + 1
        else:
            other.append(part)
    summary = [f"{code} ({count})" for code, count in sorted(code_counts.items())]
    summary.extend(sorted(dict.fromkeys(other)))
    return ", ".join(summary)


def show_findings(findings) -> None:
    grouped: dict[tuple[str, str], list[str]] = {}
    for item in findings:
        key = (item.kind, item.detail)
        grouped.setdefault(key, []).extend(item.sources)
    for (kind, detail), sources in sorted(grouped.items()):
        paths = list(dict.fromkeys(sources))
        count = len(paths)
        suffix = f" ({count} files)" if count > 1 else ""
        shown = _summarize_coverage_detail(detail) if kind == "new coverage warning" else detail
        print(f"  Guard finding: {kind}: {shown}{suffix}{_sample_paths(paths)}")


def show_status(repository) -> None:
    active_id = repository.active_generation_id()
    generations = repository.generations()
    active = next((item for item in generations if item.id == active_id), None)
    if active is None:
        print("Active medical index: none.")
    else:
        print(
            f"Active generation: {active.id}; activated at: {active.activated_at.isoformat()}"
            f"{'; forced past the coverage guard' if active.forced else ''}"
        )
        counts: dict[str, int] = {}
        for item in repository.generation_reads(active.id):
            counts[item.status] = counts.get(item.status, 0) + 1
        inventory = repository.load_inventory(active.id)
        print(
            f"  Supported files: {inventory.supported_file_count}; unsupported files: "
            f"{inventory.unsupported_file_count}; unique contents by status: "
            + (", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "none")
        )
        show_findings(active.findings)
    print("Generation\tKind\tState\tStarted at\tSources\tReason")
    for generation in generations:
        print(
            f"{generation.id}\t{generation.kind}\t{generation.state}"
            f"\t{generation.started_at.isoformat()}"
            f"\t{generation.source_count}\t{generation.reason}"
        )
        if generation.state == "staged" and generation.reason == "ACTIVATION_GUARD":
            show_findings(generation.findings)


def activate_command(generation_id: UUID, force: bool) -> int:
    try:
        source, data = configured_paths()
        with operation_lock(data), open_repository() as repository:
            repository.require_schema()
            repository.recover_interrupted()
            result = repository.activate(generation_id, discover_sources(source), force)
            logs.log_event(
                data,
                "activate",
                "activation_finished",
                generation=str(generation_id),
                activated=result.activated,
                reason=result.reason,
            )
            if result.activated:
                print(f"Generation {generation_id} is now the active index.")
                if result.findings:
                    print("Activated past the coverage guard at your request:")
                    show_findings(result.findings)
                if result.pruned:
                    print(f"Superseded generations pruned: {result.pruned}")
                print("Activation does not establish extraction accuracy or completeness.")
                return 0
            print(f"Generation {generation_id} stays staged ({result.reason}).")
            show_findings(result.findings)
            print("Review the findings, then activate with --force to accept them.")
            return 1
    except (InventoryError, OperationLockError, OperationBusyError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("OPERATION_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def show_interpretation(repository, generation_id: UUID) -> None:
    units = repository.facts.unit_outcomes(generation_id)
    counts = repository.facts.fact_counts(generation_id)
    totals: dict[str, int] = {}
    warning_totals: dict[str, int] = {}
    for unit in units:
        totals[unit["status"]] = totals.get(unit["status"], 0) + 1
        for code in unit["warnings"]:
            warning_totals[code] = warning_totals.get(code, 0) + 1
    print(
        "Processed units: "
        + (", ".join(f"{count} {status}" for status, count in sorted(totals.items())) or "none")
        + f"; facts recorded: {sum(counts.values())}"
    )
    if warning_totals:
        listed = ", ".join(
            f"{code} ({warning_totals[code]})"
            for code in sorted(warning_totals)
            if code != "TEXT_LAYER_COVERAGE_UNVERIFIED"
        )
        text_layer = warning_totals.get("TEXT_LAYER_COVERAGE_UNVERIFIED")
        if text_layer:
            listed = (
                f"{listed}; TEXT_LAYER_COVERAGE_UNVERIFIED ({text_layer})"
                if listed
                else f"TEXT_LAYER_COVERAGE_UNVERIFIED ({text_layer})"
            )
        if listed:
            print(f"Unit warnings: {listed}")
    for unit in units:
        warnings = unit["warnings"]
        if unit["status"] == "completed" and not (
            _UNIT_DETAIL_WARNINGS & set(warnings)
        ):
            continue
        if unit["status"] == "completed" and not warnings:
            continue
        print(
            f"  Unit {unit['unit_number']} ({unit['format']}): {unit['status']}; "
            f"warnings: {', '.join(warnings) or '-'}"
        )


def _log_failure(component: str, error: object) -> None:
    """Record only the fixed leading code of an error; never its details."""
    try:
        _, data = configured_paths()
    except InventoryError:
        return
    code = str(error).split(":")[0].split()[0] if str(error).strip() else "UNKNOWN"
    logs.log_event(data, component, "operation_failed", error=code)


def _log_scan(repository, data: Path, operation, outcome, elapsed: float) -> None:
    units = repository.facts.unit_outcomes(outcome.generation_id)
    inventory = repository.load_inventory(outcome.generation_id)
    activation = outcome.activation
    logs.log_event(
        data,
        "scan",
        "scan_finished",
        operation=str(operation),
        generation=str(outcome.generation_id),
        files=inventory.supported_file_count,
        unsupported=inventory.unsupported_file_count,
        units=len(units),
        partial_units=sum(unit["status"] != "completed" for unit in units),
        facts=sum(repository.facts.fact_counts(outcome.generation_id).values()),
        activated=bool(activation and activation.activated),
        reason=(activation.reason if activation and activation.reason else outcome.blocked)
        or "ACTIVATED",
        duration_ms=int(elapsed * 1000),
    )


def full_scan() -> int:
    try:
        source, data = configured_paths()
        models = Path(os.environ.get("FILEBROWNIE_MODEL_DIR", "/models"))
        ocr = TesseractOcr(models)
        vision = LlamaVisionClient(
            os.environ.get("FILEBROWNIE_MODEL_URL", "http://model:8080"), models
        )
        with operation_lock(data), open_repository() as repository:
            operation, started = uuid4(), time.monotonic()
            logs.log_event(data, "scan", "scan_started", operation=str(operation))
            step_cache_stats: dict[str, int] = {}
            outcome = run_full_scan(
                repository,
                source,
                data,
                ocr,
                vision,
                progress=scanning_progress(full_scan=True, step_cache_stats=step_cache_stats),
            )
            show_generation(repository, outcome.generation_id, step_cache_stats)
            show_interpretation(repository, outcome.generation_id)
            _log_scan(repository, data, operation, outcome, time.monotonic() - started)
            if outcome.activation is None:
                print(f"Generation stays {outcome.blocked or 'staged'}; it is not queryable.")
                if outcome.inventory_difference is not None:
                    _print_inventory_difference(outcome.inventory_difference)
                return 1
            result = outcome.activation
            if result.activated:
                print(f"Generation {outcome.generation_id} is now the active index.")
                print("Activation does not establish extraction accuracy or completeness.")
                return 0
            print(f"Generation stays staged ({result.reason}); the previous index is unchanged.")
            show_findings(result.findings)
            print(
                "Review the findings, then run: "
                f"activate {outcome.generation_id} --force"
            )
            return 1
    except provisioning.SetupError as error:
        _log_failure("scan", error)
        print(str(error), file=sys.stderr)
        return 2
    except (
        InventoryError,
        OperationLockError,
        OperationBusyError,
        DatabaseError,
        ReaderError,
        NetworkIsolationError,
    ) as error:
        _log_failure("scan", error)
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        _log_failure("scan", "SCAN_UNAVAILABLE")
        print("SCAN_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def doctor_command() -> int:
    source, data = configured_paths()
    models = Path(os.environ.get("FILEBROWNIE_MODEL_DIR", "/models"))

    def database() -> None:
        with open_repository() as repository:
            repository.require_schema()

    checks = diagnostics.run_checks(
        source,
        models,
        os.environ.get("FILEBROWNIE_MODEL_URL", "http://model:8080"),
        _wrap(database),
    )
    for item in checks:
        print(
            f"{'ok  ' if item.ok else 'FAIL'}  {item.name}"
            + ("" if item.ok else f"  [{item.code}]")
        )
    return 0 if all(item.ok for item in checks) else 1


def _wrap(action):
    """Turn storage errors into fixed codes for doctor output."""

    def run() -> None:
        try:
            action()
        except DatabaseError as error:
            raise provisioning.SetupError(str(error)) from None

    return run


def model_setup_command(verify_only: bool) -> int:
    """Touches model storage only. Never reads sources or generated medical data."""
    models = Path(os.environ.get("FILEBROWNIE_MODEL_DIR", "/models"))
    try:
        for component in provisioning.COMPONENTS:
            problems = provisioning.verify(models, component)
            if verify_only:
                for item in problems:
                    print(f"{component}: missing or altered: {item}")
                print(f"{component}: {'needs setup' if problems else 'verified'}")
                if problems:
                    print(provisioning.SETUP_HINT)
                    return 1
                continue
            changed = provisioning.provision(models, component)
            print(f"{component}: downloaded {len(changed)}; verified against pinned checksums.")
        return 0
    except provisioning.SetupError as error:
        print(str(error), file=sys.stderr)
        return 2


def _guarded(action) -> int:
    """Run `action(repository)` under the shared lock with fixed, safe error codes."""
    try:
        _, data = configured_paths()
        with operation_lock(data), open_repository() as repository:
            repository.require_schema()
            repository.ensure_seed()
            return action(repository)
    except (
        InventoryError,
        OperationLockError,
        OperationBusyError,
        DatabaseError,
        QueryError,
        RangeError,
        erasure.ErasureError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("OPERATION_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def erase_command(scope: str) -> int:
    """Remove generated data (D11). Source documents are never touched."""

    def action(repository) -> int:
        _, data = configured_paths()
        if scope == "all":
            print(
                "This permanently deletes ALL generated data, including dictionary decisions "
                "and manual checks. Source documents are not touched."
            )
            try:
                phrase = input(f"Type '{erasure.CONFIRMATION_PHRASE}' to confirm: ")
            except EOFError:
                phrase = ""
            result = erasure.erase_all(repository.connection, data, phrase)
        else:
            result = erasure.erase_derived(repository.connection, data)
        print(
            f"Erased generations: {result.generations}; cache entries: {result.cache_entries}; "
            f"generated folders cleared: {result.directories}."
        )
        if scope == "all":
            print(
                f"Erased dictionary decisions: {result.decisions}; manual checks: {result.checks}."
            )
        else:
            print(
                "Dictionary decisions and manual checks were kept. Run scan to rebuild the index."
            )
        return 0

    return _guarded(action)


def check_record_command(
    fact: str | None, source_name: str | None, page: int | None, verdict: str, note: str
) -> int:
    def action(repository) -> int:
        if verdict not in VERDICTS:
            raise QueryError("CHECK_VERDICT_INVALID")
        if len(note) > MAX_NOTE:
            raise QueryError("CHECK_NOTE_TOO_LONG")
        if (fact is None) == (source_name is None):
            raise QueryError("CHECK_NEEDS_ONE_REFERENCE")
        if fact is not None:
            generation_id = repository.active_generation_id()
            found = (
                repository.reader.find_fact(generation_id, fact)
                if generation_id is not None and len(fact) >= 6
                else None
            )
            if found is None:
                raise QueryError("FACT_NOT_FOUND_OR_AMBIGUOUS")
            table, row = found
            summary = (
                f"{row['raw_label']} {row['raw_value']} {row['unit'] or ''}".strip()
                if table == "lab_results"
                else f"{row['raw_specialty']} {row['event_type']}"
            )
            identifier = repository.checks.record(
                row["content_hash"],
                f"page {row['unit_number']}",
                verdict,
                note,
                row["id"],
                summary,
            )
        else:
            if verdict != "missed":
                raise QueryError("CHECK_SOURCE_REFERENCE_IS_FOR_MISSED_ITEMS")
            if page is None or page < 1:
                raise QueryError("CHECK_PAGE_REQUIRED")
            source, _ = configured_paths()
            record = next(
                (
                    item
                    for item in discover_sources(source).records
                    if item.relative_path == source_name and item.content_hash
                ),
                None,
            )
            if record is None:
                raise QueryError("SOURCE_NOT_FOUND")
            identifier = repository.checks.record(
                record.content_hash, f"page {page}", verdict, note
            )
        print(f"Recorded check {identifier}: {verdict}.")
        return 0

    return _guarded(action)


def check_summary_command() -> int:
    def action(repository) -> int:
        observed = repository.checks.observed_by_state(repository.active_generation_id())
        if not observed:
            print("No manual checks recorded; there is nothing to evaluate yet.")
            return 0
        print("Observed check outcomes (counts only):")
        for (verdict, state), total in observed.items():
            print(f"  {verdict}; reading was '{state}': {total}")
        print(
            "These are observed errors and omissions in the results you chose to check. "
            "No percentage is given: it needs a reviewed scope and a denominator."
        )
        return 0

    return _guarded(action)


def check_list_command() -> int:
    def action(repository) -> int:
        checks = repository.checks.listing(repository.active_generation_id())
        if not checks:
            print("No manual checks recorded.")
            return 0
        for item in checks:
            print(
                f"{item['id']}  {item['verdict']}; {item['location']}; "
                f"document {item['content_hash'][:12]}; {item['availability']}"
            )
            if item["summary"]:
                print(f"  Result: {safe(item['summary'])}")
            if item["note"]:
                print(f"  Note: {safe(item['note'])}")
        print(
            "Checks are your own records; they are not accuracy statistics. "
            "A check follows document content, never a file path."
        )
        return 0

    return _guarded(action)


def history_command(kind: str, term: str, start: str | None, end: str | None) -> int:
    window = parse_range(start, end)

    def action(repository) -> int:
        for line in render(run_query(repository, kind, term, window)):
            print(line)
        return 0

    return _guarded(action)


def fact_command(prefix: str) -> int:
    def action(repository) -> int:
        generation_id = repository.active_generation_id()
        if generation_id is None:
            raise QueryError("NO_ACTIVE_GENERATION")
        found = repository.reader.find_fact(generation_id, prefix) if len(prefix) >= 6 else None
        if found is None:
            raise QueryError("FACT_NOT_FOUND_OR_AMBIGUOUS")
        table, fact = found
        title = "laboratory result" if table == "lab_results" else "specialty event"
        print(f"Fact: {fact['id']} ({title})")
        print(f"Source: {safe(source_text(fact['sources'], fact['format'], fact['unit_number']))}")
        print(f"Verification: {fact['verification']}; extractor {fact['extractor_version']}")
        shown = (
            ("raw_label", "raw_value", "unit", "reference_interval", "flag", "specimen")
            if table == "lab_results"
            else ("raw_specialty", "event_type", "wording", "strength", "planned")
        )
        for name in shown:
            print(f"  {name}: {safe(str(fact[name]))}")
        for item in fact["dates"]:
            print(f"  date ({item['role']}): {safe(item['raw'])}")
        if fact["notes"]:
            print(f"Notes: {', '.join(fact['notes'])}")
        for title, indices in (
            ("Evidence", fact["evidence"]),
            ("Alternative interpretation evidence", fact["alternative_evidence"]),
        ):
            if not indices:
                continue
            print(f"{title} (located text):")
            for span in repository.reader.spans(
                generation_id, fact["content_hash"], fact["format"], fact["unit_number"], indices
            ):
                box = (
                    f"({span['x0']:.0f},{span['y0']:.0f})-({span['x1']:.0f},{span['y1']:.0f})"
                    if span["x0"] is not None
                    else "page-level location"
                )
                text = json.dumps(span["text"], ensure_ascii=False)
                print(f"  [{span['span_index']}] {box} via {span['reader']}: {text}")
        print(
            "Located text shows where a reading comes from; it does not prove clinical correctness."
        )
        return 0

    return _guarded(action)


def dictionary_command(action: str, label: str | None = None, concept: str | None = None) -> int:
    try:
        _, data = configured_paths()
        with operation_lock(data), open_repository() as repository:
            repository.require_schema()
            repository.ensure_seed()
            store = repository.dictionary
            if action == "review":
                snapshot = store.snapshot(repository.active_generation_id())
                print(f"Dictionary revision: {store.revision()}")
                active = repository.active_generation_id()
                if active is None:
                    print("No active generation; mapping proposals appear once a scan is active.")
                else:
                    pending = [item for item in store.proposals(active) if item["verdict"] is None]
                    print(f"Unreviewed proposals (auto-mapped, unreviewed): {len(pending)}")
                    for item in pending:
                        print(
                            f"  {safe(item['label'])} -> {snapshot.describe(item['concept_id'])}"
                        )
                decisions = store.decisions()
                print(f"Decisions: {len(decisions)}")
                for item in decisions:
                    print(
                        f"  {item['verdict']}: {safe(item['label'])} -> "
                        f"{snapshot.describe(item['concept_id'])}"
                    )
                return 0
            if not store.concept_exists(concept):
                print("UNKNOWN_CONCEPT", file=sys.stderr)
                return 2
            if action == "reverse":
                done = store.reverse(label, concept)
                print("Decision removed." if done else "No such decision.")
                return 0 if done else 1
            store.decide(label, concept, "accepted" if action == "accept" else "rejected")
            print(f"Recorded: {action} {safe(label)} -> {concept}. No rescan is needed.")
            return 0
    except (InventoryError, OperationLockError, OperationBusyError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("OPERATION_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def database_command(command: str, generation_id: UUID | None = None) -> int:
    try:
        source, data = configured_paths()
        with operation_lock(data), open_repository() as repository:
            if command == "migrate":
                repository.migrate()
                print("Database schema is current.")
                return 0
            repository.require_schema()
            recovered = repository.recover_interrupted()
            if recovered:
                print(f"Interrupted inventory generations recovered: {recovered}")
            if command == "generation":
                show_generation(repository, generation_id)
                return 0
            if command == "status":
                show_status(repository)
                return 0
            repository.load_inventory(generation_id)
            difference = repository.revalidate(generation_id, discover_sources(source))
            _print_inventory_difference(difference)
            generation = next(item for item in repository.generations() if item.id == generation_id)
            print(f"Generation state: {generation.state}")
            print("This does not activate a medical index or establish extraction coverage.")
            return 0 if difference.consistent and generation.state in ("staged", "active") else 1
    except (InventoryError, OperationLockError, OperationBusyError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("OPERATION_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def reader_scan() -> int:
    try:
        source, data = configured_paths()
        with operation_lock(data), open_repository() as repository:
            generation_id, _ = scan_sources(
                repository, source, data, progress=scanning_progress(full_scan=False)
            )
            show_generation(repository, generation_id)
            generation = next(item for item in repository.generations() if item.id == generation_id)
            reads = repository.generation_reads(generation_id)
            return (
                0
                if (
                    generation.state == "staged"
                    and all(item.status == "completed" for item in reads)
                    and all(
                        record.status == DiscoveryStatus.READY
                        for record in repository.load_inventory(generation_id).records
                    )
                )
                else 1
            )
    except (
        InventoryError,
        OperationLockError,
        OperationBusyError,
        DatabaseError,
        ReaderError,
        NetworkIsolationError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("SCAN_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def evidence_command(source_name: str | None = None, reference: UUID | None = None) -> int:
    try:
        source, data = configured_paths()
        with operation_lock(data):
            if reference is None:
                ensure_isolated_network()
                record = next(
                    (
                        item
                        for item in discover_sources(source).records
                        if item.relative_path == source_name
                    ),
                    None,
                )
                if record is None:
                    raise ReaderError("SOURCE_NOT_FOUND")
                result = read_document(source, record, data)
            else:
                result = inspect_evidence(data, str(reference))
            print(f"Evidence reference: {result.reference}")
            print(f"Reader source at creation: {safe(result.source)}")
            print(f"Reader status: {result.evidence.status}")
            count = result.evidence.page_count
            print(f"Page/image count: {count if count is not None else 'unknown'}")
            print(f"Warnings: {', '.join(result.evidence.warnings) or '-'}")
            print(
                "Reader output only. OCR, vision extraction, and medical verification are pending."
            )
            for unit in result.evidence.units:
                print(
                    f"Unit {unit.number}: {unit.status}; spans: {len(unit.spans)}; "
                    f"warnings: {', '.join(unit.warnings) or '-'}"
                )
                if reference is not None:
                    for span in unit.spans:
                        print(
                            f"  {span.bbox or 'page-level location'}: "
                            f"{json.dumps(safe(span.text), ensure_ascii=False)}"
                        )
            return 0 if result.evidence.status == "completed" else 1
    except (
        InventoryError,
        OperationLockError,
        OperationBusyError,
        ReaderError,
        NetworkIsolationError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("EVIDENCE_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("OPERATION_INTERRUPTED", file=sys.stderr)
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filebrownie",
        description="Local medical histories from PDF and JPEG sources.",
    )
    parser.add_argument("--version", action="version", version=f"FileBrownie {__version__}")
    commands = parser.add_subparsers(dest="command")
    status = commands.add_parser("status", help="Show implementation or saved-generation status.")
    status.add_argument("--database", action="store_true", help="Inspect local saved generations.")
    sources = commands.add_parser("inventory", help="List sources without parsing documents.")
    sources.add_argument(
        "--save", action="store_true", help="Persist a staged inventory generation."
    )
    commands.add_parser("migrate", help="Initialize or upgrade the local database schema.")
    validate = commands.add_parser("validate", help="Recheck a saved inventory against sources.")
    validate.add_argument("generation", type=UUID)
    scan = commands.add_parser(
        "scan",
        help="Scan one folder through readers, OCR, vision, and activation.",
        description=(
            "Scan one folder through readers, OCR, vision, and activation. "
            "Progress is printed as each file and page is processed, "
            "with an ETA from separate pdf and jpeg rates."
        ),
    )
    scan.add_argument(
        "--readers-only",
        action="store_true",
        help="Run PDF/JPEG readers without OCR, vision, or activation.",
    )
    activate = commands.add_parser("activate", help="Activate a completed staged scan.")
    activate.add_argument("generation", type=UUID)
    activate.add_argument(
        "--force", action="store_true", help="Accept coverage-guard findings (not source changes)."
    )
    for name, text in (
        ("labs", "Laboratory history by analyte or group."),
        ("visits", "Visit history by specialty or group."),
    ):
        history = commands.add_parser(name, help=text)
        history.add_argument("term", help="Name in English, Russian, or Ukrainian.")
        history.add_argument("--from", dest="start", help="YYYY, YYYY-MM, or YYYY-MM-DD.")
        history.add_argument("--to", dest="end", help="YYYY, YYYY-MM, or YYYY-MM-DD.")
    dictionary = commands.add_parser("dict", help="Review terminology mappings.")
    dictionary_actions = dictionary.add_subparsers(dest="dict_command", required=True)
    dictionary_actions.add_parser("review", help="List proposals and recorded decisions.")
    for action, text in (
        ("accept", "Accept a label-to-concept mapping."),
        ("reject", "Reject a label-to-concept mapping; it stays rejected across rescans."),
        ("reverse", "Remove a recorded decision."),
    ):
        item = dictionary_actions.add_parser(action, help=text)
        item.add_argument("label")
        item.add_argument("concept", help="Concept id such as ferritin or urology.")
    commands.add_parser("doctor", help="Check isolation, read-only mounts, and readiness.")
    erase = commands.add_parser("erase", help="Delete generated data; sources are never touched.")
    erase.add_argument(
        "scope",
        choices=("derived", "all"),
        help="derived keeps dictionary decisions and checks; all needs typed confirmation.",
    )
    check = commands.add_parser("check", help="Record and list manual acceptance checks.")
    check_actions = check.add_subparsers(dest="check_command", required=True)
    record = check_actions.add_parser("record", help="Record a check on a result or source page.")
    record.add_argument("--fact", help="Result ref from a history table (6+ characters).")
    record.add_argument("--source", help="Relative source path, for a missed item.")
    record.add_argument("--page", type=int, help="Page (or 1 for an image) for --source.")
    record.add_argument("--verdict", required=True, choices=VERDICTS)
    record.add_argument("--note", default="")
    check_actions.add_parser("list", help="List recorded checks and evidence availability.")
    check_actions.add_parser("summary", help="Count observed outcomes of recorded checks.")
    reader = commands.add_parser("read", help="Read one PDF/JPEG into local evidence artifacts.")
    reader.add_argument("source", help="Relative path from inventory output.")
    evidence = commands.add_parser("evidence", help="Inspect saved reader evidence.")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    fact = evidence_commands.add_parser("fact", help="Inspect a history row's source evidence.")
    fact.add_argument("reference", help="Ref shown in a history table (at least 6 characters).")
    show = evidence_commands.add_parser("show", help="Show text with page/image locations.")
    show.add_argument("reference", type=UUID)
    generation = evidence_commands.add_parser("generation", help="Inspect a generation's readers.")
    generation.add_argument("generation", type=UUID)
    setup = commands.add_parser("model-setup", help="Provision pinned model files (setup profile).")
    setup.add_argument("--verify", action="store_true", help="Only verify files; no network use.")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "status":
        if args.database:
            return database_command("status")
        print(
            "Scan, activate, and query commands (labs, visits) are available; they need "
            "provisioned models and the local inference service. Use `status --database` "
            "for the stored index, and `model-setup --verify` for model readiness. "
            "Extraction is machine-generated: check results against the source documents."
        )
        return 0
    if args.command == "inventory":
        return inventory(args.save)
    if args.command == "migrate":
        return database_command("migrate")
    if args.command == "validate":
        return database_command("validate", args.generation)
    if args.command == "activate":
        return activate_command(args.generation, args.force)
    if args.command in ("labs", "visits"):
        try:
            return history_command(args.command, args.term, args.start, args.end)
        except RangeError as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "dict":
        return dictionary_command(
            args.dict_command, getattr(args, "label", None), getattr(args, "concept", None)
        )
    if args.command == "read":
        return evidence_command(source_name=args.source)
    if args.command == "scan":
        if args.readers_only:
            return reader_scan()
        return full_scan()
    if args.command == "evidence":
        if args.evidence_command == "fact":
            return fact_command(args.reference)
        if args.evidence_command == "generation":
            return database_command("generation", args.generation)
        return evidence_command(reference=args.reference)
    if args.command == "model-setup":
        return model_setup_command(args.verify)
    if args.command == "doctor":
        return doctor_command()
    if args.command == "erase":
        return erase_command(args.scope)
    if args.command == "check":
        if args.check_command == "list":
            return check_list_command()
        if args.check_command == "summary":
            return check_summary_command()
        return check_record_command(args.fact, args.source, args.page, args.verdict, args.note)
    return 2
