# FileBrownie project pivot

Decision date: 2026-10-03.

This document records the agreed product direction. It describes intended behavior,
not verified implementation status. Decisions resolved during the follow-up review
are recorded in [docs/decisions.md](docs/decisions.md), and the resulting design in
[docs/architecture.md](docs/architecture.md).

## Objective and priorities

Build a private, local tool for retrieving medical histories from one folder for
one user and one patient. Deliver Phase 1 and Phase 2 now; retain broader ideas in
vNext. Prioritize avoiding missed records, incorrect readings, and unsupported
claims over speed or breadth of features.

Keep the MVP simple. Resolve decisions now only when they block implementation
architecture or required correctness, privacy, and recovery. Choose ordinary
implementation details during development; move optional features and richer
workflows to vNext. This rule supersedes earlier optional Phase 1 conveniences
as recorded in D30. Phase 2 hardens the resulting scope without adding features.

Use a CLI first. MCP, conversational interfaces, and a browser UI are not required
for either current phase. Keep the application core independent of the interface.

## Operating constraints

- Run on Windows 11 with WSL and Docker. Reference hardware: 64 GB host RAM, of
  which WSL currently exposes 31 GB, and an RTX 3060 with 12 GB VRAM. Plan for
  31 GB unless a measured need justifies raising the WSL limit.
- Reference workload: approximately 120 files totaling 232 MB. Most files have ten
  pages or fewer, with rare exceptions around 100 pages.
- The folder holds one patient's records. Phase 1 performs no patient-identity
  checks.
- Support English, Russian, and Ukrainian source documents. CLI labels and
  explanations are English; preserve original labels and evidence.
- Mount original documents strictly read-only. Do not rename, rewrite, reorganize,
  delete, or extract archives into the source folder.
- Store generated text, OCR output, facts, indexes, and temporary data separately
  in local storage. Treat them as sensitive medical data.
- Do not commit or push PII, real records, identifying filenames, private expected
  answers, credentials, or sensitive generated data. Use synthetic examples.
- Use local inference only. No external model calls in Phases 1 and 2.
- Initial setup may download public model weights and dependencies. Medical
  processing subsequently runs without external network access. Provision model
  weights separately from the image build and record their versions.
- Network-enabled model setup mounts model storage only, with no source-document
  or generated-medical-data access. Processing services never download weights
  or OCR model files; missing ones fail with setup instructions (D38, D40).
- Operational logs contain counts, timings, opaque IDs, and sanitized error codes,
  excluding filenames/paths, medical text, prompts, and raw parser exceptions.
  Intentional local CLI/evidence output is separate from logs, including
  Docker-retained container logs (D38, D44).
- All runtime components are Docker Compose services. Install dependencies into
  images, use uv for Python dependencies, and keep pyproject.toml and uv.lock in
  sync. Run application commands, tests, linters, and migrations through Compose.
- Image builds must not depend on network access beyond package registries.
- Initial indexing may take overnight. Representative queries should finish within
  five minutes. Query-speed optimization belongs in Phase 2 where justified, with
  further optimization deferred to vNext if necessary.

## Phase 1: evidence-backed histories

### Ingestion

Manually scan one configured folder. Support JPG/JPEG and selectable-text or
scanned PDF, including printed text and tables in the three source languages.
TXT, CSV, XLSX, saved HTML, and ZIP processing move to vNext (D31). List other
formats as unsupported; do not parse them or inspect/expand archives.

Password-protected files, handwriting interpretation, and interpretation
of medical images are outside the initial scope. Printed and stamped content
on handwritten pages is still extracted as weaker evidence, and the page is flagged as containing
uninterpreted handwriting. Text reports about medical images are in scope. Never
execute document content or fetch linked web resources. Report unsupported content
and known extraction uncertainty when detected; detection is not guaranteed to be
complete.

Start with manual full rescans. Build a replacement index and switch to it only
after the scan completes. A completed scan may include explicit per-file failure
statuses; an interrupted scan must not replace the previous usable index. A
completed scan whose coverage regresses from the active index stays staged until
explicitly activated. Files removed from the source disappear from current results
after the next rescan is activated. A full rescan may reuse cached step outputs for
unchanged content. Historical retention policy and incremental scanning are
deferred.

Use a conservative replacement guard: for unchanged content, any new processing
failure, new coverage warning, or decrease in extracted facts keeps the scan
staged. Newly observed failed, partially processed, or unsupported content also
keeps replacements staged. Deliberate source removals do not count as regressions.
Show a diff summary; explicit activation can accept legitimate corrections.
The first scan activates when completed with some usable supported evidence,
showing all warnings; otherwise it stays staged. An empty medical history alone
does not mean processing failed. No tolerance thresholds are required (D35).

Run one CLI operation at a time; queries are unavailable during scans. The
source folder must stay unchanged while scanning. Validate its file inventory
and content fingerprints at every activation, including forced activation of an
earlier staged scan; detected changes prevent activation and preserve the
previous active index (D42). After the
folder is stable, a new scan may reuse valid cached work. Atomic input snapshots
and concurrent access are deferred (D33).

Parse supported files defensively, with bounded rendering/processing resources.
Write rendered pages and extracted data only into generated-data storage.

### Query contract

Provide two bounded query families, optionally filtered by date range:

1. Laboratory history by analyte, such as hemoglobin or iron-related measurements.
2. Visit history by specialty, such as urology or optometry.

Exact CLI syntax is an implementation decision, not an existing command contract.
Support lookup across English, Russian, and Ukrainian terminology, tolerating
case and inflected forms, while retaining source labels (D43). Return dated tables with references to the supporting evidence.

Terminology lookup uses a dictionary of canonical analytes, specialties, and named
groups. A curated seed is committed. Scans propose mappings for unmapped labels.
Unreviewed proposals belong to their scan generation and participate in lookup
when that generation is activated, with their results shown in a simple candidate
section until accepted. Interrupted or staged scans do not change active lookup.
Derived erasure removes generated proposals; accepted and rejected decisions
persist independently. Ambiguous labels keep all proposed
meanings as candidates; do not apply one ambiguous meaning globally. Rejected
label-to-concept pairs stay rejected across rescans and model upgrades unless
the user explicitly reverses the decision. Mapping is resolved at query time,
so review decisions take effect without a rescan. Richer review workflows and
contextual disambiguation belong to vNext (D29).

For laboratory results, preserve reported values, units, reference intervals, and
date meaning where available. Do not invent missing dates or units. Distinguish
specimen, encounter, and report dates where documented. When a document gives no
date, the row remains undated. Filename-date fallback and modification-time
hints move to vNext. Keep differing units visibly separate; automatic conversion
and advanced normalization are deferred.

Laboratory timelines prefer specimen date, then report date, then an unspecified
document date, showing the role and retaining other reported dates. Specialty
timelines use the represented event's date; planned appointment dates remain
labeled planned. Missing event dates remain missing. Month/year dates keep their
precision and match date filters by calendar-period overlap, labeled `may fall
within range`. Ambiguous/conflicting dates retain supported alternatives; if any
timeline-date alternative overlaps, show a date-uncertain candidate rather than
silently excluding the row or choosing a date. Undated rows are excluded from
date-filtered results, with their count reported (D34).

Visit history is a flat timeline of every specialty-related event: referral,
appointment scheduled or confirmed, encounter, procedure, result, discharge,
invoice, or other. Each event is typed and labeled with its evidence strength
and grounded verification state (D41).
Referrals and recommendations to arrange appointments must not become completed
visits. Show weaker or conflicting evidence with explicit labels.

Provisionally, produce one row per explicitly supported event; a document may
support several events with separate references. Letterhead, specialty lists,
and isolated stamps remain mention-only evidence unless an event is supported.
Explicit appointment recommendations are `other — recommendation`, preserving
their wording. Revisit these boundaries after real-document evaluation (D36).
Episode linking remains vNext.

Preserve ambiguous readings and conflicting dates or values rather than silently
resolving them. Advanced reconciliation and deduplication are deferred; visible
uncertainty is required now.

Do not rely solely on top-k similarity search to produce a complete history.
Query structured extracted records systematically, with source text available to
inspect supporting evidence. Queries run no model; a keyword sweep of the source
text reports mention locations not covered by a matching fact's evidence references.
One matching fact must not suppress other unmatched mentions on the same page or
image. This uses existing located evidence, not a region-level coverage subsystem
(D18). Scanned content is read independently by OCR and a vision model, and
verification requires located evidence for the complete analyte/value association
and any reported specimen, unit, and date with its role. A matching number alone
is insufficient. Missing fields stay missing. Readers' conflicting analyte
assignments appear as unresolved candidates with both interpretations, separately
from confirmed result rows. Readings without grounded associations remain
unverified. Source agreement does not prove clinical correctness, and the
architecture does not promise perfect extraction.

Accept undated/unverified candidates when a continuation page lacks usable date,
unit, or header context, with a `missing context` warning. Do not invent inherited
fields. Reliable automatic cross-page reconstruction remains vNext (D37).

### Coverage and uncertainty

Every answer includes the completed scan's timestamp and relevant coverage
warnings. Track failed, skipped, unsupported, partially processed, and uncertain
content. A successful parse does not prove that every fact was extracted.

Store one processing status plus a list of warnings per page/image/file, so
successful extraction, handwriting, and unreadable regions can coexist visibly.
Medical relevance belongs to interpretation, not the general evidence status.
If one PDF page fails, retain evidence and facts from successful pages and mark
the document partially processed. Failed-page warnings remain visible. The
existing scan-activation guard still governs whether these results become active
(D32). Unknown page counts remain unknown when a file cannot be opened.

When detected laboratory table rows lack corresponding structured results,
show a possible incomplete-extraction warning with a source reference, even if
the keyword sweep finds no additional mentions. Unknown analyte relevance must
not hide this warning from laboratory queries. Basic discrepancy visibility is
required in Phase 1; advanced reconstruction and recovery are deferred.

Do not describe an empty result as proof that an event never happened. Results
describe evidence in the indexed collection. Known gaps must qualify claims such
as all visits or complete history.

A dedicated review/marking workflow is deferred, but basic visibility into gaps
is mandatory in Phase 1. Provide an explicit way to erase generated local data
without touching source documents. Erasing derived data keeps user decisions such
as dictionary reviews and manual checks; erasing everything requires explicit
confirmation.

Manual checks retain a content hash, page/location, verdict, and note rather
than depending solely on disposable result IDs. A check can reference a missed
item in source evidence without an extracted fact. Checks survive generation
pruning and derived erasure; missing generated evidence is labeled unavailable
(D33).

### Acceptance

- Deliver Phase 1 implementation before private-original evaluation. The user
  then checks real data and sets follow-up tasks. The earlier ten-analyte/group
  and five-specialty sample is a suggested review outline, not a delivery gate.
  Keep CLI support for local error/omission records. Delivery does not establish
  extraction accuracy; error rates require a reviewed scope and denominator
  (D25, D37).
- Returned facts have inspectable source references at the best available
  granularity, such as page, image region, or text location.
- Known processing failures and relevant uncertainty appear in query output.
- Source files remain unchanged and are mounted read-only.
- Medical processing works without external network access.
- Representative queries meet the five-minute allowance on reference hardware.
- Synthetic regression cases check mechanics without committing private data,
  including hostile inputs and semantic traps such as referral versus visit and
  report versus specimen date.

## Phase 2: harden existing capabilities

No new user features. The target is reliable operation for one person on the
reference machine, not a claim of certification or readiness for a hosted service.

- Verify installation and model provisioning are repeatable.
- Test interruption recovery, failed scans, and replacement-index activation.
- Verify read-only originals, local sensitive-data handling, and runtime egress
  control. Basic protections already apply in Phase 1.
- Improve operational logging with a shared event schema and centralized local
  sink for designated sanitized streams, consistent fields, bounded retention,
  and synthetic leakage checks. Do not collect medical CLI output. Keep the
  logging boundary suitable for future JSON formatters, PII screening, and
  Sentry adapters; those integrations remain vNext. No external telemetry is
  enabled, and a separate logging service requires demonstrated need (D39).
- Evaluate extraction and retrieval against the user's local manual checks.
  Document observed omissions and reading errors; do not claim unmeasured accuracy.
- Measure query latency and optimize demonstrated bottlenecks without expanding
  product scope. Velocity means query speed, not development speed.
- Exercise meaningful failure paths and preserve synthetic regression coverage.
  Add degraded-image fixtures such as skew, blur, low contrast, stamps over text,
  and partial handwriting.

## Architecture and future flexibility

Use a modular application, with a local model service where needed. Additional
services require a demonstrated need; flexibility alone does not justify them.

Separate these responsibilities:

1. Ingestion: source discovery, file identity, and content fingerprints.
2. Evidence: extracted content, locations, provenance, and processing outcomes.
3. Interpretation: versioned, typed facts linked to evidence.
4. Query: laboratory and specialty-history operations over those facts.
5. Presentation: CLI output independent of extraction and storage internals.

The evidence foundation is general-purpose. Medical concepts belong in medical
extractors and query modules, not throughout document storage. Future identity,
insurance, and immigration interpretations should reuse the foundation.

Retain extractor and schema versions so interpretations can be regenerated and
changes traced. Preserve useful typed fields for reliable queries without trying
to anticipate a universal ontology. Avoid both rigid medical-only storage and an
unstructured-blob-only design.

PostgreSQL is the database, chosen so that vNext inferred structure stays
inspectable through a database console. Derived data is versioned by scan
generation behind a single active pointer. The OCR engine, vision model, and model
runtime are chosen during implementation using small synthetic fixtures and
hardware checks. No private-original demonstration is required before delivery;
the user tests real data afterward. Record selected weights and versions without
committing private inputs or results (D6, D37).

## vNext backlog

- Add JSON log formatters, richer PII screening, Sentry adapters, and further
  observability tooling over the Phase 2 centralized local logging foundation.
  Future integrations must preserve the no-PII-outside rule; compatibility is
  not authorization for external transmission. Screening is defense in depth,
  not permission to collect arbitrary medical payloads (D38, D39).
- Add TXT, CSV, XLSX, saved HTML, and ordinary ZIP support as needed. Preserve
  sheet/cell and archive-member evidence locations, bound archive expansion,
  and never execute macros/scripts or fetch linked resources. Add format-specific
  hostile-input tests when introducing those readers. Nested archives and
  password-protected content require separate scope decisions.
- Add optional CSV, JSON, and Markdown exports, user-decision backup/export,
  dictionary-seed promotion tools, and rendered evidence crops. MVP uses terminal
  tables and text evidence references.
- Add a standalone inventory command; MVP reports source/unit counts as part of
  scanning and status. Add a reusable synthetic-fixture generator; MVP retains
  small synthetic fixtures and essential failure/semantic checks.
- Add filename-date fallback and modification-time hints, with explicit inferred
  date labels. MVP leaves rows without document dates undated.
- Add `possible duplicate` heuristics for nonidentical content. MVP retains
  content-hash identity and all source locations, without semantic deduplication.
- Discover document categories and useful fields dynamically; this is the leading
  future structural priority.
- Link related documents and entities; choose useful answer/table structures;
  provide virtual folders or collections without modifying originals.
- Permit automatic initial inferred structure, inspectable through console/DB.
  Subsequent structural changes require human approval and versioned definitions.
  Model proposals must not execute arbitrary database schema changes.
- Support passports, OHIP cards, insurance records, IRCC documents, and other
  nonmedical domains.
- Support multiple patients, multiple folders, and additional sources such as
  email and Google documents. Verify the patient identity per document against a
  locally configured identity.
- Group related specialty events, such as referral, appointment, encounter, and
  results, into episodes.
- Add autoscan, incremental ingestion, and deliberate retention policies.
- Add concurrent access and more elaborate input snapshot handling if needed.
  Add richer evidence retention/restoration for permanent checks. MVP serializes
  CLI operations, detects source changes, and preserves checks without promising
  continued availability of erased evidence.
- Add richer fact-change comparisons or configurable activation policies if
  the conservative MVP guard proves too coarse. Successful activation must
  still not be treated as proof of accurate or complete extraction.
- Add MCP, other interfaces, and broader natural-language questions.
- Provide a factual, source-cited family-doctor summary over a chosen period;
  richer links to symptoms/treatments and medical interpretation come later.
- Add document marking/review for parsing failures and OCR errors, corrections,
  advanced conflict reconciliation, and deduplication.
- Add finer region-level coverage tracking and interactive coverage overlays
  when needed. MVP uses page/file processing statuses and lists of warnings.
- Improve verification with richer table-layout reconstruction and cross-page
  header/context association, measured against source-grounded examples. Basic
  row association checks and visible unresolved candidates remain Phase 1.
- Improve omission detection beyond keyword sweeps and basic detected-row
  discrepancies, and recover partially extracted tables. Measure correlated
  reader errors and the limits of agreement before strengthening accuracy claims.
- Add broader model benchmarks, sampling tools, and automated evaluation
  workflows when real-data findings justify them. MVP uses synthetic mechanics
  checks during development and user-led real-data review after delivery.
- Explore context-sensitive terminology mappings using specimen, method, and
  surrounding labels, with richer per-record disambiguation and review workflows.
  MVP keeps unresolved meanings as candidates and preserves rejected proposals.
- Add automatic unit normalization and more complex extraction as evidence of
  need emerges. Reprioritize difficult formats based on actual Phase 1 results.
- Consider further query-speed improvements after measuring the current workflow.
- Add an optional vLLM serving backend for the vision model (OpenAI-compatible, so
  the same client contract applies) when higher scan throughput or batching is
  worth the heavier runtime. Phase 1 uses a llama.cpp server with a quantized
  model that fits the 12 GB reference GPU. A new backend keeps weights provisioned
  through the isolated setup service, the pinned image, the internal network, and
  the sanitized-log boundary, and changes the step-cache version so cached output
  is never silently reused across runtimes.
- External model exceptions remain deferred. Any future proposal must preserve
  the no-PII-outside rule and require human confirmation for every external call;
  approval alone does not authorize sending PII.

## Documentation reconciliation

Completed on 2026-10-03. The repository contained no code. The passport-oriented
project plan and Phase 1 technology decisions were deleted, and Git history
preserves them. No capability described here is implemented yet.
