# Re-scan validation — 2026-10-05

This note records how to validate the ACTIVATION_GUARD / ferritin coverage work on a
machine with private sources mounted. The implementation agent could not run a full
`filebrownie scan` here because `FILEBROWNIE_SOURCE_DIR` and `FILEBROWNIE_DATA_DIR` were
not configured in the execution environment.

## Preconditions

- Rebuild the app image after pulling these commits: `docker compose build app`
- Optional: `docker compose run --rm app filebrownie erase derived` if you need fresh
  vision JSON after prompt or dropped-retry changes (interpretation uses `interpretation-7`).
- Start inference if vision is required: `docker compose --profile inference up -d model`

## Baseline (from user run 2026-10-05)

| Metric | Value |
| --- | --- |
| Staged generation | `6392b10a-4505-4431-bfab-96a77ce8a358` |
| Guard | `ACTIVATION_GUARD` — mass `fewer extracted facts` |
| Force-activated | Yes (previous index superseded) |
| `labs ferritin` | 2 confirmed (filename dates), 1 candidate (186 ug/L), 12 unmatched mention sources |

## Validation steps (operator)

```bash
docker compose run --rm app filebrownie scan
# Expect Guard summary lines before per-file findings when staged.
# Do not use --force until extracted/verified summaries improve or are understood.

docker compose run --rm app filebrownie labs ferritin
```

## Success checks

| Check | Expected after this work |
| --- | --- |
| Guard `extracted facts` total | Rises vs force-activated `6392b10a` index (unlocated rows now stored) |
| Guard `verified facts` total | May diverge from extracted (demotion vs recovery) — read both summaries |
| `labs ferritin` | 2023-05-04 path: ambiguous `05/04/2023` aligns to filename; fewer “date uncertain” candidates when path matches one reading |
| Dynacare PDFs | More facts (verified or unverified); fewer ferritin-only unmatched mentions on pages with panel OCR |
| Activate without `--force` | Ideal when guard clears; otherwise review Guard summary counts |

## Automated checks run in CI/dev (no private sources)

```bash
docker compose run --rm app pytest
docker compose run --rm app ruff check .
docker compose run --rm app ruff format --check .
```

All passed after the extraction-coverage commit series on 2026-10-05.
