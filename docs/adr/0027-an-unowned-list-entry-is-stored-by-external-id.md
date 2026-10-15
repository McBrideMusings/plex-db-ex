# ADR-0027: An unowned list entry is stored by external id, never under an item_id

`collection_entry` stores every entry of a crowd list, including titles the library does not own, under the strongest external id the source gave, scoped by media type, with the source's title and year. It carries no `item_id`: a reader resolves an entry to a title through `external_ids` when it reads, so a title walked later joins its lists with nothing rewritten. ADR-0009's rule, that a stored reference connects only walked items, still governs `edges` and `collection_membership`.
