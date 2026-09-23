# Public BBF definition and conversion review

The [guide](../../protocol/bbf-data-elements.md) explains the selected source
authority and reproduces the work on HOST without a VM or pod connection.

- `source-review.json` records the actual publisher XML hashes, 33 matching
  parameter definitions, statistics-type comparison and links to exact fields.
  Full source files are retained outside Git.
- `unit-results.xml` and `unit-tests.log` retain the unit-suite result at this
  implementation increment. New cases verify exact encoded bytes, distinct
  units, missing/sentinel rejection, wide counters and source-hash rejection.

The source audit is reproduced with
`python3 scripts/review-bbf-data-elements.py NEW-RESULT.json`; use `--offline`
when the exact XML files are cached. Publisher changes require a new deliberate
review. The source result compares the selected definitions, not all parameters
in the complete USP and CWMP models.

There is no live experiment in this increment. The bridge enables no runtime
publisher and supplies no new physical or sustained-operation evidence.
WFA DEr3 equivalence, actual source semantics, ESP estimation and complete native
report delivery remain pending. Earlier native evidence is preserved unchanged.
