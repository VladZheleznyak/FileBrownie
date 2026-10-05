# ACTIVATION_GUARD regression — sample file diagnosis

Date: 2026-10-05. Context: generation `6392b10a-4505-4431-bfab-96a77ce8a358` staged
with mass `fewer extracted facts` findings, then force-activated. This note classifies
**why** three representative paths regressed, using pipeline behavior documented in code
and the user’s guard/ferritin output. No private document bytes were copied into the
repository.

## Shared mechanism

Interpretation is **not** step-cached; OCR and vision **are**. A re-scan re-runs
[`interpret_page`](../src/filebrownie/interpretation/extract.py) on unchanged vision JSON.
Fact counts in [`evaluate_replacement`](../src/filebrownie/ingestion/guard.py) drop when:

1. Vision emits fewer usable `lab_rows` (parse drops, `dropped` counter, empty label/value).
2. Grounding demotes or fails rows (`VALUE_NOT_AT_RESULT`, `NOT_LOCATED`, etc.).
3. **`interpret_page` discards rows** with no evidence and `NOT_LOCATED`, or
   `ASSOCIATION_NOT_LOCATED` without `alternative_evidence` (lines 634–637).

Unmatched mention sweep still sees analyte text in located spans when (3) or partial (2)
removes stored facts — consistent with `labs ferritin` showing Dynacare mentions without
matching rows.

## Sample 1 — `2023 nutritionist/2023-06 Dynacare.pdf` (127 → 48 facts)

| Layer | Likely contribution |
| --- | --- |
| Vision cache | Cached multi-page JSON; `VISION_ITEMS_DROPPED` and dense panels suggest many model rows were omitted or truncated before interpretation. |
| Grounding | Canadian “TEST STATUS / YOUR RESULT / REFERENCE” layouts often split label/value across spans; stricter `value_at_result_position` and reference-field cutoff demote rows that previously verified loosely. |
| `interpret_page` drop | Rows that fail association with empty evidence are **not stored**, shrinking the fact count versus an index that kept demoted rows. |
| Supplement | [`vision_lab_row_hints`](../src/filebrownie/interpretation/grounding.py) may not list split OCR ferritin lines; supplement pass cannot recover rows the first pass never claimed. |

**Primary fix target:** table row detection, supplement hints, and persisting unlocated vision rows as unverified facts (plan todos `dynacare-panel`, `persist-unlocated-rows`).

## Sample 2 — `2020-01-19 моча/2020-01-19 09.25.26.jpg` (10 → 0 facts)

| Layer | Likely contribution |
| --- | --- |
| Reader | JPEG path uses OCR + vision; guard cited new `VISION_ITEMS_DROPPED` on this file. |
| Vision | Photo of urinalysis dipstick/table: partial `lab_rows` dropped at parse; remaining rows may fail grounding on OCR noise. |
| `interpret_page` drop | Total loss to **zero** strongly suggests **all** vision lab rows were either not emitted or discarded at lines 634–637 after failed grounding — not a dictionary or query issue. |

**Primary fix target:** persist unverified rows; improve OCR row grouping for photo JPEGs where possible without new formats.

## Sample 3 — `2023 nutritionist/2023-05-04 VolodymyrZhelezniak-BW2023-5-4.pdf` (ferritin 186)

| Layer | Likely contribution |
| --- | --- |
| Extraction | Ferritin **186 ug/L** appears in located text (unmatched mentions) and as one **candidate** fact — row is stored but not confirmed. |
| Dates | Specimen date `05/04/2023` parses as 2023-04-05 or 2023-05-04; `DATE_ROLE_UNSUPPORTED` keeps timeline off the confirmed list; path contains `2023-05-04`. |
| Coverage | Multiple pages with `VISION_ITEMS_DROPPED` and `POSSIBLE_INCOMPLETE_TABLE_EXTRACTION` — panel rows missing or unverified beyond ferritin. |

**Primary fix target:** filename-aligned display for unsupported roles (`ferritin-dates`); panel recovery as in sample 1.

## N1–N3 verifier status (recheck-2)

Synthetic regressions **N1** (signed results), **N2** (qualitative reference vs result), and **N3**
(birth date vs specimen role) are covered by tests in [`tests/test_extract.py`](../tests/test_extract.py)
(`test_negative_sign_must_match_located_result`, `test_reference_qualitative_value_does_not_verify_as_result`,
`test_birth_date_cannot_inherit_specimen_timeline_role`). Implementation lives in
[`grounding.py`](../src/filebrownie/interpretation/grounding.py) (`value_at_result_position`,
`_result_field_text`) and [`extract.py`](../src/filebrownie/interpretation/extract.py) (date-role
captions). No additional code change required for N1–N3 unless probes fail in CI.

## Recommended follow-up

1. Implement `persist-unlocated-rows` before expecting guard fact counts to recover.
2. Tune Dynacare-style row/hint/supplement behavior; re-scan **without** `--force` until guard clears.
3. Use `filebrownie evidence fact <ref>` on active generation rows to confirm per-page fact notes after each change.
