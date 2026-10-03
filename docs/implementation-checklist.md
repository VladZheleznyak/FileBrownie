# Phase 1 and Phase 2 implementation checklist

This checklist tracks implementation of the [project pivot](../PROJECT_PIVOT_2026-10-03.md),
[decisions](decisions.md), and [architecture](architecture.md). It adds no product
scope and does not replace those documents. All items start unchecked because
no capability is implemented yet. Mark an item complete only with implementation
and relevant validation evidence; synthetic success does not establish medical
extraction accuracy. Run application commands and checks through Docker Compose.

## Phase 1: runtime and privacy

- [ ] Create Compose services for the CLI/OCR application, PostgreSQL, and local
  vision inference. Install dependencies in images using uv; commit synchronized
  `pyproject.toml` and `uv.lock`. Builds access package registries only (D7, D14).
- [ ] Choose and record a working OCR/vision/runtime baseline using synthetic
  English, Russian, and Ukrainian fixtures and basic reference-hardware checks.
  Plan for 31 GB WSL RAM and 12 GB VRAM; no private-original or formal benchmark
  gate applies (D3, D6, D37).
- [ ] Provision versioned model weights and OCR model files through separate
  network-enabled setup with model storage only. Mount model storage read-only
  into `app` and the model service. Processing never downloads weights or OCR
  files and reports missing ones as a setup error. Fit co-resident models in
  12 GB of VRAM or run OCR on the CPU (D13, D38, D40).
- [ ] Mount originals read-only; store generated medical data outside originals
  and Git. Enforce runtime egress isolation for processing services and refuse
  scans when outbound access is reachable (D12, D13).
- [ ] Allow only sanitized operational diagnostics, including third-party service
  diagnostics. Keep medical CLI/evidence output separate from logs (D38). Disable
  Docker log capture for the CLI service, run it with `run --rm`, and configure
  PostgreSQL and the model server to omit values, prompts, and responses (D44).
  Centralized logging is a Phase 2 task.

## Phase 1: ingestion, storage, and recovery

- [ ] Discover PDF and JPG/JPEG inputs; report unsupported files without parsing
  them or expanding archives. Report supported-file, page/image, and unsupported
  counts. Bound PDF/image processing resources (D1, D26, D31).
- [ ] Implement domain-independent content identity, source locations, units,
  located text, processing status, and concurrent warnings. Retain all source
  paths for byte-identical content; preserve successful pages when others fail
  and unknown page counts when enumeration fails (D22, D32).
- [ ] Implement generations, the atomic active pointer, superseded-generation
  pruning, and independent step caches keyed by exact inputs and all relevant
  versions. Serialize CLI operations and recover locks after interruption
  (D8, D9, D33).
- [ ] Validate inventory/fingerprints at every activation, repeating the check
  when an earlier staged generation is forced; detected changes and incomplete
  scans cannot be force-activated. Apply the conservative replacement guard and
  first-scan rule, expose staging reasons, and support explicit valid
  activation. Removed files disappear from results only after activation
  (D10, D33, D35, D42).
- [ ] Implement derived/all erasure with typed confirmation for `erase all`.
  Preserve dictionary reviews and content-bound manual checks through derived
  erasure/pruning; label missing generated evidence unavailable (D11, D24, D33).

## Phase 1: extraction and histories

- [ ] Implement independent OCR/vision reads, quality-checked PDF text with OCR
  fallback, located laboratory association verification, and preserved conflicting
  readings. Keep missing fields/context explicit, flag uninterpreted handwriting,
  and warn about detected table-row extraction discrepancies (D4, D5, D28, D37).
- [ ] Extract raw laboratory values, comparators, qualitative values, units,
  intervals, flags, specimen, and role-aware dates. Extract explicitly supported
  specialty events with `direct`/`indirect`/`weak` evidence strength and grounded
  verification states; keep event-type disagreements as candidates and
  distinguish mentions and recommendations from encounters (D19–D21, D34, D36,
  D41).
- [ ] Seed the multilingual dictionary and groups; implement proposal review and
  durable rejection/reversal. Publish generated proposals only with their scan
  generation, erase them as derived data, and resolve current mappings at query
  time without changing source-verification states. Apply case-, Unicode-, and
  inflection-tolerant matching; inflection-only matches stay candidates
  (D15–D17, D29, D43).
- [ ] Implement deterministic laboratory and specialty histories with date-role
  ordering, precision/alternative-preserving filters, and excluded-undated counts.
  Separate candidates from confirmed results and retain differing units
  (D18–D21, D29, D34).
- [ ] Sweep located text, tolerating case and inflection, for unmatched mentions
  even when the same page/image contains a matching fact (D43). Assemble relevant coverage warnings, including table
  discrepancies of unknown analyte relevance. Show scan timestamp, dictionary
  revision, evidence references, and careful empty-result wording (D18, D28).
- [ ] Provide English terminal tables, evidence inspection, scan/status output,
  dictionary review, and local manual-check recording, including missed items
  without extracted facts. Choose exact CLI syntax during implementation
  (D23, D24, D30).

## Phase 1: delivery checks

- [ ] Exercise the D26 synthetic format, hostile-input, semantic, recovery,
  activation, proposal-isolation, and unmatched-mention cases, plus the D41–D43
  event-grounding, forced-activation-after-change, and inflected-form cases.
  Verify read-only originals, isolated processing, and safe failure diagnostics,
  including Docker-retained logs (D25, D26, D38, D41–D44).
- [ ] Check representative query latency against the five-minute allowance on
  reference hardware using synthetic data during implementation. Document known
  limitations and deliver the CLI before user-led private-data review (D25, D37).

## Phase 2: harden the delivered capabilities

- [ ] Verify repeatable installation and separate model provisioning on the
  reference environment; document the working setup.
- [ ] Exercise interruptions, failed/partial scans, cache reuse, activation,
  pruning, and erasure. Confirm preservation of the previous usable index and
  durable user decisions where required.
- [ ] Reverify read-only originals, local sensitive-data handling, and runtime
  egress control, including failure paths.
- [ ] Centralize designated sanitized operational streams with the shared event
  fields, a simple local sink, bounded retention, and documented cleanup. Include
  app-managed logs in derived/all erasure; verify leakage prevention with
  synthetic diagnostics. Keep medical CLI output out of logs (D39).
- [ ] Evaluate extraction/retrieval against user-provided local manual checks
  after Phase 1 delivery. Record observed errors and omissions; percentages need
  a reviewed scope and denominator. This task depends on those checks becoming
  available and remains incomplete until then (D24, D25, D37).
- [ ] Measure query latency and optimize demonstrated bottlenecks within existing
  scope. Add degraded-image fixtures for skew, blur, low contrast, stamps, and
  partial handwriting, preserving prior regression coverage (D26).

Model/runtime choices, parser limits, text-layer heuristics, and cache artifact
layout, OCR GPU placement, and the inflection-matching method remain
implementation decisions. D36 and D41 stay provisional pending real-data review. Deferred features and logging integrations remain in the pivot's vNext
backlog; neither phase enables external telemetry.
