# The identity fixture is the published spec of the derivation rule

`tests/fixtures/item_id.json` — the table of GUID sets, raw paths, and the `item_id` each must
produce — is the specification of ADR-0002's rule, not a convenience for one test file. It is
committed, structured, and readable by anything; the Python implementation is checked against it
like any other reader would be.

A future reader finding what looks like ordinary test data should treat it as part of the schema's
public surface. Cases are added and never quietly reinterpreted.

## Why

ADR-0002's rule has to be reproducible by anything holding the same Plex record, or the id is not
really derivable and every claim made for it collapses. Prose can describe first-hit-wins ordering;
it cannot pin what `fs:` hashing does to a path with a trailing slash, or which of two present-but-
empty GUIDs wins. A table of concrete inputs and expected outputs can, and it stays true when the
prose goes stale.

That the fixture is executable here is what keeps it honest. A spec nothing runs drifts from the
code it describes, and the drift is silent.

## Considered options

- **Cases written inline in `tests/test_identity.py`.** Rejected: they are then Python, readable
  only by running Python, and the rule stops being publishable at all.
- **Prose in `docs/schema.md` as the specification, tests written separately.** Rejected: two
  descriptions of one rule, and nothing forces them to agree. The failure is a doc that confidently
  describes behaviour the code stopped having.
- **A hash constant pinning the fixture's bytes, so any edit fails the suite.** Rejected, and
  removed after it was tried. It made every legitimate case addition a red suite plus a constant to
  update, and it defended nothing this store owns — an edit that is wrong is caught by the cases
  themselves being wrong, and an edit that is right should not need ceremony. Worse, the version of
  this guard that existed here pinned the file against *another repository's* copy, which made a
  consumer's state a condition of this store's suite going green.

## Consequences

The fixture must carry the path-hash cases, not only the GUID-priority ones. The `fs:` fallback
depends on a specific 64-bit FNV-1a over a specific canonical path, and it is the part most able to
drift without anyone noticing — which is exactly what happened in
[issue #24](https://github.com/McBrideMusings/plex-db-ex/issues/24), where the rule was right the
whole time and the input was wrong.

`tests/test_identity.py` carries one case the fixture cannot state about itself: that the two
mount cases really do collapse to one id. If somebody edits those cases so the paths no longer
describe the same file, every other assertion still passes while the property they exist to prove
stops being tested.

Whether any other program derives the same ids, and what it does to stay in step, is that
program's concern. This store publishes the rule; it keeps no register of who reads it
(ADR-0001).
