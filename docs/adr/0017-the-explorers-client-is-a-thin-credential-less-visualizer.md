# ADR-0017: The explorer's client is a thin, credential-less visualizer

Every credential and every call to an outside service belongs to the explorer's server
process; its browser client only renders what the server already computed or fetched, and
never reaches Plex, TMDB, or any other external source on its own. `PosterProxy` is the first
instance of this: it fetches Plex's `/library/metadata/<rating_key>/thumb` server-side and
hands the browser image bytes, so `PLEX_TOKEN` never appears in the page or in a request the
browser makes.
