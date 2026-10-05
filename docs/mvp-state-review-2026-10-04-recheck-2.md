# MVP recheck 2 — 2026-10-04

Reviewed commit: `77c1679` (`Harden test isolation and align docs with delivered CLI behavior`).
This follows the recheck of `6fece51`. Application code was not changed during this
review. All inputs were synthetic; private originals and the production database
were excluded. Runtime checks used Docker Compose, a disposable test database,
read-only model weights, and the local inference service.

**Update (2026-10-04):** Findings N1–N4 were addressed in commits `392773d` through
`821006b` (signed results, qualitative result-field grounding, birth-date role
isolation, and test Compose volume override). Regressions live in
`tests/test_extract.py` and `tests/test_deployment_policy.py`. They are not open tasks.

**Verdict: the feature scope matches the MVP, and the previous regression cases are
fixed. Required source-verification guarantees still have three reproducible gaps.**
The full suite passes, but it does not cover these additional counterexamples.
Phase 2's user-led real-document evaluation correctly remains open; it is not an
additional Phase 1 delivery gate.

## What improved

- A report date claimed as a specimen date is now unsupported, cannot drive the
  timeline, and demotes the reading with `DATE_ROLE_UNSUPPORTED`.
- A reference threshold `>15` cannot replace the actual result `12`; a result `<5`
  cannot verify as exact `5`. The correct claim `<5` still verifies.
- Cancelled consultations and longer negated encounter wording no longer produce
  verified encounters. A model fragment omitting the source's negation is rejected
  too. Positive encounter wording still verifies.
- Split date captions and real Cyrillic PDF text receive additional regression
  coverage. The synthetic latency fixture now uses matching source/model dates
  and asserts its expected result coverage.
- CLI help now describes the delivered pipeline. Ruff lint and formatting are
  clean.

The earlier named-group, coverage-warning, split-phrase, dictionary, cache, and
erasure fixes remain in the tree and their regression suite passes.

## Findings at review time (since fixed)

### N1 — P1: a minus sign can disappear from a verified result

Location: `src/filebrownie/interpretation/grounding.py:119`.

Located source spans on one row:

```text
Base excess | -5 | mmol/L
```

Model claim: label `Base excess`, value `5`, unit `mmol/L`.
The interpretation result is **`verified`**, value **`5`**, with no notes.

`_STANDALONE_NUMBER` preserves some comparators but excludes a leading sign. It
matches `5` inside `-5`, and `_first_result_token` then approves the unsigned claim.
This changes the literal result while asserting source agreement, violating D4.
Preserve the signed source token and reject or demote a claim that loses its sign.
Add a regression with both an incorrect unsigned claim and the correct signed one.

### N2 — P1: a qualitative reference value can replace the actual result

Location: `src/filebrownie/interpretation/grounding.py:160`.

Located source spans on one row:

```text
HBsAg | Positive | Reference: Negative
```

Model claim: label `HBsAg`, value `Negative`.
The interpretation result is **`verified`**, value **`Negative`**, with no notes.

When the row has no numeric token, `value_at_result_position` falls back to finding
any matching nonnumeric text after the label. That includes reference text, so
this case reverses the actual positive result. D4 requires association with the
result field. Ground qualitative values at the result position, or retain an
unverified reading when the located layout cannot establish that association.

### N3 — P1: specimen context can turn a birth date into a timeline date

Locations: `src/filebrownie/interpretation/extract.py:74` and `:88`.

Located source rows, in order:

```text
Specimen: serum
Date of birth: 01.01.1970
Ferritin | 12
```

Model claim: `Ferritin = 12`, date `01.01.1970`, role `specimen`.
The resulting fact is **`verified`**, the reported date has
**`role_supported=True`**, and the timeline role is **`specimen`**, with no notes.

The inherited context supplies a `specimen` cue from the material caption. The
role matcher ignores the intervening explicit birth-date caption and accepts the
model's role. This can move a reading into the wrong chronology and date filter,
violating D4/D34. Require a date-bearing caption association; stop inheritance at
an incompatible caption or demote ambiguity. This is date-role grounding, not
patient-identity matching or a new feature.

### N4 — P2: the documented test configuration drops the model mount

Location: `compose.test.yaml:8`.

```yaml
volumes: !reset
  - models:/models:ro
```

The resolved test app has **no volume mounts**. Compose's `!reset` clears the field;
the supplied list does not become the replacement value. Consequently provisioned
weights are unavailable to the test app, and the real OCR/vision checks can skip
through their missing-model guards (`tests/test_ocr.py:93` and
`tests/test_model_service.py:60`).

Use `!override` to replace the sensitive source/data bindings with the intended
read-only model mount. Verify the resolved mount list and require the real-engine
checks to run when evaluating the provisioned reference environment. The privacy
isolation from source documents and generated data should be retained.

## Validation and limits

| Check | Result |
| --- | --- |
| App image build | Passed; build layers were cached |
| Ruff lint | Passed |
| Ruff format check | Passed; 68 files already formatted |
| `uv lock --check --offline` | Passed |
| Full Compose pytest suite | **246 passed, 0 failed, 0 skipped**, 273.62 seconds |
| Additional synthetic verifier probes | N1, N2, N3 reproduced; previous date/threshold/comparator/cancellation/negation cases rejected correctly |
| Resolved default test configuration | Test app volume list empty, confirming N4 |

The full suite used `compose.yaml`, `compose.test.yaml`, and a temporary override
that explicitly mounted only `models:/models:ro` and supplied temporary filesystem
mounts instead of private source/data bindings. This worked around N4 for the
review and exercised actual Tesseract, local vision inference, and database
integration. No models were downloaded or host dependencies installed. The
already-running disposable database service was preserved.

N1–N3 supplied fabricated structured model claims against located synthetic text.
They demonstrate verification failures, not a measured rate of model mistakes.
Conservative rejection/demotion is sufficient for this MVP; richer layout
reconstruction remains vNext.

Host-only symlink aliases and enforcing generated storage outside Git remain
installation-hardening weaknesses documented in README. No new failure with the
valid isolated configuration was observed here. Real-document accuracy remains
unmeasured, as agreed.
