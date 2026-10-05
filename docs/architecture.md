# FileBrownie architecture

This document describes the Phase 1 architecture. It follows
[the project pivot](../PROJECT_PIVOT_2026-10-03.md) and
[the decision record](decisions.md), cited below as D-numbers. The pipeline is
implemented and passes synthetic checks; it has not been evaluated on real
documents. [Implemented baseline](#implemented-baseline) records the choices
made during implementation. Synthetic success is not a claim of extraction
accuracy, and D36/D41 remain provisional.

The MVP stays simple (D30). Only architectural and essential correctness,
privacy, and recovery choices require advance decisions. Optional conveniences
and richer workflows belong to vNext.

## Runtime layout

All runtime components are Docker Compose services.

| Service | Role | Network |
| --- | --- | --- |
| `app` | CLI, scan pipeline, OCR engine, queries | Internal only |
| `db` | PostgreSQL holding generations, cache index, dictionary, and user decisions | Internal only |
| model service | Local vision model on the GPU; working baseline chosen during implementation (D6, D37) | Internal only |
| setup profile | Downloads public model weights into model storage only | Has network access; no source or generated-medical-data mounts |

The processing network is declared `internal: true`. A scan refuses to start
if outbound access is reachable (D13).

Mounts:

- The source folder is mounted read-only.
- The generated-data directory lives on the host outside the repository. It
  holds the database files, the step cache, and page rasters (D11, D12).
  Evidence crops and optional exports are deferred to vNext (D30).
- The model storage volume is mounted read-only into the model service and
  into `app` for OCR model files (D40).

Processing services, including the OCR engine, never download weights or model
files. Missing weights produce a clear setup error; the setup service has no
access to originals or medical outputs (D38, D40). `app` gets GPU access only
if the chosen OCR engine needs it, and models resident together must fit in
12 GB of VRAM (D40).

## Operational logging

Phase 1 logs allow counts, timings, opaque IDs, and sanitized error codes only.
Exclude filenames, paths, medical text, prompts, and raw parser exceptions,
including third-party/model/database diagnostics (D12, D38). Intentional local
CLI tables and evidence snippets remain product output; do not route CLI stdout
into an operational log collector. Sanitize before persistence.

Docker's own log capture is inside this boundary (D44). The CLI service
disables it and runs as one-off `docker compose run --rm` containers.
PostgreSQL and the model server are configured to omit statements, parameters,
value-bearing error details, prompts, and responses; a service that cannot
meet D38 has its Docker log capture disabled too.

Phase 2 centralizes designated sanitized streams in a shared operational event
schema and a local file sink (D39). Common fields identify opaque runs/generations,
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
| App-managed operational logs | Disposable, bounded retention (D39) | `erase derived`, `erase all` |
| User decisions | Permanent until erase all | `erase all` only |

User decisions are dictionary reviews and manual acceptance checks.

Generated mapping proposals are derived data owned by a scan generation. Only
the active generation's proposals participate in query lookup, alongside the
committed seed and durable accepted/rejected decisions. Activation publishes
facts and proposals together through the active-generation pointer; interrupted
or staged scans do not change lookup. Pruning and derived erasure remove generated
proposals without deleting review decisions. Reused cached proposals remain
subject to current decisions, including durable rejections (D15, D29).

Manual checks retain a content hash, page/location, verdict, and note, with an
optional result ID. They remain independent of disposable generation rows.
After pruning or derived erasure, missing generated evidence is shown as
unavailable; an unchanged path cannot reconnect a check to different content
(D33). Checks and notes are sensitive local user data.

## Operation consistency

One application-wide exclusive lock serializes CLI operations across processes
and containers (D14, D33). A second operation reports busy; queries cannot run
during an overnight scan. The lock is released on interruption. Activation and
pruning therefore cannot race an active query. Concurrent access is vNext.

The folder must stay unchanged during scanning. Validate the discovered file
inventory and content fingerprints at every activation: when the scan finishes
for automatic activation, and again when an earlier staged generation is
forced (D42). A detected change leaves the active generation untouched and
makes the candidate ineligible for forced activation. A fresh scan may reuse valid cache entries after the folder is
stable. This mechanism detects changes; it does not provide an atomic snapshot
of a folder that another application is modifying (D33).

For replacement scans, compare unchanged content at unit/file granularity.
A new processing failure, new coverage warning, or fewer extracted facts keeps
the generation staged. Newly observed failed/partial/unsupported content also
keeps it staged; removed sources do not count as regressions. Show triggering
references and changes so an intentional correction can be explicitly accepted
(D10, D35). No tolerance thresholds or probabilistic diff system are required.

A first scan can activate after completion with some usable supported evidence,
showing all warnings. Without usable supported evidence it stays staged. The
absence of medical facts alone does not fail a scan. All activation paths still
require source consistency and a completed scan (D33, D35).

## Responsibilities

The five responsibilities from the pivot map onto modules. Only the
interpretation and query modules know medical concepts.

1. **Ingestion:** discovers sources and records file identity and content hashes.
   Supports PDF and JPG/JPEG; records other formats as unsupported (D31).
2. **Evidence:** splits content into units, then produces located text, records
   processing outcomes, and stores one processing status plus a list of warnings
   per unit/file (D32). It is domain-independent.
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

Every expensive step reads from and writes to the step cache (D9). Steps run
sequentially in one CLI process (D14). An interruption leaves the generation
staged, and the next full scan reuses valid cached step outputs. Cache identity
includes the exact input bytes and all output-affecting step versions, including
model, prompt, and configuration (D9, D33).

Document content is never executed and linked resources are never fetched.
Archives are recorded as unsupported without inspecting or expanding members.
Rendering and parsing supported inputs use bounded resources; artifacts stay
in generated storage. Other readers and archive processing are vNext (D31).

## Evidence model

The evidence layer is domain-independent.

- **Source item:** a file path and content hash.
  Byte-identical content becomes one content record that lists every source
  item containing it (D22). Archive-member identities are deferred to vNext.
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
  layer assembles both kinds into coverage output (D32).

Medical facts reference text spans and carry extractor and schema versions, so
they can be regenerated and traced.

If some PDF pages succeed and another fails, retain evidence and facts from the
successful pages and mark the document partially processed. Keep the failed
page's status and warnings visible. If pages cannot be enumerated, record the
file-level failure and unknown page coverage. These results become queryable
only when their generation is activated under the existing guard (D10, D32).
No region-level coverage subsystem is required for the MVP.

## Fact model

Lab result, per D20 and D21:

- the raw label
- the raw value, parsed number, and comparator
- the unit and reference interval as written, and any flag
- the specimen as written, when reported
- raw dates with role, precision, supported alternatives, and source locations
- verification state: `verified`, `unverified reading`, or `conflicting`
- references to evidence spans

Specialty event, per D19, D21, and D41:

- the raw specialty label and event type
- source wording for recommendations represented as `other — recommendation`
- evidence strength: `direct`, `indirect`, or `weak`
- verification state: `verified`, `unverified reading`, or `conflicting`
- dates with their metadata
- the document class and references to evidence spans

Provisionally, a document can yield multiple explicitly supported specialty
events, each with its own evidence references (D36). Letterhead, specialty
lists, and isolated stamps remain mention-only evidence unless an event is
supported. Relevant keyword-sweep hits expose those mentions without creating
visit rows. Recommendations are not promoted to referrals, scheduled
appointments, or encounters without evidence of those events. Revisit these
boundaries after real-document evaluation; episode linking stays in vNext.

Typed columns hold fields used for filtering and ordering, such as dates and
raw labels. Canonical terms are resolved through the current dictionary at
query time (D16); stored canonical assignments are not authoritative.
Type-specific detail goes in JSONB.

Timeline dates follow D34. Laboratory rows prefer specimen, then report, then
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
value, and any reported specimen, unit, and date with its role (D4, D28).
Locations must establish that these fields belong together, using row/region
relationships and applicable headers. Finding a matching number elsewhere in
the unit is insufficient. Missing fields remain explicitly missing; reported
fields that cannot be grounded keep the reading unverified or conflicting.
Rows without document dates stay undated unless D45 supplies a single agreed
filename-inferred laboratory timeline (labeled in query output, never verified).

Continuation pages whose dates, units, or headers require unavailable cross-page
context may yield undated/unverified candidates with a `missing context` warning.
Keep their raw evidence; do not infer inherited fields solely to produce a
confirmed row. Reliable automatic cross-page reconstruction remains vNext
(D37).

Specialty events follow the same grounding rule (D41). A `verified` event has
located evidence for the raw specialty label, the wording that establishes the
event type, and any reported event date with its role. A specialty word
elsewhere on the page is insufficient. Reader disagreement on event type or
specialty becomes an unresolved candidate and never a confirmed encounter.
Evidence strength (`direct`, `indirect`, `weak`) is recorded separately from
verification.

Text normalization covers decimal separators, whitespace, Cyrillic/Latin
homoglyphs, case folding, Unicode forms, apostrophe variants, and Russian
`ё`/`е`; original strings and locations are retained. Lookup and the keyword
sweep also tolerate Russian and Ukrainian inflection (D43). Normalization does
not establish field associations or resolve ambiguous terminology by itself.

When readers disagree on which analyte owns a value, both interpretations and
their evidence are retained as an unresolved candidate. Relevant queries show
the candidate separately from confirmed rows, without selecting a reading.
Verification describes source agreement, not clinical correctness.

During scanning, detected laboratory table rows without corresponding structured
results produce a possible incomplete-extraction warning with a source reference
(D28). These warnings do not depend on keyword-sweep hits. If the affected
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

No model runs at query time (D18). An empty result is reported as "no evidence
in the indexed collection", never as proof that an event did not happen.

Candidate separation uses existing dictionary and verification states (D29),
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
- **Activation and erasure**: `active_generation` is a single-row pointer. Erase scopes follow
  D11; manual checks have no foreign keys into derived tables.
- **Isolation checks**: route check, an active payload-free connect probe to public addresses,
  Compose policy tests, and `doctor` for read-only mounts and readiness.
- **Logging**: `logs.py` writes allow-listed events with allow-listed fields to a size-rotated
  file under generated data, with an age limit; erase removes it (D39). No logging framework
  or JSON formatter; those are vNext integrations.

## Open implementation decisions

- Whether to revisit the OCR engine, vision model, and runtime after real-data
  review (D6, D37); the baseline is recorded above.
- Deskewing for tilted scans, which currently produce unverified readings.
- Full-pipeline parsing/rendering limits; the first PDF/JPEG readers have bounded
  defaults documented in the README.
- The text-layer quality heuristics (D4).

Ordinary parameters above are implementation choices, not product-interview
blockers. The architectural interview is resolved for the MVP, with D36 and
D41 provisional pending real-data review; see
[the decision record](decisions.md#architectural-interview-status).
No implementation or model-quality claim follows from agreement on the plan.

## Delivery and validation

Phase 1 implementation delivery includes required mechanics, synthetic regression
checks, private local processing, and visible known limitations. It does not
require a private-original demonstration or formal multi-candidate benchmark.
The user checks real data after delivery and sets follow-up tasks (D25, D37).
Do not equate delivery or synthetic test success with measured extraction
accuracy. Real-data results and manual checks remain sensitive local data.
