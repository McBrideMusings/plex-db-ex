---
layout: home

hero:
  name: plex-db-ex
  text: Extended Plex metadata & affinity store
  tagline: One writer, many readers. Plex stays authoritative; this carries what Plex cannot.
  actions:
    - theme: brand
      text: Read the PRD
      link: /PRD
    - theme: alt
      text: Decisions
      link: /adr/
    - theme: alt
      text: Original spec
      link: /spec

features:
  - title: Layer 1 — the cached item graph
    details: Namespaced enrichment tags, ranked affinity edges, weighted collection membership. Same for every user, expensive to acquire, cached hard.
  - title: Layer 2 — derived taste
    details: A per-user weighted attribute vector, rolled up from Layer 1 through that user's watch history. Recomputed per pass, never stored.
  - title: The library is the only hard dependency
    details: Plex is built in, and any other library plugs in as a Rhai source adapter. TMDB, Trakt, Tautulli and the rest are optional sources behind adapters. Consumers talk to the library directly and reach here only for what it cannot carry.
---
