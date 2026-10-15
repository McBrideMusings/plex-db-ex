# ADR-0021: A keyword's display name is a person's label beside enrichment

`keyword_names` holds at most one display name per stored keyword. A person sets it in Plex DB-EX, through `name_decisions.json` and the sweep's fold, and it can be wording no source sent. Readers show it in place of the keyword's readable spelling. Nothing that computes reads it: embeddings, the judges, similarity, the map's vectors and the merge fold all use `enrichment` values and `keyword_forms` spellings as before. A merge group shows its head's name, or else the newest name set on any of its members.
