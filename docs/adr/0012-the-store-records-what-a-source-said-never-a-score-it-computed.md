# The store records what a source said, never a score it computed

No column in `plexdb.db` holds a number this repository derived by applying a formula, a
threshold or a judgement to what a source reported. Rank, count, size, popularity, rating and
duration are stored as the source gave them, NULL where the source gives none, and a score the
source itself publishes is stored verbatim because the source did the arithmetic. A judge — a
model this repository asks a question, such as Jev — is a source: its answer is stored exactly as
it gave it, beside the id of the model that gave it, and the store never thresholds, rescales or
combines it. A reader that wants "does this play count", "how strong is this edge", "how good is
this" or "does this judge's answer count" computes it at read time from those columns, so
changing the formula or the threshold needs no migration.
