# Bookkeeping never shares a table with facts

`enrichment` holds facts about titles. Every writer's own fetch cursors live in
`enrichment_cursor`. A reader querying `enrichment` gets facts and needs no filter, because there
is nothing else in the table to filter — including a consumer reading the published snapshot with
plain SQLite, which ADR-0007 makes an expected thing to do.

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

- **Cursors move to their own table, `enrichment_cursor`.** `(item_id, namespace, key, fetched_at)`,
  no `value` column — a cursor never had a value, `'1'` was filler and `fetched_at` was always the
  payload. Nothing filters anything, anywhere.

- **Cursors move to their own *namespace*, rejected — it does not work.** This was the first choice,
  and building it exposed why it fails. The taste rollup does not read a namespace; it scans the
  whole table (`attributes_by_item`, `crates/plexdb-reader/src/lib.rs`). The only thing hiding
  the sentinels from it was the `_` key prefix. Renaming them into a `*_cursor` namespace and
  dropping the prefix — as this ADR originally specified — would have turned one phantom attribute
  per title into **three**, because `tmdb_edges.py` writes two more cursors of its own that are
  invisible today for exactly the same reason. The bug would have grown the moment the edge sweeps
  ran. A namespace boundary does not stop a reader that never asked for a namespace.

- **`enrichment_for` filters `_`-prefixed keys, rejected.** It fixes every consumer that goes through
  the crate, which is the intended path, and it is one line. It leaves the condition in place: the
  rule still exists, now written in two accessors instead of one, and a consumer reading the snapshot
  with SQLite directly still sees the sentinel. Moving a guard is not removing what made it necessary.

- **Documenting the convention, rejected.** `_` meaning private is a rule the reader has to know
  before they can read a row correctly. The measurement above is what that costs when they don't.

## Consequences

**A schema bump, but no re-fetch.** Schema v7 creates the table, copies every `_`-prefixed row into
it stripping the prefix, and deletes the originals — a local `INSERT ... SELECT` and `DELETE`.
`fetched_at` rides across, so a sweep after the migration still skips everything it had already
fetched. Nothing is re-requested from TMDB and no rate limit is involved. Verified on the author's
store: 11,395 rows moved, 115,453 keyword rows untouched, and the taste vector came back
byte-identical.

**The filter in the rollup is deleted, not left in.** Leaving it would re-encode the rule this ADR
exists to remove, and would quietly keep working if someone wrote a sentinel back into `enrichment`
— precisely the failure that should be loud.

**Writers touch two tables in one transaction.** A cursor and the facts it vouches for are replaced
together or not at all; split apart, an interrupted sweep could leave a cursor claiming "fetched"
over rows that had already been deleted. `wipe_namespace` clears both for the same reason —
keeping the cursors would make every title look fetched-and-empty, so `--rewipe` would silently
fetch nothing.

**The rule is now checkable rather than remembered.** "A namespace holds facts or bookkeeping" was
a convention a reviewer had to enforce. "Bookkeeping is in a different table" is enforced by which
table a query names. A test asserts `SELECT DISTINCT key FROM enrichment WHERE namespace =
'tmdb_keywords'` returns `keyword` and nothing else, so a cursor written back into the facts table
fails a test rather than surfacing months later as a phantom attribute at the top of someone's
taste profile.
