---
outline: deep
---

# The store API

Every fact the store holds for consumers is served by Plex DB-EX (`plexdb-rs explore`, port 5194)
under `/api/store/`, and these routes and their JSON replies are the store's public interface
([ADR-0031](./adr/0031-readers-reach-the-store-only-through-the-http-api)). No program outside
this repo opens `plexdb.db` or a snapshot of it; the tables behind the routes ([Schema](./schema))
may change without a consumer noticing.

Each route answers one accessor of the reader crate (`crates/plexdb-reader`): it builds a
`plexdb_reader::Call` and replies with `Reader::answer`, the same bytes `plexdb-read` prints for
that call. A list comes back as an array, a value that may be absent as the value or `null`, and a
batch as an object keyed by `item_id`. A reply reads the snapshot the explorer was started on, so it
is as fresh as the last sweep's `publish` step.

The examples below are real replies from the store pulled on 2026-11-18 (schema v31), cut to their
first entries where marked.

## Requests and errors

- `GET` routes take query parameters. `POST` routes take a JSON object with
  `Content-Type: application/json` and a `Content-Length`, at most 1 MiB.
- A batch names at most 20,000 ids (the store pulled 2026-11-18 holds 13,210 movies). Repeated ids count once.
- An `item_id` is at most 200 characters; a namespace, surface, keyword, kind, tag, edge type,
  phrase or negative at most 500.
- Every error is `{"error": "<message>"}`:

| Status | When |
|---|---|
| 400 | A parameter is missing, too long or the wrong type; a search the reader refuses (`invalid search: …`) |
| 429 | Eight `/api/store/*` requests are already running; retry |
| 503 | No snapshot published yet; the store's reader shape is not one this build reads; for `search`, `cuts` and `chapters`, no Frame index (`PLEXDB_FRAMES_DIR` unset or no `frames.db`); for `search`, no encoder configured or the encoder failed |

```json
{"error":"account must be a Plex account id, a whole number"}
```

Each request writes one line to the container log:
`explore: store <route> <arguments> status=<code> ms=<elapsed>`, a batch's ids given as their count.

## Routes

| Method | Path | Request | Reply | Accessor |
|---|---|---|---|---|
| GET | `/api/store/enrichment` | `item_id`, `namespace` | `[Fact]` | `enrichment_for` |
| POST | `/api/store/enrichment` | `{"item_ids": [..], "namespace": ".."}` | `{item_id: [Fact]}` | `enrichment_for_many` |
| GET | `/api/store/audio-facts` | `item_id` | object or `null` | `audio_facts_for` |
| POST | `/api/store/audio-facts` | `{"item_ids": [..]}` | `{item_id: object}` | `audio_facts_for_many` |
| GET | `/api/store/keyword` | `surface` | string or `null` | `keyword_for_surface` |
| GET | `/api/store/surfaces` | `keyword` | `[string]` | `surfaces_for_keyword` |
| GET | `/api/store/display-name` | `keyword` | string or `null` | `display_name` |
| GET | `/api/store/edges/from` | `item_id`, `edge_type` | `[Edge]` | `edges_from` |
| GET | `/api/store/edges/to` | `item_id`, `edge_type` | `[Edge]` | `edges_to` |
| GET | `/api/store/tag-neighbours` | `kind`, `tag` | `[Neighbour]` | `tag_neighbours` |
| GET | `/api/store/collections` | `item_id` | `[Membership]` | `collections_for` |
| GET | `/api/store/taste` | `account` | `TasteVector` | `taste_vector_for` |
| GET | `/api/store/taste/pooled` | — | `TasteVector` | `pooled_taste_vector` |
| GET | `/api/store/watched` | `account` | `[item_id]` | `watched_units_for` |
| GET | `/api/store/watched/all` | — | `[item_id]` | `watched_units` |
| POST | `/api/store/search` | see [Search](#search) | `ShotSearch` | `search_shots`, `search_drawn` |
| GET | `/api/store/cuts` | `item_id` | `[ms]` | `cuts` |
| GET | `/api/store/chapters` | `item_id` | `[ms]` | `chapters` |
| GET | `/api/store/capabilities` | — | `{"search": bool}` | `capabilities` |

Organized extras are served by `/api/extras/items` and `/api/extras/fields`, which are part of the
same public interface ([Plex DB-EX § Extras routes](./explore#extras-routes)).

## Enrichment

`GET /api/store/enrichment?item_id=imdb:tt0006333&namespace=keywords` — every fact under the
namespace, in `(key, value)` order. A `(key, value)` two sources carry appears once, with the newest
`fetched_at`; `source` is never returned. An id with nothing recorded is `[]`. (2 of 8:)

```json
[{"namespace":"keywords","key":"keyword","value":"adventure film","fetched_at":"2026-10-10T08:10:43+00:00"},
 {"namespace":"keywords","key":"keyword","value":"captain nemo","fetched_at":"2026-11-09T14:46:31+00:00"}]
```

`POST /api/store/enrichment` with
`{"item_ids": ["imdb:tt0006333", "imdb:tt0113277", "imdb:tt9999999"], "namespace": "keywords"}` —
the same, grouped by id; an id with nothing recorded is absent, never an empty list. A batch of
12,462 movie ids is one request and a 23.8 MB reply. (First fact of each:)

```json
{"imdb:tt0006333":[{"namespace":"keywords","key":"keyword","value":"adventure film","fetched_at":"2026-10-10T08:10:43+00:00"}],
 "imdb:tt0113277":[{"namespace":"keywords","key":"keyword","value":"action film","fetched_at":"2026-10-10T08:15:04+00:00"}]}
```

## Audio facts

`GET /api/store/audio-facts?item_id=fs:01a5237bad6d67f2` — the analyzer's object exactly as
`audio_facts.facts` holds it, or `null` for a title no part of which is analyzed. A Plex title is
found through its current `media_part` (the newest part answers); an extra through its primary file.
(Cut short at `…`; `bar_energy` holds one value per bar:)

```json
{"bpm":126.22170414900005,"beat_grid":{…},"downbeat_phase":2,"landing_bar":376,"landing_bar_s":715.0654254988177,
 "key":"C","scale":"major","camelot":"8B","key_strength":0.8103029727935791,"beat_confidence":0.03193807601928711,
 "bar_energy":[…],"rms_mean":…}
```

`POST /api/store/audio-facts` with `{"item_ids": [..]}` — `{item_id: object}` in id order; an id
with no facts is absent.

## Keywords

`GET /api/store/keyword?surface="Weird Al" Yankovic` — the stored keyword a spelling maps to
(lowercased, trimmed and whitespace-collapsed before the lookup), or `null` when the store recorded
no such spelling.

```json
"weird al yankovic"
```

`GET /api/store/surfaces?keyword=heist` — every raw spelling recorded for a stored keyword, sorted;
`[]` for none. The keyword is matched exactly.

```json
["heist"]
```

`GET /api/store/display-name?keyword=heist` — the name a person gave the keyword in Plex DB-EX, or
`null`. For display only: never compare, score or count by it. No keyword is named in this store yet:

```json
null
```

## Edges, tags and lists

`GET /api/store/edges/to?item_id=imdb:tt0113277&edge_type=tmdb_similar` — edges of the type into
the title, rank ascending; `/api/store/edges/from` the same out of it.

```json
[{"from_id":"imdb:tt31349844","to_id":"imdb:tt0113277","edge_type":"tmdb_similar","rank":4,"fetched_at":"2026-10-28T07:31:54+00:00"},
 {"from_id":"imdb:tt1879016","to_id":"imdb:tt0113277","edge_type":"tmdb_similar","rank":6,"fetched_at":"2026-10-17T07:31:48+00:00"}]
```

`GET /api/store/tag-neighbours?kind=movie&tag=heist` — the tags sharing titles with `tag` on the
stored tag network of `kind` (`movie` or `show`), strongest first; `shared` is titles carrying both,
`df` titles carrying the neighbour. (3 of them:)

```json
[{"tag":"heist and thrilling action","shared":123,"df":449},
 {"tag":"crime drug and gangster","shared":85,"df":751},
 {"tag":"drama","shared":78,"df":4898}]
```

`GET /api/store/collections?item_id=imdb:tt0113277` — every crowd list the title is on, by
`collection_id`; a field the source did not record is `null`, never `0`. (2 of them:)

```json
[{"collection_id":"mdblist:101881","source":"mdblist","name":"Top 250 Movies (iMDB)","url":"https://mdblist.com/lists/snoak/imdb-top-250-movies","size":250,"likes":557,"rank":103,"mentions":null,"observed_at":"2026-10-19T09:22:53+00:00"},
 {"collection_id":"mdblist:150232","source":"mdblist","name":"The Greats (Random)","url":"https://mdblist.com/lists/ayeshaamittal/the-greats-random","size":1247,"likes":0,"rank":167,"mentions":null,"observed_at":"2026-10-28T07:33:29+00:00"}]
```

## Watch history

`GET /api/store/watched?account=<plex account id>` — every title the account played at least once,
an episode rolled up to its show, sorted; `[]` for an account with no plays.
`GET /api/store/watched/all` — the same across every account. (3 of 2,093:)

```json
["fs:046e13b05353be42","fs:107c54ebbdb63529","fs:258ea371a508f2ae"]
```

`GET /api/store/taste?account=<plex account id>` — the account's taste vector (ADR-0011): each title
weighed by `sqrt(seasons watched)`, nothing under half a season, split across its attributes.
`shows_without_seasons` names each show Plex files with no season numbers, counted one unit per
play. `GET /api/store/taste/pooled` — the same over every account's plays summed. (2 of 20,330
attributes:)

```json
{"attributes":[{"namespace":"awards","key":"award","value":"13th Seiyu Awards","weight":0.007358267909110183},
               {"namespace":"awards","key":"award","value":"2nd Crunchyroll Anime Awards","weight":0.007358267909110183}],
 "shows_without_seasons":[]}
```

## The Frame index

These need the explorer's Frame index (`PLEXDB_FRAMES_DIR`), and `search` also its encoder
(`PLEXDB_FRAME_ENCODER_URL`, `PLEXDB_FRAME_MODEL`). The pulled copy has no Frame index, so on it
they answer the 503:

```json
{"error":"PLEXDB_FRAMES_DIR is not set, so there is no Frame index to search"}
```

`GET /api/store/capabilities` — whether this explorer can search. On the pulled copy:

```json
{"search":false}
```

`GET /api/store/cuts?item_id=…` — every Cut of the title in ms, ascending, scene changes and chapter
marks together, 0 excluded; `/api/store/chapters` the chapter marks alone. `[]` for a title with
none. (The fixture in `crates/plexdb/tests/explore_search.rs`:)

```json
[1500,3000,6000]
```

### Search

`POST /api/store/search` ranks every Shot of the pool's indexed titles against a phrase:

```json
{"pool": ["imdb:tt1", "imdb:tt2", "imdb:tt3"], "phrase": "a man in a suit",
 "negatives": ["rain"], "weight": 0.5, "temperature": 0.1, "draw": {"n": 2, "seed": 7}}
```

`pool` and `phrase` are required. A Shot's `score` is `phrase_sim − weight × max(negative_sims)`
(`weight` 0.5 when absent); with a `temperature`, each Shot carries `p = exp(score / T) / Σ`. With
`draw`, the reply holds only `n` Shots drawn without replacement by `p` from a generator seeded by
`seed` (0 when absent) — the same request, the same picks — or the top `n` without a temperature.
An unseen phrase is embedded through the explorer's encoder and kept in its phrase cache (ADR-0026).
`frames` lists each sampled frame as `[t_ms, sim]`. `indexed` counts the pool titles in the Frame
index, `pool` the distinct ids named, and `model` is the model they were indexed with. (The
request above against the fixture in `crates/plexdb/tests/explore_search.rs`:)

```json
{"shots":[{"item_id":"imdb:tt1","in_ms":0,"out_ms":5000,"frames":[[0,1.0],[2000,0.86775726]],"phrase_sim":1.0,"negative_sims":[1.0],"score":0.5,"p":0.4974163},
          {"item_id":"imdb:tt2","in_ms":0,"out_ms":5000,"frames":[[0,0.0],[2000,0.9848628]],"phrase_sim":0.9848628,"negative_sims":[0.9848628],"score":0.4924314,"p":0.46115828}],
 "indexed":2,"pool":3,"model":"test/clip"}
```
