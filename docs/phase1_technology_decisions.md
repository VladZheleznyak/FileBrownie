# Phase 1 Technology Decisions

This record captures the technology choices for Phase 1 and the reasoning behind them. It supersedes the suggested stack in [the project plan](filebrownie_project.md) where the two differ.

Phase 1 scope is unchanged: answer "What is my passport number?" from a local directory, select the current passport over expired ones, and return the source file as evidence.

---

## Verified Environment

These decisions assume the following, which was measured rather than assumed:

| Property | Value |
| --- | --- |
| Host | WSL2 on Ubuntu 24.04, 16 cores, 31 GB RAM, 931 GB free |
| GPU | NVIDIA GeForce RTX 3060, 12288 MiB VRAM, driver 591.86 |
| Container GPU access | Working. `nvidia` runtime registered, `/dev/dxg` present, `nvidia-smi` sees all 12 GB from inside a container |
| Docker | Engine 29.1.3, Compose v2.40.3 |
| Python | 3.12.3 |

The GPU is the 12 GB desktop RTX 3060, not the 6 GB laptop part. The 7B vision model choice below depends on this.

---

## Decision 1 - No LLM inside the application

**Decision:** Phase 1 contains no language model for query understanding or answer generation.

The MCP client is the language model. Cursor interprets "What is my passport number?" and chooses which tool to call; FileBrownie exposes deterministic tools and returns structured results. Natural-language understanding is the host's job, not ours.

This leaves exactly one place where a model is unavoidable: converting a photograph or scan of a passport into text.

**Consequences:**

- Query parsing, intent extraction, and response generation need no model, no prompt templates, and no token budget.
- It directly satisfies the deterministic control plane principle, since no model output influences retrieval or ranking.
- Phase 1 has no answer-generation quality to evaluate, only classification, extraction, and version selection.

---

## Decision 2 - Local vision model for text extraction

**Decision:** Use Qwen2.5-VL 7B served by Ollama as a Compose service with a GPU reservation.

**Alternatives considered:**

| Option | Why not chosen |
| --- | --- |
| Tesseract | Poor on ID documents. Glossy laminate, background guilloche patterns, rotation, and non-standard fonts all degrade it. Needs preprocessing to be usable at all. |
| RapidOCR (ONNX) | Better than Tesseract on photos and pip-installable, but still produces unstructured text requiring separate field extraction. Retained as a fallback for PDFs without a text layer. |
| Cloud vision API | Highest accuracy and least code, but sends passport scans to a third party, contradicting the local-first principle. Requires credential handling and redaction much earlier than planned. |

Ollama supports JSON-schema-constrained output, so extraction results land directly in a validated Pydantic model rather than requiring parsing of free-form text.

At roughly 6 GB quantized, the model leaves adequate headroom in 12 GB of VRAM. Per-page latency of a few seconds is irrelevant at Phase 1's scale of under a dozen files.

**Deferred:** A pluggable multi-backend extractor with comparative evaluation is deferred to after Phase 2. Phase 1 defines a single narrow text-extraction interface with one implementation behind it, so adding a second backend later is an addition rather than a refactor.

---

## Decision 3 - MRZ as the primary passport extraction path

**Decision:** Parse the Machine Readable Zone first and validate it with its check digits. Reading the visual inspection zone is a fallback only.

A TD3 passport carries two 44-character MRZ lines encoding the document number, nationality, date of birth, sex, and expiry date. Every field carries a check digit.

This is the single highest-leverage choice in Phase 1:

- The model's only task is reading two lines of OCR-friendly monospace text, which is far easier than interpreting a document layout.
- Check digits give a deterministic correctness signal, replacing self-reported model confidence with arithmetic verification.
- Expiry date arrives for free, which is what version selection needs.
- Invalid check digits mean the extraction fails closed rather than returning a plausible wrong number.

Use the `mrz` package for parsing and validation. It implements TD1, TD2, TD3, MRVA, and MRVB per ICAO 9303, so identity cards, residence permits, and visas are covered by the same component later. Note that it is GPLv3; acceptable for a local personal project, but a constraint to revisit if FileBrownie is ever distributed.

`passporteye` was rejected as effectively unmaintained and for pulling in scikit-image.

### Multiple issuing countries

The MRZ is an international interoperability standard, not a national convention, so multiple passport nationalities is the case it was designed for rather than an obstacle to it:

- ICAO Annex 9 Standard 3.10 required all Contracting States to issue only machine-readable passports from 1 April 2010.
- Standard 3.10.1 required non-machine-readable passports to be out of circulation by 24 November 2015.
- The TD3 layout is identical regardless of issuer. The issuing country is itself an MRZ field at positions 11–13.

What genuinely varies by country is the **visual inspection zone**: label languages, field positions, date formats, and native scripts all differ. Cross-country variation is therefore an argument for treating the MRZ as primary and the visual zone as a reluctant fallback, not the reverse.

### Cross-country cases that must be handled

These are issuer-dependent and are the real source of per-country risk:

| Case | Behaviour required |
| --- | --- |
| Document number longer than 9 characters | Per ICAO 9303-4 note j), the 9 principal characters occupy positions 1–9, position 10 holds a filler `<` **instead of a check digit** to signal truncation, and the remainder begins at positions 29–35 followed by its check digit and a filler. A naive parser returns a silently truncated passport number. This must be detected and reassembled, never truncated. |
| Unused optional/personal number field | The position 43 check digit may be either `0` or `<` at the issuing State's option. A validator demanding one form will wrongly reject valid passports from some countries. |
| Two-digit years | Dates are `YYMMDD` with no century. A documented windowing rule is required, and it must handle expired passports correctly, since that is exactly what version selection depends on. |
| Transliterated and truncated names | Non-Latin-script issuers transliterate into the MRZ under rules that vary nationally, and long names are truncated per ICAO 9303-3. Holder names from the MRZ are not reliable identity keys across countries. |
| Composite check digit scope | It covers positions 1–10, 14–20, and 22–43, excluding nationality (11–13) and sex (21). Those two fields are therefore unverified and carry no arithmetic guarantee. |

Validate each field's check digit individually rather than relying on the composite digit alone, and persist the issuing country so per-country accuracy becomes measurable rather than anecdotal.

### Known limitations

- **No issue date.** The MRZ does not encode it, though the plan lists it as version-ranking signal #4. Ranking logic must not assume issue date is ever populated.
- **Expiry is not a universal substitute.** For travel passports, expiry date is a sound recency proxy because validity periods are fixed. This does not generalize: some document types carry no expiry date at all. See Decision 9.
- **Pre-2015 archives.** Passports without any MRZ should be out of circulation, but FileBrownie deliberately retains expired documents, so a handwritten or non-machine-readable passport from the 2000s is a plausible input. These fall to the visual-zone path and should be marked low-confidence for review.
- **Unverified fields.** Nationality and sex are excluded from the composite check digit, as above.
- **Other document types.** Health cards and driver's licences have no MRZ at all and will need a different extraction path entirely.

---

## Decision 4 - PostgreSQL in Compose from the start

**Decision:** Use the `pgvector/pgvector:pg17` image, with SQLAlchemy and Alembic.

SQLite was rejected despite needing no infrastructure. Full-text search syntax and JSON column behaviour differ enough from PostgreSQL that the migration cost outweighs the saved setup, and Docker is a hard project requirement anyway.

The pgvector image is used even though Phase 1 does not enable the extension. It is the same PostgreSQL, and it avoids swapping images when embeddings are eventually needed.

**Deferred:** The vector extension stays unused, and full-text search is not built. "What is my passport number?" resolves entirely through structured metadata, so neither is on the critical path.

---

## Decision 5 - MCP over streamable HTTP, targeting Cursor

**Decision:** Serve MCP over streamable HTTP on a container port. Cursor is the Phase 1 client, configured by a committed project-local `.cursor/mcp.json` containing a `url`.

This follows from containerization. With the server in a container, stdio transport forces the client to launch `docker compose exec -T` as a subprocess, coupling client configuration to container lifecycle and making failures hard to diagnose. An HTTP port is what a container exposes naturally.

Codex was considered. It configures MCP servers in `~/.codex/config.toml`, outside the repository, and stdio is its best-established transport, which is the awkward path here. Since it would consume an identical server, adding it later is a small configuration change and this is not a lock-in.

**Deferred:** FastAPI. An MCP server plus a Typer CLI serves both consumers in Phase 1, and HTTP endpoints can be added when something actually requires them.

---

## Decision 6 - Containerized toolchain with uv

**Decision:** All dependencies and runtime live in Docker, managed by `uv` inside the image, per the containerization rules in [AGENTS.md](../AGENTS.md). No host virtualenv.

A useful side effect: system packages become cheap to add, which changes the PDF library calculus. Rasterizing scanned PDFs normally points to PyMuPDF, which is AGPL-3.0. Inside a container, `pdf2image` plus the `poppler-utils` apt package does the same work under permissive licensing.

The documents directory is bind-mounted read-only, enforcing the preserve-originals principle at the container boundary rather than in application code.

---

## Decision 7 - Single-person scope, but subject is still modeled

**Decision:** Phase 1 implements no subject matching. The subject field is modeled per Decision 9 but is either a single implied person or null.

No matching logic means no name normalization, no transliteration handling, and no entity resolution in this phase. The household entity model arrives in Phase 6 and brings real matching with it.

The field is modeled now rather than added later because subject is part of logical document identity, and because a single person may still hold documents of several kinds and issuers. Version selection therefore ranks within a logical document, using validity and `version_date`, never across subjects.

---

## Decision 8 - Synthetic test fixtures

**Decision:** Generate passport-like test images with Pillow, including valid MRZ lines produced by the `mrz` package.

Real passports are the most PII-dense documents imaginable and cannot be committed under the privacy rules in [AGENTS.md](../AGENTS.md). Synthetic fixtures are committable, make tests deterministic, and make the required expired-versus-valid pair trivial to construct.

Because passports come from several issuing countries, the fixture set must span issuers rather than testing one layout repeatedly. Cover at minimum:

- several different issuing country codes, with differing visual-zone layouts
- a document number longer than 9 characters, exercising the overflow and reassembly path
- an unused personal number field using each permitted check-digit form, `0` and `<`
- a transliterated non-Latin-script holder name, and a name long enough to be truncated
- an expiry date whose two-digit year falls either side of the century window
- a passport with no MRZ at all, forcing the visual-zone fallback

The `mrz` package generates as well as validates, so these are constructed from field values rather than hand-written strings.

---

## Decision 9 - Thin universal core with type-dependent payload

**Decision:** Only document type and a last-version date are universally present. Every other field is type-dependent, including whether an expiry date exists and whether a person is involved at all.

This is the structural consequence of supporting documents such as a Canadian passport, a Ukrainian passport, a Ukrainian internal passport, and a vehicle registration card within one system. They share almost nothing.

### Universal core

Present on every document version, as real typed columns:

| Field | Notes |
| --- | --- |
| `document_kind` | See taxonomy below |
| `issuer` | Issuing country or authority; nullable for documents with no issuer |
| `version_date` | The last available version date, used for recency ranking |
| `version_date_source` | Which signal produced `version_date`, since it is not always the same |
| `content_hash` | Identity of the original bytes |
| `source_item_id` | Where it came from |
| `processing_state` | discovered, extracting, indexed, failed, quarantined |
| `validity_status` | Computed per type, see below |

`version_date` needs an explicit, documented fallback chain because no single date is universal: issue date when extracted, then a type-specific document date, then the source item's modification time. Recording `version_date_source` keeps ranking explainable, which the demo scenario requires.

### Taxonomy: two axes

Document type is a pair, not a flat enum:

```text
document_kind   passport | internal_passport | health_card | driver_licence | vehicle_registration | ...
issuer          CA | UA | ... | null
```

So a Canadian passport is `(passport, CA)` and a Ukrainian internal passport is `(internal_passport, UA)`. Note that `passport` and `internal_passport` are different kinds, not one kind with two issuers, because they are genuinely different documents with different fields.

Field schemas are keyed on `document_kind`, so all passports share one schema regardless of issuer. Issuer-specific quirks are optional overlays registered only where fields actually differ. A flat enum of `ca_passport`, `ua_passport`, `ua_internal_passport` was rejected because it multiplies schemas by issuer and loses the reuse that makes a new country cheap to add.

### Subject: nullable and polymorphic

A document's subject is not always a person. A vehicle registration's subject is the vehicle, identified by VIN, and its registered keeper may be absent or be a company rather than a household member.

The subject is therefore a nullable polymorphic reference resolved per type:

```text
subject_type   person | vehicle | property | organization | null
subject_id     nullable
```

This corrects the logical-identity key from the version-selection section, which assumed an owner is always present:

```text
logical document = (document_kind, issuer, subject)
```

with a null subject being a legitimate value rather than a missing one.

### Validity as a per-type strategy

Validity is not universally a date comparison. Each kind registers a deterministic, independently unit-testable strategy:

| Strategy | Applies to | Rule |
| --- | --- | --- |
| Expiry comparison | Travel passports, driver's licences, the 2016 Ukrainian ID card | Compare expiry date against now |
| Age-triggered | 1994-model Ukrainian internal passport booklet | No expiry date exists; validity is unlimited but requires photographs affixed at ages 25 and 45, so the rule depends on the holder's age |
| Lifetime valid | Birth certificates, diplomas | Always valid |
| Unknown | Anything unclassified or low-confidence | Fail closed; never assert validity |

The age-triggered case is the one that justifies the whole abstraction. The 1994 Ukrainian booklet has no expiry date whatsoever under Regulation 2503 §8, and all such booklets remain valid today, so any model assuming a nullable expiry means "always valid" would be wrong in both directions.

`validity_status` is computed at ingestion and stored, so retrieval filters stay deterministic and no strategy runs at query time.

### Type-dependent fields in JSONB

Type-specific fields live in a JSONB column on `document_versions`, not in per-type tables and not in an EAV table. Per-type tables would require a migration for every new document kind, which defeats the goal of adding a country or document type cheaply.

Two constraints shape the implementation:

- **GIN indexes do not serve range predicates on values extracted from JSONB.** Any date that drives ranking or filtering must be a real typed column in the universal core, never only a payload key. This is why `version_date` and `validity_status` are promoted rather than left in the payload.
- **Phase 2 requires per-field encryption, confidence, and provenance.** A flat JSONB value map cannot carry that metadata, so payload entries use an envelope convention:

```json
{
  "document_number": { "value": "<ciphertext>", "encrypted": true, "confidence": 0.99, "provenance": "mrz" },
  "issuer_country":  { "value": "UKR", "confidence": 0.99, "provenance": "mrz" }
}
```

Validation happens at the application boundary against the Pydantic schema registered for the document kind, so an unknown kind or a schema mismatch fails closed.

**Consequence for the storage model:** this replaces the separate `document_fields` table proposed in the plan's storage model. Its per-field confidence and provenance columns are preserved as envelope keys instead. Encrypted values are not queryable in any storage format, so nothing is lost by moving them into the payload.

---

## Resolved Dependency Set

Compose services:

| Service | Image | Notes |
| --- | --- | --- |
| `app` | Built from `python:3.12-slim` | Pipeline, MCP server, and CLI. Includes `poppler-utils`. |
| `db` | `pgvector/pgvector:pg17` | Extension unused in Phase 1 |
| `ollama` | `ollama/ollama` | GPU reservation, named volume for model weights |

Python dependencies:

| Area | Packages |
| --- | --- |
| Core | `pydantic`, `pydantic-settings` |
| Database | `sqlalchemy`, `alembic`, `psycopg[binary]` |
| Interface | `mcp`, `typer` |
| Documents | `pypdf`, `pdf2image`, `pillow`, `puremagic` |
| Extraction | `mrz`, `ollama` |
| Development | `pytest`, `ruff`, `mypy`, `structlog` |

Model: `qwen2.5vl:7b`.

---

## Explicitly Deferred

| Item | Deferred to | Reason |
| --- | --- | --- |
| FastAPI / HTTP API | When a consumer exists | MCP and CLI cover Phase 1 |
| pgvector and embeddings | When semantic retrieval is needed | The target query needs no vector search |
| PostgreSQL full-text search | Alongside semantic retrieval | Structured metadata is sufficient |
| Background job queue | Phase 2 or later | Synchronous CLI ingestion suits a small directory |
| Encryption at rest | Phase 2 | Already scoped there in the plan |
| Multi-backend extractor comparison | After Phase 2 | Interface seam defined now, second backend later |
| Owner matching | Phase 6 | Single-person scope |
| File watching | Phase 3 | Manual ingestion is adequate |

Not deferred, because retrofitting them is expensive: content hashing, Pydantic schemas throughout, and the explicit processing-state machine.
