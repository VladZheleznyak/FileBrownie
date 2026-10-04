# FileBrownie decision record

Decisions resolved on 2026-10-03 while reviewing
[the project pivot](../PROJECT_PIVOT_2026-10-03.md). They refine the pivot and
take precedence where the pivot left a choice open. They describe intended
behavior. Verified implementation status and test evidence are tracked in
[implementation-checklist.md](implementation-checklist.md), not in this file.

The architecture that follows from these decisions is in
[architecture.md](architecture.md).

---

## Corpus and environment

### D1 - Corpus assumptions

About 120 files totaling 232 MB. Most files have ten pages or fewer, with rare
exceptions around 100 pages, so the corpus is estimated at 500 to 1,500 pages.
An overnight scan therefore allows roughly 20 to 60 seconds of model time per page.

Expected sources:

- Scanned and photographed Ukrainian and Russian lab printouts and visit documents.
- Patient-portal exports, which may be HTML, CSV, XLSX, or text PDF. Only PDF
  is supported in the MVP; the other formats are reported as unsupported and
  deferred to vNext (D31). The actual format distribution is not yet measured.
- Substantial handwritten content.
- Phone photos of paper, which may be skewed or poorly lit.

North American documents and non-medical files are not expected. The scanner
still classifies anything unexpected as `other` instead of assuming it cannot occur.

The Phase 1 scan reports supported file, PDF page, image, and unsupported-file
counts, so the estimate is replaced by a measurement. A standalone inventory
command is deferred to vNext (D30).

### D2 - One patient, no identity checks

The folder is assumed to contain only one patient's records. Phase 1 performs
no patient-name or date-of-birth matching. Per-document patient verification
moves to vNext.

### D3 - Memory budget

Plan for the 31 GB that WSL currently exposes, not the 64 GB host total. Raise
the WSL limit only if a measured need appears. The 12 GB of VRAM is the tighter
constraint in practice.

---

## Extraction and verification

### D4 - Two independent reads with verification

Scanned pages and images are read twice:

1. An OCR engine produces text with coordinates.
2. A local vision model extracts structured rows from the page image.

A laboratory row is `verified` only when located source evidence supports the
complete association: the raw analyte label, value, and any reported specimen,
unit, and date with its role. A matching number elsewhere on the page is not
sufficient. Missing fields remain explicitly missing. Otherwise the reading is
`unverified reading` or, when the readers disagree, `conflicting`. D28 defines how
association conflicts appear in queries.

PDF text layers provide located text directly and replace OCR in step 1.
A text layer is checked for garbled encoding and falls back to OCR when it
fails. Native spreadsheet and other text-format paths are deferred (D31).

Reason: agreement between readers, anchored to source locations and the correct
row and context, helps detect both wrong numbers and wrong associations. Reader
agreement is a source-grounding status, not proof of clinical correctness or
complete extraction.

Rejected: a vision model alone (no grounding, page-level references only) and
OCR followed by a text model (grounded, but OCR errors propagate unchecked).

### D5 - Handwriting

Handwriting is not interpreted. Printed and stamped content on a handwritten
page, such as letterhead, stamps, printed dates, and specialty names, is
extracted as weaker evidence. The page carries the warning
`handwritten content not interpreted` alongside its processing status (D32).

### D6 - OCR engine and vision model selection

Neither is chosen yet. Select the local OCR engine, vision model, and runtime
during implementation using small synthetic fixtures and hardware smoke checks.
Compare candidates only as needed to choose a working baseline; a formal
multi-candidate bake-off or demonstration on private originals is not a
prerequisite for Phase 1 delivery (D37). Record selected model weights and
versions. The user checks real data after implementation delivery and defines
follow-up tasks; private inputs and results remain local.

---

## Storage and scan lifecycle

### D7 - PostgreSQL

Use PostgreSQL as a Compose service. The deciding reason is vNext: inferred
structure must be inspectable through a console or database client, and JSONB
suits that. The pgvector extension is not required.

### D8 - Scan generations

Every derived row carries a `scan_generation_id`. A one-row active-generation
pointer is switched in a single transaction after a scan completes. Queries
read only the active generation. Superseded generations are pruned afterwards.
An interrupted scan leaves its generation staged and never activates it.

All CLI operations are serialized in the MVP (D33), so a query cannot race
activation or pruning. A scan with a detected source change is invalid for
activation; the prior active generation remains usable after the scan exits.

Page-level failures do not discard successful pages from the same document.
Their evidence and facts stay in the generation, with partial document coverage
and failed-page warnings, subject to the activation guard (D32).

### D9 - Per-step cache

Expensive step outputs are cached by content hash plus step version. A full
rescan reuses cached OCR and model outputs for unchanged content, and changing
the interpretation step does not redo OCR. An interrupted scan resumes from the
cache. The cache is independent of generations. Cached outputs must refer to
the exact input bytes processed. Step versions cover the implementation, model,
prompt, and configuration that affect an output; unchanged file paths alone
are not cache identities (D33).

This is memoization within a full rescan, not incremental scanning, which
remains deferred.

### D10 - Activation guard

A completed replacement scan activates automatically only if the simple D35
guard passes. For unchanged content, a new processing failure, a new coverage
warning, or fewer extracted facts keeps the generation staged. Newly observed
files/content that fail, are partially processed, or are unsupported also keep
it staged. Removed source files disappear normally and do not count as regressions.

Staged scans show a diff summary and require explicit forced activation. A
first scan activates automatically when it completes with some usable supported
evidence, showing all warnings; without that evidence it stays staged. An empty
medical history alone is not a scan failure. Source consistency must pass.
Forced activation overrides the coverage guard only; it cannot activate an
interrupted or source-changed scan (D33).

### D11 - Data classes and erase scopes

| Class | Examples | Regenerable | Removed by |
| --- | --- | --- | --- |
| Derived | Evidence, facts, coverage, generated mapping proposals, per generation | Yes | Erase derived, erase all |
| Cache | OCR output, model output, page rasters | Yes | Erase derived, erase all |
| Operational logs | Sanitized diagnostics, bounded retention (D39) | No, disposable | Erase derived, erase all |
| User decisions | Dictionary reviews, manual acceptance checks | No | Erase all only |

`Erase all` requires typed confirmation. Dedicated user-decision export/backup
tools are deferred to vNext (D30). No erase operation touches source documents.

### D12 - Sensitive data handling

Generated data lives in a host directory outside the repository and is ignored
by Git. Logs carry operational metadata only, never medical content. At-rest
protection relies on host disk encryption such as BitLocker. Application-level
encryption is not planned for Phase 1.

Operational logs allow counts, timings, opaque IDs, and sanitized error codes;
exclude filenames, paths, medical text, prompts, and raw parser exceptions.
Intentional local CLI/evidence output is distinct from logging (D38). Phase 2
centralizes the sanitized logging stream locally (D39).

### D13 - Network egress

Processing services run on a Compose network declared `internal: true`, with no
outbound route. A separate setup profile with network access downloads model
weights. A scan refuses to start if outbound network access is reachable from
the processing services.

The setup service mounts model storage only, without source documents or
generated medical data. Processing services do not download weights; missing
weights produce a clear setup error (D38).

### D14 - Processing model

A single CLI process runs the scan and runs GPU work sequentially. All CLI
operations share an exclusive application lock: a second operation reports
that the application is busy, including a query during a scan (D33). No job
queue service is added. Concurrent access is vNext.

---

## Terminology

### D15 - Analyte and specialty dictionary

The dictionary maps source labels in English, Russian, and Ukrainian to
canonical analytes and specialties, and defines groups such as an iron panel
(ferritin, serum iron, TIBC, transferrin saturation).

- A curated seed dictionary is committed. Analyte and specialty names are not PII.
- The dictionary is stored in the database alongside its revisions.
- During a scan, unmapped labels produce automatic mapping proposals owned by
  that generation. They participate in lookup when the generation is activated;
  interrupted or staged scans do not change active lookup. Resulting rows appear
  in a simple candidate section labeled `auto-mapped, unreviewed` until accepted
  (D29).
- Generated proposals are derived data: pruning their generation or running
  `erase derived` removes them. The committed seed and durable accepted/rejected
  decisions remain independent. Cached proposals reused in a later scan are
  subject to the current review decisions, including durable rejections.
- A review command accepts a proposal, promoting it to curated status, or
  rejects it. Decisions are user data and persist across rescans and
  `erase derived`.
- Ambiguous labels retain alternative candidates rather than applying one
  meaning globally. Rejected label-to-concept pairs persist across rescans and
  model upgrades until explicitly reversed (D29).
- Export tools to promote reviewed generic entries into the committed seed
  file are deferred to vNext (D30).

Reason: automatic proposals protect recall, while labels and review stop a
wrong mapping, such as `Fe` mapped to ferritin instead of serum iron, from
spreading silently.

### D16 - Mapping at query time

Facts store the raw source label. Mapping to canonical terms is resolved at
query time against the current dictionary, so an accepted or rejected mapping
takes effect without a rescan. Query output reports the dictionary revision used.

### D17 - Specialty granularity

Canonical specialties are fine-grained. Named query groups combine them, for
example eye care is optometry plus ophthalmology. Every row shows the source
specialty label, so the difference stays visible.

---

## Query semantics

### D18 - Deterministic queries

No model runs at query time. A query resolves terms through the dictionary,
reads typed facts from the active generation, and runs a keyword sweep. The
sweep searches located source text for the resolved synonyms and reports mention
locations not covered by a matching fact's evidence references. One matching fact
must not suppress other unmatched mentions on the same page or image. Use the
existing located evidence; this does not require a region-level coverage subsystem.
The five-minute budget is a ceiling for testing, not a design target.

The sweep cannot reveal terms missed by the text reader. Independently of sweep
hits, queries show relevant warnings when detected laboratory table rows do not
all have corresponding structured results (D28). No query-time model is added.

### D19 - Specialty history as a flat event timeline

Create one row per explicitly supported specialty event, not one row per
document or specialty mention. A document may support multiple events with
separate evidence references. The event types are referral, appointment
scheduled, appointment confirmed, encounter, procedure, result, discharge,
invoice, and other. These MVP rules are provisional pending real-document
evaluation (D36).

Each row shows its event type and evidence strength. Referrals and appointment
records are never presented as completed encounters. Grouping related events
into episodes is document linking and belongs to vNext.

Letterhead, specialty lists, and isolated stamps are mention-only evidence
unless the document supports an event. This also applies to pages dominated
by uninterpreted handwriting. An explicit recommendation to arrange an
appointment is `other — recommendation`, preserving its wording; do not
upgrade it to a referral, scheduled appointment, or encounter without evidence
for that specific event (D36).

Text reports about imaging, such as an ultrasound conclusion, are evidence.
Interpreting the images themselves is out of scope.

### D20 - Lab values

Each value keeps the raw string, a parsed number when one exists, a comparator
for values such as `<5`, the unit as written, the reference interval as
written, any abnormality flag, and qualitative results such as `negative`.
Differing units for one analyte remain separate.

### D21 - Dates

Each date records:

- its role: specimen, report, encounter, event, or unspecified; event dates
  retain their event type and whether they refer to a planned appointment
- its precision: day, month, or year
- its source location, raw reading, and any supported ambiguous/conflicting
  alternatives

Laboratory timelines use specimen date, then report date, then an unspecified
document date, with the selected role visible. Specialty timelines use the
date of the event; planned appointment dates remain labeled planned. Preserve
other reported dates as well. Dates with different roles are not conflicts
merely because they differ (D34).

Month/year dates retain their precision and match a date filter when their
possible calendar period overlaps the requested range, labeled `may fall
within range`. Ambiguous or conflicting dates retain all supported alternatives;
when any alternative overlaps, show the row as a date-uncertain candidate
rather than selecting a date or silently excluding it (D34).

When the document gives no date, the row stays in a separate `undated` section.
Filename-date fallback and modification-time hints are deferred to vNext (D30).
Undated rows are excluded from date-filtered results, but their count is reported.

### D22 - Duplicates

Byte-identical content (same hash) becomes one evidence item that lists every
source location. Archive-member identity belongs to vNext (D31). Copies that
are not identical remain separate rows. `Possible duplicate` heuristics and advanced deduplication
are deferred to vNext (D30).

---

## Output, acceptance, and testing

### D23 - Output and evidence inspection

Query output is a terminal table. An evidence command shows the source path,
PDF page or image region, and the located text snippet. CSV, JSON, and
Markdown exports and rendered evidence crops are deferred to vNext (D30).

### D24 - Manual acceptance checks

CLI commands record checks against query results or evidence references, for
example `correct`, `wrong value`, or `missed`. Checks are stored as user
decisions in the database. A check carries a content hash, page/location,
verdict, and note, with a result reference when one exists (D33). A source
reference can record a missed item without requiring an extracted result ID.
Checks survive derived-data erasure and generation pruning; unavailable
generated evidence is labeled unavailable. They are local sensitive data.
After delivery the user checks real data and defines follow-up tasks (D37).
Phase 2 reports observed errors; percentages require an explicit reviewed scope
and denominator. No pre-delivery sampling protocol or accuracy claim is required.

### D25 - Phase 1 delivery and subsequent real-data review

Deliver the implemented Phase 1 CLI with its required behavior, synthetic
regression checks, privacy constraints, and visible known limitations. The user
then checks real data and sets follow-up tasks; private-original evaluation is
not a prerequisite for implementation delivery (D37). Do not describe delivery
as proof of extraction accuracy or user acceptance.

The earlier sample of ten analytes, including a group, and five specialties is
a suggested review outline, not a delivery gate. The user determines the actual
post-delivery review scope. Phase 1 has no numeric accuracy bar, and measured
error rates require documented denominators.

### D26 - Synthetic fixtures

Phase 1 includes the following:

- Small synthetic English, Russian, and Ukrainian lab reports and specialty
  documents exercising supported formats. A reusable fixture generator is
  deferred to vNext (D30).
- Hostile inputs: malformed or oversized PDF/images, password-protected PDFs,
  embedded scripts or remote-resource references that must not execute/fetch,
  and text that tries to instruct the model. Verify that other formats, including
  ZIP, are reported as unsupported without parsing or expansion. Archive,
  spreadsheet, and HTML reader-specific adversarial fixtures move to vNext (D31).
- Semantic traps: referral versus completed visit, report date versus specimen
  date, differing units for one analyte, ambiguous dates, conflicting values,
  a missing reference interval, the same value in different analyte rows,
  disagreement on analyte assignment, and partially extracted laboratory tables.
  Date filtering covers month/year overlap and conflicting alternatives that
  fall on opposite sides of a filter boundary; no exact day is invented.
  Specialty cases include multiple distinct events in one document, incidental
  specialty mentions without events, and appointment recommendations that must
  not become referrals, scheduled appointments, or completed encounters (D36).
- Coverage/recovery mechanics: a PDF with successful and failed pages retains
  successful evidence and facts, reports partial document coverage, and still
  obeys the generation activation guard. Multiple warnings can coexist on a page.
- Scan consistency: concurrent CLI operations are refused, detected source
  changes block activation even with force, and manual checks survive derived
  erasure and pruning with missing evidence explicitly marked unavailable.
- Activation: new failures/warnings or fewer facts for unchanged content keep
  replacement scans staged; newly unsupported/failed/partial files do likewise.
  Intentional removals do not regress coverage. A first scan with usable supported
  evidence can activate even when no medical facts were extracted (D35).
- Lookup isolation: generated proposals from interrupted or staged scans do not
  change active lookup; activation publishes that generation's proposals.
  Derived erasure removes proposals while preserving accepted/rejected decisions,
  and cached proposals cannot restore a rejected pair (D15, D29).
- Keyword sweep: two relevant mentions on one page/image, with a matching fact
  for only one, still expose the unmatched mention (D18).

Phase 2 adds degradations such as skew, blur, low contrast, stamps over text,
and partial handwriting regions.

### D27 - Superseded documentation

The passport-oriented project plan and Phase 1 technology decisions were
deleted. Git history preserves them. The repository contained no code, so no
implementation audit was required.

### D28 - Row associations, unresolved candidates, and completeness warnings

Confirmed during the architectural interview on 2026-10-03:

- Verification checks the full laboratory association described in D4. Evidence
  references identify the row and its applicable context, including a header
  when the specimen, unit, or date is shared by multiple rows. An absent field
  is not invented; a reported field whose association cannot be grounded keeps
  the reading unverified or conflicting. Filename-date fallback is deferred
  to vNext (D30); rows without document dates stay undated.
- When readers assign a value to different analytes, retain both interpretations
  as one unresolved candidate with their respective source references. Show it
  separately from confirmed result rows in queries for either proposed analyte;
  do not silently choose an interpretation or present both as confirmed results.
  Automatic conflict reconciliation remains deferred.
- When a detected laboratory table has rows without corresponding structured
  results, record a possible incomplete-extraction warning with the table's
  source reference. Show it in the query output even when the keyword sweep
  finds no additional mentions. If the affected analyte cannot be determined,
  retain the warning for laboratory queries rather than assuming irrelevance.
- A warning signals a detected discrepancy, not a known number of missed
  results. Failure to detect a discrepancy does not prove completeness. Basic
  discrepancy visibility belongs in Phase 1; advanced layout reconstruction
  and extraction recovery belong in vNext.

### D29 - Minimal terminology candidates and durable rejections

Confirmed during the architectural interview on 2026-10-03:

- Unreviewed mappings appear automatically in a simple candidate section of
  query output, using proposal status to separate rows. This is presentation
  over the existing dictionary, not an additional review system. If richer
  interaction is needed, defer it to vNext rather than expanding the MVP.
- For an ambiguous label, retain alternative label-to-concept proposals and
  show relevant evidence as candidates in either proposed concept's query.
  Do not apply a single ambiguous meaning globally without review. Reviewed
  mappings use the existing accept/reject commands; context-sensitive rules
  and per-record disambiguation workflows remain deferred.
- Rejection belongs to the specific label-to-concept pair and persists across
  rescans and model upgrades. A different concept may be proposed; a rejected
  pair cannot be reactivated without an explicit user reversal. `Erase derived`
  preserves these decisions; confirmed `erase all` removes them.
- Terminology review and source verification are distinct. A verified reading
  with an unreviewed mapping is still a terminology candidate; accepting a
  mapping cannot make an unverified or conflicting reading verified.

### D30 - Simple MVP and architecture-focused interview

The user's instruction on 2026-10-03 is to keep the MVP simple and move decisions
that do not block architecture to vNext. This refines earlier Phase 1 choices.

Keep the two history query families, source grounding and references, visible
uncertainty, minimal dictionary decisions, manual acceptance records, safe scan
activation and recovery, read-only originals, and local private processing.
Keep the existing modular boundaries; do not build extension frameworks for
future domains. Phase 2 hardens these same capabilities.

Defer optional output formats, evidence crops, dedicated decision backup/export
and seed-promotion tools, a standalone inventory command, a reusable fixture
generator, filename-date fallback, modification-time hints, and nonidentical
duplicate heuristics to vNext. Scans/status still expose counts, rows without
document dates remain undated, and small synthetic correctness/failure fixtures
remain required. Input scope was subsequently narrowed to PDF and JPG/JPEG
in D31.

Basic row-association verification, unresolved candidates, and warnings about
detected extraction discrepancies remain required. Advanced reconstruction,
automatic reconciliation, contextual mappings, and richer review workflows stay
in vNext. If grounding is not possible, show uncertainty rather than requiring
an advanced subsystem to make the MVP appear complete.

Interview only choices that affect architecture or required correctness,
privacy, and recovery. Ordinary implementation parameters can be selected during
development, measured, and adjusted without further product interviews.

### D31 - PDF and JPG first

The user's assent on 2026-10-03 is taken as approval of the recommended PDF/JPG
starting scope. Phase 1 and Phase 2 support selectable-text and scanned PDF,
and JPG/JPEG images, in English, Russian, and Ukrainian. This scope choice
does not establish that the real folder contains no other formats.

TXT, CSV, XLSX, saved HTML, and ZIP readers move to vNext. Discover and report
unsupported files so the reduced format scope remains visible in coverage;
do not parse those files or inspect/expand archive members. Preserve generic
file/content/unit/evidence boundaries without implementing future readers.

Source inspection is by PDF page or image region/text location. Scans report
supported-file, page/image, and unsupported-file counts. PDF/image failure and
semantic fixtures remain in Phase 1; future reader-specific hostile-input
fixtures move with their readers to vNext. Do not infer successful coverage
for deferred formats from a completed scan.

### D32 - Processing status, warnings, and partial document results

Confirmed during the architectural interview on 2026-10-03:

- Store one processing status and a list of warnings per unit/file. Status
  describes processing completion; warnings preserve concurrent limitations,
  such as handwriting and unreadable regions. Finishing the required steps
  does not prove every fact was extracted.
- Keep medical relevance, including `no relevant content`, in the medical
  interpretation layer. It is not a domain-independent evidence status and
  does not establish that an event never occurred. Medical extraction warnings
  remain interpretation outputs; queries assemble them with evidence warnings.
- A failed page does not discard evidence and facts from successful pages.
  Mark the document partially processed and retain the failed page's status
  and warnings. If the reader cannot enumerate pages, record a file failure
  and unknown page coverage rather than inventing a page count.
- Partial results are eligible for queries only after their generation is
  activated under D10. This decision does not relax the activation guard or
  make staged results visible as active results.
- Use page/file statuses and warning references in the MVP. Finer region-level
  coverage tracking and interactive overlays are vNext.

### D33 - Serialized operations, source consistency, and durable checks

Confirmed during the architectural interview on 2026-10-03:

- Run one CLI operation at a time. Queries are unavailable during scans; a
  second operation reports busy. Use one application-wide lock across CLI
  containers/processes; no queue or concurrency subsystem is required. A
  terminated operation must not leave a stale lock that prevents recovery.
- The source folder must remain unchanged for the duration of a scan. Record
  the discovered file inventory and content fingerprints and validate them
  before activation. Detected additions, removals, or changed content prevent
  activation; leave the active generation untouched. This is change detection,
  not an atomic filesystem snapshot or a guarantee against every external edit.
- A source-changed generation cannot be activated through the coverage-guard
  override. Run a fresh full scan after the folder is stable; reuse only cached
  work whose input bytes and step versions still match. This also applies to
  the first scan. Ordinary parser failures follow D32 and D10, not this rule.
- Manual checks store a content hash, page/location, verdict, and note, with
  an optional result ID. They may reference source evidence even when no fact
  was extracted. Keep them independent of disposable derived rows so pruning
  and `erase derived` cannot delete them. Mark unavailable generated evidence
  explicitly; never reattach a check to changed content merely because a file
  path is unchanged. Confirmed `erase all` removes the checks.
- Concurrent queries/scans, elaborate input snapshots, and richer retention
  or evidence-restoration workflows move to vNext.

### D34 - Timeline date roles and uncertainty-preserving filters

Confirmed during the architectural interview on 2026-10-03:

- Laboratory timelines use specimen date first, report date second, and an
  unspecified document date third. Show the selected role and retain other
  reported dates. Different roles can legitimately have different dates.
- Specialty timelines use the date of the represented event. A planned
  appointment date remains labeled planned; a report date must not become an
  unsupported encounter date. If the event date is unavailable, preserve the
  known dates with their roles and keep the event undated.
- Month/year dates keep their reported precision. Use the corresponding
  calendar period for overlap filtering, with `may fall within range` shown
  in filtered output. Bounds used by the filter are not invented source dates
  and must not be displayed as an exact reported day.
- Preserve all supported alternatives for an ambiguous or conflicting date.
  If any alternative for the timeline role overlaps the requested range, show
  a date-uncertain candidate separately from confirmed result rows. The
  uncertainty is visible even if every alternative is inside the range. Do
  not use a lower-priority laboratory date role to hide an ambiguous specimen
  date or silently resolve the conflict.
- Retain raw dates, roles, precision, alternatives, and evidence references
  during scanning; filtering is deterministic and invokes no model. Rows with
  no usable timeline date stay undated; date-filtered queries exclude them
  but report their count (D21). Advanced reconciliation remains vNext.

### D35 - Conservative activation without tolerance thresholds

Confirmed during the architectural interview on 2026-10-03:

- Compare completed replacement scans against the active generation for
  unchanged source content. Any new processing failure, new coverage warning,
  or decrease in extracted fact count keeps the replacement staged. Compare
  located unit/file outcomes and raw extracted facts, not query-time dictionary
  mappings. Do not let improved extraction elsewhere hide a local regression.
- Newly observed files/content, including a changed file with a new content
  hash, keep a replacement staged when failed, partially processed, or
  unsupported. Deliberate removals between scans do not count as regressions;
  removed sources disappear after activation. Changes during a scan remain
  invalid for activation under D33.
- A stage summary identifies triggering source references and changes. Fewer
  facts can reflect a legitimate correction, and a new warning can reflect
  better detection; explicit activation permits the user to accept either.
  No probabilistic comparison or configurable tolerance threshold is required.
- For the first scan, activate automatically after completion if some usable
  supported evidence exists, retaining all warnings. Without usable supported
  evidence, leave it staged and explain why. Do not require medical facts:
  an empty laboratory or specialty history is not itself scan failure.
- The override applies only to valid completed scans. Interrupted and
  source-changed scans cannot be force-activated (D33). This guard detects
  visible regressions; equal counts or successful activation do not prove
  unchanged extraction accuracy or completeness.
- Richer fact-change comparisons and configurable activation policies are
  vNext if experience demonstrates a need.

### D36 - Provisional specialty-event boundaries

Approved for now during the architectural interview on 2026-10-03; revisit if
evaluation on real documents demonstrates a problem. These rules refine D19.

- Create one row per explicitly supported event, with evidence references for
  that event. A document describing a referral and a later encounter produces
  two rows. Episode linking remains vNext.
- Letterhead, lists of available specialties, and isolated stamps do not prove
  an event. Preserve them as located mention-only evidence, surfaced by the
  keyword sweep when relevant, rather than manufacturing visit rows. Mostly
  handwritten pages follow the same rule; visible printed text is still
  extracted and uninterpreted handwriting remains a coverage warning.
- Record an explicit recommendation to arrange a specialty appointment as
  `other — recommendation`, retaining its wording. Only evidence of the
  specific event permits a referral, scheduled appointment, or encounter type.
- Preserve weak or conflicting readings with explicit labels. More elaborate
  specialty-event inference, episode reconstruction, and review tools remain
  deferred. This provisional decision does not weaken source grounding.

### D37 - Missing cross-page context and review after implementation

Resolved during the architectural interview on 2026-10-03:

- Accept that continuation pages can lack dates, units, or headers in the MVP.
  Keep resulting readings undated or unverified as appropriate, with a located
  `missing context` warning. Preserve raw evidence without inventing inherited
  fields or treating an ambiguous association as confirmed. Reliable automatic
  cross-page reconstruction remains vNext.
- The user declined the proposed private-original feasibility demonstration
  before the full pipeline. Their instruction is read as: deliver Phase 1
  implementation first, then the user checks real data and sets new tasks.
  This supersedes the earlier pre-delivery real-page bake-off/review gates.
- Select a working local OCR/vision/runtime baseline during implementation with
  synthetic fixtures and basic hardware checks. Keep required mechanics and
  failure tests; do not add a formal benchmark program or real-data gate.
- Delivery is an implementation milestone, not evidence of measured medical
  extraction accuracy. Record known gaps and preserve local manual-check
  support. Real-data findings determine follow-up tasks; Phase 2 still hardens
  the agreed capabilities without adding features. Broader benchmarking,
  sampling tools, and automated evaluation workflows remain vNext.

### D38 - Sanitized logs and isolated model provisioning

Confirmed during the architectural interview on 2026-10-03:

- Operational logs contain counts, timings, opaque IDs, and sanitized error
  codes only. Do not log filenames, paths, medical text, model prompts, or raw
  parser exceptions. Apply this boundary to application and service diagnostics,
  including third-party reader/model/database logging configurations.
- Intentional local history tables and evidence inspection may show source
  details. They are product output, not operational logs; do not automatically
  capture CLI stdout into a central log sink. Sanitize before persistence,
  rather than collecting unrestricted diagnostics and redacting afterward.
- Network-enabled setup accesses model storage only, with no source-document
  or generated-medical-data mounts. Processing services access preprovisioned
  weights read-only and never attempt downloads. Missing weights fail clearly
  with setup instructions. Egress enforcement remains required in Phase 1.
- These are intended controls, not verified implementation claims. Verify
  service configuration and failure paths during implementation and Phase 2.

### D39 - Phase 2 centralized local logging foundation

Requested by the user on 2026-10-03 as Phase 2 hardening:

- Centralize operational logging through a shared event schema and a local
  collection/sink path. Use consistent fields for opaque run/generation IDs,
  component, step, severity, counts/timings, and sanitized error codes. Collect
  only designated sanitized streams, including service diagnostics where they
  can meet D38; do not ingest product output or raw prompts/SQL parameters.
- Keep this an operational hardening change with no new user-facing feature
  or mandatory logging service. Choose a simple local sink; a separate Compose
  service requires demonstrated need. App-managed log storage stays outside
  the repository and source folder, with bounded retention and documented
  cleanup. Its disposable logs are removed by derived/all erasure; ordinary
  deletion does not promise forensic erasure or removal of external backups.
- Make the common schema and logging boundary suitable for future JSON
  formatting, PII screening, and Sentry adapters. Those integrations and richer
  tooling are vNext; do not install a remote SDK or build a general observability
  framework solely to anticipate them. The existing field allowlist and
  sanitization are required now, independent of a future screening engine.
- Phase 2 verifies consistent events, bounded retention, and leakage prevention
  using synthetic identifiers/content in errors and diagnostic paths. Screening
  cannot be treated as proof that arbitrary payloads are safe to collect.
- No external telemetry or runtime egress is enabled in either current phase.
  Future Sentry support must preserve the no-PII-outside rule and obtain explicit
  authorization for external transmission; the architecture records compatibility
  intent, not permission to send logs.

---

## Consistency review refinements

Decisions D40 to D44 close gaps found in the documentation consistency review
on 2026-10-03 and were recorded with the user's approval. They refine earlier
decisions without adding features.

### D40 - OCR model files and GPU access

- The OCR engine runs in `app`. Any model or language files it needs follow
  the vision-weights rule: the setup profile provisions them into model
  storage, their versions are recorded, and they are not baked into the image.
  Mount model storage read-only into both `app` and the model service.
- Configure the OCR engine so it never downloads missing files at runtime.
  A missing OCR model is a setup error with instructions, as in D38.
- Give `app` GPU access only if the chosen OCR engine needs it. GPU work stays
  sequential (D14). Models resident at the same time, including a vision model
  the model service keeps loaded, must fit in 12 GB of VRAM; otherwise unload
  between steps or run OCR on the CPU. Verify with hardware checks (D37).

### D41 - Specialty-event grounding and evidence strength

Provisional with D36 pending real-document evaluation:

- A specialty event uses the laboratory verification states: `verified`,
  `unverified reading`, or `conflicting`. It is `verified` only when located
  text evidence supports the complete association: the raw specialty label,
  the wording that establishes the event type, such as a referral,
  consultation, or appointment phrase, and the event date with its role when
  one is reported. A specialty word elsewhere on the page is insufficient.
  Missing fields remain missing.
- When readers disagree on the event type or specialty for the same evidence,
  retain both interpretations as one unresolved candidate, shown separately
  from confirmed rows as in D28. A disagreement never yields a confirmed
  encounter.
- Evidence strength is separate from verification and has three values:
  - `direct`: the document records the event itself, such as the referral
    form for a referral, a consultation note for an encounter, or an
    appointment confirmation.
  - `indirect`: a different document reports the event, such as a history
    section or a later note mentioning an earlier consultation.
  - `weak`: the event rests only on printed or stamped content that explicitly
    names it, such as a printed consultation-form title with a stamped date,
    on a page whose substantive content is uninterpreted handwriting (D5).
- Letterhead, specialty lists, and isolated stamps still yield no event (D36).
  Verification and strength describe source support, not whether the event
  occurred as stated.
- Synthetic fixtures cover each strength value, an event-type disagreement,
  and a specialty word that appears away from the event wording.

### D42 - Source revalidation at every activation

- Every activation, automatic or forced, validates the source inventory and
  content fingerprints against the folder at the moment of activation.
  Automatic activation does this when the scan finishes; forcing an earlier
  staged generation repeats the check.
- Any addition, removal, or content change since that scan blocks activation
  and leaves the active generation untouched. Run a fresh scan, which reuses
  valid cached work (D9, D33).
- Activation holds the application lock (D33). The check is change
  detection, not an atomic snapshot.
- Synthetic fixtures cover forcing a staged generation after a source file
  changed, was added, or was removed.

### D43 - Case- and inflection-tolerant matching

- Dictionary lookup and the keyword sweep fold case and normalize Unicode
  forms, apostrophe variants, and Russian `ё`/`е`, in addition to the existing
  decimal-separator, whitespace, and homoglyph normalization. Original strings
  and locations are retained.
- Russian and Ukrainian labels and mentions also match inflected forms, such
  as `уролога` or `урологу` for `уролог`. Choose the method during
  implementation, for example stemming or inflected forms in the seed.
- The keyword sweep reports inflection-tolerant matches as ordinary mention
  hits; an extra false-positive mention is preferable to a hidden one.
- In dictionary lookup, a raw label that matches a seed or accepted term only
  through inflection tolerance appears as an `auto-mapped, unreviewed`
  candidate until accepted (D29). Durable rejections still apply. Matches that
  are exact after normalization resolve as before.
- Matching is deterministic and runs no model at query time (D18). Synthetic
  fixtures include inflected and mixed-case forms in both languages.

### D44 - Docker log capture

- Docker's own log capture is part of the D38 boundary. Disable it for the
  CLI service, for example with `logging: driver: none`, so terminal tables
  and evidence snippets are displayed but not retained by Docker. Run CLI
  operations as one-off `docker compose run --rm` containers.
- Configure PostgreSQL and the model server so their diagnostics omit
  statements, parameters, error details containing values, prompts, and
  responses. Where a service cannot meet D38, disable its Docker log capture
  as well and rely on the application's sanitized error codes.
- Docker-retained logs are outside `erase derived` and `erase all`, so medical
  content must not reach them. Verify with synthetic leakage checks in
  Phase 1; Phase 2 reverifies and centralizes only designated sanitized
  streams (D39).

## Architectural interview status

The architecture-blocking product branches raised in this review are resolved
for the current MVP. D36 and D41 remain provisional pending user-led real-data
review.
Implementation choices, including model/runtime selection, parser limits, and
cache artifact layout, are selected during development rather than by further
product gates. They remain unverified until implemented and checked.

Deferred work stays in the pivot's vNext backlog. The user checks real data
after Phase 1 implementation delivery and defines follow-up tasks (D37). Logging
centralization is Phase 2 hardening (D39), not an additional Phase 1 prerequisite.
