# ADR-0016: Keywords are one source-agnostic namespace with a source column

Every keyword source writes into the same `enrichment` namespace, `keywords`, and both
`enrichment` and `enrichment_cursor` gain `source` in their primary key —
`(item_id, namespace, source, key, value)` and `(item_id, namespace, source, key)` — so a fetch
cursor and a namespace wipe are scoped per writer, never shared across every source of a
namespace. A source's refresh deletes only `WHERE namespace = 'keywords' AND source = <that
source>`, so a keyword or cursor another source holds survives. Every keyword is normalized and
Snowball-stemmed (`plexdb/keywords.py`) before storage, with every raw spelling kept in
`keyword_forms(surface, keyword)`; a reader counts a title's keywords via `SELECT DISTINCT
(item_id, value)`, so a keyword two sources agree on is one fact, not two. The migration
introducing both columns (v10) legitimately shrinks `enrichment` by merging rows that stem to the
same value, which `store._verify`'s row-count guard accepts only down to the exact count the
migration declares (`schema.apply`'s `DeclaredShrinks`) — any drop past that number still rolls
the store back.
