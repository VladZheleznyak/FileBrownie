# FileBrownie

FileBrownie is a private, local tool for retrieving medical histories from one
folder of documents. Phase 1 is a CLI that answers two kinds of questions, with
source evidence and visible gaps:

- laboratory history by analyte or analyte group
- visit history by specialty or specialty group

It runs entirely in Docker Compose, uses only local inference during medical
processing, and never modifies source documents. Current inputs are PDF and
JPG/JPEG; other formats belong to vNext. The Phase 1 pipeline is implemented
and passes its synthetic checks: bounded PDF/JPEG readers, Tesseract OCR, a local
vision model, grounded fact verification, generation activation, dictionary-aware
`labs` and `visits` histories, manual checks, and erasure. It has **not** been
evaluated on real documents, and nothing here establishes extraction accuracy or
completeness; check results against the source documents. Start with
[Getting started](#getting-started). Implementation detail is in
[Phase 1 workflow](#phase-1-workflow).

Keep the MVP simple: terminal histories, source references, visible uncertainty,
and essential recovery. Optional exports, evidence crops, duplicate heuristics,
and richer workflows belong to vNext.

Phase 2 hardening, including centralized local operational logging, is in place.
JSON formatters, richer PII screening, and Sentry adapters remain future work.
Current phases keep logs sanitized and enable no external telemetry. User-led
real-data review is still open.

- [Architecture](docs/architecture.md): scope, components, data model, and flows
- [Todo](docs/todo.md): ordered remaining work

## Getting started

After cloning, point FileBrownie at one folder of PDF and JPEG documents,
provision the local models, scan that folder, then ask laboratory or visit
questions. `doctor` and `status` report whether the system is ready and which
index is active. Every command below runs through Docker Compose.

### Set up after cloning

You need Docker with Compose (Docker Desktop with WSL integration is the usual
setup) and an NVIDIA GPU for the local vision model.

1. Copy `.env.example` to `.env` and set two absolute directories outside this
   repository. `FILEBROWNIE_SOURCE_DIR` is the folder you dedicate to documents.
   `FILEBROWNIE_DATA_DIR` holds the database, evidence, and logs. The two
   directories must be separate, and neither may sit inside the other. Create
   both directories, and a `postgres` subdirectory inside the generated-data
   directory, before the first Compose command. A missing bind directory is an
   error.

   ```sh
   cp .env.example .env
   ```

   Edit `.env`:

   ```sh
   FILEBROWNIE_SOURCE_DIR=/absolute/path/to/documents
   FILEBROWNIE_DATA_DIR=/absolute/path/to/filebrownie-data
   FILEBROWNIE_UID=1000
   FILEBROWNIE_GID=1000
   ```

   Set `FILEBROWNIE_UID` and `FILEBROWNIE_GID` to the numeric user and group
   that own those directories (`id -u` and `id -g` in WSL). That user needs
   read access to the source folder and write access to the generated-data
   folder. Keep real records out of this checkout.

2. Put PDF, JPG, and JPEG files in the source folder. Other formats are listed
   during a scan and left unread.

3. Build the app, start the database, download the pinned model files once,
   start local inference, and initialize the schema:

   ```sh
   docker compose build app
   docker compose up -d --wait db
   docker compose --profile setup run --rm model-setup
   docker compose run --rm app filebrownie model-setup --verify
   docker compose --profile inference up -d model
   docker compose run --rm app filebrownie migrate
   ```

   `model-setup` is the command that uses the network. It downloads Tesseract
   language data (English, Russian, Ukrainian) and the vision weights (about
   5.5 GB) into Docker model storage, outside the repository and away from your
   documents. Later processing stays on the internal network. If the weights
   are missing or altered, processing stops with a setup error; run the
   `model-setup` commands above again.

### Scan the source folder

```sh
docker compose run --rm app filebrownie scan
```

`scan` reads every supported file in `FILEBROWNIE_SOURCE_DIR`: text extraction,
OCR, the local vision model, and grounded verification. Source files stay
read-only. The first successful scan becomes the active index. A later scan
stays staged when the coverage guard finds new failures, warnings, or fewer
facts; the previous index remains the one queries use. Review the printed
findings, then accept that generation with:

```sh
docker compose run --rm app filebrownie activate <generation-uuid> --force
```

One CLI operation runs at a time. A question started during a scan exits with
`OPERATION_BUSY`.

### Ask a question

Questions use the active index. A term may be English, Russian, or Ukrainian.
`--from` and `--to` accept `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`.

```sh
docker compose run --rm app filebrownie labs ferritin
docker compose run --rm app filebrownie labs iron-panel --from 2023 --to 2024-06
docker compose run --rm app filebrownie visits ophthalmology
docker compose run --rm app filebrownie evidence fact <ref-from-table>
```

`labs` answers laboratory history by analyte or analyte group. `visits` answers
visit history by specialty or specialty group. Each row shows a verification
state and an evidence reference. `verified` means the label, value, and any
unit were found together in located text. Rows marked `inferred from filename`
use a conservative date read from the source path when the document body has no
usable timeline; that date is not verified document evidence. Open a reference
with `evidence fact` and compare important results with the source document.

### Check system status

```sh
docker compose run --rm app filebrownie doctor
docker compose run --rm app filebrownie status
docker compose run --rm app filebrownie status --database
```

`doctor` is the readiness check. It reports separate source and generated-data
folders, no route to the internet, read-only source and model mounts, and a
ready database schema, OCR data, vision weights, and inference service.
Each line is `ok` or `FAIL`. Exit code `0` means every check passed; `1` means
at least one failed. Run it after setup and whenever a scan or question fails
with a setup error.

`status` reports that scan and query commands are available in this build.
`status --database` opens the database and prints the active generation, or
that none is active, plus every saved generation's kind, state, start time,
source count, and reason. Start the database first when it is stopped:

```sh
docker compose up -d --wait db
```

## Repository safety

Keep real documents, extracted data, credentials, and other personal information out of Git. Use synthetic examples in committed files.

## Scaffold setup

Day-to-day setup, scanning, questions, and status are in
[Getting started](#getting-started). This section records directory rules,
inventory behavior, and exit codes.

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
`doctor` rejects overlapping container paths and, when `FILEBROWNIE_HOST_SOURCE_DIR`
and `FILEBROWNIE_HOST_DATA_DIR` are set, overlapping host paths. It cannot resolve
host-only symlink aliases or verify that generated data is outside Git.

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
generation, and `2` for operational errors. Every activation, including forced
activation, repeats the same inventory and fingerprint check.

Saved filenames and fingerprints are sensitive derived data stored only in the
external PostgreSQL data directory. The full scan, activation, histories,
dictionary review, checks, and erasure are described in the
[Phase 1 workflow](#phase-1-workflow).

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
before creating its raster. Standalone `read` does not run OCR or vision; a full
`scan` does (see [Phase 1 workflow](#phase-1-workflow)).

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

The `db` service must be running. Plain `scan` runs the full pipeline (see the
[Phase 1 workflow](#phase-1-workflow)); `--readers-only` processes each
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
generation rows. Superseded generations are pruned after activation; `erase derived`
and `erase all` remove generated data as described below.

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
services, and PostgreSQL statement/parameter logging is disabled. `doctor` and
scan-time checks enforce egress isolation, read-only mounts, and (when configured)
separate host source and generated-data folders.

To inspect the database from DBeaver (or another client on the same machine),
merge `compose.dbeaver.yaml`, which publishes PostgreSQL on loopback only:

```sh
docker compose -f compose.yaml -f compose.dbeaver.yaml up -d --wait db
```

Connect to `localhost:15432`, database `filebrownie`, user `filebrownie`, no
password, SSL disabled. Omit `compose.dbeaver.yaml` when you do not need a host
client.

The `inference` profile runs the local vision model service (llama.cpp server,
pinned by image digest, on the internal network, offline, with server logging
disabled). The `setup` profile is the only service with network access and mounts
model storage only. See the [Phase 1 workflow](#phase-1-workflow).

## Phase 1 workflow

### Provision models once (network-enabled setup service)

```sh
docker compose --profile setup run --rm model-setup          # downloads and verifies
docker compose run --rm app filebrownie model-setup --verify # offline check
```

Downloads are pinned by URL, size, and SHA-256 in `src/filebrownie/models_manifest.json`
(Tesseract `tessdata_best` 4.1.0 for English/Russian/Ukrainian; Qwen2.5-VL-7B-Instruct
Q4_K_M with a Q8_0 multimodal projector, about 5.5 GB). Weights live in the `models` volume,
outside the image and the repository. Processing never downloads; missing or altered files
are a setup error with these instructions.

### Start the model service, check readiness, scan

```sh
docker compose up -d --wait db
docker compose --profile inference up -d model
docker compose run --rm app filebrownie migrate
docker compose run --rm app filebrownie doctor     # isolation, read-only mounts, readiness
docker compose run --rm app filebrownie scan       # OCR + vision + verify + auto-activate
```

`doctor` prints fixed labels only. It checks that there is no route to the internet and that
connecting to public addresses fails, that source and model mounts are read-only, and that the
database, OCR data, vision weights, and inference service are ready. A full `scan` repeats the
network checks and refuses to run when the internet is reachable. A first scan with usable
evidence activates itself; a replacement scan stays staged when the conservative guard finds new
failures, warnings, or fewer facts, and you decide with `activate <generation> [--force]`.
Forced activation never overrides interrupted or source-changed generations.

### Ask questions

```sh
docker compose run --rm app filebrownie labs ferritin --from 2023 --to 2024-06
docker compose run --rm app filebrownie labs iron-panel
docker compose run --rm app filebrownie visits ophthalmology
docker compose run --rm app filebrownie evidence fact <ref-from-table>
```

Terms may be English, Russian, or Ukrainian. Output separates confirmed rows from candidates
(terminology, conflicting readings, uncertain dates), keeps differing units apart, counts rows
excluded for missing dates, lists unmatched text mentions found by the keyword sweep, and lists
coverage warnings. Every row carries a verification state (`verified` means the label, value and
any unit were found together in located text, nothing more) and an evidence reference.

### Decisions, checks, and erasure

```sh
docker compose run --rm app filebrownie dict review
docker compose run --rm app filebrownie dict accept "label as printed" ferritin
docker compose run --rm app filebrownie check record --fact <ref> --verdict wrong-value --note "..."
docker compose run --rm app filebrownie check record --source report.pdf --page 2 --verdict missed
docker compose run --rm app filebrownie check list
docker compose run --rm app filebrownie check summary
docker compose run -it --rm app filebrownie erase derived
docker compose run -it --rm app filebrownie erase all   # typed confirmation
```

Checks are bound to document content hash and location, never to a path, and survive pruning and
`erase derived`; evidence that is gone is labelled unavailable. `erase derived` removes
generations, caches, rasters, proposals, and logs while keeping dictionary decisions and checks.
`erase all` also removes them and needs the typed phrase. Sources are never touched.
If database rows are deleted but filesystem cleanup fails, the command reports
`ERASE_CLEANUP_INCOMPLETE`; rerun the same erase scope to finish cleanup.

### Runtime baseline and measurements (synthetic data, RTX 3060 12 GB)

- OCR: Tesseract 5 on CPU, 150 DPI rasters upscaled for recognition, bounding boxes mapped back.
- Vision: Qwen2.5-VL-7B-Instruct Q4_K_M, llama.cpp server with schema-constrained JSON,
  temperature 0. About 7 s per synthetic page including OCR and activation; GPU memory about
  7 GB for the model (8.8 GB in use on the shared GPU), so OCR on the CPU leaves headroom.
- Queries over 1,500 synthetic facts finish in well under a second (allowance: five minutes).
- Synthetic English, Russian, and Ukrainian lab pages extracted every row; every row was
  grounded in OCR text for English and Russian, and one Ukrainian label (an OCR misread) stayed
  `unverified reading`, as designed.
- Degraded synthetic pages (blur, low contrast, handwriting-like scribble, stamp): no verified
  fact contradicted the page. A 3-degree skew left all rows `unverified reading` because row
  grouping depends on text lines being level.

### Known limitations

- Not evaluated on real documents. Event and verification heuristics stay provisional.
- `verified` shows text presence in one row; prose shaped like a result row can verify. Its
  evidence points at that prose, so inspect `evidence fact` for important results.
- Skewed or rotated scans (other than EXIF/90-degree handling) mostly yield unverified readings.
- Handwriting is flagged, not interpreted. Specimen text the page does not state is discarded.
- Per-page vision output is a claim; rows missed by the model are only caught by the table
  discrepancy warning and the keyword sweep.
- One CLI operation at a time; queries are unavailable during scans.
- Sanitized local operational log: `<generated-data>/logs/filebrownie.log` (counts, timings,
  opaque IDs, codes; 256 KiB x 4 files, 30-day age limit; removed by erase). No external telemetry.

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

If routine `docker compose run --rm app ...` commands warn about an orphan
`db-test` container, stop and remove the test database service with the commands
above, or add `--remove-orphans` to the run command.
