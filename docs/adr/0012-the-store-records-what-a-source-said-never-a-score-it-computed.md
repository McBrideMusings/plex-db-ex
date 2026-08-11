# The store records what a source said, never a score it computed

No table in `plexdb.db` holds a number this repository derived by applying a formula, a threshold,
or a judgement to something a source told it. Rank, count, size, popularity and duration are
recorded as the source gave them. Whatever combines them into "how good is this" is computed by
the reader, at read time, from those raw columns.

## Why

This was decided three times in a row before anyone noticed it was one decision.

**A play does not store whether it counts.** `plays` records `seconds_watched` and nothing else
about sufficiency. `docs/schema.md` says why: a stored `counts_as_signal` boolean *"would freeze
today's threshold into every row, and changing it later would mean rewriting history."* A reader
that wants 30 seconds writes `WHERE seconds_watched >= 30`; a reader that wants 120 writes 120.
Both read the same rows. Had the boolean been stored at 30, the second reader could not get its
answer at all — the information needed to compute it was thrown away at write time.

**An edge stores position, not importance.** `edges.rank` is *"the source's own ordering, stored
verbatim — never collapsed to a boolean — so a #2 recommendation stays distinguishable from a
#20."* The tempting shortcut is a `strong`/`weak` flag, or a 0–1 score folding rank together with
list length. Either one destroys the distance between #2 and #20 permanently, and no consumer can
reconstruct it.

**A collection membership stores rank, size, likes and mentions, not weight.** The reserved shape
in `docs/schema.md` carried a single `weight REAL` for two years before anything was built on it.
[Issue #33](https://github.com/McBrideMusings/plex-db-ex/issues/33) asked what number should go in
it and found the question had no answer: for an MDBList entry the honest quantity is "third of a
hundred", for a Letterboxd list it is "on a list eight thousand people follow", and for a subreddit
it is "named in seven comments". Those are three different measurements. Any formula flattening
them into one comparable float is a claim about how they trade off against each other — a claim
this store has no basis for and no way to revisit, since the inputs would not survive into the
row. The column was removed and the four raw facts stored instead.

**The one score this project does produce is computed at read time and stored nowhere.**
`plexdb-reader`'s `taste_vector_for` weighs a title by `sqrt(seasons watched)` and splits its mass
across attributes ([ADR-0011](./0011-a-taste-vector-weights-a-season-not-an-episode)). That
formula lives in the reader crate. Nothing in the SQLite file records a taste score, and changing
the damping tomorrow needs no migration and rewrites no history — every input is still sitting in
`plays` and `items` exactly as Plex reported it.

**The reason this rule is load-bearing here specifically is ADR-0001.** The schema is the public
API, several projects read it, and there is no version negotiation — a schema change breaks every
consumer at once. A stored score is therefore worse here than in an ordinary application: changing
the formula means a migration across repositories that cannot be coordinated, and *not* changing it
means every consumer is stuck with one project's ranking opinion baked into their data.

## Consequences

**Reading gets more work, deliberately.** A consumer wanting one number per collection membership
computes it from `rank`, `size`, `likes` and `mentions` itself. That is the cost, and it is
accepted: four columns and a formula in the consumer beats one column and no way back.

**Nullability carries meaning and must not be collapsed.** Because sources fill only what they
genuinely have, `rank IS NULL` means "this source has no ordering" and is not the same as rank 1.
`likes IS NULL` means "this source publishes no popularity number" and is not zero. A reader that
defaults these to 0 destroys exactly the distinction the shape exists to preserve — which is why
[#29](https://github.com/McBrideMusings/plex-db-ex/issues/29) requires `Option<_>` on the Rust side
rather than defaulted integers.

**A future column proposing to store a computed number should be refused, and this ADR is the
reason to cite.** The tell is a column whose value cannot be traced back to a single thing a source
said: a `score`, a `confidence`, a `strength`, a `relevance`, a normalised 0–1 anything. If the
inputs are in the store, the reader can compute it. If the inputs are not in the store, storing the
output hides that they are missing.

**This does not forbid recording a source's own score.** MDBList's `likes` and a rating out of ten
are things the source said, and they belong in the store verbatim. The line is who did the
arithmetic, not whether the value happens to be a number.

**Bookkeeping is exempt but must be marked.** `_fetched`-style sentinel keys in `enrichment` are
this repository's own values, not a source's, and they exist so staleness can be checked. They are
prefixed with `_` and readers skip them. On one real account, an earlier reader that did not skip
them found `_fetched` sentinels making up **43.6%** of a taste vector's total weight — the store
staying uninterpretive does not save a reader that interprets bookkeeping as data.
