# FileBrownie

FileBrownie is a private, local tool for retrieving medical histories from one
folder of documents. Phase 1 is a CLI that answers two kinds of questions, with
source evidence and visible gaps:

- laboratory history by analyte or analyte group
- visit history by specialty or specialty group

It runs entirely in Docker Compose, uses only local inference during medical
processing, and never modifies source documents. Current inputs are PDF and
JPG/JPEG; other formats belong to vNext. The repository currently contains a
runtime and package scaffold with read-only source discovery and PostgreSQL
inventory generations, bounded PDF/JPEG evidence readers, and reader-only scans
with a verified artifact cache, not a working medical-processing pipeline.

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
is shown locally and is not logged. It is not an atomic snapshot;
scan activation must later revalidate the full inventory and fingerprints.

To persist source metadata and fingerprints locally, initialize the database and
save an inventory:

```sh
docker compose up -d --wait db
docker compose run --rm app filebrownie migrate
docker compose run --rm app filebrownie inventory --save
docker compose run --rm app filebrownie status --database
docker compose run --rm app filebrownie validate <generation-uuid>
```

Replace the final placeholder with the generation ID printed by the save
command. Without `--save`, inventory discovery still needs no database.
Migrations are transactional and versioned by checksum. Do not edit an applied
migration; add a new version when evolving the schema. Database failures cross
the CLI boundary as sanitized error codes.

Inventory saves are atomic: a failed write does not replace or delete prior
generations. A killed operation can leave a `running` generation; the next
saved-inventory, database-status, or validation command, under the shared lock,
marks unfinished generations `interrupted`. Saved inventories stay `staged`
with reason `INVENTORY_ONLY`; no active medical index is created.

`validate` compares all source paths and fingerprints against the folder now.
Added, removed, or changed entries make the saved generation `invalid`.
Entries without usable fingerprints (including skipped symlinks and unreadable
files) cannot establish source consistency and also invalidate the generation.
Restoring earlier bytes does not reset an invalid generation. A matching
inventory remains staged and does not establish extraction coverage.
Validation exits `0` for a matching staged inventory, `1` for an invalid
generation, and `2` for operational errors. This reusable revalidation
foundation will be required at every future activation, including forced
activation; the activation/coverage guard itself is not implemented yet.

Full OCR/vision scanning, activation, histories, dictionary review, and erasure are not
implemented yet. Saved filenames and fingerprints are sensitive derived data
stored only in the external PostgreSQL data directory.

## First evidence readers

Read one supported file using its relative path from the inventory, then inspect
the resulting reference:

```sh
docker compose run --rm app filebrownie read synthetic-report.pdf
docker compose run --rm app filebrownie evidence show <evidence-uuid>
```

Replace the example filename and UUID with local values. These commands hold the
shared application lock. The reader checks runtime routing before processing
and fails closed on gateway/default routes, public IPv4 routes, global IPv6
addresses, or unavailable route information. This guard supplements the Compose
internal network; it sends no external probes or requests.

PyMuPDF reads PDF text and renders pages at 150 DPI. Text spans retain original
English, Russian, or Ukrainian strings and bounding boxes in rendered-image
pixels, accounting for page rotation. Unusable bounding boxes fall back to
page-level locations with a warning. A basic text-quality check flags sparse or
replacement-character-heavy text. Every PDF text page carries a
`TEXT_LAYER_COVERAGE_UNVERIFIED` warning: a text layer does not establish that
all visible content was read. Empty/weak text layers and JPEG images require
OCR and remain partial. Pillow verifies JPEG content and applies EXIF rotation
before creating its raster. No OCR or vision model is run yet.

The parser runs in a separate process against a fingerprint-checked generated
copy, with stdout/stderr discarded. Original paths are opened without following
symlinks. Parser diagnostics and raw exceptions are not retained. The copy is
removed afterward; evidence, rasters, and manifests stay under the external
generated-data directory in `evidence/<opaque-uuid>/`. Artifacts record content
hashes, source references, reader dependency versions, page/image counts, one
processing status, and concurrent warnings. Standalone `read` results are not
automatically registered with a generation; the reader-only scan below connects
reader artifacts to generations and a cache.

Initial limits are 64 MiB input, 200 PDF pages, 8 million pixels per page/image,
100 million retained document pixels, 256 MiB raster output, 16 MiB manifests,
768 MiB parser address space, 60 seconds CPU time, and 90 seconds wall time.
Limits are implementation defaults, not measured workload guarantees. A failed
PDF page does not discard successful pages. Written page manifests also preserve
earlier evidence if a worker times out or terminates. File-open failures keep
page counts unknown; page/resource limits expose unprocessed coverage.
Password-protected PDFs are explicitly unsupported.

Reader status `completed` means this reader finished its required steps,
not complete medical extraction. `read` and `evidence show` return `0` for
completed reader output, `1` for partial/failed/unsupported output, and `2`
for operational errors. Inspection is intentional local product output and may
show sensitive source text; Docker capture remains disabled.

## Reader-only scans and cache

Initialize or upgrade the database, then run the bounded reader stage over the
configured folder:

```sh
docker compose run --rm app filebrownie migrate
docker compose run --rm app filebrownie scan --readers-only
docker compose run --rm app filebrownie evidence generation <generation-uuid>
```

The `db` service must be running. Plain `scan` returns a setup error until
the full OCR/vision pipeline is available. `--readers-only` processes each
unique supported content/format combination once, preserving all identical-byte
source paths. Unsupported and skipped entries remain visible in the generation.
Unknown page counts stay unknown; page failures and OCR needs remain visible.
Reader outcomes and evidence references are saved per generation in PostgreSQL;
text and rasters remain in generated-data storage.

Each generation records its inventory before reading and revalidates it after
the readers finish. Additions, removals, changes, or unverifiable entries make it
`invalid`. A consistent reader scan stays `staged` with reason
`READERS_ONLY`; no active medical index or facts are published. Interruption
retains previously saved outcomes and caches. A fresh scan marks unfinished
generations interrupted and reuses valid work without continuing a damaged
generation. Completion requires an outcome for every eligible unique content.

The cache key includes exact input bytes, source format, reader implementation
and schema code, dependency/Python versions, machine architecture, and reader
limits. Before reuse, the JSON and every referenced raster must match saved
SHA-256 checksums. Missing, changed, or unsafe artifacts are cache misses and
trigger a fresh read. Ordinary partial output needing OCR or text-quality review
can be reused with its warnings; failed, interrupted, timed-out, or resource-limited
reader output is retried. Previously cached artifacts remain independent of
generation rows. Pruning and erasure are still pending.

Cached evidence records the source path at artifact creation. Use
`evidence generation` for that generation's current source aliases, and
`evidence show` for located text by artifact reference. Reader-only scans exit
`0` when all discovered entries are supported and reader outcomes completed,
`1` for partial/failed/unsupported/skipped coverage or an invalid generation,
`2` for operational errors, and `130` for keyboard interruption. These
statuses do not establish medical extraction completeness.

`app` and `db` use an internal network with no published ports. Originals and
model storage are mounted read-only into the app; generated data and PostgreSQL
files use the external generated-data directory. PostgreSQL currently trusts
clients on the isolated processing network; it must not be attached to a public
network or given published ports. Docker log capture is disabled for all runtime
services, and PostgreSQL statement/parameter logging is disabled. The reader
routing guard and synthetic parser-diagnostic checks are implemented; full
processing/service egress and diagnostic-leakage validation remain delivery tasks.

The `inference` profile reserves a local model service; the `setup` profile
reserves a network-enabled provisioning service with model storage only. Both
entry points exit with an explicit setup error until the OCR/vision baseline is
implemented. Neither downloads weights. The model volume is outside the image
and repository. GPU configuration will follow selection of the runtime.

## Development

The `src/filebrownie` package separates ingestion, domain-independent evidence,
medical interpretation, deterministic queries, storage, and CLI presentation.
Source discovery, operation locking, inventory storage, and the first evidence
readers are implemented alongside the CLI. Tests use synthetic content and run inside the app image.
Development tools are included in this
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

The normal pytest command skips database integration tests. Run the complete
suite with a separate PostgreSQL service backed by temporary memory storage:

```sh
docker compose -f compose.yaml -f compose.test.yaml --profile test run --rm app pytest
docker compose -f compose.yaml -f compose.test.yaml --profile test stop db-test
docker compose -f compose.yaml -f compose.test.yaml --profile test rm -f db-test
```

The test service has no medical-data mounts. Integration tests create isolated
schemas, use synthetic filenames/content, and never connect to the normal
`db` service. Its data disappears when the test container is stopped.
