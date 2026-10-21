# ADR-0029: A dashboard panel is a person's saved query, and its nightly value lives beside the store

The nightly sweep runs SQL a person wrote in the explorer, once per panel, through the Query tab's read-only, 5-second `run_query`. Each value goes to `panel-history.json` in the explorer's own directory, never into `plexdb.db`: a recorded count is a number this project computed, which ADR-0012 and ADR-0023 keep out of the store. A panel's history is keyed by its id and a hash of its SQL and value setting, so an edit starts a new series and a line never joins two different measurements.
