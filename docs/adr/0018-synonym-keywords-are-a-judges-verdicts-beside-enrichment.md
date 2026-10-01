# ADR-0018: Synonym keywords are a judge's verdicts beside `enrichment`

`keyword_pairs` holds, for each pair of stored keywords a judge has been asked about, the judge's own score (`jev_score`, with `jev_model`) and a person's `decision` (`accepted`, `rejected`, or NULL). `enrichment` is never rewritten to merge spellings, and no column says whether a pair counts as merged: a reader applies its own threshold to `jev_score`, and a `decision` outranks it.

The Plex TVX server records a person's decision in a file beside the snapshot, and the sweep, the one writer, writes it into `decision` at its next run.
