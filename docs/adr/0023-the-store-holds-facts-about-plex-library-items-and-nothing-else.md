# ADR-0023: The store holds facts about Plex library items, and nothing else

plex-db-ex is an extension of Plex's database. Every row it keeps describes an item Plex lists, a window inside one of those items, a play of one, a person's decision about one, or the store's own bookkeeping for fetching them. Media a consumer generates, collects or plays out — rendered video, audio beds, graphics — has no place in it.
