# Phase 1 and Phase 2 implementation checklist

This checklist tracks implementation of the [project pivot](../PROJECT_PIVOT_2026-10-03.md),
[decisions](decisions.md), and [architecture](architecture.md). It adds no product
scope and does not replace those documents.

Completed Phase 1 and Phase 2 work is recorded in the architecture's implemented
baseline. Synthetic success does not establish medical extraction accuracy.
**User-led real-data review (D37) is in progress** on private documents mounted
through Compose; nothing from that corpus is committed. Run application commands
and checks through Docker Compose.

## Remaining

- [ ] Evaluate extraction/retrieval against user-provided local manual checks
  after Phase 1 delivery. Record observed errors and omissions; percentages need
  a reviewed scope and denominator. This task depends on those checks becoming
  available and remains incomplete until then (D24, D25, D37).
  Ready for it: `check record|list|summary` count observed errors and omissions without
  percentages.

D36 and D41 stay provisional pending real-data review. Deskewing is a known gap.
Deferred features and logging integrations remain in the pivot's vNext backlog;
neither phase enables external telemetry.
