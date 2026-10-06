# Agent rules

## Current delivery scope

- Follow [docs/architecture.md](docs/architecture.md) for Phase 1, Phase 2, and deferred scope. Open work is in [docs/todo.md](docs/todo.md).
- Phase 1 is a local CLI for laboratory and specialty-visit histories from one medical-document folder. Phase 2 hardens those same capabilities without adding features. MCP, summaries, other document domains, and dynamic structure discovery belong to vNext.
- Current input formats are selectable-text/scanned PDF and JPG/JPEG only. TXT, CSV, XLSX, HTML, and ZIP processing belong to vNext; report unsupported files without parsing or expanding them.
- Keep source documents strictly read-only throughout both current phases. Keep generated medical data local and separate from originals and Git.
- Use local inference and no external network access during medical processing. Public dependencies and model weights may be downloaded during separate setup; provision weights outside the image build.
- Use English CLI labels and explanations while supporting English, Russian, and Ukrainian source evidence.
- Preserve source references and expose known gaps and uncertainty. Do not claim complete extraction merely because parsing succeeded.
- Keep the evidence foundation independent of medical types so future document domains can reuse it. Do not implement vNext features solely to preserve flexibility.
- Keep the MVP simple. Resolve only decisions that block implementation architecture or required correctness, privacy, and recovery; defer optional features and richer workflows to vNext. Specialty-event heuristics stay provisional pending real-document evaluation.
- Deliver Phase 1 implementation before user-led real-data evaluation. Use synthetic fixtures and basic hardware checks during implementation; do not require a private-original demonstration or formal benchmark as a delivery gate.
- Use sanitized operational logs only; do not capture medical CLI output or raw reader/model/database diagnostics into logs. Phase 2 centralized local logging is in place; JSON formatters, richer PII screening, and Sentry adapters are future integrations, with no external telemetry in current phases.
- Keep network-enabled model setup isolated from source documents and generated medical data. Processing services never download missing weights; report a setup error instead.

## Privacy

- Never commit or push personally identifiable information (PII), including real names, email addresses, phone numbers, postal addresses, account identifiers, credentials, or other data that can identify a person. Use synthetic or anonymized examples instead. If PII is found in a change, remove or redact it before committing or pushing.

## Containerization

- The project runs entirely in Docker, orchestrated with Docker Compose. Every runtime component, including the application, database, and any model server, is a Compose service.
- Never install project dependencies on the host and never create a host virtualenv. Dependencies are installed into images.
- Manage Python dependencies with `uv` inside the image. Commit `pyproject.toml` and `uv.lock`, and keep them in sync.
- Run application commands, tests, linters, and migrations through Compose, for example `docker compose run --rm app pytest`. Do not document or suggest host-level equivalents.
- Adding a new tool or dependency means changing a Dockerfile or `compose.yaml`, not the host.
- Mount directories containing user documents read-only until a phase explicitly introduces file mutation.
- Keep the image buildable without network access to anything beyond package registries, so a clean `docker compose build` reproduces the environment.
