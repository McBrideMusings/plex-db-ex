# ADR-0022: The store depends on no consumer

plex-db-ex reads nothing a consumer produces — no playout file, no play ledger, no consumer config — and keeps no record of who reads it or what a reader did with what it read. Every input comes from Plex, an optional external source, the media files themselves, or a person through the CLI or Plex TVX. Anything a consumer needs to remember about its own use of the data, such as what it aired and how often, lives in that consumer.
