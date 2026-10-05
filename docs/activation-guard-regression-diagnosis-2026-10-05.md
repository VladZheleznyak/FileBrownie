# ACTIVATION_GUARD regression — sample file diagnosis

Date: 2026-10-05. Context: one staged generation had mass `fewer extracted facts`
findings, then was force-activated. This note classifies why representative files
regressed, using pipeline behavior in code and local guard output. No private
document bytes or identifying paths are stored here.

## Shared mechanism

Interpretation is **not** step-cached; OCR and vision **are**. A re-scan re-runs
[`interpret_page`](../src/filebrownie/interpretation/extract.py) on unchanged vision JSON.
Fact counts in [`evaluate_replacement`](../src/filebrownie/ingestion/guard.py) drop when:

1. Vision emits fewer usable `lab_rows` (parse drops, `dropped` counter, empty label/value).
2. Grounding demotes or fails rows (`VALUE_NOT_AT_RESULT`, `NOT_LOCATED`, etc.).

Unmatched mention sweep still sees analyte text in located spans when grounding
demotes a row or vision never emits it.

Unlocated vision rows are stored as unverified facts
(`test_unlocated_lab_row_without_evidence_is_stored_unverified`). Ambiguous
numeric dates that agree with the filename are aligned
(`FILENAME_DATE_ALIGNED`). Panel-style rows and supplemental OCR hints are in
[`grounding.py`](../src/filebrownie/interpretation/grounding.py). Those are not
open tasks.

## What can still shrink a replacement index

| Layout | Remaining risk |
| --- | --- |
| Multi-column result/reference panels | Cached vision JSON can still omit rows (`VISION_ITEMS_DROPPED`). Stricter result-position grounding demotes rows an older index counted as facts. |
| Photo of a dense table | OCR noise plus dropped vision rows can still leave few or no stored facts if the model emits nothing to persist. |
| Ambiguous document dates | Unsupported roles stay off the confirmed timeline. Filename alignment applies only when path consensus matches one reading. |

## Recommended follow-up

1. Re-scan **without** `--force` and see whether the guard still reports fewer facts after the stored-unverified-row, filename-alignment, and panel-hint changes.
2. If a photo still falls to zero facts, improve OCR row grouping for that layout. Do not add new input formats.
3. Use `filebrownie evidence fact <ref>` on active generation rows to confirm per-page fact notes after each change.
