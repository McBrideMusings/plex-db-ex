# ADR-0028: List decisions and fetch attempts are keyed by list URL, in tables beside the facts

A person's decision about a crowd list lives in `list_decision`, and a failing list's fetch bookkeeping in `list_attempt`. Both are keyed by the list's canonical URL, and neither is an `enrichment_cursor` row, since a list is not an item (ADR-0023). `list_decision.added` records that a person added the list and outlasts a later `refetch` entry in `list_decisions.json`. Only the fold and the harvest write them (ADR-0013).
