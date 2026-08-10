# The identity fixture is duplicated in both repos and guarded by a hash

`tests/fixtures/entry_id.json` — the table of GUID sets and the `item_id` each must produce —
exists as a full copy in this repo *and* in `etv-station`, rather than in one place read by both.
Each copy's test asserts the file's SHA-256 against a constant recorded in both repos, so editing
one copy without the other turns both suites red with a message naming the other repo.

The duplication is deliberate. A future reader finding the same JSON twice should not
de-duplicate it.

## Why

[ADR-0002](./0002-item-id-is-the-entry-id-string) put the derivation rule in two languages, and
its failure mode is silence: if Python and Rust stop agreeing about what a title is called, every
lookup between them returns nothing and neither side errors — channels simply come back empty.
The fixture is the tripwire, so how it is shared *is* the decision.

Neither repo has CI (no `.github/workflows` in either, checked). The only moment a human learns
about drift is when they run the suite locally, which rules out anything whose guarantee depends
on a pipeline.

## Considered options

- **One copy, read across repos by relative path.** Tidier, no sync. Rejected: it holds only
  while both checkouts sit side by side. A clone of `etv-station` alone finds no file, and the
  natural failure is a skipped test — silence, which is the thing being defended against.
- **Publishing the fixture as a package both fetch.** Rejected: a release step for a file that
  changes about twice a year.
- **`etv-station` as the single named owner**, with this repo vendoring a copy. Equivalent in
  mechanism; the ownership arrow adds nothing while both repos are edited by the same person.

## What it caught immediately

Cross-checking the first version of the fixture against `etv-station`'s real derivation found
that neither repo had ever verified value-level agreement: that repo's tests pin the `fs:`
*format* — prefix plus sixteen hex digits — and the priority rules, but no concrete hash. Both
implementations could have produced different ids for the same GUID-less file and every test in
both repos would still have passed.

All 24 cases agree. The point is that this was previously unknown, not assumed.

## Consequences

Changing the derivation rule is now a two-repo commit, on purpose. The hash constant is what makes
forgetting the second one loud instead of silent.

The fixture must carry the path-hash cases, not only the GUID-priority ones — the fallback depends
on both languages implementing the same 64-bit FNV-1a, and that is the case most able to drift
without anyone noticing.
