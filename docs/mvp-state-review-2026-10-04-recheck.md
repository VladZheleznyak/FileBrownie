# MVP recheck — 2026-10-04

Reviewed commit: `6fece51` (`Fix MVP review correctness gaps in grounding and queries`).
This follows the initial review of `397aeb4`. No application code was changed during
this recheck. Private documents and the production database were excluded from all
executions; Compose ran synthetic fixtures against disposable database schemas and
read-only model weights.

**Verdict: substantial improvement, but required verification guarantees still fail.**
Several original counterexamples now behave correctly. The remaining date, value,
and event errors still allow unsupported facts to receive `verified` status, so the
MVP cannot yet be considered complete under D4/D34/D36/D41.

## Progress against the previous findings

| Finding | Rechecked status |
| --- | --- |
| F1: result association/unit inheritance | Original `15-150` range and cross-analyte unit examples now stay unverified; result association remains incomplete in new examples below |
| F2: date roles | Still open; the original report-as-specimen example still verifies |
| F3: event grounding | Original letterhead and short-negation examples now produce no event; cancellation and longer negation still verify as encounters |
| F4: missing coverage warnings | The real 201-page PDF now shows `DOCUMENT_PAGE_LIMIT`, known count 201, and recorded count 200 in query output |
| F5: named group | `iron-panel` now resolves the four expected analytes |
| F6: split phrases | Adjacent `erythrocyte`, `sedimentation`, `rate` spans now produce an unmatched mention |
| Ungrounded flags/intervals | Added conservative omission/demotion and a regression test |
| Dictionary fallback | Rejected-pair fallback restricted; direct proposal lookup now resolves its proposed concepts |
| Cache identity | OCR code hash and configured llama.cpp image identity added |
| Erasure cleanup | Explicit `ERASE_CLEANUP_INCOMPLETE`, documented retry, and a synthetic failure/retry regression added |
| Documentation | Previous README pending-implementation statements corrected; some CLI help remains stale |

The truncation probe explicitly force-activated the completed synthetic generation
before querying it. The replacement guard correctly held it staged initially; querying
the prior active generation would not establish the candidate generation's warnings.

## Remaining high-priority findings

### R1 — P1: the unsupported date role is discarded by the fix itself

Location: `src/filebrownie/interpretation/extract.py:99`.

`_supported_date_role` correctly returns `unsupported` for a source containing
`Report issued: 12.03.2024` when the model calls it a specimen date. However, the
unsupported branch constructs `ReportedDate` with **`claim.role`**, preserving
`specimen` instead of the computed unsupported state. The later demotion check
`item.role == 'unsupported'` therefore never runs for this case, and the date still
determines the specimen timeline.

The original probe still returns `verification='verified'`, `timeline_role='specimen'`,
and no notes. The new repository regression
`tests/test_extract.py::test_report_date_cannot_become_specimen_timeline` fails.
Preserve the unsupported role/state and exclude it from timeline selection while
showing its uncertainty. Complete role-bearing context/association remains required.

### R2 — P1: result verification still accepts a reference threshold or loses a comparator

Location: `src/filebrownie/interpretation/grounding.py:139`.

`value_at_result_position` removes numeric intervals and then searches the entire row.
It does not actually establish the result position or preserve the full comparator.
Two additional synthetic examples still return **verified**, with no warning:

| Source row | Model claim accepted |
| --- | --- |
| `Ferritin | 12 | ng/mL | >15` (reference threshold) | Result `15 ng/mL` |
| `Ferritin | <5 | ng/mL` | Exact result `5 ng/mL`, losing `<` |

Removing `15-150` handles the original specific case, but D4 requires the reported
result association, including its literal comparator. Verify the result field itself
or retain an unverified/conflicting reading when located evidence cannot establish it.
Add regressions for threshold references and comparator loss.

### R3 — P1: cancellation and longer negation still become verified encounters

Location: `src/filebrownie/interpretation/extract.py:224`.

The new negation check searches only the 20 characters preceding a keyword and ignores
cancellation wording. Both examples below still produce **verified, direct encounters**
when supplied as model claims:

- `Consultation with urology was cancelled`.
- `No prior or subsequent urology consultation occurred`.

The first states cancellation; the second states that no consultation occurred.
Neither establishes a completed encounter under D36/D41. Keep these as mention/candidate
or an appropriately typed cancellation, rather than confirming the model's encounter
claim. A small conservative rule is sufficient; no vNext event reconstruction is needed.

## Other remaining weaknesses

- The latency fixture prints months varying from January to September but assigns June
  to every extracted date (`tests/test_latency.py:27` and `:30`). The nonempty group
  assertion is useful, but most fixture dates do not ground and become undated/excluded.
  Generate one date string for both the PDF and model claim, and assert expected result
  coverage rather than merely at least one row.
- CLI help still says extraction is pending and scans run reader foundations only
  (`src/filebrownie/presentation/cli.py:778` and `:793`). This contradicts the delivered
  full pipeline and the updated README.
- Host overlap validation now checks configured path strings, but resolving those host
  paths inside the container cannot resolve host-only symlink aliases. There is still
  no outside-Git check. This is remaining installation hardening, not a demonstrated
  failure with the valid configuration used here.
- The checklist still broadly marks role-grounding verification complete despite R1's
  failing regression. Reconcile those completion claims after the checks pass.

## Validation

The app image rebuilt successfully and `uv lock --check --offline` passed.
Ruff lint failed with **6 errors**: three import-order errors and three long lines.
Formatting failed for **5 files**. These are small fixes but currently fail the
repository's documented checks.

The full Compose suite finished with **237 passed and 1 failed, 0 skipped**, in 274.15 seconds.
The failure is `test_report_date_cannot_become_specimen_timeline` at `tests/test_extract.py:100`.
The run included actual Tesseract, the local GPU vision service, and database integration.
Additional probes used fabricated claims to test verification boundaries; they measure
whether the verifier rejects those claims, not how often the model produces them.

Real-document accuracy is still unmeasured, as agreed. User-led evaluation remains a
later task and is not being introduced as a Phase 1 delivery gate.
