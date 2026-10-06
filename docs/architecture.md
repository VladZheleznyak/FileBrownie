# FileBrownie architecture

FileBrownie is a private, local CLI for laboratory and specialty-visit histories
from one folder of documents. Phase 1 delivers the pipeline; Phase 2 hardens
operation without adding user features. Open work is listed in
[todo.md](todo.md).

The pipeline is implemented and passes synthetic checks; it has not been
evaluated on real documents. [Implemented baseline](#implemented-baseline)
records runtime choices. Synthetic success is not a claim of extraction
accuracy. Specialty-event boundaries stay provisional until
user-led real-document review.

The MVP stays simple. Only architectural and essential correctness,
privacy, and recovery require fixed rules up front. Optional conveniences and
richer workflows belong in [Not in the current phases](#not-in-the-current-phases).

## Scope and constraints

- **Audience:** one user, one patient folder; Phase 1 performs no patient-identity
  checks.
- **Inputs:** selectable-text or scanned PDF and JPG/JPEG only. Other formats
  are listed as unsupported; archives are not expanded. Password-protected PDFs,
  handwriting interpretation, and medical-image interpretation are out of scope.
  Printed or stamped text on handwritten pages is extracted as weaker evidence
  with a handwriting flag.
- **Outputs:** two query families—laboratory history by analyte or group, visit
  history by specialty or group—with source references and visible uncertainty.
  CLI labels and explanations are English; source labels and evidence keep
  English, Russian, and Ukrainian text.
- **Privacy:** originals are read-only and never modified. Generated medical data
  lives outside the repository and Git. Medical processing uses local inference
  only; setup may download public weights into isolated model storage.
- **Environment:** Docker Compose on WSL; reference hardware is 31 GB WSL RAM and
  12 GB VRAM. Reference workload is on the order of 120 files and hundreds of
  pages; overnight indexing is acceptable. Representative queries should finish
  within five minutes on reference hardware.
- **Delivery:** Phase 1 and Phase 2 mechanics are in place. Accuracy is validated
  only through user-led review and local manual checks—not through synthetic tests
  alone.

## Runtime layout

All runtime components are Docker Compose services.

| Service | Role | Network |
| --- | --- | --- |
| `app` | CLI, scan pipeline, OCR engine, queries | Internal only |
| `db` | PostgreSQL holding generations, cache index, dictionary, and user decisions | Internal only |
| model service | Local vision model on the GPU; working baseline chosen during implementation | Internal only |
| setup profile | Downloads public model weights into model storage only | Has network access; no source or generated-medical-data mounts |

The processing network is declared `internal: true`. A scan refuses to start
if outbound access is reachable.

Mounts:

- The source folder is mounted read-only.
- The generated-data directory lives on the host outside the repository. It
  holds the database files, the step cache, and page rasters.
  Evidence crops and optional exports are deferred to vNext.
- The model storage volume is mounted read-only into the model service and
  into `app` for OCR model files.

Processing services, including the OCR engine, never download weights or model
files. Missing weights produce a clear setup error; the setup service has no
access to originals or medical outputs. `app` gets GPU access only
if the chosen OCR engine needs it, and models resident together must fit in
12 GB of VRAM.

## Operational logging

Phase 1 logs allow counts, timings, opaque IDs, and sanitized error codes only.
Exclude filenames, paths, medical text, prompts, and raw parser exceptions,
including third-party/model/database diagnostics. Intentional local
CLI tables and evidence snippets remain product output; do not route CLI stdout
into an operational log collector. Sanitize before persistence.

Docker's own log capture is inside this boundary. The CLI service
disables it and runs as one-off `docker compose run --rm` containers.
PostgreSQL and the model server are configured to omit statements, parameters,
value-bearing error details, prompts, and responses; a service that cannot
meet that rule has its Docker log capture disabled too.

Phase 2 centralizes designated sanitized streams in a shared operational event
schema and a local file sink. Common fields identify opaque runs/generations,
components, steps, severity, counts/timings, and safe error codes. A separate
Compose logging service needs demonstrated need. App-managed logs stay outside
Git and originals, use bounded retention, and are included in derived/all
erasure. Leakage prevention is checked with synthetic errors and diagnostics;
this is not a claim of forensic erasure or removal of external backups.

This boundary supports future JSON formatters, PII-screening stages, and Sentry
adapters without building those integrations now. Current sanitization does not
depend on future screening. Sentry and richer tooling remain vNext; both current
phases retain no external telemetry or runtime egress.

## Data classes

| Class | Lifetime | Erased by |
| --- | --- | --- |
| Derived, per generation | Published on activation; superseded generations are pruned | `erase derived`, `erase all` |
| Step cache | Shared by generations, keyed by content hash and step version | `erase derived`, `erase all` |
| App-managed operational logs | Disposable, bounded retention | `erase derived`, `erase all` |
| User decisions | Permanent until erase all | `erase all` only |

User decisions are dictionary reviews and manual acceptance checks.

Generated mapping proposals are derived data owned by a scan generation. Only
the active generation's proposals participate in query lookup, alongside the
committed seed and durable accepted/rejected decisions. Activation publishes
facts and proposals together through the active-generation pointer; interrupted
or staged scans do not change lookup. Pruning and derived erasure remove generated
proposals without deleting review decisions. Reused cached proposals remain
subject to current decisions, including durable rejections.

Manual checks retain a content hash, page/location, verdict, and note, with an
optional result ID. They remain independent of disposable generation rows.
After pruning or derived erasure, missing generated evidence is shown as
unavailable; an unchanged path cannot reconnect a check to different content. Checks and notes are sensitive local user data.

## Operation consistency

One application-wide exclusive lock serializes CLI operations across processes
and containers. A second operation reports busy; queries cannot run
during an overnight scan. The lock is released on interruption. Activation and
pruning therefore cannot race an active query. Concurrent access is vNext.

The folder must stay unchanged during scanning. Validate the discovered file
inventory and content fingerprints at every activation: when the scan finishes
for automatic activation, and again when an earlier staged generation is
forced. A detected change leaves the active generation untouched and
makes the candidate ineligible for forced activation. A fresh scan may reuse valid cache entries after the folder is
stable. This mechanism detects changes; it does not provide an atomic snapshot
of a folder that another application is modifying.

For replacement scans, compare unchanged content at unit/file granularity.
A new processing failure, new coverage warning, or fewer extracted facts keeps
the generation staged. Newly observed failed/partial/unsupported content also
keeps it staged; removed sources do not count as regressions. Show triggering
references and changes so an intentional correction can be explicitly accepted. No tolerance thresholds or probabilistic diff system are required.

A first scan can activate after completion with some usable supported evidence,
showing all warnings. Without usable supported evidence it stays staged. The
absence of medical facts alone does not fail a scan. All activation paths still
require source consistency and a completed scan.

## Responsibilities

PostgreSQL holds generations, the step cache, dictionary data, and user decisions
so derived structure stays inspectable through a database console. Extractor
and schema versions are stored with facts so interpretations can be regenerated.

1. **Ingestion:** discovers sources and records file identity and content hashes.
   Supports PDF and JPG/JPEG; records other formats as unsupported.
2. **Evidence:** splits content into units, then produces located text, records
   processing outcomes, and stores one processing status plus a list of warnings
   per unit/file. It is domain-independent.
3. **Interpretation:** produces versioned, typed medical facts linked to evidence
   spans, namely lab results and specialty events, and classifies documents.
   Medical relevance and medical extraction warnings belong here, not in the
   evidence layer's processing status.
4. **Query:** resolves terms through the dictionary and reads facts from the
   active generation. Runs the keyword sweep and assembles coverage warnings.
5. **Presentation:** renders CLI tables and shows evidence.

A unit is a PDF page or a JPG/JPEG image. Whole-file failures and unsupported
files are recorded even when no units can be discovered.

## Scan pipeline

```text
discover source folder
  -> identify supported files, hash content; report unsupported files
  -> split into units
  -> located text per unit:
       PDF text layer, quality-checked
       or OCR with coordinates                                  (evidence)
  -> vision-model structured extraction of the unit image       (interpretation)
  -> verify: ground row associations in located evidence      (interpretation)
  -> detect table/row extraction discrepancies, record warnings
  -> classify document; derive lab results and specialty events
  -> propose dictionary mappings for unmapped labels, owned by this generation
  -> record unit processing statuses and warnings; summarize file coverage
  -> validate source inventory and fingerprints; block activation if changed
  -> finalize generation -> activation guard -> activate or stay staged
```

Every expensive step reads from and writes to the step cache. Steps run
sequentially in one CLI process. An interruption leaves the generation
staged, and the next full scan reuses valid cached step outputs. Cache identity
includes the exact input bytes and all output-affecting step versions, including
model, prompt, and configuration.

Document content is never executed and linked resources are never fetched.
Archives are recorded as unsupported without inspecting or expanding members.
Rendering and parsing supported inputs use bounded resources; artifacts stay
in generated storage. Other readers and archive processing are vNext.

## Evidence model

The evidence layer is domain-independent.

- **Source item:** a file path and content hash.
  Byte-identical content becomes one content record that lists every source
  item containing it. Archive-member identities are deferred to vNext.
- **Unit:** an addressable part of the content, such as a PDF page number or image.
- **Text span:** located text within a unit, with a bounding box or character
  offset, and the reader that produced it.
- **Processing outcome:** a record of step, version, status, and any error for a unit.
- **Processing status:** one per unit/file, describing whether required steps
  finished, partially finished, failed, were skipped, or are unsupported.
  Finished processing does not establish complete fact extraction.
- **Warnings:** a list linked to unit/file references, so handwriting and an
  unreadable region can coexist with successfully extracted content. Evidence
  warnings describe general reading/processing limitations. Medical relevance
  and possible missing laboratory rows are interpretation outputs; the query
  layer assembles both kinds into coverage output.

Medical facts reference text spans and carry extractor and schema versions, so
they can be regenerated and traced.

If some PDF pages succeed and another fails, retain evidence and facts from the
successful pages and mark the document partially processed. Keep the failed
page's status and warnings visible. If pages cannot be enumerated, record the
file-level failure and unknown page coverage. These results become queryable
only when their generation is activated under the existing guard.
No region-level coverage subsystem is required for the MVP.

## Fact model

A laboratory result stores:

- the raw label
- the raw value, parsed number, and comparator
- the unit and reference interval as written, and any flag
- the specimen as written, when reported
- raw dates with role, precision, supported alternatives, and source locations
- verification state: `verified`, `unverified reading`, or `conflicting`
- references to evidence spans

A specialty event stores:

- the raw specialty label and event type (referral, appointment scheduled or
  confirmed, encounter, procedure, result, discharge, invoice, recommendation,
  or other)
- source wording for recommendations represented as `other — recommendation`
- evidence strength: `direct`, `indirect`, or `weak`
- verification state: `verified`, `unverified reading`, or `conflicting`
- dates with their metadata
- the document class and references to evidence spans

Visit history is a flat timeline of specialty-related events, not episodes.
Referrals and recommendations to arrange appointments must not become completed
visits. Provisionally, a document can yield multiple explicitly supported
specialty events, each with its own evidence references. Letterhead,
specialty lists, and isolated stamps remain mention-only evidence unless an
event is supported. Relevant keyword-sweep hits expose those mentions without
creating visit rows. Recommendations are not promoted to referrals, scheduled
appointments, or encounters without evidence of those events. Revisit these
boundaries after real-document evaluation; episode linking stays in vNext.

Typed columns hold fields used for filtering and ordering, such as dates and
raw labels. Canonical terms are resolved through the current dictionary at
query time; stored canonical assignments are not authoritative.
Type-specific detail goes in JSONB.

Timeline dates keep their roles. Laboratory rows prefer specimen, then report, then
unspecified document dates; the chosen role and other reported dates remain
visible. Specialty rows use their event date, with appointment dates labeled
planned. Missing event dates stay missing even when a report date is known.

Keep month/year precision and all supported date alternatives. Deterministic
date filters test overlap with possible calendar periods, labeling partial
dates `may fall within range`. Ambiguous/conflicting timeline dates that may
match appear as date-uncertain candidates. Filter bounds do not become asserted
source dates. Undated rows are excluded from filtered results, with their count
reported. Advanced date reconciliation is vNext.

## Verification

The verifier grounds the complete laboratory association: raw analyte label,
value, and any reported specimen, unit, and date with its role.
Locations must establish that these fields belong together, using row/region
relationships and applicable headers. Finding a matching number elsewhere in
the unit is insufficient. Missing fields remain explicitly missing; reported
fields that cannot be grounded keep the reading unverified or conflicting.
Rows without document dates stay undated unless filename inference supplies a
single agreed laboratory timeline, labeled in query output and never verified.

Continuation pages whose dates, units, or headers require unavailable cross-page
context may yield undated/unverified candidates with a `missing context` warning.
Keep their raw evidence; do not infer inherited fields solely to produce a
confirmed row. Reliable automatic cross-page reconstruction remains vNext.

Specialty events follow the same grounding rule. A `verified` event has
located evidence for the raw specialty label, the wording that establishes the
event type, and any reported event date with its role. A specialty word
elsewhere on the page is insufficient. Reader disagreement on event type or
specialty becomes an unresolved candidate and never a confirmed encounter.
Evidence strength (`direct`, `indirect`, `weak`) is recorded separately from
verification.

Text normalization covers decimal separators, whitespace, Cyrillic/Latin
homoglyphs, case folding, Unicode forms, apostrophe variants, and Russian
`ё`/`е`; original strings and locations are retained. Lookup and the keyword
sweep also tolerate Russian and Ukrainian inflection. Normalization does
not establish field associations or resolve ambiguous terminology by itself.

When readers disagree on which analyte owns a value, both interpretations and
their evidence are retained as an unresolved candidate. Relevant queries show
the candidate separately from confirmed rows, without selecting a reading.
Verification describes source agreement, not clinical correctness.

During scanning, detected laboratory table rows without corresponding structured
results produce a possible incomplete-extraction warning with a source reference. These warnings do not depend on keyword-sweep hits. If the affected
analyte is unknown, laboratory queries still show the warning. Detection is
limited: no warning does not establish that all rows were extracted. Advanced
layout reconstruction and recovery remain in vNext.

## Query flow

```text
query term (any of the three languages)
  -> dictionary resolution: seed, durable reviews, active-generation proposals, groups
       normalized exact matches resolve; inflection-only matches are candidates
  -> facts in the active generation matching the resolved raw labels
  -> optional date filter: role-aware overlap, preserving supported alternatives
  -> keyword sweep of located text for the same synonyms, inflection-tolerant:
       report mention locations not covered by matching facts' evidence references
       a matching fact does not suppress other mentions on the same page/image
  -> assemble output:
       dated rows, then undated rows
       markers: auto-mapped, unverified, conflicting; event evidence strength
       simple candidate section for unreviewed, ambiguous, or inflection-only
         terminology, unresolved reading associations and event types,
         and date uncertainty, separate from confirmed rows
       date roles, reported precision, other dates, and partial-date overlap labels
       coverage warnings: failed, skipped, unsupported, and partially processed
         files/units; uninterpreted handwriting;
         partial table extraction; sweep hits without facts;
         unreviewed mappings involved
       scan timestamp and dictionary revision
```

No model runs at query time. An empty result is reported as "no evidence
in the indexed collection", never as proof that an event did not happen.

Candidate separation uses existing dictionary and verification states,
without a new service or richer review workflow. Rejected label-to-concept pairs
remain excluded across rescans and model upgrades until explicitly reversed.
Terminology acceptance does not change a reading's source-verification state.

## Illustrative CLI surface

Implemented commands (run through `docker compose run --rm app filebrownie ...`):

```text
model-setup [--verify]         provision / verify pinned model files (setup profile)
doctor                         isolation, read-only mounts, readiness
migrate | status [--database]
scan                           full scan, OCR + vision + verify + guarded auto-activation
scan --readers-only | inventory [--save] | validate <gen> | read <src>
activate [--force] <generation>
labs <analyte|group> [--from --to]
visits <specialty|group> [--from --to]
evidence fact <ref> | evidence show <ref> | evidence generation <gen>
dict review | accept | reject | reverse <label> <concept>
check record --fact <ref> --verdict V [--note]
check record --source <path> --page <n> --verdict missed [--note]
check list | check summary
erase derived | erase all
```

## Implemented baseline

Decisions the earlier sections left open, as built:

- **OCR**: Tesseract 5 (`eng+rus+ukr`, tessdata_best 4.1.0) on the CPU. Rasters are upscaled
  for recognition, words are merged into cell-like spans, and boxes are mapped back to the
  stored raster. Language data is provisioned into model storage; the engine is in the image.
- **Vision**: Qwen2.5-VL-7B-Instruct Q4_K_M with a Q8_0 projector behind a llama.cpp server
  (digest-pinned image, internal network, offline, `--log-disable`), called through an
  OpenAI-compatible API with schema-constrained JSON at temperature 0. The client treats the
  reply as an untrusted claim: output is bounded and validated, then grounded against located
  text. vLLM is a vNext option.
- **Provisioning**: `models_manifest.json` pins URL, size and SHA-256 per file. Only the
  setup service downloads. Processing verifies (checksums for OCR data, size for multi-GB
  weights) and otherwise reports a setup error.
- **Cache**: one `step_cache` table keyed by step, content hash, format, unit, raster hash and
  step version (model, prompt, schema, configuration). Damaged entries are recomputed; failed
  or invalid output is never cached. Reader artifacts keep their file-based cache.
- **Text-layer gate**: PDF text with `OCR_REQUIRED` or `TEXT_LAYER_LOW_QUALITY` falls back to
  OCR; both text sources feed the same grounding step.
- **Inflection matching**: a small deterministic suffix stemmer (Russian, Ukrainian, English
  plurals) on NFKC/case/`ё`/apostrophe/homoglyph-normalized tokens. Inflection-only matches are
  candidates. Dictionary proposals come from unmapped labels containing a seed term.
- **Activation and erasure**: `active_generation` is a single-row pointer. Erase scopes follow the table above; manual checks have no foreign keys into derived tables.
- **Isolation checks**: route check, an active payload-free connect probe to public addresses,
  Compose policy tests, and `doctor` for read-only mounts and readiness.
- **Logging**: `logs.py` writes allow-listed events with allow-listed fields to a size-rotated
  file under generated data, with an age limit; erase removes it. No logging framework
  or JSON formatter; those are vNext integrations.

## Not in the current phases

Deferred product scope (vNext unless noted):

- JSON log formatters, richer PII screening, Sentry adapters, and further
  observability over the centralized local logging foundation—without external
  telemetry or sending medical payloads off the machine.
- TXT, CSV, XLSX, saved HTML, and ZIP readers with bounded expansion; format-specific
  hostile-input tests; nested archives and password-protected content as separate
  decisions.
- CSV, JSON, and Markdown exports; decision backup/export; dictionary-seed
  promotion; rendered evidence crops.
- Reusable synthetic-fixture generator; modification-time date hints; semantic
  duplicate heuristics beyond content-hash identity.
- Dynamic document categories and fields; document linking; virtual folders;
  inferred structure with human-approved schema changes.
- Nonmedical domains (identity, insurance, immigration); multiple patients and
  folders; additional sources (email, cloud documents) with per-document identity
  checks.
- Specialty episode grouping; autoscan; incremental ingestion; retention policies;
  concurrent CLI access; richer activation policies and fact diffs.
- MCP, browser UI, conversational interfaces, and broad natural-language questions.
- Family-doctor summaries; marking/review workflows for OCR failures; advanced
  conflict reconciliation and deduplication.
- Region-level coverage overlays; richer table reconstruction, cross-page header
  association, table recovery, and omission detection beyond keyword sweeps and
  basic row-discrepancy warnings.
- Model benchmarks, sampling tools, and automated evaluation pipelines.
- Context-sensitive terminology mappings and richer dictionary review workflows.
- Automatic unit normalization; optional vLLM vision backend; any external model
  calls (each requires explicit human confirmation and the no-PII-outside rule).

Phase 2 hardening without new features—repeatable install, recovery drills,
egress and read-only reverification, centralized sanitized logging, latency
measurement, and degraded-image fixtures—is implemented. Manual-check evaluation
against real documents remains open; see [todo.md](todo.md).

## Open implementation decisions

- Whether to revisit the OCR engine, vision model, and runtime after real-data
  review; the baseline is recorded above.
- Deskewing for tilted scans, which currently produce unverified readings.
- Full-pipeline parsing/rendering limits; the first PDF/JPEG readers have bounded
  defaults documented in the README.
- The text-layer quality heuristics.

Ordinary parameters above are implementation choices, not product blockers. Specialty-event
boundaries stay provisional until real-document review. Follow-up work is ordered
in [todo.md](todo.md).

## Delivery and validation

Delivery includes required mechanics, synthetic regression checks, private local
processing, and visible known limitations. It does not require a private-original
demonstration or formal multi-candidate benchmark. Do not equate delivery or
synthetic test success with measured extraction accuracy. Real-data results and
manual checks remain sensitive local data and stay out of Git.
