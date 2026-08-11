# Bookkeeping never shares a namespace with facts

An `enrichment` namespace holds facts about titles, or one module's own fetch cursors, and never
both. A reader that asks for a namespace gets every row in it and needs no filter, because there is
nothing in it to filter.

## Why

`enrich_tmdb.py` writes a per-title fetch cursor into the same namespace as the keywords it
fetched: `namespace='tmdb_keywords', key='_fetched', value='1'`, one row on each of the 11,395
titles it has walked. Real keywords in that namespace carry `key='keyword'`. The sentinel is
indistinguishable from a fact to anything that does not already know the convention.

It has already been paid for twice.

**Once inside this repository.** `taste_vector_for` filters it —
`WHERE key NOT LIKE '\_%' ESCAPE '\'`, `crates/plexdb-reader/src/lib.rs:304`. The rollup is
correct. Nothing about that line explains itself to the next person, and nothing makes the next
accessor do the same.

**Once outside it.** A pooled keyword profile computed over the live store's 25,837 plays, using
`enrichment_for`'s shape rather than the rollup's, came back:

```
1|2696.1          <- the sentinel
sitcom|436.5
workplace comedy|217.5
```

The top entry of the house's taste profile was the string `1`, at six times the weight of the real
leader. Filtered properly the same query gives `sitcom` 497.5, `dating show` 305.6,
`based on novel or book` 288.0.

The scale of the damage is not uniform, which is what makes it dangerous. A consumer scoring a
candidate by cosine over its keyword set divides by the square root of that set's size. One phantom
attribute on every title means:

| real keywords on the title | counted | divisor error |
|---|---|---|
| 10 | 11 | 4.6% low |
| 3 | 4 | 15% low |
| 1 | 2 | **41% low** |

The error is largest on sparsely-tagged titles, so it does not cancel out across the library — it
reorders the bottom of every ranked list, in the exact region where a recommender's mistakes are
most visible.

**The project already decided this once, the other way round.** `tmdb_edges.py:49` puts its cursors
in a dedicated `tmdb_edges` namespace holding nothing else, and its module docstring says so
outright: *"Not a relationship namespace — see the module docstring."* The older module mixed them;
the newer one did not. This ADR makes the newer one the rule.

## What we chose, and the rejected alternatives

- **Cursors move to their own namespace.** `tmdb_keywords` holds keywords. Nothing filters anything,
  in this crate or in any consumer, including one writing raw SQL against a snapshot.

- **`enrichment_for` filters `_`-prefixed keys, rejected.** It fixes every consumer that goes through
  the crate, which is the intended path, and it is one line. It leaves the condition in place: the
  rule still exists, now written in two accessors instead of one, and a consumer reading the snapshot
  with SQLite directly — which ADR-0007 explicitly makes safe and expected — still sees the sentinel.
  Moving a guard is not removing what made it necessary.

- **Documenting the convention, rejected.** `_` meaning private is a rule the reader has to know
  before they can read a row correctly. The measurement above is what that costs when they don't.

- **A separate `cursors` table, rejected as scope.** It is the tidier end state and it is a bigger
  change than the problem needs — `enrichment` is already namespaced, and a namespace that holds one
  module's bookkeeping is using the mechanism as designed.

## Consequences

**A schema bump, but no re-fetch.** The 11,395 sentinel rows move with a local `UPDATE` on
`namespace`. Nothing is re-requested from TMDB and no rate limit is involved.

**The filter in `taste_vector_for` is deleted, not left in.** Leaving it would re-encode the rule
this ADR exists to remove, and would quietly keep working if someone reintroduced a sentinel into a
fact namespace — which is precisely the failure that should be loud.

**A namespace name now carries a claim.** `tmdb_keywords` means "keywords about titles" and
`tmdb_keywords_cursor` means "this module's own progress". A future module that wants both writes
two namespaces. That is the whole rule.
