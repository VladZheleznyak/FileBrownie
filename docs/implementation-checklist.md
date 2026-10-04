# Phase 1 and Phase 2 implementation checklist

This checklist tracks implementation of the [project pivot](../PROJECT_PIVOT_2026-10-03.md),
[decisions](decisions.md), and [architecture](architecture.md). It adds no product
scope and does not replace those documents.

Status: the Phase 1 pipeline (readers, Tesseract OCR, local Qwen2.5-VL vision, grounded
verification, activation, dictionary, `labs`/`visits`, checks, erasure) and the Phase 2
hardening items below are implemented and covered by synthetic tests (226 tests, run through
Compose, including real OCR and the real model service on the reference GPU). A box is checked
only where implementation and validation evidence exist; the evidence is named in the item.
Synthetic success does not establish medical extraction accuracy. **No real document has been
processed**; user-led real-data review (D37) is the next step and Phase 2's manual-check
evaluation stays open until those checks exist. Run application commands and checks through
Docker Compose.

## Phase 1: runtime and privacy

- [x] Create Compose services for the CLI/OCR application, PostgreSQL, and local
  vision inference. Install dependencies in images using uv; commit synchronized
  `pyproject.toml` and `uv.lock`. Builds access package registries only (D7, D14).
  Evidence: Compose `app`/`db`/`model`/`model-setup`; uv-locked images; `tests/test_deployment_policy.py`.
- [x] Choose and record a working OCR/vision/runtime baseline using synthetic
  English, Russian, and Ukrainian fixtures and basic reference-hardware checks.
  Plan for 31 GB WSL RAM and 12 GB VRAM; no private-original or formal benchmark
  gate applies (D3, D6, D37).
  Evidence: Tesseract + Qwen2.5-VL 7B Q4_K_M via llama.cpp, recorded in README/architecture; `tests/test_model_service.py` (en/ru/uk pages, about 7 s per page, 8.8 GB GPU in use).
- [x] Provision versioned model weights and OCR model files through separate
  network-enabled setup with model storage only. Mount model storage read-only
  into `app` and the model service. Processing never downloads weights or OCR
  files and reports missing ones as a setup error. Fit co-resident models in
  12 GB of VRAM or run OCR on the CPU (D13, D38, D40).
  Evidence: `models_manifest.json`, `model-setup`, `tests/test_ocr.py`, `tests/test_llama_vision.py`; verified from a clean `docker compose build --no-cache`.
- [x] Mount originals read-only; store generated medical data outside originals
  and Git. Enforce runtime egress isolation for processing services and refuse
  scans when outbound access is reachable (D12, D13).
  Evidence: policy tests, `doctor`, outbound probe (`tests/test_network.py`, `tests/test_recovery_drills.py`).
- [x] Allow only sanitized operational diagnostics, including third-party service
  diagnostics. Keep medical CLI/evidence output separate from logs (D38). Disable
  Docker log capture for the CLI service, run it with `run --rm`, and configure
  PostgreSQL and the model server to omit values, prompts, and responses (D44).
  Centralized logging is a Phase 2 task.
  Evidence: `tests/test_privacy.py`, `tests/test_logging.py`, log-driver/server-logging policy tests.

## Phase 1: ingestion, storage, and recovery

- [x] Discover PDF and JPG/JPEG inputs; report unsupported files without parsing
  them or expanding archives. Report supported-file, page/image, and unsupported
  counts. Bound PDF/image processing resources (D1, D26, D31).
  Evidence: `tests/test_fixtures_hostile.py`, `tests/test_discovery.py`, reader tests.
- [x] Implement domain-independent content identity, source locations, units,
  located text, processing status, and concurrent warnings. Retain all source
  paths for byte-identical content; preserve successful pages when others fail
  and unknown page counts when enumeration fails (D22, D32).
  Evidence: reader and scan tests (`tests/test_readers.py`, `tests/test_scans.py`).
- [x] Implement generations, the atomic active pointer, superseded-generation
  pruning, and independent step caches keyed by exact inputs and all relevant
  versions. Serialize CLI operations and recover locks after interruption
  (D8, D9, D33).
  Evidence: `tests/test_activation.py`, `tests/test_pipeline.py`, `tests/test_recovery_drills.py`.
- [x] Validate inventory/fingerprints at every activation, repeating the check
  when an earlier staged generation is forced; detected changes and incomplete
  scans cannot be force-activated. Apply the conservative replacement guard and
  first-scan rule, expose staging reasons, and support explicit valid
  activation. Removed files disappear from results only after activation
  (D10, D33, D35, D42).
  Evidence: `tests/test_activation.py`.
- [x] Implement derived/all erasure with typed confirmation for `erase all`.
  Preserve dictionary reviews and content-bound manual checks through derived
  erasure/pruning; label missing generated evidence unavailable (D11, D24, D33).
  Evidence: `tests/test_safety.py`.

## Phase 1: extraction and histories

- [x] Implement independent OCR/vision reads, quality-checked PDF text with OCR
  fallback, located laboratory association verification, and preserved conflicting
  readings. Keep missing fields/context explicit, flag uninterpreted handwriting,
  and warn about detected table-row extraction discrepancies (D4, D5, D28, D37).
  Evidence: `tests/test_pipeline.py`, `tests/test_extract.py`, `tests/test_ocr.py`, `tests/test_model_service.py`.
- [x] Extract raw laboratory values, comparators, qualitative values, units,
  intervals, flags, specimen, and role-aware dates. Extract explicitly supported
  specialty events with `direct`/`indirect`/`weak` evidence strength and grounded
  verification states; keep event-type disagreements as candidates and
  distinguish mentions and recommendations from encounters (D19–D21, D34, D36,
  D41).
  Evidence: `tests/test_extract.py`, `tests/test_normalize_dates.py`.
- [x] Seed the multilingual dictionary and groups; implement proposal review and
  durable rejection/reversal. Publish generated proposals only with their scan
  generation, erase them as derived data, and resolve current mappings at query
  time without changing source-verification states. Apply case-, Unicode-, and
  inflection-tolerant matching; inflection-only matches stay candidates
  (D15–D17, D29, D43).
  Evidence: `tests/test_dictionary.py`.
- [x] Implement deterministic laboratory and specialty histories with date-role
  ordering, precision/alternative-preserving filters, and excluded-undated counts.
  Separate candidates from confirmed results and retain differing units
  (D18–D21, D29, D34).
  Evidence: `tests/test_history.py`, `tests/test_latency.py`.
- [x] Sweep located text, tolerating case and inflection, for unmatched mentions
  even when the same page/image contains a matching fact (D43). Assemble relevant coverage warnings, including table
  discrepancies of unknown analyte relevance. Show scan timestamp, dictionary
  revision, evidence references, and careful empty-result wording (D18, D28).
  Evidence: `tests/test_history.py`.
- [x] Provide English terminal tables, evidence inspection, scan/status output,
  dictionary review, and local manual-check recording, including missed items
  without extracted facts. Choose exact CLI syntax during implementation
  (D23, D24, D30).
  Evidence: `tests/test_history.py`, `tests/test_safety.py`, CLI commands in README.

## Phase 1: delivery checks

- [x] Exercise the D26 synthetic format, hostile-input, semantic, recovery,
  activation, proposal-isolation, and unmatched-mention cases, plus the D41–D43
  event-grounding, forced-activation-after-change, and inflected-form cases.
  Verify read-only originals, isolated processing, and safe failure diagnostics,
  including Docker-retained logs (D25, D26, D38, D41–D44).
  Evidence: `tests/test_fixtures_hostile.py` plus the reader, extract, history, activation, dictionary, and safety suites.
- [x] Check representative query latency against the five-minute allowance on
  reference hardware using synthetic data during implementation. Document known
  limitations and deliver the CLI before user-led private-data review (D25, D37).
  Evidence: `tests/test_latency.py` (1,500 facts, queries well under a second); known limitations in README.

## Phase 2: harden the delivered capabilities

- [x] Verify repeatable installation and separate model provisioning on the
  reference environment; document the working setup.
  Evidence: `docker compose build --no-cache app`, `model-setup --verify`, and the full suite passed on the rebuilt image.
- [x] Exercise interruptions, failed/partial scans, cache reuse, activation,
  pruning, and erasure. Confirm preservation of the previous usable index and
  durable user decisions where required.
  Evidence: `tests/test_recovery_drills.py`, `tests/test_scans.py`, `tests/test_safety.py`.
- [x] Reverify read-only originals, local sensitive-data handling, and runtime
  egress control, including failure paths.
  Evidence: `tests/test_privacy.py`, `tests/test_recovery_drills.py`, `tests/test_deployment_policy.py`.
- [x] Centralize designated sanitized operational streams with the shared event
  fields, a simple local sink, bounded retention, and documented cleanup. Include
  app-managed logs in derived/all erasure; verify leakage prevention with
  synthetic diagnostics. Keep medical CLI output out of logs (D39).
  Evidence: `src/filebrownie/logs.py`, `tests/test_logging.py`.
- [ ] Evaluate extraction/retrieval against user-provided local manual checks
  after Phase 1 delivery. Record observed errors and omissions; percentages need
  a reviewed scope and denominator. This task depends on those checks becoming
  available and remains incomplete until then (D24, D25, D37).
  Ready for it: `check record|list|summary` count observed errors and omissions without
  percentages.
- [x] Measure query latency and optimize demonstrated bottlenecks within existing
  scope. Add degraded-image fixtures for skew, blur, low contrast, stamps, and
  partial handwriting, preserving prior regression coverage (D26).
  Evidence: measured (no bottleneck found); degraded fixtures in `tests/test_model_service.py` (skew left readings unverified; recorded as a known limitation, no deskew added).

Model/runtime choices, parser limits, and text-layer heuristics are recorded in the
architecture's implemented baseline; deskewing is a known gap. D36 and D41 stay provisional pending real-data review. Deferred features and logging integrations remain in the pivot's vNext
backlog; neither phase enables external telemetry.
