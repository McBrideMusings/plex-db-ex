# ADR-0020: A person may decide a pair no judge saw

`keyword_pairs` holds a row for a pair only a person decided: `decision` and `decided_at` set, and `judged_at`, `jev_score`, `jev_model` and `jev_error` all NULL. `plexdb fold-merge-decisions` writes it from `merge_decisions.json` when both keywords are stored values, and deletes it when the decision is cleared. Every row carries a judge's verdict, a person's decision, or both, and `judged_at IS NULL` marks a row with only the decision. A person's merge counts by the same rule as any other in ADR-0018.
