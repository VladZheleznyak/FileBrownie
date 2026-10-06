# Todo

Ordered work that is still open. Design rules and deferred scope live in
[architecture.md](architecture.md).

1. **Re-scan without force.** Run a full `scan` on the private folder without
   `--force`. Compare the activation guard’s extracted and verified fact totals
   to the active index. Do not commit paths, values, or generation output.

2. **Photo tables.** If a dense photo still yields no stored facts, improve OCR
   row grouping for that layout. Do not add new input formats.

3. **Manual checks.** Evaluate extraction and retrieval against local expectations
   using `check record`, `check list`, and `check summary`. Record observed errors
   and omissions; do not publish an accuracy percentage without a reviewed scope
   and denominator.

4. **Post-review tuning.** After real-document review, decide whether to change
   deskew, reader limits, text-layer heuristics, or the OCR/vision baseline.

5. **Installation hardening (later).** Resolve host-only symlink aliases in path
   overlap checks and verify generated data directories are outside Git. Not a
   product feature.
