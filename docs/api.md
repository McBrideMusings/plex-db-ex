---
outline: deep
---

# The store API

Every fact the store holds for consumers is a resource on Plex DB-EX (`plexdb-rs explore`, port
5194): a title and what is filed under it, plays, accounts, keywords and the judge's tables. These
routes and their JSON replies are the store's public interface
([ADR-0031](./adr/0031-readers-reach-the-store-only-through-the-http-api)). No program outside this
repo opens `plexdb.db` or a snapshot of it; the tables behind the routes ([Schema](./schema)) may
change without a consumer noticing.

Every read is a `GET`. The one `POST` is [a search](#searches), which embeds a phrase and scores
every Shot of a pool on the server. A reply reads the snapshot the explorer was started on, so it is
as fresh as the last sweep's `publish` step.

The routes are one table in the reader crate (`crates/plexdb-reader/src/call.rs`): `Call::parse`
reads a request into a `Call` and `Reader::answer` runs it. `plexdb-read` takes the same paths, so
`plexdb-read '/items/imdb:tt0113277/edges?direction=in'` prints the bytes the route answers.

The examples below are real replies from the store pulled on 2026-11-18 (schema v31: 13,210 movies,
1,679 shows, 76,779 episodes, 912 extras), cut short where marked.

## Requests and errors

- An id, keyword or tag in a path is one segment, percent-encoded: the keyword `9/11` is
  `/keywords/9%2F11`.
- A route refuses a parameter it does not take, and a parameter given twice. A blank value
  (`after=`) counts as absent.
- An `item_id` is at most 200 characters; a keyword, tag, namespace, type, surface, cursor or phrase
  at most 500.
- A title, account, keyword or tag the store does not hold is a 404. A list with nothing in it is
  `[]`, and audio facts the store does not hold for a title it holds are `null`.
- Every error is `{"error": "<message>"}`:

| Status | When |
|---|---|
| 400 | A parameter is unknown, repeated, too long or the wrong type; `limit` is outside 1–1000; `namespace` without `include=enrichment`; a search the reader refuses (`invalid search: …`) |
| 404 | No route has the path; the title, account, keyword or tag is not in the store |
| 405 | A `GET` route asked with another method, or `/searches` with anything but `POST` |
| 429 | Eight store requests are already running; retry |
| 503 | No snapshot published yet; the store's reader shape is not one this build reads; for `cuts`, `chapters` and `searches`, no Frame index (`PLEXDB_FRAMES_DIR` unset or no `frames.db`); for `searches`, no encoder configured or the encoder failed |

```json
{"error":"limit must be a whole number from 1 to 1000, got \"5000\""}
```

Each request writes one line to the container log, the path and query as sent:

```text
explore: store GET /items?type=movie&limit=2 status=200 ms=1 bytes=551
explore: store GET /items?limit=5000 status=400 ms=0 error="limit must be a whole number from 1 to 1000, got \"5000\""
```

A search's line adds `pool=<ids> phrase=<phrase> negatives=<count> draw=<n>`.

## Pagination

Every top-level list is paged by a cursor. It takes `limit` (default 100, at most 1000) and `after`,
and answers:

```json
{"data": [...], "has_more": true, "next": "imdb:tt0113277"}
```

`next` is what to pass as `after` for the page after this one; on the last page `has_more` is
`false` and `next` is `null`. There is no total and no page number. Rows come in the order of a key
that is unique and never changes, and `after` starts after that key whether or not a row still has
it, so a title removed between two pages costs nothing:

| List | Ordered by | `next` |
|---|---|---|
| `/items` | `item_id` | the last `item_id` |
| `/accounts/{id}/watched`, `/watched` | `item_id` | the last `item_id` |
| `/plays` | `history_key` | the last `history_key` |
| `/keywords` | `keyword` | the last keyword |
| `/keyword-pairs` | `(keyword_a, keyword_b)` | the last pair as a JSON array, `["a","b"]` |

A title's own lists (`/items/{id}/…`) are whole, one title's worth each.

Walking `GET /items?type=movie&include=enrichment&namespace=keywords&limit=500` through `next`
takes 27 pages and 28.4 MB, and returns each of the 13,210 movies once with its 228,172 keyword
facts.

## Routes

| Path | Takes | Reply |
|---|---|---|
| [`/items`](#items) | `type`, `library`, `show`, `include`, `namespace`, `limit`, `after` | page of Item |
| [`/items/{id}`](#items) | `include`, `namespace` | Item |
| [`/items/{id}/enrichment`](#enrichment) | `namespace` | `[Fact]` |
| [`/items/{id}/edges`](#edges) | `type`, `direction` (`out`, `in`) | `[Edge]` |
| [`/items/{id}/collections`](#crowd-lists) | — | `[Membership]` |
| [`/items/{id}/audio-facts`](#audio-facts) | — | object or `null` |
| [`/items/{id}/external-ids`](#external-ids-and-plex-entries) | — | `[ExternalId]` |
| [`/items/{id}/plex`](#external-ids-and-plex-entries) | — | `[PlexEntry]` |
| [`/items/{id}/media-parts`](#media-parts) | — | `[MediaPart]` |
| [`/items/{id}/plays`](#plays) | — | `[Play]` |
| [`/items/{id}/role-statements`](#keyword-roles) | — | `[RoleStatement]` |
| [`/items/{id}/cuts`, `/chapters`](#cuts-and-chapters) | — | `[ms]` |
| [`/plays`](#plays) | `account`, `since`, `limit`, `after` | page of Play |
| [`/accounts`](#accounts-taste-and-watch-history) | — | `[Account]` |
| [`/accounts/{id}/taste`, `/taste`](#accounts-taste-and-watch-history) | — | TasteVector |
| [`/accounts/{id}/watched`, `/watched`](#accounts-taste-and-watch-history) | `limit`, `after` | page of `item_id` |
| [`/keywords`](#keywords) | `surface`, `limit`, `after` | page of Keyword |
| [`/keywords/{keyword}`](#keywords) | — | Keyword |
| [`/keywords/{keyword}/pairs`](#keyword-pairs) | — | `[Pair]` |
| [`/keywords/{keyword}/roles`](#keyword-roles) | — | Roles |
| [`/keyword-pairs`](#keyword-pairs) | `decision`, `limit`, `after` | page of Pair |
| [`/tags/{tag}/neighbours`](#tag-neighbours) | `kind` (required) | `[Neighbour]` |
| [`/capabilities`](#capabilities) | — | `{"search": bool}` |
| [`POST /searches`](#searches) | JSON body | ShotSearch |

Organized extras are served by `/api/extras/items` and `/api/extras/fields`, which are part of the
same public interface ([Plex DB-EX § Extras routes](./explore#extras-routes)).

## Items

`GET /items/imdb:tt0113277` — one row of `items`:

```json
{"item_id":"imdb:tt0113277","type":"movie","title":"Heat","title_sort":null,"show_title":null,"show_item_id":null,
 "season":null,"episode":null,"year":1995,"duration_ms":10218261,"content_rating":"R","studio":"Warner Bros. Pictures"}
```

`GET /items` lists them by `item_id`. Its filters:

- `type` — `movie`, `show`, `episode` or `extra`.
- `library` — a Plex library section id: titles with an entry in that section.
- `show` — a show's `item_id`: its episodes.

`include` adds facts to every title in one query per field for the whole page, a comma list of:

- `enrichment` — the title's [facts](#enrichment), every namespace, or only `namespace`'s when
  one is given.
- `external_ids` — its [external ids](#external-ids-and-plex-entries).
- `audio_facts` — its [audio facts](#audio-facts), `null` when none.
- `plex` — its [Plex entries](#external-ids-and-plex-entries).

A field not asked for is absent; asked for, a list is `[]` when the title has none.
`/items/{id}` takes the same `include`.

`GET /items?library=1&limit=1&include=enrichment,external_ids,audio_facts,plex&namespace=keywords`:

```json
{"data":[{"item_id":"fs:026b46e129ff5f7e","type":"movie","title":"Miraculous World: Paris, Tales of Shadybug and Claw Noir",
          "title_sort":"Miraculous World Paris Tales of Shadybug and Claw Noir","show_title":null,"show_item_id":null,
          "season":null,"episode":null,"year":2023,"duration_ms":2875349,"content_rating":null,"studio":null,
          "enrichment":[],"external_ids":[],"audio_facts":null,
          "plex":[{"rating_key":"162777","section_id":"1","last_seen":"2026-11-18T08:30:01+00:00"}]}],
 "has_more":true,"next":"fs:026b46e129ff5f7e"}
```

## Enrichment

`GET /items/imdb:tt0006333/enrichment?namespace=keywords` — every fact under the namespace, or under
every namespace with none, in `(namespace, key, value)` order. A `(key, value)` two sources carry
appears once, with the newest `fetched_at`; `source` is never returned. (2 of 8:)

```json
[{"namespace":"keywords","key":"keyword","value":"adventure film","fetched_at":"2026-10-10T08:10:43+00:00"},
 {"namespace":"keywords","key":"keyword","value":"captain nemo","fetched_at":"2026-11-09T14:46:31+00:00"}]
```

## Edges

`GET /items/imdb:tt0113277/edges?type=tmdb_similar&direction=in` — edges into the title, or out of
it with `direction=out` (the default), by type and then rank ascending; every type without `type`.

```json
[{"from_id":"imdb:tt31349844","to_id":"imdb:tt0113277","edge_type":"tmdb_similar","rank":4,"fetched_at":"2026-10-28T07:31:54+00:00"},
 {"from_id":"imdb:tt1879016","to_id":"imdb:tt0113277","edge_type":"tmdb_similar","rank":6,"fetched_at":"2026-10-17T07:31:48+00:00"}]
```

## Crowd lists

`GET /items/imdb:tt0113277/collections` — every crowd list the title is on, by `collection_id`; a
field the source did not record is `null`, never `0`. (2 of them:)

```json
[{"collection_id":"mdblist:101881","source":"mdblist","name":"Top 250 Movies (iMDB)","url":"https://mdblist.com/lists/snoak/imdb-top-250-movies","size":250,"likes":557,"rank":103,"mentions":null,"observed_at":"2026-10-19T09:22:53+00:00"},
 {"collection_id":"mdblist:150232","source":"mdblist","name":"The Greats (Random)","url":"https://mdblist.com/lists/ayeshaamittal/the-greats-random","size":1247,"likes":0,"rank":167,"mentions":null,"observed_at":"2026-10-28T07:33:29+00:00"}]
```

## Audio facts

`GET /items/fs:01a5237bad6d67f2/audio-facts` — the analyzer's object exactly as `audio_facts.facts`
holds it, keys in their stored order, or `null` for a title no part of which is analyzed. A Plex
title is found through its current `media_part` (the newest part answers); an extra through its
primary file. (Cut short at `…`; `bar_energy` holds one value per bar:)

```json
{"bpm":126.22170414900005,"beat_grid":{"slope":0.4753540637446333,"intercept":755.9458749808517,…},"downbeat_phase":2,
 "landing_bar":376,"landing_bar_s":715.0654254988177,"key":"C","scale":"major","camelot":"8B",…,"bar_energy":[…]}
```

## External ids and Plex entries

`GET /items/imdb:tt0113277/external-ids` — every id a source gives the title, by `(ns, kind, value)`;
`kind` scopes it to a media type, `last_seen` is the scan that last saw it.

```json
[{"ns":"imdb","value":"tt0113277","kind":"movie","last_seen":"2026-11-18T08:30:01+00:00"},
 {"ns":"tmdb","value":"949","kind":"movie","last_seen":"2026-11-18T08:30:01+00:00"},
 {"ns":"tvdb","value":"1305","kind":"movie","last_seen":"2026-11-18T08:30:01+00:00"}]
```

`GET /items/imdb:tt0113277/plex` — every Plex library entry that resolves to the title, by rating key:

```json
[{"rating_key":"6234","section_id":"1","last_seen":"2026-11-18T08:30:01+00:00"}]
```

## Media parts

`GET /items/imdb:tt0113277/media-parts` — every file Plex lists for the title, by `part_id`. (The
file path cut at `…`:)

```json
[{"part_id":14425,"item_id":"imdb:tt0113277","rating_key":"6234","file":"/media/movies/Heat (1995) {imdb-tt0113277}/…mkv",
  "size":5823038672,"container":"mkv","video_codec":"av1","audio_codec":"opus","height":800,"bitrate":4559,
  "duration_ms":10218261,"plex_updated_at":1782627521,"fetched_at":"2026-11-08T20:45:17+00:00"}]
```

## Plays

`GET /plays?limit=2` — every play, by `history_key`; `account` narrows them to one Plex account and
`since` to those at or after a Unix time in seconds. `GET /items/{id}/plays` is one title's, oldest
first. A play carries no IP address, device identifier or ingest key.

```json
{"data":[{"history_key":"/status/sessions/history/10036","item_id":"imdb:tt0708997","plex_account_id":<account>,"platform":"Xbox",
          "viewed_at":1742034044,"percent_complete":100,"paused_counter":0,"seconds_watched":2761},
         {"history_key":"/status/sessions/history/10037","item_id":"imdb:tt0708913","plex_account_id":<account>,"platform":"Xbox",
          "viewed_at":1742036822,"percent_complete":100,"paused_counter":1,"seconds_watched":2761}],
 "has_more":true,"next":"/status/sessions/history/10037"}
```

## Accounts, taste and watch history

`GET /accounts` — every Plex account the store has plays for, by id, with its play count and newest
play. An account is known only through its plays, so an id with none is a 404 on every route below.

```json
[{"account_id":<account>,"plays":10741,"last_viewed_at":1794988762}, …]
```

`GET /accounts/{id}/watched` — every title the account played at least once, an episode rolled up to
its show, by `item_id`; `GET /watched` the same across every account (2,093 titles in all). (3:)

```json
{"data":["fs:2f1202b60318b479","fs:34b65ae2451854a0","fs:9c20c90f239c9b8b"],"has_more":true,"next":"fs:9c20c90f239c9b8b"}
```

`GET /accounts/{id}/taste` — the account's taste vector (ADR-0011): each title weighed by
`sqrt(seasons watched)`, nothing under half a season, split across its attributes.
`shows_without_seasons` names each show Plex files with no season numbers, counted one unit per
play. `GET /taste` — the same over every account's plays summed. It is one vector, not a list, so it
is not paged. (2 of the house's 20,330 attributes:)

```json
{"attributes":[{"namespace":"awards","key":"award","value":"7th Crunchyroll Anime Awards","weight":0.013888888888888888},
               {"namespace":"awards","key":"award","value":"AACTA Award for Best Cinematography","weight":0.01818181818181818}],
 "shows_without_seasons":[]}
```

## Keywords

`GET /keywords/heist` — a stored keyword, the raw spellings recorded for it (sorted) and the display
name a person gave it in Plex DB-EX, or `null`. The name is for display only: never compare, score or
count by it. A keyword the store records no spelling or name for is a 404.

```json
{"keyword":"heist","name":null,"surfaces":["heist"]}
```

`GET /keywords` lists them by keyword (24,454 in all). With `surface`, it holds the one keyword that
spelling maps to, lowercased, trimmed and whitespace-collapsed before the lookup, or none.
`GET /keywords?surface=%22Weird%20Al%22%20Yankovic`:

```json
{"data":[{"keyword":"weird al yankovic","name":null,"surfaces":["\"Weird Al\" Yankovic"]}],"has_more":false,"next":null}
```

## Keyword pairs

`GET /keywords/heist/pairs` — every pair the judge scored or a person decided that holds the keyword,
by `(keyword_a, keyword_b)`, `keyword_a < keyword_b`. `jev_score` is the judge's, `decision` the
person's (`accepted`, `rejected` or `null`). (1 of them:)

```json
[{"keyword_a":"art heist","keyword_b":"heist","jev_score":0.2,"jev_model":"jev-1.13.0","judged_at":"2026-10-10T08:21:43+00:00",
  "jev_error":null,"decision":null,"decided_at":null}]
```

`GET /keyword-pairs` lists every pair; `decision` narrows it to `accepted` (109 pairs), `rejected` or
`undecided`. `GET /keyword-pairs?decision=accepted&limit=2` (the first, then the cursor):

```json
{"data":[{"keyword_a":"abandoned house","keyword_b":"deserted house","jev_score":0.88,"jev_model":"jev-1.13.0",
          "judged_at":"2026-10-10T08:20:35+00:00","jev_error":null,"decision":"accepted","decided_at":"2026-10-26T00:33:52+00:00"}, …],
 "has_more":true,"next":"[\"aboriginal australians\",\"indigenous australians\"]"}
```

## Keyword roles

`GET /keywords/abandoned%20house/roles` — each source's score for each role the keyword may play
(`tone`, `era`, `region`, `theme`, `character_trait`), and a person's decision on each, by role. (2
of 5 verdicts:)

```json
{"keyword":"abandoned house",
 "verdicts":[{"role":"character_trait","source":"jev","score":0.03,"model":"jev-1.13.0","error":null,"stated_at":"2026-10-10T08:34:52+00:00"},
             {"role":"region","source":"jev","score":0.22,"model":"jev-1.13.0","error":null,"stated_at":"2026-10-10T08:34:52+00:00"}],
 "decisions":[]}
```

`GET /items/fs:7d432f0e9f14d139/role-statements` — every source that states the title carries a
keyword in a role, by `(keyword, role, source)`:

```json
[{"keyword":"insular oceania","role":"region","source":"wikidata","stated_at":"2026-10-10T08:10:43+00:00"}]
```

## Tag neighbours

`GET /tags/heist/neighbours?kind=movie` — the tags sharing titles with `heist` on the stored tag
network of `kind` (`movie` or `show`), strongest first; `shared` is titles carrying both, `df` titles
carrying the neighbour. A tag the network does not hold is a 404. (3 of them:)

```json
[{"tag":"heist and thrilling action","shared":123,"df":449},
 {"tag":"crime drug and gangster","shared":85,"df":751},
 {"tag":"drama","shared":78,"df":4898}]
```

## The Frame index

These need the explorer's Frame index (`PLEXDB_FRAMES_DIR`), and a search also its encoder
(`PLEXDB_FRAME_ENCODER_URL`, `PLEXDB_FRAME_MODEL`). The pulled copy has no Frame index, so on it
they answer the 503:

```json
{"error":"PLEXDB_FRAMES_DIR is not set, so there is no Frame index to search"}
```

### Capabilities

`GET /capabilities` — whether this explorer can search. On the pulled copy:

```json
{"search":false}
```

### Cuts and chapters

`GET /items/{id}/cuts` — every Cut of the title in ms, ascending, scene changes and chapter marks
together, 0 excluded; `/items/{id}/chapters` the chapter marks alone. `[]` for a title with none.
(The fixture in `crates/plexdb/tests/explore_search.rs`:)

```json
[1500,3000,6000]
```

### Searches

`POST /searches` ranks every Shot of the pool's indexed titles against a phrase. The body, with
`Content-Type: application/json` and a `Content-Length`, at most 1 MiB:

```json
{"pool": ["imdb:tt1", "imdb:tt2", "imdb:tt3"], "phrase": "a man in a suit",
 "negatives": ["rain"], "weight": 0.5, "temperature": 0.1, "draw": {"n": 2, "seed": 7}}
```

`pool` (at most 20,000 ids, a repeat counted once) and `phrase` are required, and any other field is
refused. A Shot's `score` is `phrase_sim − weight × max(negative_sims)` (`weight` 0.5 when absent);
with a `temperature`, each Shot carries `p = exp(score / T) / Σ`. With `draw`, the reply holds only
`n` Shots drawn without replacement by `p` from a generator seeded by `seed` (0 when absent) — the
same request, the same picks — or the top `n` without a temperature. An unseen phrase is embedded
through the explorer's encoder and kept in its phrase cache (ADR-0026). `frames` lists each sampled
frame as `[t_ms, sim]`. `indexed` counts the pool titles in the Frame index, `pool` the distinct ids
named, and `model` is the model they were indexed with. (The request above against the fixture in
`crates/plexdb/tests/explore_search.rs`:)

```json
{"shots":[{"item_id":"imdb:tt1","in_ms":0,"out_ms":5000,"frames":[[0,1.0],[2000,0.86775726]],"phrase_sim":1.0,"negative_sims":[1.0],"score":0.5,"p":0.4974163},
          {"item_id":"imdb:tt2","in_ms":0,"out_ms":5000,"frames":[[0,0.0],[2000,0.9848628]],"phrase_sim":0.9848628,"negative_sims":[0.9848628],"score":0.4924314,"p":0.46115828}],
 "indexed":2,"pool":3,"model":"test/clip"}
```
