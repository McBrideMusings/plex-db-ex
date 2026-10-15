# ADR-0026: The reader embeds a new phrase and caches it beside its caller

A phrase reaches the reader as text. The reader normalizes it with the keyword spelling rule and looks it up, keyed by the model `frames.db` records, in a phrase cache file its caller names. On a miss it embeds the phrase and stores the result. A row unused for 90 days is deleted. The phrase cache is the only file the reader writes; the Snapshot and `frames.db` stay read-only to it.
