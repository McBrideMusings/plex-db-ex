# ADR-0016: Keywords are one source-agnostic namespace with a source column

Every keyword source writes into the same `enrichment` namespace, `keywords`, and `enrichment`
and `enrichment_cursor` carry `source` in their primary keys, so a source's refresh deletes only
`WHERE namespace = 'keywords' AND source = <that source>` and a keyword or cursor another source
holds survives. Each raw spelling a source sends for a title is kept in `keyword_surfaces`, one
row per `(item_id, source, surface)` with the rank and spoiler flag the source gave, and
`enrichment.value` is its normalized form: lowercased, punctuation stripped, and a word ending in
"s" replaced by its simplemma lemma when that lemma is shorter, except the words in
`keywords.NEVER_FOLD`. No other inflection is changed. `keyword_forms(surface, keyword)` maps each
spelling to its stored form, and a change to normalization re-derives `enrichment` from
`keyword_surfaces`. A reader counts a title's keywords via `SELECT DISTINCT (item_id, value)`, so a
keyword two sources agree on is one fact. A migration that merges rows this way declares the
shrink, and `store._verify` accepts a row count down to exactly that number.
