# MVP state review — 2026-10-04

Reviewed commit: `397aeb4` (`Implement Phase 1 local medical processing pipeline`).
The checkout was clean before this review. This report is the only repository change.
No application code was changed, and no private source documents or production database
were inspected or processed.

**Verdict: the Phase 1 feature set is substantially implemented, but it does not yet
satisfy several required correctness and coverage guarantees.** The implementation is
ready for targeted fixes and subsequent user-led evaluation. The checklist's checked
boxes overstate verification and warning visibility. Phase 2's real-data evaluation
correctly remains open; it is not a Phase 1 delivery prerequisite.

## Scope and verification

The review used `PROJECT_PIVOT_2026-10-03.md`, D29–D44 in `docs/decisions.md`,
implementation code, and synthetic fixtures. Everything executed through Compose;
no host dependencies were installed. A temporary Compose override removed the app's
source/generated-data bind mounts and supplied empty temporary filesystems. The only
persistent app mount used during testing was read-only model storage. Database work
used disposable `db-test` schemas, each removed afterward.

| Check | Result |
| --- | --- |
| Compose app build | Passed; existing layers were reused |
| Full test suite, including actual Tesseract and local GPU vision service | **226 passed, 0 skipped, 268.34 seconds** |
| Ruff lint | Passed |
| Ruff formatting | Passed; 68 files already formatted |
| `uv lock --check --offline` | Passed |
| Pinned OCR/vision model checksums | Both verified offline |
| Additional synthetic association/coverage/lookup probes | Reproduced the findings below |

Passing the existing suite establishes the behaviors it exercises. It does not resolve
the additional counterexamples or establish accuracy on real documents. The additional
probes supplied fabricated model claims deliberately: the verification layer is required
to treat model output as untrusted, so these checks exercise that layer's contract rather
than measuring the frequency of model mistakes.

## What matches the MVP

| Requirement | Observed implementation |
| --- | --- |
| Local CLI; laboratories and specialty events | `scan`, `labs`, `visits`, and evidence inspection are implemented |
| PDF/JPG/JPEG; unsupported formats reported | Defensive discovery/readers with resource limits and unsupported statuses |
| Independent reads | PDF text or Tesseract located text plus image-based Qwen extraction |
| Evidence foundation separate from medical types | Generic content/unit/span models and readers; medical interpretation is separate |
| Private processing | Internal processing network, read-only originals/models, isolated provisioning, disabled Docker log capture, sanitized local log |
| Safe generations | Serialized operations, staged scans, atomic active pointer, conservative guard, activation revalidation, recovery and pruning |
| Deterministic histories | Structured queries, date precision/alternatives, unit preservation, no query-time model calls |
| User decisions and cleanup | Dictionary review/reversal, content-bound manual checks, derived/all erasure with confirmation for all |
| Current-phase restraint | No MCP, summaries, new document domains, or dynamic structure discovery |

The standalone inventory and reader/debug commands go beyond D30's stated minimum
interface. They already support implementation diagnostics; this is minor scope drift,
not a reason to add further product features or redesign the architecture.

## Required fixes

### F1 — P1: laboratory verification accepts the wrong result and unit

Locations: `src/filebrownie/interpretation/grounding.py:144` and `:158`.

A synthetic row `Ferritin | 12 | ng/mL | 15-150` accepted a model claim of value `15`
as **verified**, although `15` occurs only in the reference interval. The verifier
checks whether the label and number occur anywhere in the same geometric row, without
establishing that the number is the reported result.

A second probe put `Ferritin | 12 | ng/mL` above `Hemoglobin | 135 | g/L`.
The claim `Hemoglobin = 135 ng/mL` also became **verified**: the header fallback searches
all preceding rows and accepts another analyte's unit.

This violates D4's complete-association requirement. Ground the result position and
restrict inherited units/specimen to supported context. When the existing located
text cannot establish the association, retain an unverified/conflicting reading.
Advanced table reconstruction is unnecessary for that conservative behavior.

### F2 — P1: date presence is treated as proof of date role and association

Location: `src/filebrownie/interpretation/extract.py:48`.

The source `Report issued: 12.03.2024` plus a located Ferritin result accepted a claim
that this was a **specimen** date, producing a verified specimen-dated fact with no
warning. `_dates` locates only the date string anywhere on the page and copies the
model's role. It does not verify the role or ownership by the represented event/result.

This violates D4/D34/D41 and can change chronology and date-filter inclusion. Verify
role-bearing context and association; preserve uncertainty when unsupported. Also review
whether unlocated date claims should determine the timeline: currently they can affect
filtering even when `DATE_NOT_LOCATED` demotes the reading.

### F3 — P1: mention-only and negated text can become verified encounters

Locations: `src/filebrownie/interpretation/extract.py:184`, `:199`, and `:230`.

Both fabricated claims below became **verified, direct encounters**:

- Source wording: `Urology clinic letterhead`.
- Source wording: `No urology consultation occurred`.

The first passes because nonempty wording is accepted even when it establishes no
event type. The second passes because the cue matcher recognizes `consultation` without
checking negation. Document class supplied by the model determines direct strength.

D36/D41 require positively supported events and mention-only handling for letterhead,
specialty lists, and stamps. Require wording that establishes the claimed event;
contradictory or insufficient wording must remain a candidate or mention. Provisional
event heuristics do not suspend these requirements.

### F4 — P1: query output hides known missing coverage

Locations: `src/filebrownie/storage/facts.py:289` and `:314`;
`src/filebrownie/query/history.py:154`.

A real synthetic 201-page PDF reached the 200-page reader limit. The stored file outcome
was `partial`, page count `201`, recorded units `200`, and warning `DOCUMENT_PAGE_LIMIT`.
The laboratory query returned **no coverage warnings**.

`file_problems` excludes files that have any unit outcomes, so file-level truncation can
vanish when the retained units completed. Completed-unit warnings are also filtered to
three selected codes. A separate probe stored `VISION_ITEMS_DROPPED`, yet the query again
said no coverage warnings were recorded. `TEXT_LAYER_COVERAGE_UNVERIFIED` is similarly
stored but omitted.

D22/D28/D32 require visible known gaps in every relevant answer. Include file-level
partial/truncation warnings regardless of retained units, and propagate relevant
completed-unit uncertainty/drop warnings. Keep generic completeness wording in addition
to specific warnings; it cannot replace them.

### F5 — P2: the documented named-group query does not resolve

Location: `src/filebrownie/query/dictionary.py:76`.

`resolve('iron-panel', 'analyte')` returns no group and no concepts.
`resolve('iron panel', 'analyte')` returns the expected four concepts. Group IDs are
not recognized as lookup terms, and token normalization preserves the hyphen.
The README explicitly documents `filebrownie labs iron-panel`.

Support the documented ID or consistently document a quoted display name. Add a check
that the documented command retrieves the expected members. The latency test currently
uses this failing group term and does not assert a nonempty answer; its fixtures also
omit structured dates while the group query applies a date filter. That benchmark's
group timing therefore exercises an empty result rather than a representative history.

### F6 — P2: the mention sweep loses multiword terms split into located spans

Location: `src/filebrownie/query/history.py:129`.

A real synthetic PDF contained adjacent located spans `erythrocyte`, `sedimentation`,
and `rate`, with no extracted fact. Querying `erythrocyte sedimentation rate` returned
no unmatched mention. Each phrase is checked against one span independently, so the
fallback cannot recognize the full phrase across ordinary PDF/OCR span boundaries.

This weakens D18/D43's omission visibility. Match phrases over the existing located
row/text sequence while retaining constituent references. This needs no new region-level
coverage subsystem. Span-wide suppression can also conceal an additional occurrence
inside a span already used by a fact; review that boundary conservatively.

## Other discrepancies and weak points

- **Ungrounded result fields:** `extract.py:102` preserves a model-supplied reference
  interval and flag without grounding. A fabricated `99-999` interval and `H` flag
  survived on a verified fact although neither existed in the source. Ground these
  fields or label their uncertainty separately so the table does not imply that the
  entire row's displayed attributes were checked.
- **Dictionary fallback:** `dictionary.py:124` returns a confirmed raw match after
  rejected concept matches were skipped. A rejected `Ferritin` → `ferritin` pair
  still yielded a confirmed raw match in the canonical `ferritin` scope. Querying an
  active proposed label directly also loses its proposal marker. Distinguish explicit
  raw-label lookup from concept lookup so candidate/rejection presentation remains
  consistent; this is a small query-time policy fix, not a richer review workflow.
- **Cache version coverage:** the generic reader fingerprint includes code/dependency
  hashes, but OCR/vision step versions do not cover all output-affecting code/runtime
  settings. In particular, OCR preprocessing/TSV implementation and the llama.cpp
  runtime image/version are absent from those step identities. Upgrades could reuse
  outputs from an earlier implementation; include them in D9 cache identities.
- **Configuration relies on operator discipline:** `configured_paths` compares
  container paths `/sources` and `/data`. It cannot detect overlapping host bind
  directories or enforce that host generated data is outside Git. README instructions
  are correct, but an invalid Compose configuration can defeat that separation.
  A host-mount configuration check would strengthen Phase 2 installation validation.
- **Erasure recovery:** database deletion commits before filesystem cleanup. A cleanup
  failure can leave sensitive artifacts after rows are deleted. The operation reports
  an error and can be retried; document and exercise that partial failure explicitly.
- **Documentation drift:** README lines 122, 153, 214, and 231 still describe activation,
  OCR/vision, pruning/erasure, and isolation checks as unimplemented/pending. The decision
  record introduction says nothing is implemented yet, while the checklist broadly
  marks delivery guarantees complete. Keep intended decisions distinct from verified
  implementation status and reopen affected checklist items.

Unverified readings are visibly marked in the main dated/undated tables; this review
is not treating their presence alone as a defect. The concern is unsupported readings
that receive stronger verification or lose their coverage/candidate qualifications.

## Evaluation and suggested order

Fix F1–F4 first and add focused synthetic regressions for these exact counterexamples.
Fix F5/F6 and dictionary/cache issues next; reconcile the README/checklist with the
actual behavior. Then perform the agreed user-led real-data evaluation using local
manual checks. Keep D36/D41 provisional and record observed omissions/reading errors
without claiming an accuracy percentage lacking a reviewed denominator.

Known limitations remain material: no measured real-document accuracy, skew/rotation
sensitivity beyond existing orientation handling, uninterpreted handwriting, missing
cross-page context, and heuristic table discrepancy detection. These were already
acknowledged or deferred; they do not justify adding vNext features to the MVP.

The Compose test/model services started for this review were stopped afterward.
Private source and generated-data mounts were excluded from all review runs.
