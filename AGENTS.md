# Agent rules

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
