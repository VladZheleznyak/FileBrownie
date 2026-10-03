# FileBrownie

FileBrownie is a private, local tool for retrieving medical histories from one
folder of documents. Phase 1 is a CLI that answers two kinds of questions, with
source evidence and visible gaps:

- laboratory history by analyte or analyte group
- visit history by specialty or specialty group

It runs entirely in Docker Compose, uses only local inference during medical
processing, and never modifies source documents. Current inputs are PDF and
JPG/JPEG; other formats belong to vNext. The repository currently contains a
runtime and package scaffold with read-only source discovery, not a working
medical-processing pipeline.

Keep the MVP simple: terminal histories, source references, visible uncertainty,
and essential recovery. Optional exports, evidence crops, duplicate heuristics,
and richer workflows belong to vNext.

Phase 2 hardens these capabilities and adds centralized local operational logging,
with future support for JSON, PII screening, and Sentry. Current phases keep
logs sanitized and enable no external telemetry. Real-data review follows
Phase 1 implementation delivery.

- [Project pivot](PROJECT_PIVOT_2026-10-03.md): scope for Phase 1, Phase 2, and vNext
- [Decision record](docs/decisions.md): resolved design decisions and their reasons
- [Architecture](docs/architecture.md): intended components, data model, and flows
- [Implementation checklist](docs/implementation-checklist.md): delivery and validation tasks for both phases

## Repository safety

Keep real documents, extracted data, credentials, and other personal information out of Git. Use synthetic examples in committed files.

## Scaffold setup

Docker Desktop with WSL integration and Docker Compose are required. Copy
`.env.example` to `.env`, then set absolute source and generated-data directory
paths. Both must be outside this repository, and the generated-data directory
must be separate from the source directory (not nested within it). Create those
directories and a `postgres` subdirectory inside the generated-data directory
before running Compose. Missing bind directories cause an error rather than
being silently created. Set `FILEBROWNIE_UID` and `FILEBROWNIE_GID` to the WSL
owner's numeric user/group IDs if they differ from the default `1000`. The app
runs as this user, who needs read access to sources and write access to the
generated-data directory. Do not place real records in this checkout.

Build and inspect the scaffold:

```sh
docker compose build app
docker compose run --rm app filebrownie --version
docker compose run --rm app filebrownie status
docker compose run --rm app filebrownie inventory
docker compose up -d db
```

The image's default command is `filebrownie --help`. When overriding it, include
the executable name:

```sh
docker compose run --rm app filebrownie --version
docker compose run --rm app filebrownie status
docker compose run --rm app pytest
docker compose run --rm app ruff check src tests
docker compose run --rm app ruff format --check src tests
```

`status` reports implementation status only; it does not connect to the
database, inspect sources, or claim that an index exists. `inventory` recursively
lists sources, recognizes PDF/JPG/JPEG extensions case-insensitively, and computes
SHA-256 content fingerprints in bounded chunks. It reports unsupported files
without parsing them or expanding archives. Byte-identical files retain all
their source paths. A supported extension does not establish valid file content
or successful extraction. No document reader runs in this command.

Symlinks and special files are skipped with explicit warnings. Unreadable files,
detected changes during hashing, and directories beyond the depth limit of 64
are reported as failures. Filenames are escaped in terminal output. Exit codes
are `0` for a finished inventory (which can include unsupported formats), `1`
for an inventory with failed/skipped entries, and `2` for setup errors or a busy
operation. The shared lock is stored in generated-data storage and released by
the kernel when the process exits, including after interruption. The inventory
is shown locally and is not persisted or logged. It is not an atomic snapshot;
scan activation must later revalidate the full inventory and fingerprints.

Scan, activation,
histories, evidence inspection, review, and erasure are not implemented yet.

`app` and `db` use an internal network with no published ports. Originals and
model storage are mounted read-only into the app; generated data and PostgreSQL
files use the external generated-data directory. PostgreSQL currently trusts
clients on the isolated processing network; it must not be attached to a public
network or given published ports. Docker log capture is disabled for all runtime
services, and PostgreSQL statement/parameter logging is disabled. Runtime egress
refusal and sensitive-diagnostic leakage checks remain implementation tasks.

The `inference` profile reserves a local model service; the `setup` profile
reserves a network-enabled provisioning service with model storage only. Both
entry points exit with an explicit setup error until the OCR/vision baseline is
implemented. Neither downloads weights. The model volume is outside the image
and repository. GPU configuration will follow selection of the runtime.

## Development

The `src/filebrownie` package separates ingestion, domain-independent evidence,
medical interpretation, deterministic queries, storage, and CLI presentation.
Source discovery and operation locking are implemented alongside the CLI. Tests use synthetic
content and run inside the app image. Development tools are included in this
initial image; no dependencies or virtual environment are installed on the host.

After editing dependency declarations, refresh the lockfile using the isolated
development service, then rebuild the app:

```sh
docker compose -f compose.yaml -f compose.dev.yaml --profile dev run --rm --build tooling uv lock
docker compose build app
```

The development tooling service has network access for package registries and
mounts the checkout only. Keep source records and medical outputs outside the
checkout. Image builds install uv and locked packages from package registries;
model provisioning is separate. Use `docker compose down` to stop the database;
this does not erase generated data or model storage.
