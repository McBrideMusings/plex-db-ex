# ADR-0016: Keywords are one source-agnostic namespace with a source column

Every keyword source writes into the same `enrichment` namespace, `keywords`, and `enrichment`'s
primary key gains `source`: `(item_id, namespace, source, key, value)`. A source's refresh deletes
only `WHERE namespace = 'keywords' AND source = <that source>`, so a keyword another source still
lists on an item survives. Every keyword is normalized and Snowball-stemmed
(`plexdb/keywords.py`) before storage, and every raw spelling is kept in `keyword_forms(surface,
keyword)` — this is what lets sources refresh independently and a query name one namespace
regardless of who reported the keyword.
