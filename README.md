# FileBrownie

FileBrownie is a private, local tool for retrieving medical histories from one
folder of documents. Phase 1 is a CLI that answers two kinds of questions, with
source evidence and visible gaps:

- laboratory history by analyte or analyte group
- visit history by specialty or specialty group

It runs entirely in Docker Compose, uses only local inference during medical
processing, and never modifies source documents. Current inputs are PDF and
JPG/JPEG; other formats belong to vNext. No capability is implemented yet.

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
